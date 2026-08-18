"""BRD-24 acceptance: единый batch mutation endpoint + composite undo.

Покрывает epic-level AC:

- POST /elements/batch: patch и delete в одном body.
- Один composite action в board_actions.
- apply_undo/redo для composite: loop по items с per-item kind.
- Advisory lock — concurrency сериализуется без ошибок.
- Frame cascade: batch move frame → children едут, undo восстанавливает.
- Permission: guest без can_write → 403.
- Attrs mixed types: rect + text, fill change — reserve-логика ``skipped``.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


# ─────────────────── Single item через batch (regression) ────────────

async def test_single_move_via_batch_and_undo(
    client, fake_headers, test_board, make_element, db,
):
    el_id = await make_element(test_board, x=100.0, y=200.0)

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(el_id), "op": "patch", "patch": {"x": 500.0}}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == [str(el_id)]
    assert body["skipped"] == []

    # DB check: element.x updated + composite action записан.
    el = await db.get(BoardElement, el_id)
    assert el.x == 500.0

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    items = action.delta.get("items") or []
    assert len(items) == 1
    assert items[0]["kind"] == "move"
    assert items[0]["target_id"] == str(el_id)
    assert items[0]["before"] == {"x": 100.0}
    assert items[0]["after"] == {"x": 500.0}

    # Undo: возвращает x=100.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text

    await db.refresh(el)
    assert el.x == 100.0


# ─────────────────── Multi-select move (BRD-25 preview) ──────────────

async def test_multi_move_composite_undo_returns_all_at_once(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board, x=100.0 * i, y=0.0) for i in range(1, 4)]

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(i), "op": "patch", "patch": {"x": 999.0}} for i in ids
        ]},
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["applied"]) == {str(i) for i in ids}

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    assert len(action.delta["items"]) == 3

    # Один Ctrl+Z должен вернуть всех трёх.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text

    for i, original_x in zip(ids, [100.0, 200.0, 300.0]):
        el = await db.get(BoardElement, i)
        await db.refresh(el)
        assert el.x == original_x, f"element {i} not restored: x={el.x}"


# ─────────────────── Single delete через batch + undo ───────────────

async def test_single_delete_via_batch_and_undo(
    client, fake_headers, test_board, make_element, db,
):
    el_id = await make_element(test_board)

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(el_id), "op": "delete"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == [str(el_id)]

    el = await db.get(BoardElement, el_id)
    await db.refresh(el)
    assert el.deleted_at is not None

    # Undo восстанавливает.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text

    await db.refresh(el)
    assert el.deleted_at is None


# ─────────────────── Mixed batch: patch + delete в одном call ────────

async def test_mixed_batch_patch_and_delete_are_one_composite(
    client, fake_headers, test_board, make_element, db,
):
    keep_id = await make_element(test_board, x=10.0)
    drop_id = await make_element(test_board, x=20.0)

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(keep_id), "op": "patch", "patch": {"x": 555.0}},
            {"id": str(drop_id), "op": "delete"},
        ]},
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["applied"]) == {str(keep_id), str(drop_id)}

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    items_by_id = {i["target_id"]: i for i in action.delta["items"]}
    assert items_by_id[str(keep_id)]["kind"] == "move"
    assert items_by_id[str(drop_id)]["kind"] == "delete"

    # Undo: обе операции откатываются одним pop'ом.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200

    keep = await db.get(BoardElement, keep_id)
    drop = await db.get(BoardElement, drop_id)
    await db.refresh(keep)
    await db.refresh(drop)
    assert keep.x == 10.0
    assert drop.deleted_at is None


# ─────────────────── Frame cascade + undo ─────────────────────────────

async def test_frame_move_with_explicit_children_and_undo(
    client, fake_headers, test_board, make_element, db,
):
    """BRD-35: backend больше НЕ выполняет hidden cascade. Caller
    (frontend drag handler) обязан включить children в batch как
    отдельные items с их новыми absolute positions."""
    frame_id = await make_element(test_board, type_="frame", x=0.0, y=0.0, w=400.0, h=300.0)
    ch1 = await make_element(test_board, x=20.0, y=30.0, parent_id=frame_id)
    ch2 = await make_element(test_board, x=50.0, y=60.0, parent_id=frame_id)

    # Frontend вычисляет новые positions и посылает все items explicit.
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [
            {"id": str(frame_id), "op": "patch", "patch": {"x": 100.0, "y": 200.0}},
            {"id": str(ch1), "op": "patch", "patch": {"x": 120.0, "y": 230.0}},
            {"id": str(ch2), "op": "patch", "patch": {"x": 150.0, "y": 260.0}},
        ]},
    )
    assert r.status_code == 200, r.text

    c1 = await db.get(BoardElement, ch1)
    c2 = await db.get(BoardElement, ch2)
    await db.refresh(c1)
    await db.refresh(c2)
    assert c1.x == 120.0 and c1.y == 230.0
    assert c2.x == 150.0 and c2.y == 260.0

    # Composite action содержит 3 отдельных item snapshots — БЕЗ
    # cascade_children (BRD-35 удаление).
    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert len(action.delta["items"]) == 3
    for item in action.delta["items"]:
        assert "cascade_children" not in item

    # Undo: frame + children все возвращаются на исходные (per-item snapshot).
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200

    frame = await db.get(BoardElement, frame_id)
    await db.refresh(frame)
    await db.refresh(c1)
    await db.refresh(c2)
    assert frame.x == 0.0 and frame.y == 0.0
    assert c1.x == 20.0 and c1.y == 30.0
    assert c2.x == 50.0 and c2.y == 60.0


# ─────────────────── Advisory lock — concurrency ─────────────────────

async def test_concurrent_batches_are_serialized_no_errors(
    client, fake_headers, test_board, make_element, db,
):
    a = await make_element(test_board, x=1.0)
    b = await make_element(test_board, x=2.0)

    async def do_batch(target_id, new_x):
        return await client.post(
            f"/boards/{test_board}/elements/batch",
            headers=fake_headers,
            json={"items": [{"id": str(target_id), "op": "patch", "patch": {"x": new_x}}]},
        )

    r1, r2 = await asyncio.gather(do_batch(a, 100.0), do_batch(b, 200.0))
    assert r1.status_code == 200
    assert r2.status_code == 200

    await db.commit()
    ea = await db.get(BoardElement, a)
    eb = await db.get(BoardElement, b)
    await db.refresh(ea)
    await db.refresh(eb)
    assert ea.x == 100.0
    assert eb.x == 200.0

    # Оба action-record'а отдельные (composite) — не потеряны из-за lock.
    n = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
    )).scalars().all()
    kinds = [x.kind for x in n]
    assert kinds.count("composite") == 2


# ─────────────────── Permission: guest → 403 ─────────────────────────

async def test_guest_without_write_gets_403(
    client, guest_headers, test_board, make_element,
):
    el_id = await make_element(test_board)  # created by owner (Юрий)
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=guest_headers,
        json={"items": [{"id": str(el_id), "op": "patch", "patch": {"x": 999.0}}]},
    )
    assert r.status_code == 403, r.text


# ─────────────────── Validation: item id вне доски → 400 ─────────────

async def test_item_not_in_board_returns_400(
    client, fake_headers, test_board,
):
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(uuid.uuid4()), "op": "patch", "patch": {"x": 1.0}}]},
    )
    assert r.status_code == 400
    assert "invalid_batch_item" in r.text


# ─────────────────── Attrs merge (BRD-27 preview) ────────────────────

async def test_attrs_change_via_batch_and_undo_restores(
    client, fake_headers, test_board, make_element, db,
):
    el_id = await make_element(test_board, attrs={"fill": "red"})

    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(el_id), "op": "patch", "patch": {"attrs": {"fill": "blue"}}}]},
    )
    assert r.status_code == 200, r.text
    el = await db.get(BoardElement, el_id)
    await db.refresh(el)
    assert el.attrs["fill"] == "blue"

    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    await db.refresh(el)
    assert el.attrs["fill"] == "red"
