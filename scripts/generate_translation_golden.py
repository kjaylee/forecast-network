#!/usr/bin/env python3
"""Export what a display translation must preserve, so a Rust port can be held to it.

A translation is presentation only — the canonical specification is never rewritten — but
"presentation only" is doing real work in that sentence. A translated question that drops a
negation, rounds a threshold, or turns a deadline into a different date is a *different
question* shown to a user who is about to stake something on it. So the checks here are the
ones that stand between a faithful rendering and a plausible-looking different one:

  * every literal number in the source must survive, and no new one may appear — with one
    allowance, that a month name may become its number in a translation;
  * every source URL must survive exactly;
  * rule identifiers, order and outcomes must not move;
  * and the prose must actually be in the language that was asked for.

The vector exports `validate_translation` over a corpus that isolates each rule, plus the
three shapes of refusal a caller has to be able to tell apart. `_numbers` is the part that
needs the most care: its pattern uses both a lookbehind and a lookahead, neither of which
Rust's `regex` has.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import AIRejected  # noqa: E402
from forecast_application.display_translations import (  # noqa: E402
    DisplayTranslations,
    _numbers,
    checked_language,
    digest,
    validate_translation,
)
from forecast_application.errors import AppError  # noqa: E402
from tests.test_web_ai import Transport, coordinator  # noqa: E402

GOLDEN = ROOT / "tests/golden/display-translation-golden.json"


def source(**changes) -> dict:
    document = {
        "schemaVersion": 1, "forecastId": "forecast-1", "specificationHash": "a" * 64,
        "language": "en",
        "title": "Will Acme announce Product X?",
        "question": "Will Acme officially announce Product X before 2026-09-21T12:00:00Z?",
        "rules": [
            {"clauseId": "yes-rule", "outcome": "YES",
             "condition": "An announcement dated before 2026-09-21T12:00:00Z names Product X."},
            {"clauseId": "no-rule", "outcome": "NO",
             "condition": "No qualifying announcement exists at 2026-09-21T12:00:00Z."},
            {"clauseId": "invalid-rule", "outcome": "INVALID",
             "condition": "The named company cannot be uniquely identified."},
        ],
        "invalidationRules": ["Apply INVALID if the named product has multiple identities."],
        "aiRationale": "Available source context suggests 62%, with launch uncertainty.",
        "openAt": 1000, "closeAt": 400000,
    }
    document.update(changes)
    return document


def translated(document: dict, **changes) -> dict:
    output = {key: document[key] for key in ("title", "question", "rules", "invalidationRules", "aiRationale")}
    output["rules"] = [dict(rule) for rule in output["rules"]]
    output["invalidationRules"] = list(output["invalidationRules"])
    output.update(changes)
    return output


def case(name: str, document: dict, output, language: str = "en") -> dict:
    entry = {"name": name, "language": language, "source": document, "output": output}
    try:
        entry["accepted"] = validate_translation(document, output, language)
    except AIRejected as exc:
        entry["error"] = {"code": exc.code, "message": str(exc)}
    return entry


async def generated_cases() -> list:
    """`generate_translation` end to end, replayed through the provider harness.

    Two shapes: a faithful translation the independent review accepts, and one the review
    rejects — because the validator above cannot see a dropped negation, and the review is the
    only check that can.
    """
    from forecast_domain.serialization import to_dict

    base = source()
    faithful = {
        "title": "Will Acme announce Product X?",
        "question": "Will Acme officially announce Product X before 2026-09-21T12:00:00Z?",
        "rules": [dict(rule) for rule in base["rules"]],
        "invalidationRules": list(base["invalidationRules"]),
        "aiRationale": base["aiRationale"],
    }
    review_ok = {"faithful": True, "language_correct": True, "numbers_and_dates_preserved": True,
                 "explanation": "Every field matches its source."}
    review_no = {"faithful": False, "language_correct": True, "numbers_and_dates_preserved": True,
                 "explanation": "The negation in the NO clause was dropped."}
    cases = []
    for name, review in (("accepted", review_ok), ("refused-by-review", review_no)):
        transport = Transport([faithful, review])
        result = None
        error = None
        try:
            result = await coordinator(transport).translate_display(base, "en")
        except Exception as exc:  # noqa: BLE001 - the reference's refusals are all AIRejected
            error = {"code": getattr(exc, "code", None), "message": str(exc),
                     "artifacts": [{"kind": item.kind, "hash": item.content_hash} for item in getattr(exc, "artifacts", ())]}
        entry = {"name": name, "source": base, "responses": [faithful, review],
                 "calls": [{"url": call["url"], "body": call["body"]} for call in transport.calls]}
        if result is not None:
            entry["expect"] = {"body": result.body,
                               "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                                             for item in result.artifacts]}
        else:
            entry["error"] = error
        cases.append(entry)
    return cases


def build() -> dict:
    import asyncio
    base = source()
    korean = "애크미가 2026-09-21T12:00:00Z 전에 제품 X를 공식 발표할까요?"
    japanese = "アクメは 2026-09-21T12:00:00Z より前に製品 X を発表しますか?"
    chinese = "Acme 會在 2026-09-21T12:00:00Z 前宣布產品 X 嗎?"

    cases = [
        # The direct translation, which is also the identity: nothing to change in English.
        case("english-identity", base, translated(base)),
        # Each rule that has to hold, one at a time.
        case("wrong-fields",
             base, {**translated(base), "extra": 1}),
        case("title-oversized",
             base, translated(base, title="x" * 241)),
        case("rule-count",
             base, translated(base, rules=translated(base)["rules"][:2])),
        case("rule-identity-swapped",
             base, translated(base, rules=[
                 {**rule, "outcome": "NO"} if index == 0 else rule
                 for index, rule in enumerate(translated(base)["rules"])])),
        case("invalidation-count",
             base, translated(base, invalidationRules=[])),
        case("rationale-presence",
             base, translated(base, aiRationale=None)),
        case("prose-empty",
             base, translated(base, question="   ")),
        case("prose-oversized",
             base, translated(base, question="x" * 16001)),
        case("control-character",
             base, translated(base, question="A question with a \x07 bell.")),
        # Numbers: the rule this module exists for.
        case("number-dropped",
             base, translated(base, question="Will Acme announce Product X before the deadline?")),
        case("number-invented",
             base, translated(base, question="Will Acme announce Product X 2 times before 2026-09-21T12:00:00Z?")),
        case("number-regrouped",
             source(question="The threshold is 1,234,567 units before 2026-09-21T12:00:00Z."),
             translated(source(question="The threshold is 1,234,567 units before 2026-09-21T12:00:00Z."),
                        question="The threshold is 1234567 units before 2026-09-21T12:00:00Z.")),
        case("number-insignificant-zeroes",
             source(question="Will the count reach 1.50 before 2026-09-21T12:00:00Z?"),
             translated(source(question="Will the count reach 1.50 before 2026-09-21T12:00:00Z?"),
                        question="Will the count reach 1.5 before 2026-09-21T12:00:00Z?")),
        case("month-name-becomes-its-number",
             source(question="Will Acme announce Product X by September 2026?"),
             translated(source(question="Will Acme announce Product X by September 2026?"),
                        question="Will Acme announce Product X by 9 2026?")),
        case("month-number-is-not-otherwise-allowed",
             source(question="Will Acme announce Product X by the deadline?"),
             translated(source(question="Will Acme announce Product X by the deadline?"),
                        question="Will Acme announce Product X by 9?")),
        # URLs.
        case("url-dropped",
             source(question="See https://www.apple.com/newsroom/ for the announcement."),
             translated(source(question="See https://www.apple.com/newsroom/ for the announcement."),
                        question="See the newsroom for the announcement.")),
        case("url-changed",
             source(question="See https://www.apple.com/newsroom/ for the announcement."),
             translated(source(question="See https://www.apple.com/newsroom/ for the announcement."),
                        question="See https://www.apple.com/news/ for the announcement.")),
        case("url-inside-full-width-brackets",
             source(question="See https://www.apple.com/newsroom/ for the announcement."),
             translated(source(question="See https://www.apple.com/newsroom/ for the announcement."),
                        question="「https://www.apple.com/newsroom/」を参照。")),
        # The script of the prose has to be the one that was asked for.
        case("korean-present", base,
             translated(base, question=korean), "ko"),
        case("korean-missing", base,
             translated(base, question="Acme will announce Product X."), "ko"),
        case("japanese-present", base,
             translated(base, question=japanese), "ja"),
        case("japanese-missing", base,
             translated(base, question="Acme will announce Product X."), "ja"),
        case("chinese-present", base,
             translated(base, question=chinese), "zh-Hant"),
        case("chinese-missing", base,
             translated(base, question="Acme will announce Product X."), "zh-Hant"),
        # The retained byte boundary.
        case("translation-too-large", base,
             translated(base, invalidationRules=["x" * 16000] * 1,
                        title="y" * 240,
                        question="z" * 16000,
                        rules=[{**rule, "condition": "q" * 16000} for rule in translated(base)["rules"]])),
    ]

    languages = []
    for language in ("en", "ko", "ja", "zh-Hant", "fr", "", 3):
        try:
            languages.append({"language": language, "accepted": checked_language(language)})
        except AppError as exc:
            languages.append({"language": language, "code": exc.code})

    number_corpus = [
        "1,234,567", "1234567", "1.50", "1.5", "007", "0.00", "-12.5", "+3", "2026-09-21",
        "2026-09-21T12:00:00Z", "12:00", "62%", "1e3", "12,000,000", "1,23", "9,999", "0",
        "3.14159", "-0", "1,000.50", "999,999,999,999",
        # `Decimal.normalize()` strips trailing zeros and *keeps the exponent*, so a value with
        # seven of them comes back in scientific notation. A port that renders 12,000,000 as
        # "12000000" would compare unequal to every translation that wrote it the same way.
        "1000000", "100.00", "10.0100", "0.0000001", "0.000001", "-1000000", "12000000",
    ]

    fixture = source()
    return {
        "description": "validate_translation: the rules that stand between a faithful display "
                       "translation and a plausible-looking different question.",
        "prefixes": {"source": "forecast-network:sha256:display-source:v1\n",
                     "translation": "forecast-network:sha256:display-translation:v1\n"},
        "numbers": [{"value": text, "parsed": sorted(_numbers(text))} for text in number_corpus],
        "languages": languages,
        "envelope": {"payload": fixture, "envelope": DisplayTranslations.envelope(dict(fixture))},
        "digest": {"payload": fixture, "hash": digest("forecast-network:sha256:display-source:v1\n", fixture)},
        "cases": cases,
        "generated": asyncio.run(generated_cases()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if arguments.write:
        GOLDEN.write_text(document)
        print(f"wrote {GOLDEN.relative_to(ROOT)}")
        return 0
    if arguments.check:
        current = GOLDEN.read_text() if GOLDEN.exists() else ""
        if current != document:
            print(f"{GOLDEN.relative_to(ROOT)} is stale; regenerate with --write", file=sys.stderr)
            return 1
        print(f"{GOLDEN.relative_to(ROOT)} is current")
        return 0
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
