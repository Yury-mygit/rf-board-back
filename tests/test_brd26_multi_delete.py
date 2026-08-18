"""BRD-26 regression: multi-select Delete → один composite action,
undo восстанавливает все элементы одним pop'ом.

Инфра BRD-24 (batch endpoint с op=delete) уже поддерживает multi
delete как composite. Frontend fix: `deleteBoardSelected` теперь
отправляет один batch (было — dead reference `eraseElements` из
BRD-20 refactor).
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


async def test_multi_delete_one_composite_action(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board, x=100.0 * i) for i in range(1, 4)]
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(i), "op": "delete"} for i in ids]},
    )
    assert r.status_code == 200
    assert set(r.json()["applied"]) == {str(i) for i in ids}

    # Один composite action c 3 delete-items.
    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    assert len(action.delta["items"]) == 3
    for item in action.delta["items"]:
        assert item["kind"] == "delete"

    # Все элементы soft-deleted.
    for i in ids:
        el = await _fresh(db, i)
        assert el.deleted_at is not None

    # Один undo → все восстановлены.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    for i in ids:
        el = await _fresh(db, i)
        assert el.deleted_at is None


async def test_multi_delete_mixed_types_and_undo(
    client, fake_headers, test_board, make_element, db,
):
    """Mixed types (rect + text + line): все восстанавливаются одним pop'ом."""
    rect = await make_element(test_board, type_="rect")
    txt = await make_element(test_board, type_="text", attrs={"text": "hello"})
    line = await make_element(test_board, type_="line")
    ids = [rect, txt, line]

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(i), "op": "delete"} for i in ids]},
    )
    assert r.status_code == 200
    for i in ids:
        el = await _fresh(db, i)
        assert el.deleted_at is not None

    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    for i in ids:
        el = await _fresh(db, i)
        assert el.deleted_at is None
    # text должен сохранить attrs.
    txt_el = await _fresh(db, txt)
    assert txt_el.attrs.get("text") == "hello"
