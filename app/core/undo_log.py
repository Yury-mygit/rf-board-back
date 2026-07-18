"""BRD-18: event-sourced лог действий для undo/redo (модель γ2).

Каждая mutation в board (`create_element` / `patch_element` /
`delete_element` / `upsert_element_by_ref` / `delete_element_by_ref`)
вызывает `record_action(...)` в **той же transaction'е**, что и сама
mutation. Тогда:

- `touchers` элемента пополняется executor'ом атомарно с mutation.
- Row в `board_actions` вставляется с `associated_users = union of
  executor + prior touchers всех target'ов`.

Undo/Redo endpoints и `compute_inverse` — в BRD-19.

Правила `classify_patch` — определяем kind по diff'у полей before/after.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import now_ms
from app.models.models import BoardAction, BoardElement

# BRD-19: N=100 per (board, user) stack — оставшееся выталкивается.
UNDO_STACK_CAP = 100


def snapshot_element(el: BoardElement) -> dict:
    """Полный snapshot для восстановления через undo delete."""
    return {
        "id": str(el.id),
        "type": el.type,
        "external_ref": str(el.external_ref) if el.external_ref else None,
        "parent_id": str(el.parent_id) if el.parent_id else None,
        "z_index": el.z_index,
        "x": el.x,
        "y": el.y,
        "w": el.w,
        "h": el.h,
        "attrs": el.attrs or {},
        "created_at": el.created_at,
    }


def classify_patch(before: dict, after: dict) -> tuple[str, dict]:
    """По diff'у полей возвращает (kind, delta) для лога.

    - move — только x/y изменились → dx/dy delta.
    - resize — w/h (± x/y при anchor-resize) → dx/dy/dw/dh.
    - parent — parent_id → before/after.
    - z_order — z_index → before/after.
    - attrs — attrs dict → per-key {before, after}.
    - composite — mixed changes → список задействованных полей
      (сервер применяет через snapshot-восстановление в BRD-19).
    """
    changed = set()
    for k in ("x", "y", "w", "h", "z_index", "parent_id", "attrs"):
        if before.get(k) != after.get(k):
            changed.add(k)

    if not changed:
        return "noop", {}

    if changed == {"parent_id"}:
        return "parent", {
            "before": before.get("parent_id"),
            "after": after.get("parent_id"),
        }
    if changed == {"z_index"}:
        return "z_order", {
            "before": before.get("z_index"),
            "after": after.get("z_index"),
        }
    if changed <= {"x", "y"}:
        return "move", {
            "dx": (after.get("x") or 0) - (before.get("x") or 0),
            "dy": (after.get("y") or 0) - (before.get("y") or 0),
        }
    if changed <= {"x", "y", "w", "h"}:
        return "resize", {
            "dx": (after.get("x") or 0) - (before.get("x") or 0),
            "dy": (after.get("y") or 0) - (before.get("y") or 0),
            "dw": (after.get("w") or 0) - (before.get("w") or 0),
            "dh": (after.get("h") or 0) - (before.get("h") or 0),
        }
    if changed == {"attrs"}:
        before_attrs = before.get("attrs") or {}
        after_attrs = after.get("attrs") or {}
        keys = set(before_attrs) | set(after_attrs)
        delta: dict[str, dict] = {}
        for k in keys:
            if before_attrs.get(k) != after_attrs.get(k):
                delta[k] = {
                    "before": before_attrs.get(k),
                    "after": after_attrs.get(k),
                }
        return "attrs", delta

    # Смешанное изменение — композит без сложной классификации;
    # BRD-19 compute_inverse обработает через snapshot before/after.
    return "composite", {
        "changed_fields": sorted(changed),
        "before": {k: before.get(k) for k in changed},
        "after": {k: after.get(k) for k in changed},
    }


async def _load_touchers(
    db: AsyncSession, target_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Читает touchers всех target'ов ONE query."""
    if not target_ids:
        return {}
    rows = (
        await db.execute(
            select(BoardElement.id, BoardElement.touchers).where(
                BoardElement.id.in_(target_ids)
            )
        )
    ).all()
    return {row.id: list(row.touchers or []) for row in rows}


async def record_action(
    db: AsyncSession,
    *,
    board_id: uuid.UUID,
    executor_uuid: uuid.UUID,
    kind: str,
    target_ids: list[uuid.UUID],
    delta: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    ts_ms: int | None = None,
) -> BoardAction | None:
    """Пишет action row + обновляет touchers у всех target'ов.

    Возвращает вставленный row (или None если kind='noop' — тогда
    ничего не пишем, mutation была холостая).

    ВАЖНО: вызов должен быть в текущей transaction'е mutation'а —
    commit делает caller (endpoint).
    """
    if kind == "noop":
        return None

    ts = ts_ms if ts_ms is not None else now_ms()
    executor_str = str(executor_uuid)

    touchers_map = await _load_touchers(db, target_ids)

    associated: set[str] = {executor_str}
    for tid in target_ids:
        for u in touchers_map.get(tid, []):
            associated.add(u)

    # Пополняем touchers у каждого target'а executor'ом, если ещё нет.
    for tid, current in touchers_map.items():
        if executor_str not in current:
            new_list = list(current) + [executor_str]
            await db.execute(
                update(BoardElement)
                .where(BoardElement.id == tid)
                .values(touchers=new_list)
            )

    # Для target'ов, ещё не существующих в БД на момент action'а
    # (kind='create' — element только что вставили), touchers = [executor].
    # SQLAlchemy обновит default=list через ORM, но explicit set надёжнее:
    missing_ids = [tid for tid in target_ids if tid not in touchers_map]
    if missing_ids:
        await db.execute(
            update(BoardElement)
            .where(BoardElement.id.in_(missing_ids))
            .values(touchers=[executor_str])
        )

    action = BoardAction(
        id=uuid.uuid4(),
        board_id=board_id,
        executor_uuid=executor_uuid,
        associated_users=sorted(associated),
        kind=kind,
        target_ids=[str(t) for t in target_ids],
        delta=delta or {},
        snapshot=snapshot,
        ts_ms=ts,
        undone=False,
        pruned=False,
    )
    db.add(action)
    await db.flush()  # чтобы pruning-запрос увидел новый row в этой tx.

    # BRD-19: cap N=100 per user. При превышении — mark самого старого
    # non-pruned как pruned. Инвариант: до этой mutation'а stack каждого
    # user'а ≤ cap; новый action может поднять его до cap+1, значит
    # достаточно одного prune-hit per associated_user.
    for u in associated:
        stack_size = (
            await db.execute(
                select(func.count(BoardAction.id)).where(
                    BoardAction.board_id == board_id,
                    BoardAction.pruned.is_(False),
                    BoardAction.associated_users.op("@>")(
                        func.jsonb_build_array(text(f"'{u}'::text"))
                    ),
                )
            )
        ).scalar_one()
        if stack_size > UNDO_STACK_CAP:
            oldest_id = (
                await db.execute(
                    select(BoardAction.id).where(
                        BoardAction.board_id == board_id,
                        BoardAction.pruned.is_(False),
                        BoardAction.associated_users.op("@>")(
                            func.jsonb_build_array(text(f"'{u}'::text"))
                        ),
                    )
                    .order_by(BoardAction.ts_ms.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if oldest_id is not None:
                await db.execute(
                    update(BoardAction)
                    .where(BoardAction.id == oldest_id)
                    .values(pruned=True)
                )

    return action


async def attach_undo_state(
    db: AsyncSession,
    *,
    board_id: uuid.UUID,
    event: dict,
    associated_users: list[str] | None,
) -> None:
    """BRD-19: обогащает SSE event map'ой undo_state per user.

    Клиент фильтрует по своему user_uuid — обновляет кнопки Undo/Redo.
    Ленивый import — избегаем цикла (undo_engine → undo_log).
    """
    if not associated_users:
        return
    from app.core.undo_engine import compute_undo_state_map

    event["undo_state"] = await compute_undo_state_map(
        db, board_id=board_id, user_uuids=associated_users
    )
