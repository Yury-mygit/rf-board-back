"""BRD-29 regression: undo/redo для child-of-frame через composite batch.

Root cause (гипотеза до fix'а): `_apply_item` в composite handler
не имеет ветки `if kind == "mixed"`. Singleton `apply_undo`/`apply_redo`
поддерживают mixed (undo_engine.py:192-208 / :434-...), но
composite `_apply_item` (:213-324) — нет. Batch endpoint ВСЕГДА
пишет `kind="composite"` даже для single-item batch (line 548:
`kind="composite"`), поэтому любой single-target patch с диффом
`{x, y, parent_id}` → composite → item kind="mixed" → _apply_item
возвращает None → ни undo, ни redo не работают.

Scenarios:

- Child внутри frame, move в пределах того же frame (parent_id не
  меняется) → item kind="move" → **работает** (control test).
- Child внутри frame, move ЗА пределы frame (parent_id → null) →
  item kind="mixed" → **не работает** до fix'а.
- Child, move между двумя frame'ами (parent_id → other frame) →
  item kind="mixed" → **не работает** до fix'а.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


# ─── Control: child остаётся в том же frame → kind="move" ────────────

async def test_child_move_within_same_frame_undo_redo(
    client, fake_headers, test_board, make_element, db,
):
    frame = await make_element(
        test_board, type_="frame", x=0.0, y=0.0, w=1000.0, h=1000.0,
    )
    child = await make_element(
        test_board, x=100.0, y=100.0, w=50.0, h=50.0, parent_id=frame,
    )

    # Move в пределах frame + parentId неизменный.
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{
            "id": str(child), "op": "patch",
            "patch": {"x": 300.0, "y": 400.0, "parentId": str(frame)},
        }]},
    )
    assert r.status_code == 200, r.text

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    assert action.delta["items"][0]["kind"] == "move"

    # Undo — вернулся на исходное.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (100.0, 100.0)

    # Redo — снова на новое место.
    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (300.0, 400.0)


# ─── Regression: child вылетел из frame → kind="mixed" ───────────────

async def test_child_move_out_of_frame_undo_redo(
    client, fake_headers, test_board, make_element, db,
):
    frame = await make_element(
        test_board, type_="frame", x=0.0, y=0.0, w=1000.0, h=1000.0,
    )
    child = await make_element(
        test_board, x=100.0, y=100.0, w=50.0, h=50.0, parent_id=frame,
    )

    # Move + drop parent_id (вылетел из frame): x, y, parent_id
    # изменяются одновременно.
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{
            "id": str(child), "op": "patch",
            "patch": {"x": 5000.0, "y": 5000.0, "parentId": None},
        }]},
    )
    assert r.status_code == 200, r.text

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    item = action.delta["items"][0]
    # До fix'а: item kind="mixed", _apply_item не поддерживает.
    assert item["kind"] == "mixed"
    # before/after содержат все три поля.
    assert set(item["before"].keys()) >= {"x", "y", "parent_id"}
    assert set(item["after"].keys()) >= {"x", "y", "parent_id"}

    # Sanity — сам move в БД применился.
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (5000.0, 5000.0)
    assert ch.parent_id is None

    # Undo — child должен вернуться в frame на исходное место.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (100.0, 100.0), (
        f"undo не восстановил x/y: (x={ch.x}, y={ch.y})"
    )
    assert ch.parent_id == frame, (
        f"undo не восстановил parent_id: {ch.parent_id} vs frame={frame}"
    )

    # Redo — снова наружу.
    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (5000.0, 5000.0), (
        f"redo не применил move: (x={ch.x}, y={ch.y})"
    )
    assert ch.parent_id is None, (
        f"redo не сбросил parent_id: {ch.parent_id}"
    )


# ─── Regression: child переехал в другой frame → kind="mixed" ────────

async def test_child_move_between_frames_undo_redo(
    client, fake_headers, test_board, make_element, db,
):
    frame_a = await make_element(
        test_board, type_="frame", x=0.0, y=0.0, w=1000.0, h=1000.0,
    )
    frame_b = await make_element(
        test_board, type_="frame", x=2000.0, y=0.0, w=1000.0, h=1000.0,
    )
    child = await make_element(
        test_board, x=100.0, y=100.0, w=50.0, h=50.0, parent_id=frame_a,
    )

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{
            "id": str(child), "op": "patch",
            "patch": {"x": 2100.0, "y": 200.0, "parentId": str(frame_b)},
        }]},
    )
    assert r.status_code == 200, r.text

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    item = action.delta["items"][0]
    assert item["kind"] == "mixed"

    # Undo — обратно в frame_a на исходные координаты.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (100.0, 100.0), (
        f"undo не восстановил x/y: (x={ch.x}, y={ch.y})"
    )
    assert ch.parent_id == frame_a, (
        f"undo не восстановил parent_id: {ch.parent_id} vs frame_a={frame_a}"
    )

    # Redo — обратно в frame_b.
    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    ch = await _fresh(db, child)
    assert (ch.x, ch.y) == (2100.0, 200.0)
    assert ch.parent_id == frame_b
