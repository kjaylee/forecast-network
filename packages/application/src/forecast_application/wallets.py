"""Optional Solana wallet ownership links through a bound signMessage challenge.

Authentication, CSRF, rate limits and Ed25519 verification are supplied by the
Worker adapter. No wallet secret, transaction, token, or RPC client enters here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .database import Database
from .errors import AppError
from .points import PointsService

CHAIN = "solana:devnet"
PURPOSE = "link_forecast_profile"
CHALLENGE_LIFETIME_MS = 5 * 60 * 1000
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SignatureVerifier = Callable[[bytes, bytes, bytes], Awaitable[bool]]
_FIELD_PRIME = 2**255-19
_CURVE_D = -121665*pow(121666, _FIELD_PRIME-2, _FIELD_PRIME) % _FIELD_PRIME
_SQRT_MINUS_ONE = pow(2, (_FIELD_PRIME-1)//4, _FIELD_PRIME)


def _valid_ed25519_public_key(raw: bytes) -> bool:
    """RFC 8032 §§5.1.3–5.1.4 decoding plus rejection of the full 8-torsion.

    https://www.rfc-editor.org/rfc/rfc8032#section-5.1.3
    Public points only: no secret-dependent operations or signature algorithm is
    implemented here. Some WebCrypto implementations accept vacuous signatures
    for small-order keys, so verify_signature alone is insufficient for ownership.
    """
    p = _FIELD_PRIME
    encoded = int.from_bytes(raw, "little")
    sign, y = encoded >> 255, encoded & ((1 << 255)-1)
    if y >= p:
        return False
    y_squared = y*y % p
    denominator = (_CURVE_D*y_squared+1) % p
    if denominator == 0:
        return False
    x_squared = (y_squared-1)*pow(denominator, p-2, p) % p
    x = pow(x_squared, (p+3)//8, p)
    if x*x % p != x_squared:
        x = x*_SQRT_MINUS_ONE % p
    if x*x % p != x_squared or x == 0 and sign == 1:
        return False
    if x % 2 != sign:
        x = p-x
    # RFC projective doubling avoids inversions. After three doublings [8]P
    # must not equal (0:Z:Z); this rejects every order-1/2/4/8 point.
    z = 1
    for _ in range(3):
        square_x, square_y, twice_square_z = x*x % p, y*y % p, 2*z*z % p
        total = (square_x+square_y) % p
        difference = (square_x-square_y) % p
        cross = (total-(x+y)**2) % p
        factor = (twice_square_z+difference) % p
        x, y, z = cross*factor % p, difference*total % p, factor*difference % p
    return z != 0 and not (x == 0 and (y-z) % p == 0)


def encode_address(raw: bytes) -> str:
    """Canonical base58 representation; exposed for portable verification tests."""
    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, digit = divmod(number, 58)
        digits = BASE58_ALPHABET[digit]+digits
    leading = len(raw)-len(raw.lstrip(b"\0"))
    return "1"*leading+digits


def decode_address(value: str) -> bytes:
    if type(value) is not str or not 32 <= len(value) <= 44:
        raise AppError(400, "wallet_address_invalid", "Enter a valid Solana wallet address.")
    number = 0
    for character in value:
        position = BASE58_ALPHABET.find(character)
        if position < 0:
            raise AppError(400, "wallet_address_invalid", "Enter a valid Solana wallet address.")
        number = number*58+position
    leading = len(value)-len(value.lstrip("1"))
    raw = b"\0"*leading+number.to_bytes((number.bit_length()+7)//8, "big")
    if len(raw) != 32 or encode_address(raw) != value:
        raise AppError(400, "wallet_address_invalid", "Enter a canonical 32-byte Solana public key.")
    if not _valid_ed25519_public_key(raw):
        raise AppError(400, "wallet_address_invalid", "Use a valid, non-small-order Ed25519 wallet public key.")
    return raw


def decode_signature(value: str) -> bytes:
    if type(value) is not str or len(value) != 88:
        raise AppError(400, "wallet_signature_invalid", "The wallet returned an invalid signature.")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AppError(400, "wallet_signature_invalid", "The wallet returned an invalid signature.") from exc
    if len(raw) != 64 or base64.b64encode(raw).decode("ascii") != value:
        raise AppError(400, "wallet_signature_invalid", "The wallet returned an invalid signature.")
    return raw


class WalletService:
    def __init__(self, db: Database, now_ms: Callable[[], int], random_token: Callable[[], str],
                 verify_signature: SignatureVerifier, origin: str):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or origin != f"{parsed.scheme}://{parsed.netloc}"
                or len(origin) > 255 or any(ord(character) < 33 for character in origin)
                or parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Wallet origin must be an exact HTTPS origin or local development origin")
        self.db, self.now_ms, self.random_token = db, now_ms, random_token
        self.verify_signature, self.origin = verify_signature, origin
        self.points = PointsService(db)

    def _token(self) -> str:
        value = self.random_token()
        if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
            raise RuntimeError("Wallet challenges require a cryptographically secure random token")
        return value

    async def _user(self, user_id: str) -> None:
        if not await self.db.first("SELECT id FROM users WHERE id=?", (user_id,)):
            raise AppError(401, "authentication_required", "Please sign in to connect a wallet.")

    async def _login_identity_guard(self, user_id: str, address: str | None = None) -> None:
        identity = await self.db.first(
            "SELECT address FROM wallet_identities WHERE user_id=? AND status='active'", (user_id,))
        if identity and identity["address"] != address:
            raise AppError(409, "wallet_login_rotation_required",
                           "Your sign-in wallet cannot be removed or replaced from a session. Keep using this wallet to sign in.")

    async def get_wallet(self, user_id: str) -> dict[str, Any]:
        await self._user(user_id)
        row = await self.db.first("SELECT address,chain,linked_at FROM wallet_links WHERE user_id=?", (user_id,))
        return {"wallet": {"address": row["address"], "chain": row["chain"], "linkedAt": row["linked_at"]} if row else None,
                "points": await self.points.summary(user_id)}

    async def challenge(self, user_id: str, address: str) -> dict[str, Any]:
        await self._user(user_id)
        decode_address(address)
        await self._login_identity_guard(user_id, address)
        identifier = "wc_" + self._token()
        now = self.now_ms()
        expiry = now+CHALLENGE_LIFETIME_MS
        expires_utc = datetime.fromtimestamp(expiry/1000, timezone.utc).isoformat(timespec="milliseconds")
        message = (
            "Forecast Network wallet connection\n"
            "Purpose: Link this wallet to your Forecast Network profile.\n"
            "This signature verifies wallet ownership only. It does not authorize a transaction or transfer.\n"
            f"User: {user_id}\nAddress: {address}\nOrigin: {self.origin}\n"
            f"Chain: {CHAIN} (Solana Devnet)\nPurpose ID: {PURPOSE}\n"
            f"Challenge: {identifier}\nIssued at: {now}\nExpires at: {expiry}\n"
            f"Expires at (UTC): {expires_utc}\n"
        )
        await self.db.execute(
            "INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (identifier, user_id, address, self.origin, PURPOSE, CHAIN, message, now, expiry))
        return {"challengeId": identifier, "address": address, "message": message, "expiresAt": expiry, "chain": CHAIN}

    def _check_challenge(self, row: dict[str, Any] | None, user_id: str, address: str) -> dict[str, Any]:
        if row is None or row["user_id"] != user_id or row["address"] != address \
                or row["origin"] != self.origin or row["purpose"] != PURPOSE or row["chain"] != CHAIN:
            raise AppError(400, "wallet_challenge_invalid", "This wallet request does not match your account, wallet, or site.")
        if row["used_at"] is not None:
            raise AppError(409, "wallet_challenge_used", "This wallet signature has already been used. Request a new challenge.")
        if row["revoked_at"] is not None:
            raise AppError(409, "wallet_challenge_revoked", "This wallet request was canceled. Connect again for a new challenge.")
        if row["expires_at"] <= self.now_ms():
            raise AppError(410, "wallet_challenge_expired", "The wallet request has expired. Connect again for a new challenge.")
        return row

    async def link(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        await self._user(user_id)
        if type(body) is not dict or set(body) != {"challengeId", "address", "signature"}:
            raise AppError(400, "wallet_challenge_invalid", "A challenge, wallet address, and signature are required.")
        identifier, address = body["challengeId"], body["address"]
        if type(identifier) is not str or not re.fullmatch(r"wc_[A-Za-z0-9_-]{32,128}", identifier):
            raise AppError(400, "wallet_challenge_invalid", "Request a new wallet challenge.")
        public_key, signature = decode_address(address), decode_signature(body["signature"])
        row = self._check_challenge(await self.db.first("SELECT * FROM wallet_challenges WHERE id=?", (identifier,)),
                                    user_id, address)
        try:
            async with asyncio.timeout(10):
                verified = await self.verify_signature(public_key, row["message"].encode("utf-8"), signature)
        except Exception as exc:
            raise AppError(503, "wallet_verification_unavailable", "Wallet verification is temporarily unavailable. Please try again.") from exc
        if verified is not True:
            raise AppError(401, "wallet_signature_invalid", "The signature does not match this wallet and connection request.")
        await self._login_identity_guard(user_id, address)
        self._check_challenge(row, user_id, address)
        existing = await self.db.first("SELECT user_id FROM wallet_links WHERE address=?", (address,))
        if existing and existing["user_id"] != user_id:
            raise AppError(409, "wallet_already_linked", "This wallet is already linked to another profile.")
        now, guard = self.now_ms(), self._token()
        audit = json.dumps({"schemaVersion": 1, "kind": "wallet_linked", "userId": user_id,
            "address": address, "origin": self.origin, "purpose": PURPOSE, "chain": CHAIN,
            "challengeId": identifier,
            "messageSha256": hashlib.sha256(row["message"].encode("utf-8")).hexdigest(),
            "signatureSha256": hashlib.sha256(signature).hexdigest(), "signatureVerified": True,
            "verification": "ed25519_sign_message", "verifiedAt": now}, sort_keys=True, separators=(",", ":"))
        try:
            await self.db.batch((
                ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_challenges "
                 "WHERE id=? AND user_id=? AND address=? AND origin=? AND purpose=? AND chain=? "
                 "AND expires_at>? AND used_at IS NULL AND revoked_at IS NULL) AND NOT EXISTS(SELECT 1 FROM wallet_links "
                 "WHERE address=? AND user_id!=?) THEN 1 ELSE 0 END",
                 (guard, identifier, user_id, address, self.origin, PURPOSE, CHAIN, now, address, user_id)),
                ("UPDATE wallet_challenges SET used_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL", (now, identifier)),
                ("INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) VALUES(?,?,?,?,?,1) "
                 "ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,chain=excluded.chain,"
                 "linked_at=excluded.linked_at,generation=excluded.generation,revision=wallet_links.revision+1",
                 (user_id, address, CHAIN, now, identifier)),
                ("INSERT INTO wallet_audit(id,user_id,address,kind,challenge_id,body,created_at) "
                 "VALUES(?,?,?,'wallet_linked',?,?,?)", (self._token(), user_id, address, identifier, audit, now)),
                ("DELETE FROM mutation_guards WHERE token=?", (guard,))))
        except Exception as exc:
            self._check_challenge(await self.db.first("SELECT * FROM wallet_challenges WHERE id=?", (identifier,)),
                                  user_id, address)
            owner = await self.db.first("SELECT user_id FROM wallet_links WHERE address=?", (address,))
            if owner and owner["user_id"] != user_id:
                raise AppError(409, "wallet_already_linked", "This wallet is already linked to another profile.") from exc
            raise AppError(503, "wallet_storage_unavailable", "The wallet link could not be saved. Please try again later.") from exc
        return await self.get_wallet(user_id)

    async def unlink(self, user_id: str) -> dict[str, Any]:
        await self._user(user_id)
        await self._login_identity_guard(user_id)
        row = await self.db.first("SELECT * FROM wallet_links WHERE user_id=?", (user_id,))
        now, guard = self.now_ms(), self._token()
        if row is None:
            # Disconnect also cancels an in-flight signature request before any
            # wallet is linked. A racing newer link is protected by this guard.
            try:
                await self.db.batch((
                    ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS "
                     "(SELECT 1 FROM wallet_links WHERE user_id=?) THEN 1 ELSE 0 END", (guard, user_id)),
                    ("UPDATE wallet_challenges SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL",
                     (now, user_id)),
                    ("DELETE FROM mutation_guards WHERE token=?", (guard,))))
            except Exception as exc:
                if await self.db.first("SELECT user_id FROM wallet_links WHERE user_id=?", (user_id,)):
                    raise AppError(409, "wallet_conflict", "The wallet link changed. Refresh your profile and try again.") from exc
                raise AppError(503, "wallet_storage_unavailable", "Pending wallet requests could not be canceled. Please try again later.") from exc
            return {"wallet": None, "points": await self.points.summary(user_id)}
        audit = json.dumps({"schemaVersion": 1, "kind": "wallet_unlinked", "userId": user_id,
            "address": row["address"], "chain": CHAIN, "origin": self.origin, "unlinkedAt": now,
            "previousRevision": row["revision"], "previousGeneration": row["generation"]}, sort_keys=True, separators=(",", ":"))
        try:
            await self.db.batch((
                ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_links "
                 "WHERE user_id=? AND address=? AND revision=? AND generation=?) THEN 1 ELSE 0 END",
                 (guard, user_id, row["address"], row["revision"], row["generation"])),
                ("DELETE FROM wallet_links WHERE user_id=? AND revision=? AND generation=?",
                 (user_id, row["revision"], row["generation"])),
                ("UPDATE wallet_challenges SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL",
                 (now, user_id)),
                ("INSERT INTO wallet_audit(id,user_id,address,kind,body,created_at) VALUES(?,?,?,'wallet_unlinked',?,?)",
                 (self._token(), user_id, row["address"], audit, now)),
                ("DELETE FROM mutation_guards WHERE token=?", (guard,))))
        except Exception as exc:
            current = await self.db.first("SELECT revision,generation FROM wallet_links WHERE user_id=?", (user_id,))
            if current is None:
                return {"wallet": None, "points": await self.points.summary(user_id)}
            if current["revision"] == row["revision"] and current["generation"] == row["generation"]:
                raise AppError(503, "wallet_storage_unavailable", "The wallet link could not be removed. Please try again later.") from exc
            raise AppError(409, "wallet_conflict", "The wallet link changed. Refresh your profile and try again.") from exc
        return {"wallet": None, "points": await self.points.summary(user_id)}
