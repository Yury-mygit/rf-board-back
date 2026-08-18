"""BRD-30 acceptance: between-op на z-order endpoint.

Contract:

- `POST /boards/{board_id}/elements/{element_id}/z-order` расширен
  op `"between"`. Body:

  ```
  {
    "op": "between",
    "beforeId": UUID | null,
    "afterId": UUID | null,
    "elementIds": [UUID] | null,
    "cascadeFrame": bool  # default true
  }
  ```

  - beforeId — target(s) пойдут ПОД этим элементом (визуально сзади).
  - afterId — target(s) пойдут НАД этим элементом.
  - Один из двух опционален. Оба None → 400 `invalid_z_order_target`.
  - elementIds — multi-select; если не задан, target = [{URL id}].
  - cascadeFrame — если target = frame и cascadeFrame=True,
    children едут с frame'ом (z_rank shifting'ом), сохраняя
    относительный порядок между собой.

- Response: `{items: [{id, z_index, z_rank, warning?}]}`.
  `warning` (opt) = `"cross_parent"` если target parent_id
  отличается от beforeId/afterId parent_id.

- Undo/redo: single-target between → composite kind="mixed"
  (BRD-29 fix already supports); z_rank в diff.

- Sort order после между-op: z_rank ASC (D7).

- Legacy op (front/back/forward/backward) продолжают работать,
  дополнительно обновляют z_rank (D10).
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


# ─── Between: single target, оба beforeId и afterId ─────────────────

async def test_between_single_target_lands_between(
    client, fake_headers, test_board, make_element, db,
):
    """Между два element'а: target получит z_rank в интервале."""
    a = await make_element(test_board)  # z_index=0
    b = await make_element(test_board)  # z_index=1
    c = await make_element(test_board)  # z_index=2
    # Хотим переместить c в позицию между a и b.
    r = await client.post(
        f"/boards/{test_board}/elements/{c}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(a), "beforeId": str(b)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    item = next(x for x in body["items"] if x["id"] == str(c))
    a_rank = (await _fresh(db, a)).z_rank
    b_rank = (await _fresh(db, b)).z_rank
    c_rank = (await _fresh(db, c)).z_rank
    assert a_rank < c_rank < b_rank
    assert item["zRank"] == c_rank


# ─── Between: только beforeId (target к нижнему краю) ───────────────

async def test_between_only_before_id_moves_below(
    client, fake_headers, test_board, make_element, db,
):
    """beforeId=X, afterId=None → target уходит ПОД X (rank_before)."""
    a = await make_element(test_board)  # z_index=0
    b = await make_element(test_board)  # z_index=1
    r = await client.post(
        f"/boards/{test_board}/elements/{b}/z-order",
        headers=fake_headers,
        json={"op": "between", "beforeId": str(a)},
    )
    assert r.status_code == 200, r.text
    a_rank = (await _fresh(db, a)).z_rank
    b_rank = (await _fresh(db, b)).z_rank
    assert b_rank < a_rank


# ─── Between: только afterId (target к верхнему краю) ───────────────

async def test_between_only_after_id_moves_above(
    client, fake_headers, test_board, make_element, db,
):
    a = await make_element(test_board)
    b = await make_element(test_board)
    # Переместить a НАД b.
    r = await client.post(
        f"/boards/{test_board}/elements/{a}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(b)},
    )
    assert r.status_code == 200, r.text
    a_rank = (await _fresh(db, a)).z_rank
    b_rank = (await _fresh(db, b)).z_rank
    assert a_rank > b_rank


# ─── Between: multi-select — соседний блок ──────────────────────────

async def test_between_multi_select_lands_as_adjacent_block(
    client, fake_headers, test_board, make_element, db,
):
    """3 target'а между a и b: сохраняют взаимный порядок, все между."""
    a = await make_element(test_board)  # bottom
    x = await make_element(test_board)
    y = await make_element(test_board)
    z = await make_element(test_board)
    b = await make_element(test_board)  # top
    # x, y, z — вставить как блок между a и b.
    r = await client.post(
        f"/boards/{test_board}/elements/{x}/z-order",
        headers=fake_headers,
        json={
            "op": "between",
            "afterId": str(a), "beforeId": str(b),
            "elementIds": [str(x), str(y), str(z)],
        },
    )
    assert r.status_code == 200, r.text
    a_r = (await _fresh(db, a)).z_rank
    b_r = (await _fresh(db, b)).z_rank
    x_r = (await _fresh(db, x)).z_rank
    y_r = (await _fresh(db, y)).z_rank
    z_r = (await _fresh(db, z)).z_rank
    assert a_r < x_r < y_r < z_r < b_r


# ─── Between: frame cascade с флагом ────────────────────────────────

async def test_between_frame_cascade_true_moves_children(
    client, fake_headers, test_board, make_element, db,
):
    """Если target = frame и cascadeFrame=true, children едут с frame."""
    a = await make_element(test_board)
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    child = await make_element(test_board, parent_id=frame)
    b = await make_element(test_board)

    r = await client.post(
        f"/boards/{test_board}/elements/{frame}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(a), "beforeId": str(b),
              "cascadeFrame": True},
    )
    assert r.status_code == 200, r.text
    a_r = (await _fresh(db, a)).z_rank
    b_r = (await _fresh(db, b)).z_rank
    frame_r = (await _fresh(db, frame)).z_rank
    child_r = (await _fresh(db, child)).z_rank
    assert a_r < frame_r < b_r
    # Child edет вместе — тоже между a и b, но > frame (сохраняя порядок).
    assert frame_r < child_r
    assert child_r < b_r


async def test_between_frame_cascade_false_moves_only_frame(
    client, fake_headers, test_board, make_element, db,
):
    """cascadeFrame=False — двигается только frame, child остаётся."""
    a = await make_element(test_board)
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    child = await make_element(test_board, parent_id=frame)
    b = await make_element(test_board)
    child_r_before = (await _fresh(db, child)).z_rank

    r = await client.post(
        f"/boards/{test_board}/elements/{frame}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(a), "beforeId": str(b),
              "cascadeFrame": False},
    )
    assert r.status_code == 200, r.text
    child_r_after = (await _fresh(db, child)).z_rank
    assert child_r_before == child_r_after


# ─── Between: cross-parent warning ──────────────────────────────────

async def test_between_cross_parent_warning(
    client, fake_headers, test_board, make_element, db,
):
    """Target parent_id != beforeId/afterId parent_id → warning."""
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    child_a = await make_element(test_board, parent_id=frame)
    child_b = await make_element(test_board, parent_id=frame)
    outsider = await make_element(test_board)  # parent_id=None

    r = await client.post(
        f"/boards/{test_board}/elements/{outsider}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(child_a),
              "beforeId": str(child_b)},
    )
    assert r.status_code == 200, r.text
    item = next(x for x in r.json()["items"] if x["id"] == str(outsider))
    assert item.get("warning") == "cross_parent"


async def test_between_same_parent_no_warning(
    client, fake_headers, test_board, make_element, db,
):
    frame = await make_element(test_board, type_="frame", w=400.0, h=300.0)
    child_a = await make_element(test_board, parent_id=frame)
    child_b = await make_element(test_board, parent_id=frame)
    child_c = await make_element(test_board, parent_id=frame)

    r = await client.post(
        f"/boards/{test_board}/elements/{child_c}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(child_a),
              "beforeId": str(child_b)},
    )
    assert r.status_code == 200, r.text
    item = next(x for x in r.json()["items"] if x["id"] == str(child_c))
    assert item.get("warning") is None


# ─── Between: validation errors ─────────────────────────────────────

async def test_between_both_ids_none_returns_400(
    client, fake_headers, test_board, make_element,
):
    el = await make_element(test_board)
    r = await client.post(
        f"/boards/{test_board}/elements/{el}/z-order",
        headers=fake_headers,
        json={"op": "between"},
    )
    assert r.status_code == 400
    assert "invalid_z_order_target" in r.text


async def test_between_invalid_before_id_returns_400(
    client, fake_headers, test_board, make_element,
):
    el = await make_element(test_board)
    bogus = uuid.uuid4()
    r = await client.post(
        f"/boards/{test_board}/elements/{el}/z-order",
        headers=fake_headers,
        json={"op": "between", "beforeId": str(bogus)},
    )
    assert r.status_code == 400
    assert "invalid_z_order_target" in r.text


async def test_between_before_after_swapped_returns_400(
    client, fake_headers, test_board, make_element,
):
    """afterId.z_rank > beforeId.z_rank (перепутано направление)."""
    a = await make_element(test_board)  # z=0, rank меньше
    b = await make_element(test_board)  # z=1, rank больше
    c = await make_element(test_board)
    # Юзер путает: afterId=b (top), beforeId=a (bottom) → диапазон
    # некорректный (b > a), нельзя вставить между.
    r = await client.post(
        f"/boards/{test_board}/elements/{c}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(b), "beforeId": str(a)},
    )
    assert r.status_code == 400
    assert "invalid_z_order_target" in r.text


# ─── Undo/redo для between-op ───────────────────────────────────────

async def test_between_undo_restores_original_z_rank(
    client, fake_headers, test_board, make_element, db,
):
    a = await make_element(test_board)
    b = await make_element(test_board)
    c = await make_element(test_board)
    c_r_before = (await _fresh(db, c)).z_rank

    r = await client.post(
        f"/boards/{test_board}/elements/{c}/z-order",
        headers=fake_headers,
        json={"op": "between", "afterId": str(a), "beforeId": str(b)},
    )
    assert r.status_code == 200

    # Action записан.
    action = (await db.execute(
        select(BoardAction).where(BoardAction.board_id == test_board)
        .order_by(BoardAction.ts_ms.desc()).limit(1)
    )).scalar_one()
    assert action.kind == "composite"
    item = action.delta["items"][0]
    assert item["kind"] == "mixed"
    assert "z_rank" in item["before"] and "z_rank" in item["after"]

    # Undo — z_rank возвращается.
    r = await client.post(f"/boards/{test_board}/undo", headers=fake_headers)
    assert r.status_code == 200
    c_r_after_undo = (await _fresh(db, c)).z_rank
    assert c_r_after_undo == c_r_before

    # Redo — снова к новому.
    r = await client.post(f"/boards/{test_board}/redo", headers=fake_headers)
    assert r.status_code == 200
    c_r_after_redo = (await _fresh(db, c)).z_rank
    assert c_r_after_undo != c_r_after_redo


# ─── Migration correctness: все элементы получают z_rank ────────────

async def test_migration_all_elements_have_z_rank(
    client, fake_headers, test_board, make_element, db,
):
    """После создания элемента у него должен быть z_rank (D10 legacy op
    авто-заполняет при create)."""
    ids = [await make_element(test_board) for _ in range(3)]
    for i in ids:
        el = await _fresh(db, i)
        assert el.z_rank is not None
    # z_rank monotonically возрастает по порядку создания.
    ranks = [(await _fresh(db, i)).z_rank for i in ids]
    assert ranks == sorted(ranks)


# ─── Legacy op пишет z_rank (D10) ───────────────────────────────────

async def test_legacy_front_updates_z_rank(
    client, fake_headers, test_board, make_element, db,
):
    a = await make_element(test_board)
    b = await make_element(test_board)
    c = await make_element(test_board)
    a_r_before = (await _fresh(db, a)).z_rank

    # a → front (был bottom).
    r = await client.post(
        f"/boards/{test_board}/elements/{a}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200
    # z_rank должен обновиться, теперь > b_r и > c_r.
    a_r_after = (await _fresh(db, a)).z_rank
    b_r = (await _fresh(db, b)).z_rank
    c_r = (await _fresh(db, c)).z_rank
    assert a_r_after > a_r_before
    assert a_r_after > b_r
    assert a_r_after > c_r


# ─── Sort order: GET /boards возвращает по z_rank ASC ───────────────

async def test_get_board_orders_by_z_rank(
    client, fake_headers, test_board, make_element,
):
    ids = [await make_element(test_board) for _ in range(3)]
    # a=ids[0] в front → должен стать последним в порядке.
    r = await client.post(
        f"/boards/{test_board}/elements/{ids[0]}/z-order",
        headers=fake_headers,
        json={"op": "front"},
    )
    assert r.status_code == 200

    r = await client.get(f"/boards/{test_board}", headers=fake_headers)
    assert r.status_code == 200
    els = r.json()["elements"]
    order = [e["id"] for e in els]
    # ids[0] должен быть в конце (topmost).
    assert order[-1] == str(ids[0])


# ─── Permission: guest → 403 ────────────────────────────────────────

async def test_between_guest_gets_403(
    client, guest_headers, test_board, make_element,
):
    a = await make_element(test_board)
    b = await make_element(test_board)
    c = await make_element(test_board)
    r = await client.post(
        f"/boards/{test_board}/elements/{c}/z-order",
        headers=guest_headers,
        json={"op": "between", "afterId": str(a), "beforeId": str(b)},
    )
    assert r.status_code == 403
