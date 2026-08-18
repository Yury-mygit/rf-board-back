"""BRD-19: движок undo/redo — apply inverse/forward + per-user state.

Использует action-лог из BRD-18 (`board_actions` + `board_elements.touchers`).

Публичный API:
- `apply_undo(db, action)` — откатывает action, возвращает SSE payload dict.
- `apply_redo(db, action)` — применяет action заново, возвращает SSE payload dict.
- `describe_action(action)` — human-readable описание для tooltip.
- `compute_undo_state(db, board_id, user_uuid)` — {canUndo, canRedo, next_*_desc}.
- `compute_undo_state_map(db, board_id, user_uuids)` — batch.

Модель γ2:
- Stack пользователя U = board_actions WHERE :U ∈ associated_users AND NOT
  pruned, sort'но по ts_ms DESC.
- Undo: mark undone=true. Redo: mark undone=false.
- SSE broadcast — стандартный element_* payload с sidecar undo_state.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import now_ms
from app.models.models import BoardAction, BoardElement


def _el_payload(el: BoardElement) -> dict:
    """Локальная копия сериализатора (импорт из boards.py создал бы cycle)."""
    return {
        "id": str(el.id),
        "board_id": str(el.board_id),
        "type": el.type,
        "external_ref": str(el.external_ref) if el.external_ref else None,
        "parent_id": str(el.parent_id) if el.parent_id else None,
        "z_index": el.z_index,
        "x": el.x, "y": el.y, "w": el.w, "h": el.h,
        "attrs": el.attrs or {},
        "created_at": el.created_at, "updated_at": el.updated_at,
        "deleted_at": el.deleted_at,
    }


# ─────────────────────────── description ─────────────────────────────

_KIND_LABELS = {
    "create": "создание объекта",
    "delete": "удаление объекта",
    "move": "перемещение объекта",
    "resize": "изменение размера",
    "attrs": "изменение свойств",
    "parent": "перепривязку к фрейму",
    "z_order": "изменение слоя",
    "mixed": "изменение нескольких свойств",
    "composite": "групповое действие",
}


def describe_action(action: BoardAction) -> str:
    """Короткая строка для UI tooltip."""
    label = _KIND_LABELS.get(action.kind, action.kind)
    n = len(action.target_ids or [])
    if n > 1:
        return f"{label} × {n}"
    return label


# ─────────────────────────── apply undo / redo ─────────────────────────────

async def _get_element(db: AsyncSession, target_id_str: str) -> BoardElement | None:
    return await db.get(BoardElement, uuid.UUID(target_id_str))


async def apply_undo(db: AsyncSession, action: BoardAction) -> dict | None:
    """Откатывает action. Возвращает SSE payload dict или None если no-op.

    Мутации выполняются напрямую через ORM — БЕЗ вызовов endpoint-функций,
    чтобы не логировать откат как новый action.
    """
    kind = action.kind
    ts = now_ms()

    # BRD-24: composite (multi-target) обрабатывается до pre-compute'а
    # первого target'а, т.к. items имеют собственные target_id.
    if kind == "composite":
        items = (action.delta or {}).get("items") or []
        pieces = []
        for item in items:
            piece = await _apply_item(db, item, ts, direction="undo")
            if piece is not None:
                pieces.append(piece)
        if not pieces:
            return None
        return {"type": "elements_batch_patched", "items": pieces}

    tid = action.target_ids[0] if action.target_ids else None
    if tid is None:
        return None
    el = await _get_element(db, tid)
    if el is None:
        return None

    if kind == "create":
        # undo create → soft-delete.
        if el.deleted_at is not None:
            return None  # уже удалён
        el.deleted_at = ts
        el.updated_at = ts
        return {
            "type": "element_deleted",
            "element_id": str(el.id),
            "ts": ts,
        }

    if kind == "delete":
        # undo delete → clear deleted_at (attrs сохранились с момента delete).
        if el.deleted_at is None:
            return None  # уже восстановлен
        el.deleted_at = None
        el.updated_at = ts
        return {"type": "element_upserted", "element": _el_payload(el)}

    if kind == "move":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        el.x = el.x - float(delta.get("dx", 0) or 0)
        el.y = el.y - float(delta.get("dy", 0) or 0)
        el.updated_at = ts
        # Cascade children для frame move — восстанавливаем before-позиции.
        cascade = delta.get("cascade_children") or []
        cascade_dx = -float(delta.get("dx", 0) or 0)
        cascade_dy = -float(delta.get("dy", 0) or 0)
        for c_info in cascade:
            child = await _get_element(db, c_info["id"])
            if child is None or child.deleted_at is not None:
                continue
            child.x = float(c_info["x"])
            child.y = float(c_info["y"])
            child.updated_at = ts
        payload = {"type": "element_patched", "element": _el_payload(el)}
        if cascade_dx or cascade_dy:
            payload["cascade_dx"] = cascade_dx
            payload["cascade_dy"] = cascade_dy
        return payload

    if kind == "resize":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        el.x = el.x - float(delta.get("dx", 0) or 0)
        el.y = el.y - float(delta.get("dy", 0) or 0)
        el.w = el.w - float(delta.get("dw", 0) or 0)
        el.h = el.h - float(delta.get("dh", 0) or 0)
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "attrs":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        # per-key merge — восстанавливаем только затронутые keys.
        merged = dict(el.attrs or {})
        for k, diff in delta.items():
            before = (diff or {}).get("before") if isinstance(diff, dict) else None
            if before is None:
                merged.pop(k, None)
            else:
                merged[k] = before
        el.attrs = merged
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "parent":
        if el.deleted_at is not None:
            return None
        before = (action.delta or {}).get("before")
        el.parent_id = uuid.UUID(before) if before else None
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "z_order":
        if el.deleted_at is not None:
            return None
        before = (action.delta or {}).get("before")
        el.z_index = int(before) if before is not None else 0
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "mixed":
        if el.deleted_at is not None:
            return None
        # mixed: single-target snapshot changed_fields.
        delta = action.delta or {}
        before = delta.get("before") or {}
        for k, v in before.items():
            if k == "attrs":
                el.attrs = v or {}
            elif k == "parent_id":
                el.parent_id = uuid.UUID(v) if v else None
            elif k == "z_index":
                el.z_index = int(v) if v is not None else 0
            else:
                setattr(el, k, v)
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    return None


async def _apply_item(
    db: AsyncSession, item: dict, ts: int, *, direction: str
) -> dict | None:
    """BRD-24: применяет один per-target sub-action в composite.

    `direction="undo"` — восстанавливает `before`; `"redo"` — применяет `after`.
    Возвращает payload piece для bulk-event:
      - `{"element": {...}}` для upsert/patch/restore
      - `{"element_id": "<id>", "deleted": true}` для soft-delete
    None если item не применим (target missing, no-op на current state).
    """
    kind = item.get("kind")
    tid = item.get("target_id")
    if not tid:
        return None
    el = await _get_element(db, tid)
    if el is None:
        return None
    snap = item.get("before" if direction == "undo" else "after") or {}

    # delete/create — semantic flip между undo и redo одинаково для обоих
    # направлений: undo create = soft-delete, redo create = un-soft-delete;
    # undo delete = un-soft-delete, redo delete = soft-delete.
    if kind == "create":
        target_deleted = direction == "undo"
        if target_deleted:
            if el.deleted_at is not None:
                return None
            el.deleted_at = ts
            el.updated_at = ts
            return {"element_id": str(el.id), "deleted": True}
        else:
            if el.deleted_at is None:
                return None
            el.deleted_at = None
            el.updated_at = ts
            return {"element": _el_payload(el)}

    if kind == "delete":
        target_deleted = direction == "redo"
        if target_deleted:
            if el.deleted_at is not None:
                return None
            el.deleted_at = ts
            el.updated_at = ts
            return {"element_id": str(el.id), "deleted": True}
        else:
            if el.deleted_at is None:
                return None
            el.deleted_at = None
            el.updated_at = ts
            return {"element": _el_payload(el)}

    if el.deleted_at is not None:
        return None

    if kind in ("move", "resize"):
        for k in ("x", "y", "w", "h"):
            if k in snap:
                setattr(el, k, snap[k])
        # BRD-35: legacy `cascade_children` в старых delta rows —
        # backward compat, продолжаем apply чтобы pre-refactor history
        # undo работал корректно. Новые actions (после BRD-35) НЕ пишут
        # cascade_children в delta — каждый child = отдельный item.
        cascade = item.get("cascade_children") or []
        if cascade:
            if direction == "undo":
                for c in cascade:
                    child = await _get_element(db, c["id"])
                    if child is None or child.deleted_at is not None:
                        continue
                    child.x = float(c["x"])
                    child.y = float(c["y"])
                    child.updated_at = ts
            else:
                before_pos = item.get("before") or {}
                dx = float(snap.get("x", 0)) - float(before_pos.get("x", 0))
                dy = float(snap.get("y", 0)) - float(before_pos.get("y", 0))
                for c in cascade:
                    child = await _get_element(db, c["id"])
                    if child is None or child.deleted_at is not None:
                        continue
                    child.x = float(c["x"]) + dx
                    child.y = float(c["y"]) + dy
                    child.updated_at = ts
        el.updated_at = ts
        return {"element": _el_payload(el)}

    if kind == "attrs":
        # snap = {key: value_or_null}; None → key deleted.
        merged = dict(el.attrs or {})
        for k, v in snap.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        el.attrs = merged
        el.updated_at = ts
        return {"element": _el_payload(el)}

    if kind == "parent":
        v = snap.get("parent_id")
        el.parent_id = uuid.UUID(v) if v else None
        el.updated_at = ts
        return {"element": _el_payload(el)}

    if kind == "z_order":
        v = snap.get("z_index")
        if v is not None:
            el.z_index = int(v)
        el.updated_at = ts
        return {"element": _el_payload(el)}

    if kind == "mixed":
        # BRD-29 fix: composite items с несколькими изменившимися полями
        # (типично для drag child'а из frame — x/y/parent_id меняются
        # одновременно). Формат snap повторяет singleton mixed
        # (undo_engine.py:192-208): плоский dict per changed field;
        # attrs — per-key под ключом "attrs".
        for k, v in snap.items():
            if k == "attrs":
                merged = dict(el.attrs or {})
                for ak, av in (v or {}).items():
                    if av is None:
                        merged.pop(ak, None)
                    else:
                        merged[ak] = av
                el.attrs = merged
            elif k == "parent_id":
                el.parent_id = uuid.UUID(v) if v else None
            elif k == "z_index":
                el.z_index = int(v) if v is not None else 0
            else:
                setattr(el, k, v)
        el.updated_at = ts
        return {"element": _el_payload(el)}

    return None


async def apply_redo(db: AsyncSession, action: BoardAction) -> dict | None:
    """Применяет action заново (после undo). Возвращает SSE payload dict."""
    kind = action.kind
    ts = now_ms()

    # BRD-24: composite (multi-target) — до pre-compute'а первого target'а.
    if kind == "composite":
        items = (action.delta or {}).get("items") or []
        pieces = []
        for item in items:
            piece = await _apply_item(db, item, ts, direction="redo")
            if piece is not None:
                pieces.append(piece)
        if not pieces:
            return None
        return {"type": "elements_batch_patched", "items": pieces}

    tid = action.target_ids[0] if action.target_ids else None
    if tid is None:
        return None
    el = await _get_element(db, tid)
    if el is None:
        return None

    if kind == "create":
        # redo create → un-soft-delete (был удалён предыдущим undo).
        if el.deleted_at is None:
            return None
        el.deleted_at = None
        el.updated_at = ts
        return {"type": "element_upserted", "element": _el_payload(el)}

    if kind == "delete":
        if el.deleted_at is not None:
            return None
        el.deleted_at = ts
        el.updated_at = ts
        return {
            "type": "element_deleted",
            "element_id": str(el.id),
            "ts": ts,
        }

    if kind == "move":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        el.x = el.x + float(delta.get("dx", 0) or 0)
        el.y = el.y + float(delta.get("dy", 0) or 0)
        el.updated_at = ts
        cascade = delta.get("cascade_children") or []
        cascade_dx = float(delta.get("dx", 0) or 0)
        cascade_dy = float(delta.get("dy", 0) or 0)
        for c_info in cascade:
            child = await _get_element(db, c_info["id"])
            if child is None or child.deleted_at is not None:
                continue
            # Redo: смещение от before-позиции на +dx/+dy.
            child.x = float(c_info["x"]) + cascade_dx
            child.y = float(c_info["y"]) + cascade_dy
            child.updated_at = ts
        payload = {"type": "element_patched", "element": _el_payload(el)}
        if cascade_dx or cascade_dy:
            payload["cascade_dx"] = cascade_dx
            payload["cascade_dy"] = cascade_dy
        return payload

    if kind == "resize":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        el.x = el.x + float(delta.get("dx", 0) or 0)
        el.y = el.y + float(delta.get("dy", 0) or 0)
        el.w = el.w + float(delta.get("dw", 0) or 0)
        el.h = el.h + float(delta.get("dh", 0) or 0)
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "attrs":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        merged = dict(el.attrs or {})
        for k, diff in delta.items():
            after = (diff or {}).get("after") if isinstance(diff, dict) else None
            if after is None:
                merged.pop(k, None)
            else:
                merged[k] = after
        el.attrs = merged
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "parent":
        if el.deleted_at is not None:
            return None
        after = (action.delta or {}).get("after")
        el.parent_id = uuid.UUID(after) if after else None
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "z_order":
        if el.deleted_at is not None:
            return None
        after = (action.delta or {}).get("after")
        el.z_index = int(after) if after is not None else 0
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    if kind == "mixed":
        if el.deleted_at is not None:
            return None
        delta = action.delta or {}
        after = delta.get("after") or {}
        for k, v in after.items():
            if k == "attrs":
                el.attrs = v or {}
            elif k == "parent_id":
                el.parent_id = uuid.UUID(v) if v else None
            elif k == "z_index":
                el.z_index = int(v) if v is not None else 0
            else:
                setattr(el, k, v)
        el.updated_at = ts
        return {"type": "element_patched", "element": _el_payload(el)}

    return None


# ─────────────────────────── per-user state ─────────────────────────────

def _user_in_associated(user_uuid_str: str):
    """SQLAlchemy expression: :user IN associated_users JSONB array."""
    return BoardAction.associated_users.op("@>")(
        func.jsonb_build_array(text(f"'{user_uuid_str}'::text"))
    )


async def compute_undo_state(
    db: AsyncSession,
    *,
    board_id: uuid.UUID,
    user_uuid: uuid.UUID,
) -> dict:
    """Один запрос на канду undoable, один на redoable — top-1 по ts_ms."""
    user_str = str(user_uuid)
    # Top undoable: latest non-undone action в стеке пользователя.
    undo_row = (
        await db.execute(
            select(BoardAction)
            .where(
                BoardAction.board_id == board_id,
                BoardAction.pruned.is_(False),
                BoardAction.undone.is_(False),
                _user_in_associated(user_str),
            )
            .order_by(BoardAction.ts_ms.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Top redoable: latest undone action.
    redo_row = (
        await db.execute(
            select(BoardAction)
            .where(
                BoardAction.board_id == board_id,
                BoardAction.pruned.is_(False),
                BoardAction.undone.is_(True),
                _user_in_associated(user_str),
            )
            .order_by(BoardAction.ts_ms.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "canUndo": undo_row is not None,
        "canRedo": redo_row is not None,
        "next_undo_desc": describe_action(undo_row) if undo_row else None,
        "next_redo_desc": describe_action(redo_row) if redo_row else None,
    }


async def compute_undo_state_map(
    db: AsyncSession,
    *,
    board_id: uuid.UUID,
    user_uuids: Iterable[str],
) -> dict[str, dict]:
    """Батч: {user_uuid_str: undo_state_dict}. N в реальных условиях 1-5."""
    result: dict[str, dict] = {}
    for u in set(user_uuids):
        try:
            uid = uuid.UUID(u)
        except (ValueError, TypeError):
            continue
        result[u] = await compute_undo_state(
            db, board_id=board_id, user_uuid=uid
        )
    return result


# ─────────────────────────── stack locate ─────────────────────────────

async def pop_undoable(
    db: AsyncSession,
    *,
    board_id: uuid.UUID,
    user_uuid: uuid.UUID,
) -> BoardAction | None:
    """SELECT top-1 non-undone action of user's stack FOR UPDATE.

    Возвращает row (уже locked) или None если stack пуст.
    Caller ставит undone=true после apply.
    """
    user_str = str(user_uuid)
    row = (
        await db.execute(
            select(BoardAction)
            .where(
                BoardAction.board_id == board_id,
                BoardAction.pruned.is_(False),
                BoardAction.undone.is_(False),
                _user_in_associated(user_str),
            )
            .order_by(BoardAction.ts_ms.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    return row


async def pop_redoable(
    db: AsyncSession,
    *,
    board_id: uuid.UUID,
    user_uuid: uuid.UUID,
) -> BoardAction | None:
    """SELECT top-1 undone action of user's redo stack FOR UPDATE.

    BRD-33 fix: `ORDER BY ts_ms ASC` (не DESC). Semantic — redo идёт
    в обратном порядке к undo. Пример: undo снял move2 (последний по
    ts_ms), потом move1. Redo должен применить move1 первым (последний
    undone). Move1 имеет меньший ts_ms → ORDER ASC LIMIT 1 → move1.

    Cherry-pick edge cases (mid-stack undo без последующего) LIFO не
    покрывает — v2, отдельная задача.
    """
    user_str = str(user_uuid)
    row = (
        await db.execute(
            select(BoardAction)
            .where(
                BoardAction.board_id == board_id,
                BoardAction.pruned.is_(False),
                BoardAction.undone.is_(True),
                _user_in_associated(user_str),
            )
            .order_by(BoardAction.ts_ms.asc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    return row
