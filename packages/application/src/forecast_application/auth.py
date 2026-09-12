"""Recovery credentials are high-entropy random secrets, never passwords."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .database import Database, Statement
from .errors import AppError, invalid

SESSION_LIFETIME_MS = 30 * 24 * 60 * 60 * 1000


def text(value: Any, limit: int, *, minimum: int = 1) -> str:
    if type(value) is not str or not minimum <= len(value.strip()) <= limit:
        raise invalid()
    result = value.strip()
    if any(ord(char) < 32 for char in result) or any(0xD800 <= ord(char) <= 0xDFFF for char in result):
        raise invalid()
    return result


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "displayName": row["display_name"], "handle": row["handle"],
            "createdAt": row["created_at"]}


class Authentication:
    def __init__(self, db: Database, now_ms: Callable[[], int],
                 token_hash: Callable[[str], str], random_token: Callable[[], str]):
        self.db, self.now_ms = db, now_ms
        self.token_hash, self.random_token = token_hash, random_token

    def token(self) -> str:
        value = self.random_token()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value):
            raise RuntimeError("Random token provider must supply at least 192 random bits")
        return value

    async def register(self, display_name: str) -> dict[str, Any]:
        display_name = text(display_name, 40)
        code, session, identifier = self.token(), self.token(), self.token()
        now = self.now_ms()
        uid = "u_" + identifier[:24]
        handle = "f_" + identifier[:12].lower()
        await self.db.batch((
            ("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
             (uid, display_name, handle, self.token_hash("recovery:" + code), now)),
            ("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
             (self.token_hash("session:" + session), uid, now, now + SESSION_LIFETIME_MS)),
        ))
        return {"user": {"id": uid, "displayName": display_name, "handle": handle, "createdAt": now},
                "recoveryCode": code, "sessionToken": session}

    async def login(self, recovery_code: str, context_token: str | None = None) -> dict[str, Any]:
        if type(recovery_code) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", recovery_code):
            raise AppError(401, "invalid_recovery_code", "Enter an old Forecast recovery code, never a wallet recovery phrase.")
        context = None
        if context_token is not None:
            if type(context_token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", context_token):
                raise AppError(409, "wallet_context_required", "Prepare sign-in again before restoring your profile.")
            context = await self.db.first("SELECT * FROM wallet_login_contexts WHERE token_hash=?",
                                          (self.token_hash("wallet-context:"+context_token),))
            if not context or context["revoked_at"] is not None or context["expires_at"] <= self.now_ms():
                raise AppError(409, "wallet_context_required", "Prepare sign-in again before restoring your profile.")
        user = await self.db.first("SELECT * FROM users WHERE recovery_hash=? AND NOT EXISTS "
                                   "(SELECT 1 FROM wallet_identities i WHERE i.user_id=users.id AND i.converted_at IS NOT NULL)",
                                   (self.token_hash("recovery:" + recovery_code),))
        if user is None:
            raise AppError(401, "invalid_recovery_code", "Check your recovery code and try again.")
        session, now = self.token(), self.now_ms()
        guard = self.token()
        session_hash = self.token_hash("session:" + session)
        statements: list[Statement] = [
            ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM users "
             "WHERE id=? AND recovery_hash=?) AND NOT EXISTS(SELECT 1 FROM wallet_identities "
             "WHERE user_id=? AND converted_at IS NOT NULL) THEN 1 ELSE 0 END",
             (guard, user["id"], self.token_hash("recovery:" + recovery_code), user["id"])),
        ]
        if context is not None:
            statements.extend([
                # Check active session as well as latest pending proof: a wallet
                # verify can commit without changing its latest challenge ID.
                ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_login_contexts "
                 "WHERE token_hash=? AND epoch=? AND latest_challenge_id IS ? AND active_session_hash IS ? "
                 "AND revoked_at IS NULL AND expires_at>?) THEN 1 ELSE 0 END",
                 (guard+":context", context["token_hash"], context["epoch"], context["latest_challenge_id"],
                  context["active_session_hash"], now)),
                ("UPDATE wallet_login_challenges SET revoked_at=? WHERE context_hash=? AND used_at IS NULL AND revoked_at IS NULL",
                 (now, context["token_hash"])),
                ("DELETE FROM sessions WHERE context_hash=?", (context["token_hash"],)),
                ("INSERT INTO sessions(token_hash,user_id,created_at,expires_at,context_hash,context_epoch) VALUES(?,?,?,?,?,?)",
                 (session_hash, user["id"], now, now+SESSION_LIFETIME_MS, context["token_hash"], context["epoch"])),
                ("UPDATE wallet_login_contexts SET latest_challenge_id=NULL,active_session_hash=? WHERE token_hash=?",
                 (session_hash, context["token_hash"])),
            ])
        else:
            statements.append(("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                               (session_hash, user["id"], now, now+SESSION_LIFETIME_MS)))
        statements.append(("DELETE FROM mutation_guards WHERE token IN (?,?)", (guard, guard+":context")))
        try:
            await self.db.batch(statements)
        except Exception as exc:
            if await self.db.first("SELECT address FROM wallet_identities WHERE user_id=? AND converted_at IS NOT NULL",
                                   (user["id"],)):
                raise AppError(401, "invalid_recovery_code", "Sign in with your wallet to continue.") from exc
            if context is not None:
                latest = await self.db.first("SELECT * FROM wallet_login_contexts WHERE token_hash=?", (context["token_hash"],))
                if (not latest or latest["revoked_at"] is not None or latest["expires_at"] <= self.now_ms()
                        or latest["epoch"] != context["epoch"] or latest["latest_challenge_id"] != context["latest_challenge_id"]
                        or latest["active_session_hash"] != context["active_session_hash"]):
                    raise AppError(409, "wallet_login_changed", "This sign-in request changed. Start sign-in again.") from exc
            raise AppError(503, "authentication_unavailable", "Sign-in is temporarily unavailable.") from exc
        return {"user": public_user(user), "sessionToken": session}

    async def authenticate(self, session_token: str | None, context_token: str | None = None) -> dict[str, Any] | None:
        if not session_token or len(session_token) > 256 or context_token is not None and len(context_token) > 256:
            return None
        row = await self.db.first(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "LEFT JOIN wallet_login_contexts c ON c.token_hash=s.context_hash "
            "WHERE s.token_hash=? AND s.expires_at>? AND ((s.context_hash IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM wallet_identities i WHERE i.user_id=u.id AND i.converted_at IS NOT NULL)) "
            "OR (s.context_hash=? AND c.epoch=s.context_epoch AND c.revoked_at IS NULL AND c.expires_at>? "
            "AND c.active_session_hash=s.token_hash))",
            (self.token_hash("session:" + session_token), self.now_ms(),
             self.token_hash("wallet-context:" + context_token) if context_token else None, self.now_ms()))
        return public_user(row) if row else None

    async def logout(self, session_token: str | None, context_token: str | None = None) -> dict[str, bool]:
        session_hash = self.token_hash("session:" + session_token) if session_token else None
        context_hash = self.token_hash("wallet-context:" + context_token) if context_token else None
        bound = await self.db.first("SELECT context_hash FROM sessions WHERE token_hash=?", (session_hash,))
        bound_hash = bound["context_hash"] if bound else None
        # Include the session's bound context even if a late cookie overwrote the
        # browser context. Revoked contexts can never issue another session.
        await self.db.batch((
            ("UPDATE wallet_login_contexts SET revoked_at=COALESCE(revoked_at,?),epoch=epoch+1,active_session_hash=NULL "
             "WHERE token_hash IN (?,?)", (self.now_ms(), context_hash, bound_hash)),
            ("UPDATE wallet_login_challenges SET revoked_at=? WHERE used_at IS NULL AND revoked_at IS NULL "
             "AND context_hash IN (?,?)", (self.now_ms(), context_hash, bound_hash)),
            ("DELETE FROM sessions WHERE token_hash=? OR context_hash IN (?,?)", (session_hash, context_hash, bound_hash)),
        ))
        return {"ok": True}
