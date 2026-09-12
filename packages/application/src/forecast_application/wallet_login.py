"""Wallet sign-in with browser-bound proofs and atomic identity conversion.

No transaction, private key, recovery credential, or RPC is requested. Signature
verification is an injected effect; all authorization is repeated after it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from .auth import SESSION_LIFETIME_MS, Authentication, public_user, text
from .database import Database, Statement
from .errors import AppError
from .points import PointsService
from .wallets import (
    CHAIN,
    CHALLENGE_LIFETIME_MS,
    SignatureVerifier,
    WalletService,
    decode_address,
    decode_signature,
)

PURPOSE = "sign_in_and_link_forecast_profile"


def _failure(code: str = "wallet_login_changed") -> AppError:
    return AppError(409, code, "This sign-in request changed or was canceled. Start wallet sign-in again.")


class WalletLogin:
    def __init__(self, db: Database, *, now_ms: Callable[[], int], token_hash: Callable[[str], str],
                 random_token: Callable[[], str], verify_signature: SignatureVerifier, origin: str,
                 on_create: Callable[[], Awaitable[None]] | None = None):
        # Reuse the exact origin and public key boundary of the existing adapter.
        WalletService(db, now_ms, random_token, verify_signature, origin)
        self.db, self.now_ms, self.token_hash = db, now_ms, token_hash
        self.auth = Authentication(db, now_ms, token_hash, random_token)
        self.verify_signature, self.origin = verify_signature, origin
        self.on_create = on_create

    def _context_hash(self, token: str | None) -> str:
        if type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise _failure("wallet_context_required")
        return self.token_hash("wallet-context:" + token)

    async def context(self, context_token: str | None = None) -> dict[str, Any]:
        now = self.now_ms()
        if context_token:
            key = self._context_hash(context_token)
            row = await self.db.first("SELECT * FROM wallet_login_contexts WHERE token_hash=?", (key,))
            if row and row["revoked_at"] is None and row["expires_at"] > now:
                return {"contextToken": context_token, "expiresAt": row["expires_at"]}
        token = self.auth.token()
        await self.db.execute(
            "INSERT INTO wallet_login_contexts(token_hash,created_at,expires_at) VALUES(?,?,?)",
            (self._context_hash(token), now, now+SESSION_LIFETIME_MS))
        return {"contextToken": token, "expiresAt": now+SESSION_LIFETIME_MS}

    async def _context(self, token: str | None) -> dict[str, Any]:
        row = await self.db.first("SELECT * FROM wallet_login_contexts WHERE token_hash=?", (self._context_hash(token),))
        if not row or row["revoked_at"] is not None or row["expires_at"] <= self.now_ms():
            raise _failure("wallet_context_required")
        return row

    async def _owner(self, address: str) -> str | None:
        identity = await self.db.first("SELECT * FROM wallet_identities WHERE address=?", (address,))
        if identity:
            if identity["status"] != "active":
                raise _failure("wallet_identity_retired")
            return str(identity["user_id"])
        linked = await self.db.first("SELECT user_id FROM wallet_links WHERE address=?", (address,))
        if linked:
            return str(linked["user_id"])
        historical = await self.db.first(
            "SELECT user_id FROM wallet_audit WHERE address=? UNION ALL "
            "SELECT user_id FROM point_awards WHERE wallet_address=? LIMIT 1", (address, address))
        if historical:
            raise _failure("wallet_identity_retired")
        return None

    async def challenge(self, context_token: str | None, body: dict[str, Any],
                        session_token: str | None = None) -> dict[str, Any]:
        if (type(body) is not dict or not {"address", "mode", "expectedUserId"} <= set(body)
                or set(body)-{"address", "mode", "expectedUserId", "displayName"}):
            raise AppError(400, "wallet_challenge_invalid", "Choose your wallet and sign-in action.")
        address, mode = body["address"], body["mode"]
        decode_address(address)
        if type(mode) is not str or mode not in {"login", "migrate"}:
            raise AppError(400, "wallet_challenge_invalid", "Choose a valid sign-in action.")
        context = await self._context(context_token)
        current = await self.auth.authenticate(session_token, context_token)
        owner = await self._owner(address)
        source_hash = None
        if mode == "migrate":
            if not current or type(body["expectedUserId"]) is not str or body["expectedUserId"] != current["id"]:
                raise _failure("account_changed")
            target = str(current["id"])
            if owner is not None and owner != target:
                raise _failure("wallet_already_linked")
            other = await self.db.first("SELECT address FROM wallet_links WHERE user_id=? AND address!=?", (target, address))
            if other or await self.db.first("SELECT address FROM wallet_identities WHERE user_id=? AND status='active' AND address!=?",
                                           (target, address)):
                raise _failure("wallet_login_rotation_required")
            source_hash = self.token_hash("session:" + str(session_token))
        else:
            if body["expectedUserId"] is not None:
                raise AppError(400, "wallet_challenge_invalid", "Use migration to keep an existing guest profile.")
            target = owner or "u_" + self.auth.token()[:24]
            if current and current["id"] != target:
                raise _failure("wallet_migration_required")
        display_name = text(body.get("displayName", "Forecaster " + address[:8]), 40)
        identifier, now = "wl_"+self.auth.token(), self.now_ms()
        expiry = now+CHALLENGE_LIFETIME_MS
        # Keyed commitment prevents dictionary-testing public creator IDs against
        # an unauthenticated address challenge. The nonce scopes each commitment.
        commitment = self.token_hash("wallet-login-target:"+identifier+":"+target)
        message = (
            "Forecast Network wallet sign-in\n"
            "Sign in to the profile associated with this wallet, creating and linking one if needed.\n"
            "This signature verifies wallet ownership only. It does not authorize a transaction or transfer.\n"
            f"Domain: {urlsplit(self.origin).netloc}\nURI: {self.origin}/auth/wallet\nOrigin: {self.origin}\n"
            f"Address: {address}\nChain: {CHAIN}\nPurpose ID: {PURPOSE}\n"
            "Proof roles: sign_in_forecast_profile, link_forecast_profile\n"
            f"Mode: {mode}\nProfile commitment: {commitment}\n"
            + (f"Existing profile: {target}\n" if mode == "migrate" else "")
            + f"Challenge: {identifier}\nIssued at: {now}\nExpires at: {expiry}\n"
        )
        guard = self.auth.token()
        try:
            await self.db.batch((
                ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_login_contexts "
                 "WHERE token_hash=? AND epoch=? AND active_session_hash IS ? AND latest_challenge_id IS ? "
                 "AND revoked_at IS NULL AND expires_at>?) THEN 1 ELSE 0 END",
                 (guard, context["token_hash"], context["epoch"], context["active_session_hash"],
                  context["latest_challenge_id"], now)),
                ("UPDATE wallet_login_challenges SET revoked_at=? WHERE context_hash=? AND used_at IS NULL AND revoked_at IS NULL",
                 (now, context["token_hash"])),
                ("INSERT INTO wallet_login_challenges(id,context_hash,context_epoch,address,target_user_id,mode,source_session_hash,"
                 "display_name,origin,purpose,chain,message,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (identifier, context["token_hash"], context["epoch"], address, target, mode, source_hash,
                  display_name, self.origin, PURPOSE, CHAIN, message, now, expiry)),
                ("UPDATE wallet_login_contexts SET latest_challenge_id=? WHERE token_hash=?", (identifier, context["token_hash"])),
                ("DELETE FROM mutation_guards WHERE token=?", (guard,)),
            ))
        except Exception as exc:
            raise _failure() from exc
        return {"challengeId": identifier, "address": address, "message": message,
                "expiresAt": expiry, "chain": CHAIN, "mode": mode}

    async def _challenge(self, identifier: str, context_token: str | None, address: str) -> dict[str, Any]:
        context = await self._context(context_token)
        row = await self.db.first("SELECT * FROM wallet_login_challenges WHERE id=?", (identifier,))
        if (not row or row["context_hash"] != context["token_hash"] or row["context_epoch"] != context["epoch"]
                or context["latest_challenge_id"] != identifier or row["address"] != address
                or row["origin"] != self.origin or row["purpose"] != PURPOSE or row["chain"] != CHAIN
                or row["used_at"] is not None or row["revoked_at"] is not None):
            raise _failure()
        if row["expires_at"] <= self.now_ms():
            raise AppError(410, "wallet_challenge_expired", "The signature request expired. Start wallet sign-in again.")
        return row

    async def verify(self, context_token: str | None, body: dict[str, Any],
                     session_token: str | None = None) -> dict[str, Any]:
        if type(body) is not dict or set(body) != {"challengeId", "address", "signature"}:
            raise AppError(400, "wallet_challenge_invalid", "A challenge, wallet address, and signature are required.")
        identifier, address = body["challengeId"], body["address"]
        if type(identifier) is not str or not re.fullmatch(r"wl_[A-Za-z0-9_-]{32,256}", identifier):
            raise _failure()
        key, signature = decode_address(address), decode_signature(body["signature"])
        row = await self._challenge(identifier, context_token, address)
        if row["mode"] == "migrate" and (not session_token or self.token_hash("session:"+session_token) != row["source_session_hash"]):
            raise _failure("account_changed")
        try:
            async with asyncio.timeout(10):
                valid = await self.verify_signature(key, row["message"].encode(), signature)
        except Exception as exc:
            raise AppError(503, "wallet_verification_unavailable", "Wallet verification is temporarily unavailable.") from exc
        if valid is not True:
            raise AppError(401, "wallet_signature_invalid", "The signature does not match this wallet and sign-in request.")
        row = await self._challenge(identifier, context_token, address)
        owner = await self._owner(address)
        uid, now = row["target_user_id"], self.now_ms()
        if owner is not None and owner != uid:
            raise _failure()
        user = await self.db.first("SELECT * FROM users WHERE id=?", (uid,))
        if row["mode"] == "login" and user is not None and owner != uid:
            raise _failure()
        if row["mode"] == "login" and user is None and owner is None and self.on_create is not None:
            # Charge account-creation quota only after ownership proof. Failed
            # signatures and ordinary returning-wallet logins cannot exhaust it.
            await self.on_create()
            # The callback may await a remote limiter; cancellation and expiry
            # still take precedence before the guarded identity transaction.
            row = await self._challenge(identifier, context_token, address)
            now = self.now_ms()
        session, guard = self.auth.token(), self.auth.token()
        session_hash = self.token_hash("session:"+session)
        audit = json.dumps({"schemaVersion": 1, "kind": "wallet_signed_in", "challengeId": identifier,
            "userId": uid, "address": address, "origin": self.origin, "chain": CHAIN, "purpose": PURPOSE,
            "proofRoles": ["sign_in_forecast_profile", "link_forecast_profile"], "mode": row["mode"],
            "messageSha256": hashlib.sha256(row["message"].encode()).hexdigest(),
            "signatureSha256": hashlib.sha256(signature).hexdigest(), "signatureVerified": True,
            "verification": "ed25519_sign_message", "verifiedAt": now}, sort_keys=True, separators=(",", ":"))
        statements: list[Statement] = [
            ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_login_challenges n "
             "JOIN wallet_login_contexts c ON c.token_hash=n.context_hash WHERE n.id=? AND n.context_hash=? AND n.address=? "
             "AND n.target_user_id=? AND n.origin=? AND n.chain=? AND n.purpose=? AND n.used_at IS NULL AND n.revoked_at IS NULL "
             "AND n.expires_at>? AND c.revoked_at IS NULL AND c.expires_at>? AND c.epoch=n.context_epoch "
             "AND c.latest_challenge_id=n.id) AND NOT EXISTS(SELECT 1 FROM wallet_identities WHERE address=? "
             "AND (user_id!=? OR status!='active')) AND NOT EXISTS(SELECT 1 FROM wallet_identities WHERE user_id=? "
             "AND status='active' AND address!=?) AND NOT EXISTS(SELECT 1 FROM wallet_links WHERE "
             "(address=? AND user_id!=?) OR (user_id=? AND address!=?)) AND (EXISTS(SELECT 1 FROM wallet_identities "
             "WHERE address=? AND user_id=? AND status='active') OR EXISTS(SELECT 1 FROM wallet_links WHERE address=? AND user_id=?) "
             "OR (NOT EXISTS(SELECT 1 FROM wallet_audit WHERE address=?) AND NOT EXISTS(SELECT 1 FROM point_awards "
             "WHERE wallet_address=?))) THEN 1 ELSE 0 END",
             (guard, identifier, row["context_hash"], address, uid, self.origin, CHAIN, PURPOSE, now, now,
              address, uid, uid, address, address, uid, uid, address, address, uid, address, uid, address, address)),
        ]
        if row["mode"] == "migrate":
            statements.append((
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM sessions s "
                "LEFT JOIN wallet_login_contexts c ON c.token_hash=s.context_hash WHERE s.token_hash=? AND s.user_id=? "
                "AND s.expires_at>? AND (s.context_hash IS NULL OR (s.context_hash=? AND s.context_epoch=c.epoch "
                "AND c.active_session_hash=s.token_hash AND c.revoked_at IS NULL AND c.expires_at>?))) THEN 1 ELSE 0 END",
                (guard+":migration", row["source_session_hash"], uid, now, row["context_hash"], now)))
        elif user is None:
            # Losing concurrent signup cannot replace the UID in the signed profile commitment.
            statements.append((
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS(SELECT 1 FROM users WHERE id=?) "
                "AND NOT EXISTS(SELECT 1 FROM wallet_identities WHERE address=?) "
                "AND NOT EXISTS(SELECT 1 FROM wallet_audit WHERE address=?) "
                "AND NOT EXISTS(SELECT 1 FROM point_awards WHERE wallet_address=?) THEN 1 ELSE 0 END",
                (guard+":signup", uid, address, address, address)))
            statements.append(("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                               (uid, row["display_name"], "f_"+uid[2:14].lower(), "disabled-wallet:"+uid, now)))
        else:
            statements.append((
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_links "
                "WHERE address=? AND user_id=?) OR EXISTS(SELECT 1 FROM wallet_identities "
                "WHERE address=? AND user_id=? AND status='active') THEN 1 ELSE 0 END",
                (guard+":owner", address, uid, address, uid)))
        statements.extend([
            ("UPDATE wallet_login_challenges SET used_at=? WHERE id=?", (now, identifier)),
            # This retained proof explicitly signs both sign-in and profile linking.
            # It is not presented as a legacy link-only message in either audit.
            ("INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at,used_at) "
             "VALUES(?,?,?,?,'link_forecast_profile',?,?,?,?,?)",
             (identifier, uid, address, self.origin, CHAIN, row["message"], row["created_at"], row["expires_at"], now)),
            ("INSERT INTO wallet_identities(address,user_id,status,created_at) VALUES(?,?,'active',?) ON CONFLICT(address) DO NOTHING",
             (address, uid, now)),
            ("INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) VALUES(?,?,?,?,?,1) "
             "ON CONFLICT(user_id) DO UPDATE SET linked_at=excluded.linked_at,generation=excluded.generation,revision=wallet_links.revision+1",
             (uid, address, CHAIN, now, identifier)),
            ("DELETE FROM sessions WHERE user_id=? AND EXISTS(SELECT 1 FROM wallet_identities "
             "WHERE address=? AND converted_at IS NULL)", (uid, address)),
            ("UPDATE users SET recovery_hash=? WHERE id=?", ("disabled-wallet:"+uid, uid)),
            ("UPDATE wallet_identities SET converted_at=? WHERE address=? AND converted_at IS NULL", (now, address)),
            ("DELETE FROM sessions WHERE context_hash=?", (row["context_hash"],)),
            ("INSERT INTO sessions(token_hash,user_id,created_at,expires_at,context_hash,context_epoch) VALUES(?,?,?,?,?,?)",
             (session_hash, uid, now, now+SESSION_LIFETIME_MS, row["context_hash"], row["context_epoch"])),
            ("UPDATE wallet_login_contexts SET active_session_hash=? WHERE token_hash=?", (session_hash, row["context_hash"])),
            ("INSERT INTO wallet_login_audit(id,challenge_id,user_id,address,body,created_at) VALUES(?,?,?,?,?,?)",
             (self.auth.token(), identifier, uid, address, audit, now)),
            ("DELETE FROM mutation_guards WHERE token IN (?,?,?,?)", (guard, guard+":migration", guard+":signup", guard+":owner")),
        ])
        try:
            await self.db.batch(statements)
        except Exception as exc:
            raise _failure() from exc
        saved = await self.db.first("SELECT * FROM users WHERE id=?", (uid,))
        if saved is None:
            raise RuntimeError("Wallet sign-in transaction did not persist its profile")
        return {"user": public_user(saved), "sessionToken": session,
                "wallet": {"address": address, "chain": CHAIN, "linkedAt": now},
                "points": await PointsService(self.db).summary(uid)}

    async def cancel(self, context_token: str | None) -> dict[str, bool]:
        self._context_hash(context_token)
        return await self.auth.logout(None, context_token)
