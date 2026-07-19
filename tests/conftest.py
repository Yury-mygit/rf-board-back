"""BRD-24 / REG-2 pilot: pytest fixtures для board-backend acceptance-suite.

Referential template для будущих сервисов платформы. Ключевые примитивы:

- ``fake_headers`` — Caddy-style X-User-* headers (curator-owner).
- ``client`` — httpx.AsyncClient к внутреннему uvicorn (`http://127.0.0.1:8000`).
- ``db`` — прямой доступ к БД через существующий ``AsyncSessionLocal`` для
  assertion-checks (записался ли action, изменился ли элемент и т.п.).
- ``test_board`` — свежая доска с cleanup (hard-delete actions/elements/board
  по завершению теста).

Изоляция БД — через уникальные board_id + cleanup, а не transaction
rollback: endpoint делает свой ``session.commit()``, откатить нельзя без
refactor'а сервисов. Cleanup-hard-delete для тестов ОК: prod живёт под
soft-delete.

Запуск::

    docker exec board_dev_app pytest tests/ -v
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal
from app.models.models import Board, BoardAction, BoardElement, BoardGrant

# Юрий (creator/curator) — используется во всех тестах как default actor.
YURY_UUID = "5ecac682-8e86-4537-abe2-d79a65493a02"
# Второй фейк-юзер для permission-тестов (guest, без grant'а).
GUEST_UUID = "9c0ebecd-1234-5678-9abc-def000000001"


def _headers(*, uuid_: str = YURY_UUID, curator: bool = True) -> dict:
    return {
        "X-User-Uuid": uuid_,
        "X-User-Handle": "test",
        "X-User-Email": "",
        "X-User-Telegram": "",
        "X-User-Is-Curator": "1" if curator else "",
        "Content-Type": "application/json",
    }


@pytest.fixture
def fake_headers() -> dict:
    return _headers()


@pytest.fixture
def guest_headers() -> dict:
    return _headers(uuid_=GUEST_UUID, curator=False)


@pytest.fixture
async def client():
    """HTTP клиент к running uvicorn (`board_dev_app` внутри контейнера).

    Реальный HTTP (не ASGITransport) — нужен чтобы endpoint работал со
    своим asyncpg pool'ом, независимым от тестового ``db`` fixture'а.
    Иначе получаем ``InterfaceError: another operation is in progress``.
    """
    async with AsyncClient(base_url="http://127.0.0.1:8000/api/v1") as c:
        yield c


@pytest.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


@pytest.fixture
async def test_board(client, fake_headers, db):
    """Создаёт новую доску (curator+owner=Юрий) и cleanup после теста.

    Возвращает ``uuid.UUID``. Cleanup — hard-delete всех actions +
    elements + grants + board (изоляция от параллельных тестов и
    prod-данных на dev-БД).
    """
    board_id = uuid.uuid4()
    r = await client.post(
        "/boards",
        headers=fake_headers,
        json={
            "id": str(board_id),
            "title": "BRD-24 pilot test",
            "createdAt": 0,
            "updatedAt": 0,
        },
    )
    assert r.status_code == 201, r.text
    try:
        yield board_id
    finally:
        await db.execute(delete(BoardAction).where(BoardAction.board_id == board_id))
        await db.execute(delete(BoardElement).where(BoardElement.board_id == board_id))
        await db.execute(delete(BoardGrant).where(BoardGrant.board_id == board_id))
        await db.execute(delete(Board).where(Board.id == board_id))
        await db.commit()


async def _make_element(
    client: AsyncClient,
    headers: dict,
    board_id: uuid.UUID,
    *,
    type_: str = "rect",
    x: float = 0.0,
    y: float = 0.0,
    w: float = 100.0,
    h: float = 50.0,
    attrs: dict | None = None,
    parent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Helper: создаёт элемент через POST /elements. Возвращает id."""
    el_id = uuid.uuid4()
    body = {
        "id": str(el_id),
        "type": type_,
        "x": x, "y": y, "w": w, "h": h,
        "attrs": attrs or {},
        "createdAt": 0,
        "updatedAt": 0,
    }
    if parent_id is not None:
        body["parentId"] = str(parent_id)
    r = await client.post(
        f"/boards/{board_id}/elements",
        headers=headers,
        json=body,
    )
    assert r.status_code == 201, r.text
    return el_id


@pytest.fixture
async def make_element(client, fake_headers):
    """Возвращает async-функцию для создания элементов в тестах."""
    async def _factory(board_id, **kw):
        return await _make_element(client, fake_headers, board_id, **kw)
    return _factory
