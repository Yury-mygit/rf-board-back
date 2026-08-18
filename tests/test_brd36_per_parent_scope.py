"""BRD-36 regression: per-parent scope для z-order operations.

Front/Back/Forward/Backward двигают target только среди siblings того же
parent'а — не выкидывают за пределы контейнера.
"""
from __future__ import annotations

from app.models.models import BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


async def test_back_on_child_stays_above_frame(
    client, fake_headers, test_board, make_element, db,
):
    """rect в frame → Back → z_rank ≥ frame's z_rank (не выкидывает
    под frame)."""
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    ch1 = await make_element(test_board, parent_id=frame)
    ch2 = await make_element(test_board, parent_id=frame)
    ch3 = await make_element(test_board, parent_id=frame)
    frame_z_rank = (await _fresh(db, frame)).z_rank

    # Back для ch3 (был topmost sibling).
    r = await client.post(
        f"/boards/{test_board}/elements/{ch3}/z-order",
        headers=fake_headers,
        json={"op": "back"},
    )
    assert r.status_code == 200

    ch3_after = await _fresh(db, ch3)
    ch1_r = (await _fresh(db, ch1)).z_rank
    # ch3 стал нижним среди siblings, но НЕ ниже frame'а.
    assert ch3_after.z_rank < ch1_r  # ниже sibling
    assert ch3_after.z_rank > frame_z_rank, (
        f"BRD-36 bug: ch3 z_rank {ch3_after.z_rank} ниже frame {frame_z_rank}"
    )


async def test_front_on_child_stays_within_frame(
    client, fake_headers, test_board, make_element, db,
):
    """rect в frame → Front → z_rank выше siblings, но не задевает
    top-level elements за пределами frame'а."""
    other_frame = await make_element(test_board, type_="frame", w=200.0, h=200.0)
    other_child = await make_element(test_board, parent_id=other_frame)
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    ch1 = await make_element(test_board, parent_id=frame)
    ch2 = await make_element(test_board, parent_id=frame)
    top_rect = await make_element(test_board)  # parent=null, top-level
    top_rect_r_before = (await _fresh(db, top_rect)).z_rank
    other_child_r_before = (await _fresh(db, other_child)).z_rank

    # Front для ch1 — должен быть только среди siblings frame'а.
    r = await client.post(
        f"/boards/{test_board}/elements/{ch1}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200

    ch1_after = await _fresh(db, ch1)
    ch2_r = (await _fresh(db, ch2)).z_rank
    # ch1 выше ch2 (sibling), но других parent'ов не задевает.
    assert ch1_after.z_rank > ch2_r
    # top_rect и other_child не изменились.
    assert (await _fresh(db, top_rect)).z_rank == top_rect_r_before
    assert (await _fresh(db, other_child)).z_rank == other_child_r_before


async def test_frame_own_front_among_top_level(
    client, fake_headers, test_board, make_element, db,
):
    """frame Front среди top-level (parent=null) — включая другие
    frame'ы и top-level rects, но не задевая children."""
    frame_a = await make_element(test_board, type_="frame", w=200.0, h=200.0)
    frame_b = await make_element(test_board, type_="frame", w=200.0, h=200.0)
    child_b = await make_element(test_board, parent_id=frame_b)
    child_b_r_before = (await _fresh(db, child_b)).z_rank

    # Front для frame_a — среди top-level.
    r = await client.post(
        f"/boards/{test_board}/elements/{frame_a}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200

    frame_a_after = await _fresh(db, frame_a)
    frame_b_r = (await _fresh(db, frame_b)).z_rank
    # frame_a стал выше frame_b (top-level sibling).
    assert frame_a_after.z_rank > frame_b_r
    # child_b (внутри frame_b) не задет.
    assert (await _fresh(db, child_b)).z_rank == child_b_r_before


async def test_forward_swaps_only_with_sibling(
    client, fake_headers, test_board, make_element, db,
):
    """Forward для rect в frame — swap только с siblings, не с
    top-level."""
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    ch1 = await make_element(test_board, parent_id=frame)
    ch2 = await make_element(test_board, parent_id=frame)
    top_rect = await make_element(test_board)  # top-level, higher z
    top_r_before = (await _fresh(db, top_rect)).z_rank
    ch2_r_before = (await _fresh(db, ch2)).z_rank

    # Forward для ch1 — должен swap с ch2 (следующий sibling), не с top_rect.
    r = await client.post(
        f"/boards/{test_board}/elements/{ch1}/z-order",
        headers=fake_headers,
        json={"op": "forward"},
    )
    assert r.status_code == 200

    ch1_after = await _fresh(db, ch1)
    ch2_after = await _fresh(db, ch2)
    assert ch1_after.z_rank > ch2_after.z_rank  # swapped
    # top_rect не тронут.
    assert (await _fresh(db, top_rect)).z_rank == top_r_before
