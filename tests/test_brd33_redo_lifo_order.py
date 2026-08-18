"""BRD-33 regression: redo применяет actions в LIFO-обратном порядке к undo.

До fix'а: `pop_redoable` использовал `ORDER BY ts_ms DESC` → redo
возвращал action с наибольшим ts_ms (самый недавний по созданию, не
самый недавно undone). Из-за этого после `create → move1 → move2 →
undo x2 → redo` элемент оказывался в позиции после move2, а не после
move1.

После fix'а: `ORDER BY ts_ms ASC` → возвращает самое старое undone
action = самое недавнее undone (LIFO семантика).
"""
from __future__ import annotations

from app.models.models import BoardElement


async def _fresh(db, id_):
    obj = await db.get(BoardElement, id_)
    if obj is not None:
        await db.refresh(obj)
    return obj


async def test_redo_returns_to_move1_after_two_undos(
    client, fake_headers, test_board, make_element, db,
):
    """create → move1 → move2 → undo x2 → redo → position after move1."""
    rect = await make_element(test_board, x=100.0, y=100.0)

    # move1
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(rect), "op": "patch", "patch": {"x": 300.0, "y": 300.0}}]},
    )
    assert r.status_code == 200

    # move2
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(rect), "op": "patch", "patch": {"x": 500.0, "y": 500.0}}]},
    )
    assert r.status_code == 200

    # undo x2
    for _ in range(2):
        r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
        assert r.status_code == 200

    # Rect должен быть на creation pos (100, 100) — undo снял move1 и move2.
    el = await _fresh(db, rect)
    assert (el.x, el.y) == (100.0, 100.0)

    # redo — должен применить move1 первым (LIFO).
    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    el = await _fresh(db, rect)
    assert (el.x, el.y) == (300.0, 300.0), (
        f"BRD-33 regression: redo применил не move1: (x={el.x}, y={el.y})"
    )


async def test_undo_redo_full_cycle_preserves_order(
    client, fake_headers, test_board, make_element, db,
):
    """create → move1 → move2 → undo x3 → redo x3 — state consistency
    на каждом шаге."""
    rect = await make_element(test_board, x=100.0, y=100.0)
    for x, y in [(300.0, 300.0), (500.0, 500.0)]:
        await client.post(
            f"/boards/{test_board}/elements/batch",
            headers=fake_headers,
            json={"items": [{"id": str(rect), "op": "patch", "patch": {"x": x, "y": y}}]},
        )

    # undo x3 — move2, move1, create.
    for expected in [(300.0, 300.0), (100.0, 100.0), None]:
        await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
        el = await _fresh(db, rect)
        if expected is None:
            assert el.deleted_at is not None
        else:
            assert (el.x, el.y) == expected

    # redo x3 — create, move1, move2.
    expected_seq = [(100.0, 100.0), (300.0, 300.0), (500.0, 500.0)]
    for expected in expected_seq:
        await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
        el = await _fresh(db, rect)
        assert el.deleted_at is None
        assert (el.x, el.y) == expected
