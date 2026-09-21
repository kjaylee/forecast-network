#!/usr/bin/env python3
"""Export the Seeker parsers, so a Rust port can be held to them.

Everything here reads someone else's RPC reply. The replies are `jsonParsed` Token-2022
accounts, which means the shape is a convention rather than a contract: a provider can
return a partial account, an amount as a number instead of a string, or an extension list
that is not a list at all. The reference refuses each of those rather than reading past
them, and a port that is merely *lenient* would report a verified Seeker where the
reference reported nothing.

The vector covers the three parsers that decide that — the token accounts, the mint
accounts and the genesis-member search — plus the display arithmetic, which is the one
place a number becomes text a person reads.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/application/src"), str(ROOT / "packages/domain/src"), str(ROOT)]

from forecast_application.seeker import (  # noqa: E402
    SGT_GROUP,
    SKR_DECIMALS,
    SKR_MINT,
    _mint_accounts,
    _parsed_info,
    _token_accounts,
    candidate_mints,
    genesis_member,
    projection,
    skr_atomic,
    skr_display,
)
from golden_cli import golden_main  # noqa: E402

GOLDEN = ROOT / "tests/golden/seeker-golden.json"


def token(mint: str, amount: str, decimals: int = 0) -> dict:
    return {"account": {"data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": {"amount": amount, "decimals": decimals, "uiAmountString": amount},
    }}}}}


def mint_account(mint: str, *, group: str | None = SGT_GROUP, number: int | None = 3, supply: str = "1") -> dict:
    extensions = []
    if group is not None:
        extensions.append({"extension": "tokenGroupMember",
                           "state": {"group": group, "mint": mint, "memberNumber": number}})
    return {"data": {"parsed": {"info": {"decimals": 0, "supply": supply, "extensions": extensions}}}}


def case(name: str, call) -> dict:
    try:
        return {"name": name, "result": call()}
    except ValueError as error:
        return {"name": name, "error": str(error)}


def build() -> dict:
    good_token = token("SGTmint", "1")
    good_mint = mint_account("SGTmint")

    token_accounts = [
        good_token,
        token("SGTother", "1"),
        token("SKR", "5000000", 6),
        token("zero", "0"),
        token("two", "2"),
    ]
    mint_accounts = [good_mint, mint_account("SGTother", group=None), mint_account("third")]

    parsed = [
        {"name": "well-formed", "value": good_token, "parsed": _parsed_info(good_token)},
        {"name": "missing-data", "value": {}, "parsed": _parsed_info({})},
        {"name": "not-a-dict", "value": {"data": None}, "parsed": _parsed_info({"data": None})},
        {"name": "info-not-a-dict", "value": {"data": {"parsed": {"info": 7}}}, "parsed": _parsed_info({"data": {"parsed": {"info": 7}}})},
    ]

    candidate_cases = [
        case("candidate:many", lambda: candidate_mints(token_accounts)),
        case("candidate:empty", lambda: candidate_mints([])),
        case("candidate:no-unit", lambda: candidate_mints([token("a", "0"), token("b", "7")])),
        case("candidate:decimals", lambda: candidate_mints([token("a", "1", 6)])),
        case("candidate:duplicate", lambda: candidate_mints([good_token, good_token])),
        # A non-dict entry is not covered: the reference would raise AttributeError there, and a
        # corpus that records a crash as behaviour is a corpus nobody should port against.
        case("candidate:malformed", lambda: candidate_mints([{"account": {}}, {"no": "account"}])),
    ]

    token_refusals = [
        case("token:ok", lambda: len(_token_accounts({"value": token_accounts}))),
        case("token:not-a-list", lambda: len(_token_accounts({"value": "x"}))),
        case("token:no-value", lambda: len(_token_accounts([good_token]))),
        case("token:amount-not-a-string", lambda: len(_token_accounts({"value": [
            {"account": {"data": {"parsed": {"info": {"mint": "m", "tokenAmount": {"amount": 1, "decimals": 0}}}}}}]}))),
        case("token:decimals-not-an-int", lambda: len(_token_accounts({"value": [
            {"account": {"data": {"parsed": {"info": {"mint": "m", "tokenAmount": {"amount": "1", "decimals": True}}}}}}]}))),
        case("token:non-ascii-amount", lambda: len(_token_accounts({"value": [
            {"account": {"data": {"parsed": {"info": {"mint": "m", "tokenAmount": {"amount": "١", "decimals": 0}}}}}}]}))),
        case("token:missing-amount", lambda: len(_token_accounts({"value": [
            {"account": {"data": {"parsed": {"info": {"mint": "m"}}}}}]}))),
    ]

    def mint_reply(info: dict) -> dict:
        """A reply whose mint account carries exactly the info given, valid or not."""
        return {"value": [{"data": {"parsed": {"info": info}}}]}

    def member_extension(number) -> dict:
        return {"extension": "tokenGroupMember",
                "state": {"group": SGT_GROUP, "mint": "m", "memberNumber": number}}

    mint_refusals = [
        case("mint:ok", lambda: len(_mint_accounts({"value": mint_accounts}, 3))),
        case("mint:count", lambda: len(_mint_accounts({"value": mint_accounts}, 2))),
        case("mint:supply-not-decimal", lambda: len(_mint_accounts(
            mint_reply({"decimals": 0, "supply": "x", "extensions": []}), 1))),
        case("mint:supply-not-a-string", lambda: len(_mint_accounts(
            mint_reply({"decimals": 0, "supply": 1, "extensions": []}), 1))),
        case("mint:decimals-not-an-int", lambda: len(_mint_accounts(
            mint_reply({"decimals": True, "supply": "1", "extensions": []}), 1))),
        case("mint:extensions-not-a-list", lambda: len(_mint_accounts(
            mint_reply({"decimals": 0, "supply": "1", "extensions": "x"}), 1))),
        case("mint:extension-not-a-dict", lambda: len(_mint_accounts(
            mint_reply({"decimals": 0, "supply": "1", "extensions": ["x"]}), 1))),
        case("mint:extension-unnamed", lambda: len(_mint_accounts(
            mint_reply({"decimals": 0, "supply": "1", "extensions": [{}]}), 1))),
        case("mint:member-state-incomplete", lambda: len(_mint_accounts(
            mint_reply({"decimals": 0, "supply": "1", "extensions": [
                {"extension": "tokenGroupMember", "state": {"group": "g"}}]}), 1))),
        # The search runs over the *parsed accounts*, so the reply is parsed first — a fixture
        # that hands the reply straight to the search would report `None` for every input.
        case("mint:member-number-not-an-int", lambda: genesis_member(
            _mint_accounts(mint_reply({"decimals": 0, "supply": "1",
                                       "extensions": [member_extension("3")]}), 1), ["m"])),
    ]

    members = [
        case("member:found", lambda: genesis_member([mint_account("SGTmint")], ["SGTmint"])),
        case("member:first", lambda: genesis_member(mint_accounts, ["SGTother", "SGTmint", "third"])),
        case("member:wrong-group", lambda: genesis_member([mint_account("m", group="other")], ["m"])),
        case("member:mint-mismatch", lambda: genesis_member([mint_account("m")], ["different"])),
        case("member:none", lambda: genesis_member([], [])),
        case("member:no-number", lambda: genesis_member([mint_account("m", number=None)], ["m"])),
        case("member:missing-extension", lambda: genesis_member([mint_account("m", group=None)], ["m"])),
    ]

    atomic_cases = [
        case("skr:sum", lambda: skr_atomic([token(SKR_MINT, "1500000", 6), token(SKR_MINT, "500000", 6)])),
        case("skr:other-mint", lambda: skr_atomic([token("other", "999", 6)])),
        case("skr:malformed", lambda: skr_atomic([{"account": {}}, token(SKR_MINT, "7", 6)])),
        case("skr:empty", lambda: skr_atomic([])),
    ]

    displays = [{"atomic": value, "display": skr_display(value)}
                for value in (0, 1, 999999, 1000000, 1500000, 123456789, 10**12, 10**6 + 10)]

    rows = [
        {"name": "absent", "row": None, "projection": projection(None)},
        {"name": "present", "row": {"member_number": 3, "skr_atomic": 1500000, "verified_at": 10, "refreshed_at": 20},
         "projection": projection({"member_number": 3, "skr_atomic": 1500000, "verified_at": 10, "refreshed_at": 20})},
        {"name": "empty-row", "row": {}, "projection": projection({})},
    ]

    return {
        "description": "The Seeker parsers: what a jsonParsed RPC reply has to look like before "
                       "anything is read out of it, and the display arithmetic.",
        "constants": {"sgtGroup": SGT_GROUP, "skrMint": SKR_MINT, "skrDecimals": SKR_DECIMALS},
        "parsed": parsed,
        "candidates": candidate_cases,
        "tokenAccounts": token_refusals,
        "mintAccounts": mint_refusals,
        "members": members,
        "atomic": atomic_cases,
        "displays": displays,
        "projections": rows,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
