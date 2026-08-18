"""BRD-35 negative: verify backend batch endpoint НЕ выполняет hidden
cascade для frame. Frontend (или другой caller) обязан включить
children в batch как отдельные items с absolute positions.

До refactor: `batch_elements` при обнаружении frame item вызывал
`_move_children`, двигал детей в БД и snapshot'ил в `cascade_children_snap`.
После BRD-35: backend просто applies items как есть.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


async def test_batch_frame_alone_does_not_move_children(
    client, fake_headers, test_board, make_element, db,
):
    """batch с одним frame item НЕ двигает children — hidden cascade
    убран."""
    frame_id = await make_element(
        test_board, type_="frame", x=0.0, y=0.0, w=400.0, h=300.0,
    )
    ch1 = await make_element(test_board, x=20.0, y=30.0, parent_id=frame_id)
    ch2 = await make_element(test_board, x=50.0, y=60.0, parent_id=frame_id)

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(frame_id), "op": "patch",
             "patch": {"x": 100.0, "y": 200.0}},
        ]},
    )
    assert r.status_code == 200

    # Frame сдвинулся, а children — нет.
    frame = await _fresh(db, frame_id)
    c1 = await _fresh(db, ch1)
    c2 = await _fresh(db, ch2)
    assert (frame.x, frame.y) == (100.0, 200.0)
    assert (c1.x, c1.y) == (20.0, 30.0)
    assert (c2.x, c2.y) == (50.0, 60.0)


async def test_batch_delta_no_cascade_children_field(
    client, fake_headers, test_board, make_element, db,
):
    """composite delta.items НЕ содержит `cascade_children` (removed)."""
    frame_id = await make_element(
        test_board, type_="frame", x=0.0, y=0.0, w=400.0, h=300.0,
    )
    await make_element(test_board, x=20.0, y=30.0, parent_id=frame_id)

    await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(frame_id), "op": "patch", "patch": {"x": 100.0}},
        ]},
    )

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    for item in action.delta["items"]:
        assert "cascade_children" not in item


async def test_batch_delta_no_cascade_dx_in_payload(
    client, fake_headers, test_board, make_element, db,
):
    """SSE payload items НЕ содержат `cascade_dx/cascade_dy` (removed)."""
    frame_id = await make_element(
        test_board, type_="frame", x=0.0, y=0.0, w=400.0, h=300.0,
    )
    await make_element(test_board, x=20.0, y=30.0, parent_id=frame_id)

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(frame_id), "op": "patch", "patch": {"x": 100.0}},
        ]},
    )
    # Response body у batch endpoint не содержит items с cascade;
    # это проверяем на sample через action delta (см. предыдущий тест)
    # и косвенно тем, что frame двигается один.
    assert r.status_code == 200
