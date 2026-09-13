"""Wallet-signed Devnet memo attestations of a forecaster's own receipt.

The Worker cannot reach public Devnet RPC (Cloudflare egress is refused), so the phone
does the network work: it fetches a recent blockhash, asks this service for a
relayer-fee-paid transaction, has the Seed Vault wallet co-sign it, submits it and
reports the signature. This module only builds messages, signs as the relayer and
records what the device reports. Memo v2 requires every listed account to sign, which
is exactly the proof wanted: the forecaster's key, not ours, vouches for the memo.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

from forecast_domain.serialization import content_hash

from .database import Database
from .errors import AppError
from .solana_wire import (
    AccountMeta,
    Instruction,
    base58_decode,
    base58_encode,
    compile_message,
    shortvec,
)

MEMO_PROGRAM = base58_decode("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", length=32)
MEMO_VERSION = "forecast-attest-v1"
DAY_MS = 86_400_000
_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{86,88}$")


def memo_text(forecast_id: str, receipt_hash: str) -> str:
    return f"{MEMO_VERSION} forecast={forecast_id} receipt={receipt_hash}"


def partially_signed_transaction(message: bytes, signers: tuple[bytes, ...], signatures: dict[bytes, bytes]) -> bytes:
    """Serialize with real signatures where known and zeroed slots for the wallet to fill."""
    wire = shortvec(len(signers))
    for key in signers:
        signature = signatures.get(key, bytes(64))
        if len(signature) != 64:
            raise ValueError("signature must be 64 bytes")
        wire += signature
    return wire + message


class Attestations:
    def __init__(self, db: Database, *, relayer: bytes | None, sign: Callable[[bytes], Awaitable[bytes]] | None,
                 now_ms: Callable[[], int], random_token: Callable[[], str],
                 rate_limit: Callable[[str, int, int], Awaitable[None]]):
        self.db, self.relayer, self.sign = db, relayer, sign
        self.now_ms, self.random_token, self.rate_limit = now_ms, random_token, rate_limit

    @property
    def available(self) -> bool:
        return self.relayer is not None and self.sign is not None

    async def _receipt(self, user_id: str, forecast_id: str) -> tuple[str, str]:
        row = await self.db.first("SELECT body FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?",
                                  (forecast_id, user_id))
        if row is None:
            raise AppError(409, "attestation_requires_forecast", "Record a forecast before stamping it on-chain.")
        wallet = await self.db.first("SELECT address FROM wallet_identities WHERE user_id=? AND status='active'", (user_id,))
        if wallet is None:
            raise AppError(409, "attestation_requires_wallet", "Sign in with your wallet to stamp a forecast on-chain.")
        return hashlib.sha256(row["body"].encode()).hexdigest(), str(wallet["address"])

    async def prepare(self, user_id: str, forecast_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.available or self.relayer is None or self.sign is None:
            raise AppError(503, "attestation_unavailable", "On-chain stamping is not configured.")
        blockhash = body.get("blockhash")
        if type(blockhash) is not str:
            raise AppError(400, "attestation_blockhash", "A recent Devnet blockhash is required.")
        try:
            blockhash_bytes = base58_decode(blockhash, length=32)
        except ValueError as exc:
            raise AppError(400, "attestation_blockhash", "A recent Devnet blockhash is required.") from exc
        await self.rate_limit("attest:" + user_id, 20, DAY_MS)
        receipt_hash, address = await self._receipt(user_id, forecast_id)
        signer = base58_decode(address, length=32)
        memo = memo_text(forecast_id, receipt_hash)
        instruction = Instruction(program_id=MEMO_PROGRAM,
                                  accounts=(AccountMeta(pubkey=signer, is_signer=True, is_writable=False),),
                                  data=memo.encode("utf-8"))
        compiled = compile_message(self.relayer, blockhash_bytes, (instruction,))
        relayer_signature = await self.sign(compiled.data)
        transaction = partially_signed_transaction(compiled.data, compiled.signer_keys, {self.relayer: relayer_signature})
        message_hash = hashlib.sha256(compiled.data).hexdigest()
        attestation_id = "at_" + self.random_token()[:24]
        now = self.now_ms()
        await self.db.execute(
            "INSERT INTO forecast_attestations(id,forecast_id,user_id,address,receipt_hash,memo,blockhash,message_hash,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)",
            (attestation_id, forecast_id, user_id, address, receipt_hash, memo, blockhash, message_hash, now, now))
        return {"attestationId": attestation_id, "transaction": base64.b64encode(transaction).decode("ascii"),
                "memo": memo, "signerAddress": address, "feePayer": base58_encode(self.relayer),
                "cluster": "devnet", "expiresAt": now + 90_000}

    async def confirm(self, user_id: str, forecast_id: str, body: dict[str, Any]) -> dict[str, Any]:
        attestation_id, signature, slot = body.get("attestationId"), body.get("signature"), body.get("slot")
        if type(attestation_id) is not str or type(signature) is not str or not _SIGNATURE.fullmatch(signature):
            raise AppError(400, "attestation_confirm_invalid", "A transaction signature is required.")
        if slot is not None and (type(slot) is not int or slot < 0):
            raise AppError(400, "attestation_confirm_invalid", "A transaction signature is required.")
        row = await self.db.first("SELECT * FROM forecast_attestations WHERE id=? AND user_id=? AND forecast_id=?",
                                  (attestation_id, user_id, forecast_id))
        if row is None:
            raise AppError(404, "attestation_not_found", "This attestation was not prepared here.")
        if row["status"] == "prepared":
            await self.db.execute("UPDATE forecast_attestations SET status='submitted',signature=?,reported_slot=?,updated_at=? "
                                  "WHERE id=? AND status='prepared'", (signature, slot, self.now_ms(), attestation_id))
        elif row["signature"] != signature:
            raise AppError(409, "attestation_conflict", "This attestation already has a different signature.")
        status = await self.status(user_id, forecast_id)
        if status is None:
            raise AppError(404, "attestation_not_found", "This attestation was not prepared here.")
        return status

    async def status(self, user_id: str | None, forecast_id: str) -> dict[str, Any] | None:
        if user_id is None:
            return None
        row = await self.db.first(
            "SELECT id,status,signature,memo,reported_slot,created_at FROM forecast_attestations "
            "WHERE user_id=? AND forecast_id=? AND status IN ('submitted','verified') ORDER BY created_at DESC LIMIT 1",
            (user_id, forecast_id))
        if row is None:
            return {"status": "none", "available": self.available}
        return {"status": row["status"], "signature": row["signature"], "memo": row["memo"],
                "slot": row["reported_slot"], "at": row["created_at"], "cluster": "devnet",
                "explorer": f"https://explorer.solana.com/tx/{row['signature']}?cluster=devnet",
                "available": self.available, "commitment": content_hash({"memo": row["memo"]})}
