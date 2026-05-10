"""Перенос таблиц boards + board_elements из SQLite ln_dev → postgres board_dev.

Запуск:
    docker run --rm -v ln_dev_db:/sqlite:ro -v $(pwd)/scripts:/app/scripts:ro \\
        --network shared -e PG_DSN=postgresql://... \\
        board_dev-app:latest python -m scripts.migrate_from_sqlite
"""

import asyncio
import json
import os
import sqlite3
import sys
import uuid

import asyncpg


def to_uuid(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return uuid.UUID(bytes=v)
    return uuid.UUID(v)


BOARD_FIELDS = ["id", "title", "created_at", "updated_at", "deleted_at"]
ELEMENT_FIELDS = [
    "id", "board_id", "type", "parent_id", "z_index",
    "x", "y", "w", "h", "attrs",
    "created_at", "updated_at", "deleted_at",
]


async def migrate(sqlite_path: str):
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    pg = await asyncpg.connect(dsn=os.environ["PG_DSN"])
    try:
        await pg.set_type_codec(
            "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

        # boards
        rows = src.execute("SELECT * FROM boards").fetchall()
        nb = 0
        for r in rows:
            placeholders = ", ".join(f"${i + 1}" for i in range(len(BOARD_FIELDS)))
            cols = ", ".join(BOARD_FIELDS)
            await pg.execute(
                f"INSERT INTO boards ({cols}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                to_uuid(r["id"]), r["title"],
                r["created_at"], r["updated_at"], r["deleted_at"],
            )
            nb += 1

        # board_elements — двухпроходом: сначала всё с parent_id=NULL,
        # потом UPDATE для строк с непустым parent_id (дерево внутри таблицы).
        rows = src.execute("SELECT * FROM board_elements").fetchall()
        ne = 0
        cols_no_parent = [c for c in ELEMENT_FIELDS if c != "parent_id"]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols_no_parent)))
        cols = ", ".join(cols_no_parent)
        for r in rows:
            attrs = r["attrs"]
            if isinstance(attrs, str):
                attrs = json.loads(attrs) if attrs else {}
            await pg.execute(
                f"INSERT INTO board_elements ({cols}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                to_uuid(r["id"]), to_uuid(r["board_id"]), r["type"],
                r["z_index"], r["x"], r["y"], r["w"], r["h"], attrs,
                r["created_at"], r["updated_at"], r["deleted_at"],
            )
            ne += 1

        np_ = 0
        for r in rows:
            if r["parent_id"] is None:
                continue
            await pg.execute(
                "UPDATE board_elements SET parent_id = $1 WHERE id = $2",
                to_uuid(r["parent_id"]), to_uuid(r["id"]),
            )
            np_ += 1

        print(f"migrated: boards={nb} elements={ne} parent_links={np_}")
    finally:
        await pg.close()
        src.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/sqlite/livenotes.db"
    asyncio.run(migrate(path))
