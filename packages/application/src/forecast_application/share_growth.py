"""First-party attribution for shared forecast links."""

from __future__ import annotations

import re
from typing import Any

from .database import Database

DAY_MS = 86_400_000
SHARE_WINDOW_MS = 7 * DAY_MS

ISSUE_LINK = (
    "INSERT INTO forecast_share_links(code,forecast_id,sharer_id,created_at) "
    "SELECT lower(hex(randomblob(16))),id,?,? FROM forecasts "
    "WHERE id=? AND state='OPEN' AND open_at<=? AND close_at>? RETURNING code"
)
OPEN_LINK = (
    "UPDATE forecast_share_links SET opened_at=? WHERE code=? AND forecast_id=? "
    "AND opened_at IS NULL AND created_at<=? AND created_at>? "
    "AND (? IS NULL OR sharer_id<>?)"
)
SHARE_LINK = (
    "UPDATE forecast_share_links SET shared_at=? WHERE code=? AND forecast_id=? AND sharer_id=? "
    "AND shared_at IS NULL AND created_at<=? AND created_at>?"
)
CLAIM_LINK = (
    "INSERT INTO forecast_share_claims(recipient_id,forecast_id,code,claimed_at) "
    "SELECT ?,forecast_id,code,? FROM forecast_share_links WHERE code=? AND forecast_id=? "
    "AND sharer_id<>? AND created_at<=? AND created_at>? "
    "ON CONFLICT(recipient_id,forecast_id) DO NOTHING"
)


def share_code(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None


async def issue_link(db: Database, forecast_id: str, sharer_id: str, now: int) -> str | None:
    result = await db.execute(ISSUE_LINK, (sharer_id, now, forecast_id, now, now))
    rows = result["results"]
    return share_code(rows[0]["code"]) if rows else None


async def open_link(
    db: Database, forecast_id: str, code: str, user_id: str | None, now: int,
) -> None:
    await db.execute(OPEN_LINK, (now, code, forecast_id, now, now - SHARE_WINDOW_MS, user_id, user_id))


async def mark_shared(
    db: Database, forecast_id: str, code: str, user_id: str, now: int,
) -> None:
    await db.execute(SHARE_LINK, (now, code, forecast_id, user_id, now, now - SHARE_WINDOW_MS))


async def claim_link(db: Database, forecast_id: str, code: str, user_id: str, now: int) -> None:
    await db.batch((
        (OPEN_LINK, (now, code, forecast_id, now, now - SHARE_WINDOW_MS, user_id, user_id)),
        (CLAIM_LINK, (user_id, now, code, forecast_id, user_id, now, now - SHARE_WINDOW_MS)),
    ))
