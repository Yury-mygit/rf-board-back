"""BRD-34 regression: 2 последовательных drag'а того же элемента без
снятия выбора должны создавать 2 отдельных composite actions, не 1.

Bug на frontend: `flushMoveBatch` skip'ил single-item items (`< 2`), save
шёл через `scheduleElementSave` debounce 1000ms. Второй drag за <1s
cancel'ил pending debounce → один PATCH с итоговой позицией → одна action
с `before=pos1, after=pos3` (pos2 теряется).

Backend fix не нужен — endpoint корректно пишет одну action на batch.
Test проверяет что backend видит 2 batch call'а как 2 action'а
(что и было). Frontend-fix — в `main.js:flushMoveBatch` (см. коммит).

Ниже тест эмулирует правильное поведение frontend'а: два последовательных
batch patch без гэпа. Verify: 2 composite actions, undo LIFO 3→2→1.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


async def test_two_drags_produce_two_actions_undo_lifo(
    client, fake_headers, test_board, make_element, db,
):
    """create → move to pos2 → move to pos3 → undo → pos2 → undo → pos1."""
    rect = await make_element(test_board, x=100.0, y=100.0)

    # Drag 1: pos1 → pos2
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(rect), "op": "patch", "patch": {"x": 200.0, "y": 200.0}}]},
    )
    assert r.status_code == 200

    # Drag 2: pos2 → pos3 (сразу, без гэпа — эмуляция быстрого второго drag)
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(rect), "op": "patch", "patch": {"x": 300.0, "y": 300.0}}]},
    )
    assert r.status_code == 200

    # В лог должно быть 2 composite actions (не 1) для этого rect.
    actions = (await db.execute(
        select(BoardAction).where(
            BoardAction.board_id == test_board,
            BoardAction.kind == "composite",
        ).order_by(BoardAction.ts_ms.asc())
    )).scalars().all()
    # Filter only те, где target rect.
    rect_actions = [
        a for a in actions
        if any(str(rect) in t for t in (a.target_ids or []))
    ]
    assert len(rect_actions) == 2, (
        f"BRD-34: должно быть 2 отдельных action'а, было {len(rect_actions)}"
    )

    # undo #1 — возврат в pos2
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    el = await _fresh(db, rect)
    assert (el.x, el.y) == (200.0, 200.0), (
        f"undo #1 не вернул в pos2: (x={el.x}, y={el.y})"
    )

    # undo #2 — возврат в pos1
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    el = await _fresh(db, rect)
    assert (el.x, el.y) == (100.0, 100.0), (
        f"undo #2 не вернул в pos1: (x={el.x}, y={el.y})"
    )

    # redo x2 → возврат в pos3 через pos2.
    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    el = await _fresh(db, rect)
    assert (el.x, el.y) == (200.0, 200.0)

    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    el = await _fresh(db, rect)
    assert (el.x, el.y) == (300.0, 300.0)
