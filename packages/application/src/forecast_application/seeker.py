"""Seeker ownership verified on mainnet.

Every Solana Seeker mints one Seeker Genesis Token (SGT): a non-transferable Token-2022
mint that is a member of the SGT group. Finding a member of that group in the signed-in
wallet proves the account belongs to a Seeker owner, which is worth showing on a public
record. The SKR balance is read alongside as plain information; it grants nothing.

The Worker reads mainnet through an RPC endpoint that accepts Cloudflare egress. The
RPC client is injected so the parsing here stays testable without a network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .database import Database
from .errors import AppError

SGT_GROUP = "GT22s89nU4iWFkNXj1Bw6uYhJJWDRPpShHt4Bk8f99Te"
SKR_MINT = "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3"
SKR_DECIMALS = 6
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
REFRESH_MS = 6 * 3_600_000

Rpc = Callable[[str, list[Any]], Awaitable[Any]]


def _parsed_info(account: Any) -> dict[str, Any]:
    try:
        info = account["data"]["parsed"]["info"]
    except (KeyError, TypeError):
        return {}
    return dict(info) if isinstance(info, dict) else {}


def candidate_mints(token_accounts: list[Any]) -> list[str]:
    """Token-2022 accounts holding exactly one indivisible unit; only those can be an SGT."""
    mints = []
    for entry in token_accounts or []:
        info = _parsed_info(entry.get("account", {}))
        amount = info.get("tokenAmount") or {}
        if amount.get("decimals") == 0 and amount.get("amount") == "1" and isinstance(info.get("mint"), str):
            mints.append(info["mint"])
    return mints


def genesis_member(mint_accounts: list[Any], mints: list[str]) -> tuple[str, int | None] | None:
    """The first mint whose tokenGroupMember extension points at the SGT group."""
    for mint, account in zip(mints, mint_accounts or []):
        for extension in _parsed_info(account).get("extensions") or []:
            if extension.get("extension") != "tokenGroupMember":
                continue
            state = extension.get("state") or {}
            if state.get("group") == SGT_GROUP and state.get("mint") == mint:
                number = state.get("memberNumber")
                return mint, number if isinstance(number, int) else None
    return None


def skr_atomic(token_accounts: list[Any]) -> int:
    total = 0
    for entry in token_accounts or []:
        info = _parsed_info(entry.get("account", {}))
        if info.get("mint") != SKR_MINT:
            continue
        try:
            total += int((info.get("tokenAmount") or {}).get("amount", "0"))
        except (TypeError, ValueError):
            continue
    return total


def skr_display(atomic: int) -> str:
    whole, fraction = divmod(atomic, 10 ** SKR_DECIMALS)
    text = f"{whole}.{fraction:0{SKR_DECIMALS}d}".rstrip("0").rstrip(".")
    return text or "0"


def projection(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"verified": True, "memberNumber": row["member_number"], "skr": skr_display(int(row["skr_atomic"])),
            "verifiedAt": row["verified_at"], "refreshedAt": row["refreshed_at"]}


class SeekerVerification:
    def __init__(self, db: Database, *, rpc: Rpc | None, now_ms: Callable[[], int],
                 rate_limit: Callable[[str, int, int], Awaitable[None]]):
        self.db, self.rpc, self.now_ms, self.rate_limit = db, rpc, now_ms, rate_limit

    @property
    def available(self) -> bool:
        return self.rpc is not None

    async def status(self, user_id: str) -> dict[str, Any] | None:
        return projection(await self.db.first("SELECT * FROM seeker_verifications WHERE user_id=?", (user_id,)))

    async def public_badge(self, user_id: str) -> dict[str, Any] | None:
        row = await self.db.first("SELECT member_number FROM seeker_verifications WHERE user_id=?", (user_id,))
        return {"memberNumber": row["member_number"]} if row else None

    async def _address(self, user_id: str) -> str:
        identity = await self.db.first("SELECT address FROM wallet_identities WHERE user_id=? AND status='active' "
                                       "AND converted_at IS NOT NULL", (user_id,))
        if identity:
            return str(identity["address"])
        link = await self.db.first("SELECT address FROM wallet_links WHERE user_id=?", (user_id,))
        if link:
            return str(link["address"])
        raise AppError(409, "wallet_required", "Sign in with your Seeker wallet before verifying.")

    async def verify(self, user_id: str) -> dict[str, Any]:
        if self.rpc is None:
            raise AppError(503, "seeker_unavailable", "Seeker verification is not configured.")
        await self.rate_limit("seeker-verify:" + user_id, 6, 3_600_000)
        address = await self._address(user_id)
        try:
            token_2022 = await self.rpc("getTokenAccountsByOwner",
                                        [address, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed"}])
            mints = candidate_mints((token_2022 or {}).get("value") or [])
            member = None
            if mints:
                accounts = await self.rpc("getMultipleAccounts", [mints[:100], {"encoding": "jsonParsed"}])
                member = genesis_member((accounts or {}).get("value") or [], mints[:100])
            skr = await self.rpc("getTokenAccountsByOwner",
                                 [address, {"mint": SKR_MINT}, {"encoding": "jsonParsed"}])
        except AppError:
            raise
        except Exception as exc:  # transport or malformed RPC reply; never leak the endpoint
            raise AppError(503, "seeker_rpc_unavailable", "Solana could not be reached. Try again shortly.") from exc
        if member is None:
            raise AppError(409, "seeker_not_found", "No Seeker Genesis Token was found in this wallet.")
        mint, number = member
        slot = int(((skr or {}).get("context") or {}).get("slot") or 0)
        atomic = skr_atomic((skr or {}).get("value") or [])
        now = self.now_ms()
        taken = await self.db.first("SELECT user_id FROM seeker_verifications WHERE genesis_mint=? AND user_id!=?",
                                    (mint, user_id))
        if taken:
            raise AppError(409, "seeker_already_claimed", "This Seeker is already verified on another account.")
        await self.db.execute(
            "INSERT INTO seeker_verifications(user_id,address,genesis_mint,member_number,skr_atomic,slot,verified_at,refreshed_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,"
            "genesis_mint=excluded.genesis_mint,member_number=excluded.member_number,skr_atomic=excluded.skr_atomic,"
            "slot=excluded.slot,refreshed_at=excluded.refreshed_at",
            (user_id, address, mint, number, str(atomic), slot, now, now))
        status = await self.status(user_id)
        assert status is not None
        return status
