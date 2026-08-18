"""BRD-6 acceptance: server-authoritative z-order.

Покрывает decision D3-D11:

- Single front/back/forward/backward → kind="z_order" в undo-log,
  undo восстанавливает z_index.
- Multi-select → kind="composite" с массивом delta; один undo
  восстанавливает всех.
- Frame cascade → z-order на frame сдвигает его children (по parent_id).
- No-op guard (D10): front на уже верхнем / back на уже нижнем —
  200 OK, но НИ UPDATE, НИ record_action.
- Permission (D4): guest без can_write → 403.
- Validation: element_id вне доски → 400.
- Advisory lock (D11): 2 параллельных z-order → serialize без ошибок.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models.models import BoardAction, BoardElement


async def _fresh(db, id_):
    """Re-load элемент из БД (нужно после external HTTP mutation, которая
    коммитит в своём session'е — наш ``db`` fixture держит stale cache)."""
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


# ─────────────────── Single front (kind=z_order) + undo ──────────────

async def test_single_front_and_undo(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board, x=0.0) for _ in range(3)]
    # Порядок создания = z 0/1/2. Двигаем самый нижний (ids[0]) → front.
    before_z = [
        (await _fresh(db, i)).z_index for i in ids
    ]
    assert before_z == [0, 1, 2]

    r = await client.post(
        f"/boards/{test_board}/elements/{ids[0]}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"]
    # ids[0] должен уйти выше max_other (был 2 → стал 3).
    new_top = next(x for x in body["items"] if x["id"] == str(ids[0]))
    assert new_top["zIndex"] == 3

    for i in ids:
        await _fresh(db, i)
    assert (await _fresh(db, ids[0])).z_index == 3

    # Action записан как z_order (single flat delta).
    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "z_order"
    assert action.delta == {"before": 0, "after": 3}
    assert action.target_ids == [str(ids[0])]

    # Undo — z вернётся к 0.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text
    await _fresh(db, ids[0])
    assert (await _fresh(db, ids[0])).z_index == 0


# ─────────────────── Single back + undo ──────────────────────────────

async def test_single_back_and_undo(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board) for _ in range(3)]
    # Двигаем верхний (ids[2], z=2) → back.
    r = await client.post(
        f"/boards/{test_board}/elements/{ids[2]}/z-order",
        headers=fake_headers,
        json={"op": "back"},
    )
    assert r.status_code == 200, r.text
    await _fresh(db, ids[2])
    # min_other = 0, len(affected) = 1 → base = 0-1 = -1.
    assert (await _fresh(db, ids[2])).z_index == -1

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "z_order"

    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text
    await _fresh(db, ids[2])
    assert (await _fresh(db, ids[2])).z_index == 2


# ─────────────────── Single forward (swap с ближайшим сверху) ────────

async def test_single_forward_swaps_with_neighbor(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board) for _ in range(3)]
    # z=0/1/2. Forward на среднем (ids[1], z=1) → swap с ids[2] (z=2).
    r = await client.post(
        f"/boards/{test_board}/elements/{ids[1]}/z-order",
        headers=fake_headers,
        json={"op": "forward"},
    )
    assert r.status_code == 200, r.text

    for i in ids:
        await _fresh(db, i)
    assert (await _fresh(db, ids[1])).z_index == 2
    assert (await _fresh(db, ids[2])).z_index == 1

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    # Изменились ДВА элемента (сам + сосед по swap), поэтому composite.
    assert action.kind == "composite"
    assert len(action.delta["items"]) == 2

    # Undo восстанавливает обоих.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text
    for i in ids:
        await _fresh(db, i)
    assert (await _fresh(db, ids[1])).z_index == 1
    assert (await _fresh(db, ids[2])).z_index == 2


# ─────────────────── Single backward — симметрично forward ───────────

async def test_single_backward_swaps_with_neighbor(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board) for _ in range(3)]
    # Backward на среднем (z=1) → swap с ids[0] (z=0).
    r = await client.post(
        f"/boards/{test_board}/elements/{ids[1]}/z-order",
        headers=fake_headers,
        json={"op": "backward"},
    )
    assert r.status_code == 200, r.text

    for i in ids:
        await _fresh(db, i)
    assert (await _fresh(db, ids[0])).z_index == 1
    assert (await _fresh(db, ids[1])).z_index == 0


# ─────────────────── No-op guard: front на верхнем → 0 actions ───────

async def test_front_noop_when_already_top(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board) for _ in range(3)]
    top = ids[2]  # z=2, уже верхний
    n_before = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
    )).scalars().all()

    r = await client.post(
        f"/boards/{test_board}/elements/{top}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200, r.text

    # z не изменился.
    await _fresh(db, top)
    assert (await _fresh(db, top)).z_index == 2

    # Action НЕ добавлен.
    n_after = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
    )).scalars().all()
    assert len(n_after) == len(n_before), (
        f"noop должен не писать action: было {len(n_before)}, стало {len(n_after)}"
    )


async def test_back_noop_when_already_bottom(
    client, fake_headers, test_board, make_element, db,
):
    ids = [await make_element(test_board) for _ in range(3)]
    bot = ids[0]  # z=0, уже нижний
    n_before = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
    )).scalars().all()

    r = await client.post(
        f"/boards/{test_board}/elements/{bot}/z-order",
        headers=fake_headers,
        json={"op": "back"},
    )
    assert r.status_code == 200, r.text
    await _fresh(db, bot)
    assert (await _fresh(db, bot)).z_index == 0

    n_after = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
    )).scalars().all()
    assert len(n_after) == len(n_before)


# ─────────────────── Multi-select front (composite) ──────────────────

async def test_multi_select_front_composite_undo(
    client, fake_headers, test_board, make_element, db,
):
    # 4 rect'а: z=0/1/2/3. Выделяем два нижних (z=0, z=1) → front.
    ids = [await make_element(test_board) for _ in range(4)]
    selection = [ids[0], ids[1]]

    r = await client.post(
        f"/boards/{test_board}/elements/{selection[0]}/z-order",
        headers=fake_headers,
        json={"op": "front", "elementIds": [str(x) for x in selection]},
    )
    assert r.status_code == 200, r.text

    for i in ids:
        await _fresh(db, i)
    # non-affected max = ids[3] z=3, base = 4.
    # affected sorted by z ASC = [ids[0], ids[1]] → 4, 5.
    assert (await _fresh(db, ids[0])).z_index == 4
    assert (await _fresh(db, ids[1])).z_index == 5
    assert (await _fresh(db, ids[2])).z_index == 2
    assert (await _fresh(db, ids[3])).z_index == 3

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    assert len(action.delta["items"]) == 2

    # Undo — оба back.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text
    for i in ids:
        await _fresh(db, i)
    assert (await _fresh(db, ids[0])).z_index == 0
    assert (await _fresh(db, ids[1])).z_index == 1


# ─────────────────── Multi-select forward — per-element swap ─────────

async def test_multi_select_forward_per_element_shift(
    client, fake_headers, test_board, make_element, db,
):
    # 4 rect'а: z=0/1/2/3. Выделяем ids[0] и ids[2].
    # Iteration top-down (D8): ids[2] сначала → swap с ids[3] (z=3);
    # затем ids[0] → swap с ids[1] (z=1, ближайший non-affected сверху).
    ids = [await make_element(test_board) for _ in range(4)]
    selection = [ids[0], ids[2]]

    r = await client.post(
        f"/boards/{test_board}/elements/{selection[0]}/z-order",
        headers=fake_headers,
        json={"op": "forward", "elementIds": [str(x) for x in selection]},
    )
    assert r.status_code == 200, r.text

    for i in ids:
        await _fresh(db, i)
    # ids[2] был z=2, swap с ids[3] z=3 → ids[2]=3, ids[3]=2.
    # ids[0] был z=0, swap с ids[1] z=1 → ids[0]=1, ids[1]=0.
    assert (await _fresh(db, ids[0])).z_index == 1
    assert (await _fresh(db, ids[1])).z_index == 0
    assert (await _fresh(db, ids[2])).z_index == 3
    assert (await _fresh(db, ids[3])).z_index == 2

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    # 4 target'а изменились (2 selected + 2 swap-соседа).
    assert len(action.delta["items"]) == 4


# ─────────────────── Frame cascade: front на frame → дети едут ───────

async def test_frame_cascade_front_includes_children(
    client, fake_headers, test_board, make_element, db,
):
    # Layout: rect_bg (z=0), frame (z=1), child_a (z=2), child_b (z=3),
    #         rect_fg (z=4). Front на frame → frame+children поверх rect_fg.
    rect_bg = await make_element(test_board)
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    child_a = await make_element(test_board, parent_id=frame)
    child_b = await make_element(test_board, parent_id=frame)
    rect_fg = await make_element(test_board)

    r = await client.post(
        f"/boards/{test_board}/elements/{frame}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200, r.text

    for i in (rect_bg, frame, child_a, child_b, rect_fg):
        await _fresh(db, i)
    # non-affected = [rect_bg (0), rect_fg (4)]. max_other = 4, base = 5.
    # affected sorted by z ASC = [frame (1), child_a (2), child_b (3)] → 5,6,7.
    assert (await _fresh(db, rect_bg)).z_index == 0
    assert (await _fresh(db, rect_fg)).z_index == 4
    assert (await _fresh(db, frame)).z_index == 5
    assert (await _fresh(db, child_a)).z_index == 6
    assert (await _fresh(db, child_b)).z_index == 7

    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    assert len(action.delta["items"]) == 3

    # Undo восстанавливает всю группу.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200, r.text
    for i in (rect_bg, frame, child_a, child_b, rect_fg):
        await _fresh(db, i)
    assert (await _fresh(db, frame)).z_index == 1
    assert (await _fresh(db, child_a)).z_index == 2
    assert (await _fresh(db, child_b)).z_index == 3


# ─────────────────── Permission: guest → 403 ─────────────────────────

async def test_guest_without_write_gets_403(
    client, guest_headers, test_board, make_element,
):
    el_id = await make_element(test_board)
    r = await client.post(
        f"/boards/{test_board}/elements/{el_id}/z-order",
        headers=guest_headers,
        json={"op": "front"},
    )
    assert r.status_code == 403, r.text


# ─────────────────── Validation: bogus id → 400 ──────────────────────

async def test_invalid_element_id_returns_400(
    client, fake_headers, test_board,
):
    bogus = uuid.uuid4()
    r = await client.post(
        f"/boards/{test_board}/elements/{bogus}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 400, r.text
    assert "invalid_z_order_target" in r.text


# ─────────────────── Advisory lock — 2 параллельных z-order ──────────

async def test_concurrent_z_orders_serialized_no_errors(
    client, fake_headers, test_board, make_element, db,
):
    a = await make_element(test_board)
    b = await make_element(test_board)
    # a: z=0, b: z=1. Оба front одновременно.

    async def do_front(el_id):
        return await client.post(
            f"/boards/{test_board}/elements/{el_id}/z-order",
            headers=fake_headers,
            json={"op": "front"},
        )

    r1, r2 = await asyncio.gather(do_front(a), do_front(b))
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # Оба action-record'а записаны (kind=z_order, single-target).
    actions = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.asc())
    )).scalars().all()
    zorder_actions = [x for x in actions if x.kind == "z_order"]
    assert len(zorder_actions) == 2

    # Финальный порядок — последний action «выиграл» и стал самым верхним.
    await _fresh(db, a)
    await _fresh(db, b)
    za = (await _fresh(db, a)).z_index
    zb = (await _fresh(db, b)).z_index
    assert za != zb  # разный z, никаких коллизий
