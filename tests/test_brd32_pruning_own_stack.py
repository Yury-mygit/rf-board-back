"""BRD-32 regression: prune cap применяется к own stack (executor_uuid),
не к associated_users.

До fix'а: если у user X 100 own-actions в стеке, любой новый action user'а
Y, где X попал в associated (через touchers), считался в X's stack size →
> 100 → prune oldest. Oldest выбирался ORDER BY ts_ms ASC. Если Y's action
имел меньший ts_ms (случай отстающего клока), Y's action помечался
pruned мгновенно, undo/redo его не видели.

После fix'а: cap применяется только к own executor_uuid stack. Actions
user'а Y добавляются только в Y's stack; associated_users X'а видит их
для undo γ2, но prune X'ов stack не задевает Y'а.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.undo_log import UNDO_STACK_CAP, record_action
from app.models.models import BoardAction, BoardElement


BOT_UUID = "31e1c03a-489f-4f33-bad9-1d7bb0ac3472"


async def test_new_action_not_pruned_when_bot_stack_at_cap(
    client, fake_headers, test_board, make_element, db,
):
    """Заполнить бот'ский stack ровно cap actions'ами (с ts_ms из
    будущего). Затем сделать user action с меньшим ts_ms. User's action
    не должен быть pruned."""
    # Bot creates cap+ actions напрямую через record_action (в обход
    # HTTP endpoint'а — быстрее).
    bot_uuid = uuid.UUID(BOT_UUID)
    # Один элемент, много синтетических actions на нём — чтобы стек
    # bot'а был при cap.
    el_id = await make_element(test_board)
    # Воспроизводим реальный сценарий: bot's actions имеют seed ts_ms
    # из будущего (VM clock отстаёт), Y's action имеет real now_ms() —
    # т.е. меньше. Со старым algo (filter по associated_users) Y's action
    # становился oldest в bot's stack и prune'ился мгновенно.
    future_ts = 9_999_999_999_999
    for i in range(UNDO_STACK_CAP):
        await record_action(
            db,
            board_id=test_board,
            executor_uuid=bot_uuid,
            kind="attrs",
            target_ids=[el_id],
            delta={"before": {"opacity": i}, "after": {"opacity": i + 1}},
            ts_ms=future_ts + i,
        )
    await db.commit()

    # Sanity — bot has 100 non-pruned own actions.
    bot_stack = (await db.execute(
        select(BoardAction).where(
            BoardAction.board_id == test_board,
            BoardAction.executor_uuid == bot_uuid,
            BoardAction.pruned.is_(False),
        )
    )).scalars().all()
    assert len(bot_stack) == UNDO_STACK_CAP

    # User (Юрий, fake_headers UUID) делает batch move. Ts из real
    # now_ms() < future_ts (сервер отстаёт).
    r = await client.post(
        f"/boards/{test_board}/elements/batch",
        headers=fake_headers,
        json={"items": [{"id": str(el_id), "op": "patch", "patch": {"x": 999.0}}]},
    )
    assert r.status_code == 200, r.text

    # Найти user's batch-move action (composite kind) — должен быть не pruned.
    user_uuid_str = fake_headers["X-User-Uuid"]
    user_batch = (await db.execute(
        select(BoardAction).where(
            BoardAction.board_id == test_board,
            BoardAction.executor_uuid == uuid.UUID(user_uuid_str),
            BoardAction.kind == "composite",
        )
    )).scalars().all()
    assert len(user_batch) == 1
    assert not user_batch[0].pruned, (
        "BRD-32 regression: user's action не должен быть pruned из-за bot's stack"
    )
    assert not user_batch[0].undone

    # Bot's oldest должен быть pruned (потому что new action от user'а
    # ассоциирован с bot'ом → +1 в bot's stack при старом фильтре).
    # После fix — bot's stack растёт только собственными actions, не
    # трогается.
    bot_stack_after = (await db.execute(
        select(BoardAction).where(
            BoardAction.board_id == test_board,
            BoardAction.executor_uuid == bot_uuid,
            BoardAction.pruned.is_(False),
        )
    )).scalars().all()
    # Bot's stack не изменился (user action не увеличил bot's cap).
    assert len(bot_stack_after) == UNDO_STACK_CAP

    # Y's batch не pruned — undo найдёт его в стеке (может потребоваться
    # несколько pop'ов чтобы пройти bot's actions с future ts). Regression
    # покрывает главное: prune не жертвует Y's action.


async def test_own_cap_still_enforced(
    client, fake_headers, test_board, make_element, db,
):
    """При overflow OWN stack — prune oldest OWN action (не чужой)."""
    el_id = await make_element(test_board)
    # cap + 1 actions от одного user'а.
    for i in range(UNDO_STACK_CAP + 5):
        r = await client.post(
            f"/boards/{test_board}/elements/batch",
            headers=fake_headers,
            json={"items": [{
                "id": str(el_id), "op": "patch",
                "patch": {"x": float(i)},
            }]},
        )
        assert r.status_code == 200, r.text

    # Own non-pruned count = cap (не больше).
    user_uuid = uuid.UUID(fake_headers["X-User-Uuid"])
    own_active = (await db.execute(
        select(BoardAction).where(
            BoardAction.board_id == test_board,
            BoardAction.executor_uuid == user_uuid,
            BoardAction.pruned.is_(False),
        )
    )).scalars().all()
    assert len(own_active) == UNDO_STACK_CAP, (
        f"own cap не enforced: {len(own_active)} != {UNDO_STACK_CAP}"
    )

    # Oldest pruned — тот с самым маленьким ts_ms.
    pruned_actions = (await db.execute(
        select(BoardAction).where(
            BoardAction.board_id == test_board,
            BoardAction.executor_uuid == user_uuid,
            BoardAction.pruned.is_(True),
        ).order_by(BoardAction.ts_ms.asc())
    )).scalars().all()
    assert len(pruned_actions) == 5  # первые 5 pruned
