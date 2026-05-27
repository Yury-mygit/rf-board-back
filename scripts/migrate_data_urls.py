"""Миграция board_elements.attrs.src=data:... → attrs.asset_id через media-dev.

Idempotent: повторный запуск находит 0 строк (после успеха src убран из attrs).

Запуск (one-off, на shared docker network — без модификации board_dev_app):

    docker run --rm --network shared \\
      -v /root/web/dev/board/backend/scripts/migrate_data_urls.py:/migrate.py:ro \\
      -e DATABASE_URL='postgresql://board_dev:<pw>@db_shared:5432/board_dev' \\
      -e MEDIA_INTERNAL_URL='http://media_dev_app:8028' \\
      board_dev-app:latest \\
      python /migrate.py [--dry-run]
"""
import argparse
import asyncio
import base64
import json
import os
import time

import asyncpg
import httpx

DATABASE_URL = os.environ["DATABASE_URL"].replace("+asyncpg", "")
MEDIA_URL = os.environ.get("MEDIA_INTERNAL_URL", "http://media_dev_app:8000")
MIGRATOR_EMAIL = "migration@board.dev"


def parse_data_url(src: str) -> tuple[str, bytes]:
    if not src.startswith("data:"):
        raise ValueError("not a data URL")
    head, b64 = src.split(",", 1)
    mime_part = head[5:]
    if ";base64" not in mime_part:
        raise ValueError("only base64-encoded data URLs supported")
    mime = mime_part.split(";")[0] or "application/octet-stream"
    return mime, base64.b64decode(b64)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = await asyncpg.connect(DATABASE_URL)
    for tname in ("json", "jsonb"):
        await conn.set_type_codec(
            tname, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    rows = await conn.fetch(
        "SELECT id, attrs FROM board_elements "
        "WHERE attrs->>'src' LIKE 'data:%' AND deleted_at IS NULL"
    )
    print(f"Found {len(rows)} elements with data: URL src")

    if args.dry_run:
        for r in rows[:10]:
            src = (r["attrs"] or {}).get("src", "")
            print(f"  {r['id']}: len={len(src)} prefix={src[:40]}...")
        if len(rows) > 10:
            print(f"  ... and {len(rows)-10} more")
        await conn.close()
        return

    ok = err = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for row in rows:
            attrs = row["attrs"] or {}
            src = attrs.get("src", "")
            try:
                mime, blob = parse_data_url(src)
                files = {"file": (f"migrated-{row['id']}.bin", blob, mime)}
                headers = {"X-User-Email": MIGRATOR_EMAIL}
                resp = await client.post(
                    f"{MEDIA_URL}/api/v1/assets", files=files, headers=headers
                )
                resp.raise_for_status()
                result = resp.json()
                asset_id = result["id"]

                new_attrs = {k: v for k, v in attrs.items() if k != "src"}
                new_attrs["asset_id"] = asset_id

                await conn.execute(
                    "UPDATE board_elements SET attrs = $1, updated_at = $2 WHERE id = $3",
                    new_attrs,
                    int(time.time() * 1000),
                    row["id"],
                )
                ok += 1
                tag = "dedup" if result.get("deduplicated") else "new"
                print(f"  OK  {row['id']} ({len(blob)}B {mime}) → asset {asset_id} [{tag}]")
            except Exception as e:
                err += 1
                print(f"  ERR {row['id']}: {e!r}")

    print(f"\nDone. ok={ok} err={err}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
