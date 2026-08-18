from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import (
    AuthCtx,
    BoardCaps,
    current_user,
    require_board,
    visible_boards_query,
    your_capabilities_map,
)
from app.core.board_pubsub import publish as bp_publish
from app.core.database import get_db
from app.core.exceptions import APIError
from app.core.lexorank import rank_after, rank_before, rank_between
from app.core.undo_log import (
    attach_undo_state,
    classify_patch,
    record_action,
    snapshot_element,
)
from app.core.utils import now_ms
from app.models.models import Board, BoardElement
from app.schemas.board import (
    BoardCreate,
    BoardElementBatchItem,
    BoardElementCreate,
    BoardElementResponse,
    BoardElementsBatchRequest,
    BoardElementsBatchResponse,
    BoardElementUpsertByRef,
    BoardElementZOrderItem,
    BoardElementZOrderRequest,
    BoardElementZOrderResponse,
    BoardFull,
    BoardPatch,
    BoardResponse,
)


async def _move_children(
    db: AsyncSession, board_id: UUID, parent_id: UUID,
    dx: float, dy: float, ts: int,
) -> list[BoardElement]:
    """Рекурсивно сдвигает всех потомков (frame и dx/dy) — см. карта
    `cards/board/bug/2026-05-30-frame-move-no-cascade-to-children.md`.

    Возвращает плоский список перемещённых элементов (для emit publish).
    """
    moved: list[BoardElement] = []
    children = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.parent_id == parent_id,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    for c in children:
        c.x = c.x + dx
        c.y = c.y + dy
        c.updated_at = ts
        moved.append(c)
        # рекурсия: если ребёнок — frame, его потомки тоже двигаются
        if c.type == "frame":
            moved.extend(await _move_children(db, board_id, c.id, dx, dy, ts))
    return moved


def _el_payload(el: BoardElement) -> dict:
    """Сериализация элемента для SSE payload (плоский dict)."""
    return {
        "id": str(el.id),
        "board_id": str(el.board_id),
        "type": el.type,
        "external_ref": str(el.external_ref) if el.external_ref else None,
        "parent_id": str(el.parent_id) if el.parent_id else None,
        "z_index": el.z_index,
        "z_rank": el.z_rank,
        "x": el.x, "y": el.y, "w": el.w, "h": el.h,
        "attrs": el.attrs or {},
        "created_at": el.created_at, "updated_at": el.updated_at,
        "deleted_at": el.deleted_at,
    }

router = APIRouter(prefix="/boards", tags=["boards"])


_NO_CAPS = BoardCaps()


def _board_dict(b: Board, caps: BoardCaps | None) -> dict:
    """BRD-3: единый сериализатор Board → dict с capability-флагами."""
    c = caps or _NO_CAPS
    return {
        "id": b.id,
        "title": b.title,
        "order_index": b.order_index,
        "owner_uuid": b.owner_uuid,
        "is_owner": c.is_owner,
        "is_curator": c.is_curator,
        "can_read": c.can_read,
        "can_write": c.can_write,
        "can_share": c.can_share,
        "created_at": b.created_at,
        "updated_at": b.updated_at,
        "deleted_at": b.deleted_at,
    }


@router.get("", response_model=list[BoardResponse])
async def list_boards(
    include_deleted: bool = Query(default=False, alias="includeDeleted"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> list[dict]:
    q = visible_boards_query(ctx)
    if not include_deleted:
        q = q.where(Board.deleted_at.is_(None))
    q = q.order_by(Board.order_index.asc(), Board.updated_at.desc())
    boards = list((await db.execute(q)).scalars().all())
    caps = await your_capabilities_map(db, ctx, boards)
    return [_board_dict(b, caps.get(b.id)) for b in boards]


@router.post("", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
async def create_board(
    body: BoardCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    if await db.get(Board, body.id):
        raise APIError(409, "conflict", f"Board with id '{body.id}' already exists")
    board = Board(**body.model_dump(), deleted_at=None, owner_uuid=ctx.user_uuid)
    db.add(board)
    await db.commit()
    await db.refresh(board)
    bp_publish(board.id, {
        "type": "board_created",
        "board": {
            "id": str(board.id), "title": board.title,
            "created_at": board.created_at, "updated_at": board.updated_at,
        },
    })
    # Создатель → owner (или curator, если так). Всё разрешено.
    caps = BoardCaps(
        is_owner=not ctx.is_curator, is_curator=ctx.is_curator,
        can_read=True, can_write=True, can_share=True,
    )
    return _board_dict(board, caps)


@router.get("/{board_id}", response_model=BoardFull)
async def get_board(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    board = await require_board(db, ctx, board_id, "read")
    q = (
        select(BoardElement)
        .where(BoardElement.board_id == board_id, BoardElement.deleted_at.is_(None))
        .order_by(BoardElement.z_rank.asc(), BoardElement.id.asc())
    )
    elements = (await db.execute(q)).scalars().all()
    caps = await your_capabilities_map(db, ctx, [board])
    return {
        **_board_dict(board, caps.get(board.id)),
        "elements": list(elements),
    }


@router.patch("/{board_id}", response_model=BoardResponse)
async def patch_board(
    board_id: UUID,
    body: BoardPatch,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    board = await require_board(db, ctx, board_id, "write")
    if body.updated_at >= board.updated_at:
        data = body.model_dump(exclude_unset=True)
        updated_at = data.pop("updated_at")
        for key, value in data.items():
            setattr(board, key, value)
        board.updated_at = updated_at
        await db.commit()
        await db.refresh(board)
        bp_publish(board.id, {
            "type": "board_patched",
            "board": {
                "id": str(board.id), "title": board.title,
                "created_at": board.created_at, "updated_at": board.updated_at,
            },
        })
    caps = await your_capabilities_map(db, ctx, [board])
    return _board_dict(board, caps.get(board.id))


@router.delete("/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> None:
    board = await db.get(Board, board_id)
    if not board:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
    if not ctx.is_curator and board.owner_uuid != ctx.user_uuid:
        raise APIError(403, "forbidden", "Only board owner or curator can delete")
    live_count = (
        await db.execute(
            select(func.count(BoardElement.id)).where(
                BoardElement.board_id == board_id,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if live_count > 0:
        raise APIError(
            409,
            "board_not_empty",
            f"Board has {live_count} live element(s); delete them first",
        )
    ts = now_ms()
    board.deleted_at = ts
    board.updated_at = ts
    await db.commit()
    bp_publish(board_id, {"type": "board_deleted", "board_id": str(board_id), "ts": ts})


# ── Elements ──────────────────────────────────────────────────────────────────


async def _validate_parent(
    db: AsyncSession, board_id: UUID, element_id: UUID, parent_id: UUID
) -> None:
    if parent_id == element_id:
        raise APIError(400, "invalid_parent", "Element cannot be its own parent")
    parent = await db.get(BoardElement, parent_id)
    if (
        not parent
        or parent.deleted_at is not None
        or parent.board_id != board_id
        or parent.type != "frame"
    ):
        raise APIError(400, "invalid_parent", f"Parent '{parent_id}' is not a frame in this board")


@router.post(
    "/{board_id}/elements",
    response_model=BoardElementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_element(
    board_id: UUID,
    body: BoardElementCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")
    if await db.get(BoardElement, body.id):
        raise APIError(409, "conflict", f"Element with id '{body.id}' already exists")
    if body.parent_id is not None:
        await _validate_parent(db, board_id, body.id, body.parent_id)
    max_z = (
        await db.execute(
            select(func.max(BoardElement.z_index)).where(BoardElement.board_id == board_id)
        )
    ).scalar()
    next_z = (max_z + 1) if max_z is not None else 0
    # BRD-30 D10: z_rank выставляется в rank_after(max) — новый элемент
    # становится самым верхним, соответствует поведению z_index.
    max_rank = (
        await db.execute(
            select(func.max(BoardElement.z_rank)).where(BoardElement.board_id == board_id)
        )
    ).scalar()
    next_rank = rank_after(max_rank) if max_rank else "V"
    element = BoardElement(
        **body.model_dump(),
        board_id=board_id,
        z_index=next_z,
        z_rank=next_rank,
        deleted_at=None,
    )
    db.add(element)
    await db.flush()  # чтобы element.id был доступен record_action до commit'а
    action = await record_action(
        db,
        board_id=board_id,
        executor_uuid=ctx.user_uuid,
        kind="create",
        target_ids=[element.id],
        snapshot=snapshot_element(element),
    )
    associated = list(action.associated_users) if action else []
    await db.commit()
    await db.refresh(element)
    event = {"type": "element_upserted", "element": _el_payload(element)}
    await attach_undo_state(db, board_id=board_id, event=event, associated_users=associated)
    bp_publish(board_id, event)
    return element


# BRD-24 Stage 7: legacy single `PATCH /elements/{id}` + `DELETE /elements/{id}`
# удалены. Frontend api.patchElement/deleteElement теперь ходят через
# `POST /elements/batch` c items=[one]. Единая точка входа для всех mutations.


# ── Batch mutations (BRD-24) ──────────────────────────────────────────────
# Единая точка входа для всех multi-target мутаций (patch + delete).
# Пишет один composite action в log; per-item heterogeneous delta.


def _item_snapshot(el: BoardElement) -> dict:
    """Минимальный per-item snapshot для composite delta (без external_ref
    и created_at — они не меняются в patch/delete)."""
    return {
        "x": el.x, "y": el.y, "w": el.w, "h": el.h,
        "z_index": el.z_index,
        "z_rank": el.z_rank,
        "parent_id": str(el.parent_id) if el.parent_id else None,
        "attrs": dict(el.attrs or {}),
    }


def _diff_snapshots(before: dict, after: dict) -> tuple[dict, dict]:
    """Возвращает (before_diff, after_diff) — только изменившиеся ключи."""
    b_diff: dict = {}
    a_diff: dict = {}
    for k in ("x", "y", "w", "h", "z_index", "z_rank", "parent_id"):
        if before.get(k) != after.get(k):
            b_diff[k] = before.get(k)
            a_diff[k] = after.get(k)
    b_attrs = before.get("attrs") or {}
    a_attrs = after.get("attrs") or {}
    if b_attrs != a_attrs:
        # attrs хранятся per-key {before, after} для partial merge при undo
        keys = set(b_attrs) | set(a_attrs)
        b_attrs_diff: dict = {}
        a_attrs_diff: dict = {}
        for k in keys:
            if b_attrs.get(k) != a_attrs.get(k):
                b_attrs_diff[k] = b_attrs.get(k)
                a_attrs_diff[k] = a_attrs.get(k)
        if b_attrs_diff:
            b_diff["attrs"] = b_attrs_diff
        if a_attrs_diff:
            a_diff["attrs"] = a_attrs_diff
    return b_diff, a_diff


def _classify_item_kind(before_diff: dict, after_diff: dict) -> str:
    """Per-item kind по diff: move / resize / attrs / parent / z_order / mixed."""
    changed = set(after_diff.keys())
    if changed == {"parent_id"}:
        return "parent"
    if changed == {"z_index"}:
        return "z_order"
    if changed <= {"x", "y"}:
        return "move"
    if changed <= {"x", "y", "w", "h"}:
        return "resize"
    if changed == {"attrs"}:
        return "attrs"
    return "mixed"


@router.post(
    "/{board_id}/elements/batch",
    response_model=BoardElementsBatchResponse,
)
async def batch_elements(
    board_id: UUID,
    body: BoardElementsBatchRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElementsBatchResponse:
    """BRD-24: единый batch mutation endpoint (patch + delete).

    Атомарно в одной tx: `pg_advisory_xact_lock(board_id)` сериализует
    concurrent batch calls на доске. Один composite `record_action` +
    один SSE bulk-event.
    """
    await require_board(db, ctx, board_id, "write")

    if not body.items:
        return BoardElementsBatchResponse(applied=[], skipped=[])

    # BRD-24 D7: per-board advisory lock. Board UUID → int64 через hashtext.
    # Держится до commit'а; concurrent batch на этой доске сериализуются.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)").bindparams(
            key=str(board_id)
        )
    )

    ts = now_ms()
    ids = [it.id for it in body.items]
    rows = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.id.in_(ids),
            )
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}

    # Validation: каждый id должен принадлежать доске (BRD-24 D6: 400).
    for it in body.items:
        if it.id not in by_id:
            raise APIError(
                400,
                "invalid_batch_item",
                f"Element '{it.id}' does not exist in board '{board_id}'",
            )
        if it.op == "patch" and it.patch is None:
            raise APIError(
                400,
                "invalid_batch_item",
                f"Item '{it.id}' has op=patch but no patch fields",
            )

    delta_items: list[dict] = []
    payload_items: list[dict] = []
    applied: list[UUID] = []
    skipped: list[dict] = []
    associated_all: set[str] = set()

    for it in body.items:
        el = by_id[it.id]

        if it.op == "delete":
            if el.deleted_at is not None:
                skipped.append({"id": str(el.id), "reason": "already_deleted"})
                continue
            snap = snapshot_element(el)
            if el.type == "frame":
                children = (
                    await db.execute(
                        select(BoardElement).where(
                            BoardElement.board_id == board_id,
                            BoardElement.parent_id == el.id,
                            BoardElement.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
                snap["children"] = [snapshot_element(c) for c in children]
            el.deleted_at = ts
            el.updated_at = ts
            delta_items.append({
                "target_id": str(el.id),
                "kind": "delete",
                "before": snap,   # snapshot для восстановления при undo
                "after": None,
            })
            payload_items.append({
                "element_id": str(el.id),
                "deleted": True,
            })
            applied.append(el.id)
            continue

        # op == "patch"
        if el.deleted_at is not None:
            skipped.append({"id": str(el.id), "reason": "deleted"})
            continue

        patch = it.patch.model_dump(exclude_unset=True)
        if not patch:
            skipped.append({"id": str(el.id), "reason": "empty_patch"})
            continue

        if "parent_id" in patch and patch["parent_id"] is not None:
            await _validate_parent(db, board_id, el.id, patch["parent_id"])

        # BRD-35: backend больше НЕ выполняет hidden cascade для frame.
        # Frontend (или другой caller) обязан включить children как
        # отдельные items в batch. Backend просто applies каждый item.
        before_full = _item_snapshot(el)
        for k, v in patch.items():
            setattr(el, k, v)
        el.updated_at = ts
        after_full = _item_snapshot(el)
        before_diff, after_diff = _diff_snapshots(before_full, after_full)
        if not after_diff:
            skipped.append({"id": str(el.id), "reason": "noop"})
            continue

        item_kind = _classify_item_kind(before_diff, after_diff)
        if item_kind == "attrs":
            item_before = before_diff.get("attrs") or {}
            item_after = after_diff.get("attrs") or {}
        else:
            item_before = before_diff
            item_after = after_diff
        delta_items.append({
            "target_id": str(el.id),
            "kind": item_kind,
            "before": item_before,
            "after": item_after,
        })
        payload_items.append({"element": _el_payload(el)})
        applied.append(el.id)

    if not delta_items:
        # Все items skipped — не пишем action и не broadcast'им.
        await db.commit()
        return BoardElementsBatchResponse(applied=applied, skipped=skipped)

    action = await record_action(
        db,
        board_id=board_id,
        executor_uuid=ctx.user_uuid,
        kind="composite",
        target_ids=[UUID(it["target_id"]) for it in delta_items],
        delta={"items": delta_items},
        ts_ms=ts,
    )
    associated_all = set(action.associated_users) if action else set()

    await db.commit()

    event: dict = {
        "type": "elements_batch_patched",
        "items": payload_items,
        "actor_uuid": str(ctx.user_uuid),
        "ts": ts,
    }
    await attach_undo_state(
        db, board_id=board_id, event=event,
        associated_users=list(associated_all),
    )
    bp_publish(board_id, event)

    return BoardElementsBatchResponse(applied=applied, skipped=skipped)


# ── BRD-6: z-order (front/back/forward/backward) ─────────────────────────────
# Server-authoritative endpoint (client не вычисляет z_index — decision D3).
# Multi-select через body.element_ids (D8: per-element relative shift).
# Frame cascade — расширяем target set рекурсивными children (D9).
# Advisory lock — per-board serialization (D11).
# No-op guard — если ничего не меняется, ни UPDATE ни record_action (D10).


def _collect_frame_descendants(
    all_elements: list[BoardElement], parent_id: UUID, out: set[UUID],
) -> None:
    """BRD-6 D9: recursive fetch descendants (по parent_id) для frame cascade."""
    for el in all_elements:
        if el.parent_id == parent_id and el.id not in out:
            out.add(el.id)
            if el.type == "frame":
                _collect_frame_descendants(all_elements, el.id, out)


async def _z_order_between(
    db: AsyncSession,
    ctx: AuthCtx,
    board_id: UUID,
    element_id: UUID,
    body: BoardElementZOrderRequest,
) -> BoardElementZOrderResponse:
    """BRD-30: op="between" — вставка target(s) в z_rank слот между
    before_id/after_id элементами.

    Semantic:
    - before_id — target(s) кладутся ПОД (rank < before.z_rank).
    - after_id  — target(s) кладутся НАД (rank > after.z_rank).
    - Оба заданы → strictly between.
    - Один из двух обязателен, иначе 400.

    Multi-select: N target'ы вставляются как соседний блок с midpoint-ranks
    в интервале (after.z_rank, before.z_rank), сохраняя их взаимный порядок
    (по текущему z_rank).

    Cascade frame: если target = frame и cascade_frame=True (default),
    children (по parent_id, recursive) вставляются сразу после frame'а
    в том же интервале, сохраняя их взаимный порядок.
    """
    if body.before_id is None and body.after_id is None:
        raise APIError(
            400, "invalid_z_order_target",
            "op=between requires beforeId and/or afterId",
        )

    # Fetch anchors.
    anchor_ids = [i for i in (body.before_id, body.after_id) if i is not None]
    anchors = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.id.in_(anchor_ids),
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    anchors_by_id = {a.id: a for a in anchors}
    for anchor_id in anchor_ids:
        if anchor_id not in anchors_by_id:
            raise APIError(
                400, "invalid_z_order_target",
                f"Anchor element {anchor_id} not on board {board_id}",
            )
    before_el = anchors_by_id.get(body.before_id) if body.before_id else None
    after_el = anchors_by_id.get(body.after_id) if body.after_id else None
    before_rank = before_el.z_rank if before_el else None
    after_rank = after_el.z_rank if after_el else None
    # Direction check.
    if before_rank and after_rank and after_rank >= before_rank:
        raise APIError(
            400, "invalid_z_order_target",
            f"afterId.z_rank ({after_rank}) must be < beforeId.z_rank "
            f"({before_rank}) — направление перепутано",
        )

    # Determine target set. element_ids overrides URL element_id.
    primary_ids: list[UUID] = (
        list(body.element_ids) if body.element_ids else [element_id]
    )
    all_elements = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.deleted_at.is_(None),
            )
            .order_by(BoardElement.z_rank.asc(), BoardElement.id.asc())
        )
    ).scalars().all()
    by_id = {el.id: el for el in all_elements}

    for pid in primary_ids:
        if pid not in by_id:
            raise APIError(
                400, "invalid_z_order_target",
                f"Element {pid} not on board {board_id}",
            )

    # BRD-35: backend больше НЕ выполняет hidden cascade. Caller (frontend
    # layer panel или API) обязан включить children через `elementIds`.
    effective_ids: list[UUID] = sorted(primary_ids, key=lambda i: by_id[i].z_rank)

    # BRD-36: auto-reparent. Anchor parent_id — целевой parent для target'ов.
    # Если target parent_id ≠ anchor parent_id → auto меняем target.parent_id
    # (delta.kind=mixed включит parent_id в diff, undo восстановит).
    anchor_parents = {a.parent_id for a in anchors}
    target_parent = anchors[0].parent_id if len(anchor_parents) == 1 else None

    # Chain of ranks. Cursor moves from after_rank upward to before_rank.
    new_ranks: dict[UUID, str] = {}
    cursor = after_rank
    for eid in effective_ids:
        new_rank = rank_between(cursor, before_rank)
        new_ranks[eid] = new_rank
        cursor = new_rank

    ts = now_ms()
    delta_items: list[dict] = []
    payload_items: list[dict] = []
    response_items: list[BoardElementZOrderItem] = []
    for eid in effective_ids:
        el = by_id[eid]
        before_snap = _item_snapshot(el)
        el.z_rank = new_ranks[eid]
        # BRD-36: auto-reparent если anchors из одного parent'а и не
        # совпадает с текущим target parent_id.
        if target_parent is not None and el.parent_id != target_parent:
            el.parent_id = target_parent
        el.updated_at = ts
        after_snap = _item_snapshot(el)
        before_diff, after_diff = _diff_snapshots(before_snap, after_snap)
        if not after_diff:
            continue  # no-op skip
        delta_items.append({
            "target_id": str(eid),
            "kind": "mixed",  # BRD-29 composite handler support
            "before": before_diff,
            "after": after_diff,
        })
        payload_items.append({"element": _el_payload(el)})
        response_items.append(BoardElementZOrderItem(
            id=eid, z_index=el.z_index, z_rank=el.z_rank,
        ))

    if not delta_items:
        return BoardElementZOrderResponse(items=response_items)

    action = await record_action(
        db,
        board_id=board_id,
        executor_uuid=ctx.user_uuid,
        kind="composite",
        target_ids=[UUID(i["target_id"]) for i in delta_items],
        delta={"items": delta_items},
        ts_ms=ts,
    )
    associated_all = set(action.associated_users) if action else set()

    await db.commit()

    event = {
        "type": "elements_batch_patched",
        "items": payload_items,
        "actor_uuid": str(ctx.user_uuid),
        "ts": ts,
    }
    await attach_undo_state(
        db, board_id=board_id, event=event,
        associated_users=list(associated_all),
    )
    bp_publish(board_id, event)

    return BoardElementZOrderResponse(items=response_items)


@router.post(
    "/{board_id}/elements/{element_id}/z-order",
    response_model=BoardElementZOrderResponse,
)
async def z_order_element(
    board_id: UUID,
    element_id: UUID,
    body: BoardElementZOrderRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElementZOrderResponse:
    """BRD-6: server-authoritative z-order (front/back/forward/backward)
    с multi-select + frame cascade + advisory lock + no-op guard.

    - Single (без element_ids) → kind="z_order" в undo-log (плоский delta).
    - Multi/cascade → kind="composite" с массивом per-item delta (см. BRD-24
      + apply_undo/redo `_apply_item`).
    - SSE `element_patched` для каждого затронутого элемента (D6) — DOM
      reorder на клиенте через BRD-22 `reorderNodeByZ`.
    """
    await require_board(db, ctx, board_id, "write")

    # BRD-6 D11: per-board advisory lock (та же техника, что batch).
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)").bindparams(
            key=str(board_id)
        )
    )

    # BRD-30: op="between" — отдельный path (z_rank driven, не z_index).
    if body.op == "between":
        return await _z_order_between(
            db, ctx, board_id, element_id, body,
        )

    primary_ids: set[UUID] = set(body.element_ids) if body.element_ids else {element_id}

    all_elements = (
        await db.execute(
            select(BoardElement)
            .where(
                BoardElement.board_id == board_id,
                BoardElement.deleted_at.is_(None),
            )
            .order_by(BoardElement.z_index.asc(), BoardElement.id.asc())
        )
    ).scalars().all()
    by_id = {el.id: el for el in all_elements}

    missing = [pid for pid in primary_ids if pid not in by_id]
    if missing:
        raise APIError(
            400, "invalid_z_order_target",
            f"Element(s) {[str(m) for m in missing]} not on board {board_id}",
        )

    # BRD-6 D9: cascade — расширить primary_ids всеми descendants любого
    # primary-frame'а.
    affected: set[UUID] = set(primary_ids)
    for pid in list(primary_ids):
        if by_id[pid].type == "frame":
            _collect_frame_descendants(all_elements, pid, affected)

    # BRD-36: per-parent scope. non_affected — только siblings target'а
    # (same parent_id как у primary). Так Front/Back/Forward/Backward
    # не выкидывают target за пределы его контейнера (rect в frame → Back
    # → нижний слой среди siblings, но НЕ ниже frame'а самого).
    # Primary_parent берётся у первого primary (все primary должны быть
    # из одного parent'а на практике — multi-select — либо все top-level,
    # либо все children одного frame'а).
    first_primary = next(iter(primary_ids))
    scope_parent_id = by_id[first_primary].parent_id
    non_affected = [
        el for el in all_elements
        if el.id not in affected and el.parent_id == scope_parent_id
    ]
    affected_els = [el for el in all_elements if el.id in affected]  # sorted by z ASC
    op = body.op

    # z_by_id — рабочая копия, обновляется алгоритмами swap'а (forward/backward).
    z_by_id: dict[UUID, int] = {el.id: el.z_index for el in all_elements}

    if op == "front":
        max_other = max((el.z_index for el in non_affected), default=None)
        min_affected = min(el.z_index for el in affected_els)
        # BRD-6 D10 semantic no-op: если все affected уже выше всех non-affected,
        # не сдвигаем — иначе compression переписывает z без семантической
        # причины (генерирует лишний action + SSE).
        if max_other is not None and min_affected > max_other:
            pass
        else:
            base = max_other + 1 if max_other is not None else 0
            for i, el in enumerate(affected_els):
                z_by_id[el.id] = base + i
    elif op == "back":
        min_other = min((el.z_index for el in non_affected), default=None)
        max_affected = max(el.z_index for el in affected_els)
        if min_other is not None and max_affected < min_other:
            pass
        else:
            base = min_other - len(affected_els) if min_other is not None else 0
            for i, el in enumerate(affected_els):
                z_by_id[el.id] = base + i
    elif op == "forward":
        # BRD-6 D8: per-element swap с ближайшим non-affected higher z.
        # Iterate top-down чтобы избежать конфликтов при последовательных swap'ах.
        for el in reversed(affected_els):
            my_z = z_by_id[el.id]
            candidate: BoardElement | None = None
            candidate_z: int | None = None
            for other in non_affected:
                other_z = z_by_id[other.id]
                if other_z > my_z and (candidate_z is None or other_z < candidate_z):
                    candidate = other
                    candidate_z = other_z
            if candidate is not None:
                z_by_id[el.id], z_by_id[candidate.id] = candidate_z, my_z
    elif op == "backward":
        for el in affected_els:
            my_z = z_by_id[el.id]
            candidate = None
            candidate_z = None
            for other in non_affected:
                other_z = z_by_id[other.id]
                if other_z < my_z and (candidate_z is None or other_z > candidate_z):
                    candidate = other
                    candidate_z = other_z
            if candidate is not None:
                z_by_id[el.id], z_by_id[candidate.id] = candidate_z, my_z

    # Собираем реальные изменения (BRD-6 D10 no-op guard).
    changes: dict[UUID, tuple[int, int]] = {}  # id → (before_z, after_z)
    for el in all_elements:
        new_z = z_by_id[el.id]
        if new_z != el.z_index:
            changes[el.id] = (el.z_index, new_z)

    if not changes:
        # No-op: ни UPDATE, ни record_action, ни SSE.
        return BoardElementZOrderResponse(items=[
            BoardElementZOrderItem(
                id=pid, z_index=by_id[pid].z_index, z_rank=by_id[pid].z_rank,
            )
            for pid in primary_ids
        ])

    ts = now_ms()
    # BRD-30 D10 + BRD-36: legacy op обновляет z_rank точечно среди
    # siblings того же parent'а (не глобально) — сохраняем плотность
    # между-op inserts + per-parent scope.
    non_affected_ids = {
        el.id for el in all_elements
        if el.id not in changes and el.parent_id == scope_parent_id
    }

    if op == "front":
        max_rank = max(
            (by_id[i].z_rank for i in non_affected_ids), default=None,
        )
        # affected sorted by NEW z_index — задаём rank цепочкой вверх.
        cursor_rank = max_rank
        for eid in sorted(changes.keys(), key=lambda i: changes[i][1]):
            cursor_rank = rank_after(cursor_rank) if cursor_rank else "V"
            by_id[eid].z_rank = cursor_rank
    elif op == "back":
        min_rank = min(
            (by_id[i].z_rank for i in non_affected_ids), default=None,
        )
        # BRD-36: для child of frame — lower bound = frame's z_rank. Иначе
        # child уходит визуально под frame. `rank_between(parent_rank,
        # min_sibling)` гарантирует что child остаётся выше frame.
        parent_rank = None
        if scope_parent_id is not None and scope_parent_id in by_id:
            parent_rank = by_id[scope_parent_id].z_rank
        # affected sorted DESC by new z_index — задаём rank цепочкой вниз.
        cursor_rank = min_rank
        for eid in sorted(changes.keys(), key=lambda i: -changes[i][1]):
            if cursor_rank is None:
                cursor_rank = "V"
            elif parent_rank is not None:
                # Ограничить снизу: rank_between(parent_rank, cursor_rank).
                cursor_rank = rank_between(parent_rank, cursor_rank)
            else:
                cursor_rank = rank_before(cursor_rank)
            by_id[eid].z_rank = cursor_rank
    elif op in ("forward", "backward"):
        # forward/backward — swap z_index парами. Swap partner'ы = elements,
        # у которых z_index стал прежним значением affected'а. Идентифицируем
        # по (before_z, after_z) для affected и симметрично для non-affected.
        # Проще: для каждого changed non-affected — swap его z_rank с
        # соответствующим affected.
        swap_pairs: list[tuple[UUID, UUID]] = []
        affected_changes = [
            (eid, before_z, after_z)
            for eid, (before_z, after_z) in changes.items()
            if eid not in non_affected_ids  # sanity — они все не в non_affected по определению
        ]
        # non-affected changed = те же еиds что-то поменяли (swap partner'ы).
        # В нашем алгоритме для forward/backward пары точно есть.
        for eid, before_z, after_z in affected_changes:
            partner = next(
                (
                    other_eid for other_eid, (ob, oa) in changes.items()
                    if other_eid != eid and ob == after_z and oa == before_z
                ),
                None,
            )
            if partner is not None and (partner, eid) not in swap_pairs:
                swap_pairs.append((eid, partner))
        for a_id, b_id in swap_pairs:
            by_id[a_id].z_rank, by_id[b_id].z_rank = (
                by_id[b_id].z_rank, by_id[a_id].z_rank,
            )
    for eid, (_before, after) in changes.items():
        el = by_id[eid]
        el.z_index = after
        el.updated_at = ts

    # BRD-6 D5: single → kind="z_order" flat delta; multi/cascade → composite.
    associated_all: set[str] = set()
    if len(changes) == 1:
        (eid, (before_z, after_z)), = changes.items()
        action = await record_action(
            db,
            board_id=board_id,
            executor_uuid=ctx.user_uuid,
            kind="z_order",
            target_ids=[eid],
            delta={"before": before_z, "after": after_z},
            ts_ms=ts,
        )
    else:
        delta_items = [
            {
                "target_id": str(eid),
                "kind": "z_order",
                "before": {"z_index": before_z},
                "after": {"z_index": after_z},
            }
            for eid, (before_z, after_z) in changes.items()
        ]
        action = await record_action(
            db,
            board_id=board_id,
            executor_uuid=ctx.user_uuid,
            kind="composite",
            target_ids=list(changes.keys()),
            delta={"items": delta_items},
            ts_ms=ts,
        )
    if action is not None:
        associated_all = set(action.associated_users or [])

    await db.commit()

    # BRD-6 D6: per-element `element_patched` (существующий SSE contract).
    # DOM reorder на клиенте — BRD-22 `reorderNodeByZ`.
    for eid in changes:
        event = {
            "type": "element_patched",
            "element": _el_payload(by_id[eid]),
        }
        await attach_undo_state(
            db, board_id=board_id, event=event,
            associated_users=list(associated_all),
        )
        bp_publish(board_id, event)

    # Response — все изменённые (не только primary): фронтенду проще применить
    # все обновления одним loop'ом.
    return BoardElementZOrderResponse(items=[
        BoardElementZOrderItem(
            id=eid, z_index=after, z_rank=by_id[eid].z_rank,
        )
        for eid, (_before, after) in changes.items()
    ])


# ── Upsert / lookup / delete по external_ref ──────────────────────────────────
# Используется auto_designer (Python pkg + CLI) для повторяемого рисования
# фреймов: каждый screen имеет стабильный `external_ref` (UUID), скрипт
# делает upsert по нему, internal `id` сохраняется.
# Карта: cards/board/feature/2026-05-30-board-external-ref-stable-id.md.


@router.post(
    "/{board_id}/elements/by-ref",
    response_model=BoardElementResponse,
)
async def upsert_element_by_ref(
    board_id: UUID,
    body: BoardElementUpsertByRef,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")

    existing = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.external_ref == body.external_ref,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # UPDATE: id не меняем (PK + потенциальные FK). z_index сохраняем.
        if body.parent_id is not None:
            await _validate_parent(db, board_id, existing.id, body.parent_id)
        # BRD-18: снимок «до» для classify_patch.
        before = snapshot_element(existing)
        # cascade-move для frame: запоминаем старые координаты
        old_x, old_y = existing.x, existing.y
        existing.type = body.type
        existing.parent_id = body.parent_id
        existing.x = body.x
        existing.y = body.y
        existing.w = body.w
        existing.h = body.h
        existing.attrs = body.attrs
        existing.updated_at = body.updated_at
        cascade_dx = cascade_dy = 0.0
        cascade_children_before: list[dict] = []
        if existing.type == "frame":
            cascade_dx = body.x - old_x
            cascade_dy = body.y - old_y
            if cascade_dx or cascade_dy:
                pre_children = (
                    await db.execute(
                        select(BoardElement).where(
                            BoardElement.board_id == board_id,
                            BoardElement.parent_id == existing.id,
                            BoardElement.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
                for c in pre_children:
                    cascade_children_before.append({"id": str(c.id), "x": c.x, "y": c.y})
                await _move_children(
                    db, board_id, existing.id, cascade_dx, cascade_dy, body.updated_at,
                )
        after = snapshot_element(existing)
        kind, delta = classify_patch(before, after)
        if cascade_children_before:
            delta["cascade_children"] = cascade_children_before
        action = await record_action(
            db,
            board_id=board_id,
            executor_uuid=ctx.user_uuid,
            kind=kind,
            target_ids=[existing.id],
            delta=delta,
            ts_ms=body.updated_at,
        )
        associated = list(action.associated_users) if action else []
        await db.commit()
        await db.refresh(existing)
        payload = _el_payload(existing)
        if cascade_dx or cascade_dy:
            payload["cascade_dx"] = cascade_dx
            payload["cascade_dy"] = cascade_dy
        event = {"type": "element_upserted", "element": payload}
        await attach_undo_state(db, board_id=board_id, event=event, associated_users=associated)
        bp_publish(board_id, event)
        return existing

    # INSERT: новый элемент с переданным `id` и `external_ref`.
    if await db.get(BoardElement, body.id):
        raise APIError(
            409,
            "conflict",
            f"Element with id '{body.id}' already exists (but with different external_ref)",
        )
    if body.parent_id is not None:
        await _validate_parent(db, board_id, body.id, body.parent_id)
    max_z = (
        await db.execute(
            select(func.max(BoardElement.z_index)).where(BoardElement.board_id == board_id)
        )
    ).scalar()
    next_z = (max_z + 1) if max_z is not None else 0
    element = BoardElement(
        id=body.id,
        board_id=board_id,
        external_ref=body.external_ref,
        type=body.type,
        parent_id=body.parent_id,
        z_index=next_z,
        x=body.x,
        y=body.y,
        w=body.w,
        h=body.h,
        attrs=body.attrs,
        created_at=body.created_at,
        updated_at=body.updated_at,
        deleted_at=None,
    )
    db.add(element)
    await db.flush()
    action = await record_action(
        db,
        board_id=board_id,
        executor_uuid=ctx.user_uuid,
        kind="create",
        target_ids=[element.id],
        snapshot=snapshot_element(element),
        ts_ms=body.updated_at,
    )
    associated = list(action.associated_users) if action else []
    await db.commit()
    await db.refresh(element)
    event = {"type": "element_upserted", "element": _el_payload(element)}
    await attach_undo_state(db, board_id=board_id, event=event, associated_users=associated)
    bp_publish(board_id, event)
    return element


@router.get(
    "/{board_id}/elements/by-ref/{external_ref}",
    response_model=BoardElementResponse,
)
async def get_element_by_ref(
    board_id: UUID,
    external_ref: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "read")
    element = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.external_ref == external_ref,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if element is None:
        raise APIError(
            404,
            "element_not_found",
            f"Element with external_ref '{external_ref}' does not exist in board '{board_id}'",
        )
    return element


@router.delete(
    "/{board_id}/elements/by-ref/{external_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_element_by_ref(
    board_id: UUID,
    external_ref: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> None:
    await require_board(db, ctx, board_id, "write")
    element = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.external_ref == external_ref,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if element is None:
        raise APIError(
            404,
            "element_not_found",
            f"Element with external_ref '{external_ref}' does not exist in board '{board_id}'",
        )
    ts = now_ms()
    # BRD-18: snapshot для undo delete (включая cascade children для frame).
    snap = snapshot_element(element)
    if element.type == "frame":
        children = (
            await db.execute(
                select(BoardElement).where(
                    BoardElement.board_id == board_id,
                    BoardElement.parent_id == element.id,
                    BoardElement.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        snap["children"] = [snapshot_element(c) for c in children]
    element.deleted_at = ts
    element.updated_at = ts
    action = await record_action(
        db,
        board_id=board_id,
        executor_uuid=ctx.user_uuid,
        kind="delete",
        target_ids=[element.id],
        snapshot=snap,
        ts_ms=ts,
    )
    associated = list(action.associated_users) if action else []
    await db.commit()
    event = {
        "type": "element_deleted",
        "element_id": str(element.id),
        "external_ref": str(external_ref),
        "ts": ts,
    }
    await attach_undo_state(db, board_id=board_id, event=event, associated_users=associated)
    bp_publish(board_id, event)


@router.post(
    "/{board_id}/elements/{element_id}/restore",
    response_model=BoardElementResponse,
)
async def restore_element(
    board_id: UUID,
    element_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")
    element = await db.get(BoardElement, element_id)
    if not element or element.board_id != board_id:
        raise APIError(404, "element_not_found", f"Element with id '{element_id}' does not exist")
    if element.deleted_at is None:
        raise APIError(400, "not_deleted", f"Element with id '{element_id}' is not deleted")
    if element.parent_id is not None:
        await _validate_parent(db, board_id, element_id, element.parent_id)
    ts = now_ms()
    element.deleted_at = None
    element.updated_at = ts
    await db.commit()
    await db.refresh(element)
    bp_publish(board_id, {"type": "element_upserted", "element": _el_payload(element)})
    return element
