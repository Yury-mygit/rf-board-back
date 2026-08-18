"""BRD-27 regression: multi-select attrs change → один composite action
c per-item attrs delta. Undo восстанавливает индивидуальные before-values.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


async def test_multi_attrs_one_composite_undo_restores_individual(
    client, fake_headers, test_board, make_element, db,
):
    """3 rect'а с разными fill'ами → batch attrs change → 1 composite,
    undo восстанавливает индивидуальные fills."""
    fills = ["red", "green", "blue"]
    ids = []
    for f in fills:
        el = await make_element(test_board, attrs={"fill": f})
        ids.append(el)

    # Batch: изменить fill на "purple" у всех (frontend отправляет full attrs).
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(i), "op": "patch", "patch": {"attrs": {"fill": "purple"}}}
            for i in ids
        ]},
    )
    assert r.status_code == 200
    assert set(r.json()["applied"]) == {str(i) for i in ids}

    # Один composite action c 3 attrs-items.
    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    assert len(action.delta["items"]) == 3
    for item in action.delta["items"]:
        assert item["kind"] == "attrs"

    # Verify current state.
    for i in ids:
        el = await _fresh(db, i)
        assert el.attrs.get("fill") == "purple"

    # Undo → каждый возвращается на свой original fill.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    for i, orig in zip(ids, fills):
        el = await _fresh(db, i)
        assert el.attrs.get("fill") == orig, (
            f"BRD-27: fill не восстановлен individual: got {el.attrs.get('fill')}, expected {orig}"
        )


async def test_multi_attrs_partial_change_only(
    client, fake_headers, test_board, make_element, db,
):
    """Изменяется только fill; stroke и другие attrs не задеты."""
    ids = []
    for f in ["red", "green"]:
        el = await make_element(test_board, attrs={"fill": f, "stroke": "black", "opacity": 0.5})
        ids.append(el)

    # Batch fill change, отправляем full attrs (frontend behavior).
    for i, f in zip(ids, ["red", "green"]):
        pass  # используем existing attrs
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(i), "op": "patch",
             "patch": {"attrs": {"fill": "yellow", "stroke": "black", "opacity": 0.5}}}
            for i in ids
        ]},
    )
    assert r.status_code == 200

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    # attrs kind — diff содержит только changed keys (fill), не stroke/opacity.
    for item in action.delta["items"]:
        assert item["kind"] == "attrs"
        # before/after — per-key attrs delta, only "fill" changed
        assert "fill" in item["before"] and "fill" in item["after"]
        assert item["after"]["fill"] == "yellow"

    # Verify current state.
    for i in ids:
        el = await _fresh(db, i)
        assert el.attrs.get("fill") == "yellow"
        assert el.attrs.get("stroke") == "black"

    # Undo restores fill only.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    for i, orig in zip(ids, ["red", "green"]):
        el = await _fresh(db, i)
        assert el.attrs.get("fill") == orig
        assert el.attrs.get("stroke") == "black"
