"""User-context dep + ACL helpers для board.

Caddy snippet `auth_required board-dev` инжектит:
- `X-User-Uuid` — обязательный, запрос без него = bypass edge auth.
- `X-User-Email` — может быть пустым для TG-only юзеров.
- `X-User-Telegram` — `str(tg_id)` если есть TG-identity, иначе пусто.
- `X-User-Handle` — всегда non-empty (после #137).
- `X-User-Is-Curator` — `"1"` если у юзера curator-флаг в auth.

`verify_token` остаётся defence-in-depth по shared API_KEY (см.
карта 2026-06-23-board-ownership-and-grants.md, Notes).

Карта: cards/board/feature/2026-06-23-board-ownership-and-grants.md
(D1-D9 + D4-rework 2026-06-27, Stage 2 / R2).
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Header
from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import APIError
from app.models.models import Board, BoardGrant


@dataclass(frozen=True)
class AuthCtx:
    user_uuid: UUID
    user_email: str  # lowercased; пустая строка если нет email
    user_telegram: str  # str(tg_id); пустая если нет TG-identity
    user_handle: str  # всегда non-empty после #137
    is_curator: bool


def current_user(
    x_user_uuid: str | None = Header(default=None, alias="X-User-Uuid"),
    x_user_email: str = Header(default="", alias="X-User-Email"),
    x_user_telegram: str = Header(default="", alias="X-User-Telegram"),
    x_user_handle: str = Header(default="", alias="X-User-Handle"),
    x_user_is_curator: str = Header(default="", alias="X-User-Is-Curator"),
) -> AuthCtx:
    if not x_user_uuid:
        raise APIError(
            401,
            "unauthorized",
            "Missing X-User-Uuid (request bypassed edge auth)",
        )
    try:
        uuid_ = UUID(x_user_uuid)
    except ValueError:
        raise APIError(401, "unauthorized", "Malformed X-User-Uuid")
    return AuthCtx(
        user_uuid=uuid_,
        user_email=x_user_email.strip().lower(),
        user_telegram=x_user_telegram.strip(),
        user_handle=x_user_handle.strip().lower(),
        is_curator=x_user_is_curator == "1",
    )


def _attr_match_clause(ctx: AuthCtx):
    """OR-список совпадений по любому из 3 каналов (для pending grants).

    Каждое условие: `subject_attr_kind='X' AND subject_attr_value=<ctx.X>`.
    Пустые значения у юзера (нет email / нет TG) — соответствующий клоз
    не добавляем, иначе grant по `email=''` залогинит любого без email.
    """
    clauses = []
    if ctx.user_email:
        clauses.append(
            and_(
                BoardGrant.subject_attr_kind == "email",
                BoardGrant.subject_attr_value == ctx.user_email,
            )
        )
    if ctx.user_telegram:
        clauses.append(
            and_(
                BoardGrant.subject_attr_kind == "telegram",
                BoardGrant.subject_attr_value == ctx.user_telegram,
            )
        )
    if ctx.user_handle:
        clauses.append(
            and_(
                BoardGrant.subject_attr_kind == "handle",
                BoardGrant.subject_attr_value == ctx.user_handle,
            )
        )
    return or_(*clauses) if clauses else None


def visible_boards_query(ctx: AuthCtx) -> Select:
    """SELECT по доскам, видимым юзеру: owner OR grant. Curator = no filter.

    Caller добавляет `deleted_at IS NULL` и order_by.
    """
    q = select(Board)
    if ctx.is_curator:
        return q
    attr_match = _attr_match_clause(ctx)
    if attr_match is None:
        # Юзер без любого attribute — может видеть только свои доски.
        return q.where(Board.owner_uuid == ctx.user_uuid)
    grant_clause = (
        select(BoardGrant.board_id)
        .where(
            BoardGrant.board_id == Board.id,
            or_(
                BoardGrant.subject_uuid == ctx.user_uuid,
                and_(BoardGrant.subject_uuid.is_(None), attr_match),
            ),
        )
        .exists()
    )
    return q.where(or_(Board.owner_uuid == ctx.user_uuid, grant_clause))


async def require_board(
    db: AsyncSession,
    ctx: AuthCtx,
    board_id: UUID,
    min_level: int,
    include_deleted: bool = False,
) -> Board:
    """ACL-gate: curator → ok; owner → ok; grant.level >= min_level → ok.

    Matching grant:
    - subject_uuid == ctx.user_uuid (резолвленный grant), ИЛИ
    - subject_uuid IS NULL AND (attr_kind, attr_value) совпал с любым из
      ctx.{email, telegram, handle}.

    Side effect: lazy-bind grant.subject_uuid при матче по attribute.
    Идемпотентно — UPDATE только если subject_uuid IS NULL.

    Returns: Board (загруженный, можно дальше править).
    Raises: 404 если доска не найдена / удалена; 403 если нет доступа.
    """
    board = await db.get(Board, board_id)
    if not board or (board.deleted_at is not None and not include_deleted):
        raise APIError(
            404, "board_not_found", f"Board with id '{board_id}' does not exist"
        )

    if ctx.is_curator:
        return board
    if board.owner_uuid == ctx.user_uuid:
        return board

    attr_match = _attr_match_clause(ctx)
    if attr_match is None:
        # Юзер без attributes — pending grants ему не светят. Resolve
        # только по subject_uuid (грантов, привязанных к нему ранее).
        grant_predicate = BoardGrant.subject_uuid == ctx.user_uuid
    else:
        grant_predicate = or_(
            BoardGrant.subject_uuid == ctx.user_uuid,
            and_(BoardGrant.subject_uuid.is_(None), attr_match),
        )

    grant = (
        await db.execute(
            select(BoardGrant).where(
                BoardGrant.board_id == board_id,
                grant_predicate,
            )
        )
    ).scalar_one_or_none()

    if grant is None or grant.level < min_level:
        raise APIError(
            403,
            "forbidden",
            f"No access to board '{board_id}' at level {min_level}",
        )

    if grant.subject_uuid is None:
        # Lazy-bind: первый hit от юзера, у которого attribute совпал.
        # UPDATE по composite PK; idempotent (только NULL→uuid).
        await db.execute(
            update(BoardGrant)
            .where(
                BoardGrant.board_id == grant.board_id,
                BoardGrant.subject_attr_kind == grant.subject_attr_kind,
                BoardGrant.subject_attr_value == grant.subject_attr_value,
            )
            .values(subject_uuid=ctx.user_uuid)
        )
        await db.commit()

    return board


async def your_role_map(
    db: AsyncSession, ctx: AuthCtx, boards: list[Board]
) -> dict[UUID, str]:
    """Возвращает {board_id: role} для UI (Stage 7c). Значения:
    `curator` | `owner` | `write` | `read`.

    Один батч-запрос за max grant level для не-owner досок. Curator всегда
    'curator' (bypass всех проверок).
    """
    if not boards:
        return {}
    if ctx.is_curator:
        return {b.id: "curator" for b in boards}

    owner_ids: set[UUID] = set()
    non_owner_ids: list[UUID] = []
    for b in boards:
        if b.owner_uuid == ctx.user_uuid:
            owner_ids.add(b.id)
        else:
            non_owner_ids.append(b.id)

    grant_levels: dict[UUID, int] = {}
    if non_owner_ids:
        attr_match = _attr_match_clause(ctx)
        if attr_match is None:
            grant_filter = BoardGrant.subject_uuid == ctx.user_uuid
        else:
            grant_filter = or_(
                BoardGrant.subject_uuid == ctx.user_uuid,
                and_(BoardGrant.subject_uuid.is_(None), attr_match),
            )
        rows = (
            await db.execute(
                select(BoardGrant.board_id, func.max(BoardGrant.level))
                .where(BoardGrant.board_id.in_(non_owner_ids), grant_filter)
                .group_by(BoardGrant.board_id)
            )
        ).all()
        grant_levels = {bid: lvl for bid, lvl in rows}

    out: dict[UUID, str] = {}
    for b in boards:
        if b.id in owner_ids:
            out[b.id] = "owner"
        else:
            lvl = grant_levels.get(b.id)
            out[b.id] = "write" if lvl == 300 else "read"
    return out


__all__ = [
    "AuthCtx",
    "current_user",
    "visible_boards_query",
    "require_board",
    "your_role_map",
]
