"""Bounded, injected Devnet RPC transport for authority-attested commitments.

RPC supplies finalized ledger observations, not an independent light-client proof.
Sending returns a transaction identifier only; callers must reconcile finalized
account bytes. The spend authorizer reserves the worst-case cost durably before
signing and must retain reservations after uncertain network outcomes.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from .solana_registry import RegistryAccount
from .solana_wire import (
    AccountMeta,
    Instruction,
    assemble_transaction,
    base58_decode,
    base58_encode,
    compile_message,
    config_address,
    decode_config,
    decode_forecast,
    encode_advance,
    encode_register,
)
from .solana_wire import (
    forecast_address as derive_forecast_address,
)

Rpc = Callable[[str, list[Any]], Awaitable[Any]]
Signer = Callable[[bytes], Awaitable[bytes]]
SpendAuthorizer = Callable[[int], Awaitable[None]]
_MAX_INTEGER = 9_007_199_254_740_991
_SYSTEM_PROGRAM = bytes(32)
_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
_CLOCK_SYSVAR = "SysvarC1ock11111111111111111111111111111111"
_SYSVAR_OWNER = "Sysvar1111111111111111111111111111111111111"


class SolanaRpcError(ValueError):
    """An RPC observation or transaction failed a fail-closed transport gate."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SolanaRpcError(message)


def _integer(value: Any, name: str) -> int:
    _require(type(value) is int and 0 <= value <= _MAX_INTEGER, f"Invalid {name}")
    return int(value)


def _public_key(value: Any) -> bytes:
    _require(type(value) is bytes and len(value) == 32 and any(value), "Invalid public key")
    return bytes(value)


def _context(response: Any) -> tuple[int, Any]:
    _require(type(response) is dict and "value" in response, "Invalid RPC response")
    context = response.get("context")
    _require(type(context) is dict, "Missing RPC context")
    return _integer(context.get("slot"), "context slot"), response["value"]


class SolanaRpcTransport:
    def __init__(self, rpc: Rpc, sign: Signer, *, program_id: bytes, relayer: bytes,
                 expected_genesis_hash: str, authorize_spend: SpendAuthorizer,
                 max_fee_lamports: int = 20_000, max_rent_lamports: int = 10_000_000,
                 balance_floor_lamports: int = 10_000_000):
        self.program_id, self.relayer = _public_key(program_id), _public_key(relayer)
        _require(program_id != relayer, "Program and relayer must differ")
        base58_decode(expected_genesis_hash, length=32)
        self.expected_genesis_hash = expected_genesis_hash
        self.rpc, self.sign, self.authorize_spend = rpc, sign, authorize_spend
        self.max_fee_lamports = _integer(max_fee_lamports, "fee cap")
        self.max_rent_lamports = _integer(max_rent_lamports, "rent cap")
        self.balance_floor_lamports = _integer(balance_floor_lamports, "balance floor")
        _require(self.max_fee_lamports > 0 and self.max_rent_lamports > 0,
                 "Positive fee and rent caps required")
        self.config = config_address(self.program_id)[0]

    async def _call(self, method: str, params: list[Any]) -> Any:
        try:
            return await self.rpc(method, params)
        except Exception as error:
            # Provider messages may contain request headers, tokens or raw bodies.
            print(json.dumps({"event": "registry_rpc_transport_error",
                "method": method, "errorType": type(error).__name__,
                "frames": [{"function": frame.name, "line": frame.lineno}
                           for frame in traceback.extract_tb(error.__traceback__)[-4:]]}))
            raise SolanaRpcError(f"Solana RPC unavailable: {method}") from None

    async def genesis_hash(self) -> str:
        value = await self._call("getGenesisHash", [])
        _require(type(value) is str and value == self.expected_genesis_hash,
                 "Solana cluster does not match the pinned genesis")
        return str(value)

    async def finalized_time_ms(self) -> int:
        """Read the same Clock value used by the registry's on-chain time gate.

        getBlockTime is a separate block-time estimate and cannot attest that
        Clock::get().unix_timestamp has reached a program's challenge deadline.
        """
        await self.genesis_hash()
        slot, value = _context(await self._call("getAccountInfo", [_CLOCK_SYSVAR, {
            "encoding": "base64", "commitment": "finalized"}]))
        _require(type(value) is dict and value.get("owner") == _SYSVAR_OWNER
                 and value.get("executable") is False, "Invalid Clock sysvar account")
        _require(_integer(value.get("lamports"), "Clock account balance") > 0,
                 "Clock sysvar account is not allocated")
        encoded = value.get("data")
        _require(type(encoded) is list and len(encoded) == 2 and encoded[1] == "base64"
                 and type(encoded[0]) is str and len(encoded[0]) == 56,
                 "Invalid Clock sysvar encoding")
        try:
            data = base64.b64decode(encoded[0], validate=True)
        except (ValueError, binascii.Error):
            raise SolanaRpcError("Invalid Clock sysvar base64") from None
        _require(len(data) == 40 and base64.b64encode(data).decode("ascii") == encoded[0],
                 "Invalid Clock sysvar layout")
        clock_slot, _, _, _, seconds = struct.unpack("<QqQQq", data)
        _require(clock_slot == slot, "Clock sysvar does not match the finalized context")
        return _integer(seconds * 1000, "finalized Clock time in milliseconds")

    async def _valid_blockhash(self, blockhash: bytes, last_height: int, min_slot: int) -> None:
        slot, valid = _context(await self._call("isBlockhashValid", [base58_encode(blockhash), {
            "commitment": "finalized", "minContextSlot": min_slot}]))
        _require(slot >= min_slot and valid is True, "Transaction blockhash expired or RPC is stale")
        height = _integer(await self._call("getBlockHeight", [{"commitment": "finalized",
                          "minContextSlot": min_slot}]), "current block height")
        _require(height <= last_height, "Transaction block height expired")

    async def account(self, address: bytes) -> RegistryAccount | None:
        _public_key(address)
        await self.genesis_hash()
        slot, value = _context(await self._call("getAccountInfo", [base58_encode(address), {
            "encoding": "base64", "commitment": "finalized"}]))
        if value is None:
            return None
        _require(type(value) is dict and value.get("executable") is False,
                 "Invalid registry account")
        owner = base58_decode(value.get("owner"), length=32)
        _require(owner in (self.program_id, _SYSTEM_PROGRAM),
                 "Registry account has the wrong owner")
        _integer(value.get("lamports"), "account balance")
        encoded = value.get("data")
        _require(type(encoded) is list and len(encoded) == 2 and encoded[1] == "base64"
                 and type(encoded[0]) is str and len(encoded[0]) <= 480,
                 "Invalid or oversized registry account data")
        try:
            data = base64.b64decode(encoded[0], validate=True)
        except (ValueError, binascii.Error):
            raise SolanaRpcError("Invalid registry base64 data") from None
        _require(base64.b64encode(data).decode("ascii") == encoded[0],
                 "Noncanonical registry data")
        if owner == _SYSTEM_PROGRAM:
            # Anyone can transfer lamports to a predictable PDA. The program can
            # allocate/assign that empty System account; prefunding must not deny
            # publication. Nonempty System accounts are never registry state.
            _require(data == b"", "System-owned registry target contains unexpected data")
            return None
        if address == self.config:
            decode_config(data)
        else:
            decoded = decode_forecast(data)
            _require(derive_forecast_address(self.program_id, decoded.forecast_id_hash)[0]
                     == address, "Registry account PDA mismatch")
        return RegistryAccount(address, owner, data, slot, True)

    def _instruction(self, data: bytes, address: bytes, register: bool) -> None:
        _public_key(address)
        _require(type(register) is bool and type(data) is bytes, "Invalid instruction")
        _require(len(data) == (193 if register else 255) and data[0] == (1 if register else 2),
                 "Only exact registry register/advance instructions may be relayed")
        if register:
            opened, closed, revision, occurred = struct.unpack_from("<qqQq", data, 97)
            canonical = encode_register(
                forecast_id_hash=data[1:33], creator_hash=data[33:65],
                specification_hash=data[65:97], open_at_ms=opened, close_at_ms=closed,
                revision=revision, occurred_at_ms=occurred, event_hash=data[129:161],
                snapshot_hash=data[161:193])
            _require(derive_forecast_address(self.program_id, data[1:33])[0] == address,
                     "Publication instruction targets the wrong PDA")
        else:
            revision, occurred = struct.unpack_from("<Qq", data, 1)
            challenge, pending, material = struct.unpack_from("<qHH", data, 243)
            canonical = encode_advance(
                revision=revision, occurred_at_ms=occurred, previous_event_hash=data[17:49],
                event_hash=data[49:81], snapshot_hash=data[81:113], state=data[113],
                outcome=data[114], resolution_hash=data[115:147], dispute_hash=data[147:179],
                reputation_hash=data[179:211], trigger_hash=data[211:243],
                challenge_until_ms=challenge, pending_disputes=pending, material_disputes=material)
        _require(canonical == data, "Noncanonical instruction")

    async def send(self, instruction: bytes, forecast_address: bytes, *, register: bool) -> str:
        self._instruction(instruction, forecast_address, register)
        await self.genesis_hash()
        # A misconfigured RPC/program must fail before any operational signing.
        _, program = _context(await self._call("getAccountInfo", [
            base58_encode(self.program_id), {"encoding": "base64", "commitment": "finalized",
                                            "dataSlice": {"offset": 0, "length": 0}}]))
        _require(type(program) is dict and program.get("executable") is True
                 and program.get("owner") == _LOADER, "Pinned registry program is not executable")
        config = await self.account(self.config)
        if config is None:
            raise SolanaRpcError("Registry configuration is missing")
        _require(decode_config(config.data).relayer == self.relayer, "Relayer is not authorized")
        target = await self.account(forecast_address)
        _require(target is None if register else target is not None,
                 "Registry target does not match the requested operation")
        if not register and target is not None:
            current = decode_forecast(target.data)
            revision = struct.unpack_from("<Q", instruction, 1)[0]
            _require(revision == current.revision + 1 and instruction[17:49] == current.event_hash,
                     "Advance does not extend the finalized predecessor")
        context_slot, block = _context(await self._call("getLatestBlockhash", [
            {"commitment": "finalized"}]))
        _require(context_slot >= max(config.slot, target.slot if target else 0),
                 "Blockhash RPC is behind the observed registry state")
        _require(type(block) is dict, "Invalid blockhash response")
        blockhash = base58_decode(block.get("blockhash"), length=32)
        last_height = _integer(block.get("lastValidBlockHeight"), "blockhash expiry")
        accounts: tuple[AccountMeta, ...] = (
            AccountMeta(self.relayer, True, True), AccountMeta(self.config),
            AccountMeta(forecast_address, False, True))
        if register:
            accounts += (AccountMeta(_SYSTEM_PROGRAM),)
        message = compile_message(self.relayer, blockhash, (
            Instruction(self.program_id, accounts, instruction),))
        _require(message.signer_keys == (self.relayer,), "Unexpected transaction signer")
        fee_slot, fee = _context(await self._call("getFeeForMessage", [
            base64.b64encode(message.data).decode("ascii"), {
                "commitment": "finalized", "minContextSlot": context_slot}]))
        _require(fee_slot >= context_slot, "Fee RPC is behind the observed blockhash")
        fee = _integer(fee, "transaction fee")
        _require(0 < fee <= self.max_fee_lamports, "Transaction fee exceeds cap")
        rent = _integer(await self._call("getMinimumBalanceForRentExemption", [
            360, {"commitment": "finalized"}]), "account rent") if register else 0
        _require(rent <= self.max_rent_lamports and (not register or rent > 0),
                 "Account rent exceeds cap")
        balance_slot, balance = _context(await self._call("getBalance", [base58_encode(self.relayer), {
            "commitment": "finalized", "minContextSlot": context_slot}]))
        _require(balance_slot >= context_slot, "Balance RPC is behind the observed blockhash")
        _require(_integer(balance, "relayer balance") >= fee + rent + self.balance_floor_lamports,
                 "Relayer balance is below the protected floor")
        await self._valid_blockhash(blockhash, last_height, context_slot)
        await self.authorize_spend(fee + rent)
        signature = await self.sign(message.data)
        _require(type(signature) is bytes and len(signature) == 64 and any(signature),
                 "Signer returned an invalid signature")
        transaction = assemble_transaction(message, {self.relayer: signature})
        encoded = base64.b64encode(transaction).decode("ascii")
        simulation_slot, simulation = _context(await self._call("simulateTransaction", [encoded, {
            "encoding": "base64", "sigVerify": True, "replaceRecentBlockhash": False,
            "commitment": "confirmed", "minContextSlot": context_slot}]))
        _require(simulation_slot >= context_slot and type(simulation) is dict
                 and "err" in simulation and simulation["err"] is None,
                 "Transaction simulation failed")
        await self._valid_blockhash(blockhash, last_height, context_slot)
        expected_signature = base58_encode(signature)
        returned = await self._call("sendTransaction", [encoded, {
            "encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed",
            "maxRetries": 0, "minContextSlot": context_slot}])
        _require(type(returned) is str and returned == expected_signature,
                 "RPC returned a different transaction signature; reconcile before retry")
        return expected_signature

    async def signature_finalized(self, signature: str) -> bool:
        base58_decode(signature, length=64)
        await self.genesis_hash()
        _, values = _context(await self._call("getSignatureStatuses", [
            [signature], {"searchTransactionHistory": True}]))
        _require(type(values) is list and len(values) == 1, "Invalid transaction status response")
        status = values[0]
        if status is None:
            return False
        _require(type(status) is dict and "err" in status, "Invalid transaction status")
        _require(status["err"] is None, "Transaction failed on chain")
        _integer(status.get("slot"), "transaction slot")
        confirmation = status.get("confirmationStatus")
        _require(confirmation in ("processed", "confirmed", "finalized"),
                 "Unknown transaction confirmation state")
        if confirmation == "finalized":
            _require("confirmations" in status and status["confirmations"] is None,
                     "Inconsistent finalized transaction status")
            return True
        return False
