"""Point policy, ledger conservation, grant eligibility, and atomicity checks."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

from forecast_application.database import SQLiteDatabase
from forecast_application.errors import AppError
from forecast_application.points import (
    MAX_BALANCE,
    POLICY_VERSION,
    PointsService,
    reservation_sql,
    settlement_sql,
)
from forecast_domain import (
    Command,
    Forecast,
    apply_command,
    content_hash,
    create_forecast,
    dumps,
    loads,
)
from forecast_domain.lifecycle import (
    BeginChallenge,
    BeginResolution,
    BeginValidation,
    Finalize,
    Lock,
    ProposeResolution,
    Publish,
    SubmitForecast,
)
from forecast_domain.models import ForecastChoice, Outcome, UserForecast

from tests import model_fixtures as fixtures

ROOT = Path(__file__).resolve().parents[1]


class PointsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.points = PointsService(self.db)
        self.now = 1_000_000
        self.counter = 0
        for uid in ("user-a", "user-b", "user-c"):
            await self.user(uid)

    async def asyncTearDown(self):
        self.connection.close()

    async def user(self, uid):
        await self.db.execute("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                              (uid, uid, uid, "hash:"+uid, self.now-200))

    async def opened(self, fid):
        spec = fixtures.specification(canonical_question="Will "+fid+" be announced?",
                                      open_at_ms=self.now-100, close_at_ms=self.now+1000)
        forecast = create_forecast(forecast_id=fid, creator_id="user-a", specification=spec, now_ms=self.now-100)
        validation = apply_command(forecast, Command(idempotency_key="validate", expected_revision=0,
            payload=BeginValidation()), now_ms=self.now-90)
        published = apply_command(validation.forecast, Command(idempotency_key="publish", expected_revision=1,
            payload=Publish(assessment=fixtures.validation(spec, validated_at_ms=self.now))), now_ms=self.now)
        forecast = published.forecast
        await self.db.batch((
            ("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
             "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (fid, "user-a", "draft:"+fid, dumps(forecast), forecast.revision, "OPEN", spec.category.value,
              spec.share_title, spec.canonical_question, spec.canonical_question.casefold(), spec.specification_hash,
              spec.open_at_ms, spec.close_at_ms, forecast.created_at_ms, self.now, "publish")),
            *self.event_sql(validation), *self.event_sql(published)))
        return forecast

    @staticmethod
    def event_sql(result):
        return [("INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                 (e.forecast_id, e.revision, content_hash(e), dumps(e), e.occurred_at_ms)) for e in result.events]

    async def reserve(self, uid, fid, amount, *, outcome="YES", operation=None, extra=()):
        self.counter += 1
        operation = operation or f"operation-{self.counter}"
        row = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (fid,))
        forecast = loads(Forecast, row["snapshot"])
        vote = UserForecast(forecast_id=fid, forecaster_id=uid, specification_hash=forecast.specification_hash,
            outcome=ForecastChoice(outcome), confidence=70, submitted_at_ms=self.now)
        result = apply_command(forecast, Command(idempotency_key=operation,
            expected_revision=forecast.revision, payload=SubmitForecast(user_forecast=vote)), now_ms=self.now)
        guard = "caller:"+operation
        await self.db.batch((
            ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM forecasts "
             "WHERE id=? AND revision=?) THEN 1 ELSE 0 END", (guard, fid, forecast.revision)),
            ("UPDATE forecasts SET snapshot=?,revision=?,updated_at=? WHERE id=?",
             (dumps(result.forecast), result.forecast.revision, self.now, fid)),
            *self.event_sql(result),
            ("INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,revision,body) "
             "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(forecast_id,user_id) DO UPDATE SET outcome=excluded.outcome,"
             "revision=excluded.revision,body=excluded.body",
             (fid, uid, outcome, 70, 70 if outcome == "YES" else 30, self.now, result.forecast.revision, dumps(vote))),
            *reservation_sql(uid, fid, amount, outcome, result.forecast.revision, operation, self.now),
            *extra,
            ("DELETE FROM mutation_guards WHERE token=?", (guard,))))
        return result.forecast.revision, operation

    async def finalize(self, fid, outcome=Outcome.YES):
        row = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (fid,))
        forecast = loads(Forecast, row["snapshot"])
        close = forecast.specification.close_at_ms
        payloads = (
            (Lock(), close), (BeginResolution(), close+1),
            (ProposeResolution(resolution=fixtures.resolution(forecast.specification, forecast_id=fid,
                proposed_at_ms=close+1000, outcome=outcome)), close+1000),
            (BeginChallenge(duration_ms=1000), close+1100), (Finalize(), close+2100),
        )
        statements = []
        for payload, at in payloads:
            result = apply_command(forecast, Command(idempotency_key="final:"+str(forecast.revision),
                expected_revision=forecast.revision, payload=payload), now_ms=at)
            forecast = result.forecast
            statements.extend(self.event_sql(result))
        statements.append(("UPDATE forecasts SET snapshot=?,revision=?,state=?,finalized_outcome=?,updated_at=? WHERE id=?",
            (dumps(forecast), forecast.revision, forecast.state.value, outcome.value, forecast.updated_at_ms, fid)))
        await self.db.batch(statements)
        self.now = forecast.updated_at_ms
        return forecast

    async def wallet(self, uid, address):
        self.counter += 1
        cid = f"wallet-generation-{self.counter}"
        await self.db.batch((
            ("INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at,used_at) "
             "VALUES(?,?,?,'https://forecast.example','link_forecast_profile','solana:devnet','verified fixture',?,?,?)",
             (cid, uid, address, self.now, self.now+300000, self.now)),
            ("INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) VALUES(?,?,'solana:devnet',?,?,1) "
             "ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,linked_at=excluded.linked_at,"
             "generation=excluded.generation,revision=wallet_links.revision+1", (uid, address, self.now, cid))))

    async def assert_conserved(self, uid):
        row = await self.db.first("SELECT a.available,a.committed,"
            "(SELECT COALESCE(SUM(available_delta),0) FROM point_ledger WHERE user_id=a.user_id) AS ledger_available,"
            "(SELECT COALESCE(SUM(committed_delta),0) FROM point_ledger WHERE user_id=a.user_id) AS ledger_committed,"
            "(SELECT COALESCE(SUM(amount),0) FROM point_positions WHERE user_id=a.user_id AND status='committed') AS holds "
            "FROM point_accounts a WHERE user_id=?", (uid,))
        self.assertEqual(row["available"], row["ledger_available"])
        self.assertEqual(row["committed"], row["ledger_committed"])
        self.assertEqual(row["committed"], row["holds"])

    async def test_profile_grant_once_and_no_rename_login_or_duplicate_insert_farming(self):
        summary = await self.points.summary("user-a")
        self.assertEqual((summary["userId"], summary["available"], summary["committed"], summary["total"]),
                         ("user-a", 1000, 0, 1000))
        self.assertTrue(summary["onboarding"]["profile"]["completed"])
        await self.db.execute("UPDATE users SET display_name='Changed' WHERE id='user-a'")
        await self.db.execute("INSERT OR IGNORE INTO users SELECT * FROM users WHERE id='user-a'")
        await self.db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES('session','user-a',0,999999999)")
        self.assertEqual((await self.points.summary("user-a"))["available"], 1000)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_awards WHERE user_id='user-a'"))["n"], 1)
        await self.assert_conserved("user-a")

    async def test_profile_grant_rolls_back_with_failed_registration_batch(self):
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.batch((
                ("INSERT INTO users VALUES('rollback-user','Name','rollback-handle','rollback-hash',1)", ()),
                ("INSERT INTO mutation_guards VALUES('fail',0)", ())))
        self.assertIsNone(await self.db.first("SELECT * FROM point_accounts WHERE user_id='rollback-user'"))
        self.assertIsNone(await self.db.first("SELECT * FROM point_ledger WHERE user_id='rollback-user'"))

    async def test_wallet_grant_once_per_account_and_address_lifetime(self):
        await self.wallet("user-a", "address-one")
        self.assertEqual((await self.points.summary("user-a"))["available"], 1500)
        await self.db.execute("DELETE FROM wallet_links WHERE user_id='user-a'")
        await self.wallet("user-a", "address-one")
        await self.wallet("user-a", "address-two")
        self.assertEqual((await self.points.summary("user-a"))["available"], 1500)
        self.assertIsNone(await self.db.first("SELECT id FROM point_awards WHERE wallet_address='address-two'"))
        await self.db.execute("DELETE FROM wallet_links WHERE user_id='user-a'")
        await self.wallet("user-b", "address-one")
        blocked = await self.points.summary("user-b")
        self.assertEqual(blocked["available"], 1000)
        self.assertFalse(blocked["onboarding"]["wallet"]["eligible"])
        self.assertEqual(blocked["onboarding"]["wallet"]["reason"], "wallet_already_rewarded")
        self.assertNotIn("user-a", json.dumps(blocked))
        await self.wallet("user-b", "address-two")
        self.assertEqual((await self.points.summary("user-b"))["available"], 1500)
        await self.assert_conserved("user-a")
        await self.assert_conserved("user-b")

    async def test_wallet_bonus_and_lifetime_claim_roll_back_with_failed_link(self):
        await self.db.execute("CREATE TRIGGER fail_wallet AFTER INSERT ON wallet_links "
                              "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.wallet("user-a", "rollback-address")
        self.assertEqual((await self.points.summary("user-a"))["available"], 1000)
        self.assertIsNone(await self.db.first("SELECT id FROM point_awards WHERE wallet_address='rollback-address'"))

    async def test_wallet_award_requires_matching_current_link_but_exact_ignored_retry_survives_unlink(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "points_wallet_not_verified"):
            await self.db.execute("INSERT INTO point_awards(id,user_id,kind,wallet_address,amount,policy_version,created_at) "
                "VALUES('forged-award','user-a','wallet','unverified-wallet',500,?,?)", (POLICY_VERSION, self.now))
        await self.wallet("user-b", "other-users-wallet")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "points_wallet_not_verified"):
            await self.db.execute("INSERT INTO point_awards(id,user_id,kind,wallet_address,amount,policy_version,created_at) "
                "VALUES('foreign-award','user-a','wallet','other-users-wallet',500,?,?)", (POLICY_VERSION, self.now))
        await self.db.execute("DELETE FROM wallet_links WHERE user_id='user-b'")
        await self.db.execute("INSERT OR IGNORE INTO point_awards SELECT * FROM point_awards WHERE user_id='user-b' AND kind='wallet'")
        self.assertEqual((await self.points.summary("user-b"))["available"],1500)
        self.assertEqual((await self.points.summary("user-a"))["available"],1000)

    async def test_hold_increase_decrease_practice_release_and_outcome_updates(self):
        await self.opened("market-a")
        for amount, outcome, available, committed in ((400,"YES",600,400),(700,"NO",300,700),(200,"YES",800,200),(0,"NO",1000,0)):
            await self.reserve("user-a", "market-a", amount, outcome=outcome)
            summary = await self.points.summary("user-a")
            self.assertEqual((summary["available"], summary["committed"], summary["total"]), (available, committed, 1000))
            position = await self.points.position("user-a", "market-a")
            self.assertEqual(position["amount"], amount)
            self.assertEqual(position["outcome"], outcome)
            self.assertEqual(position["status"], "practice" if amount == 0 else "committed")
            self.assertEqual(position["policyVersion"], POLICY_VERSION)
            await self.assert_conserved("user-a")

    async def test_integer_stake_bounds_and_nonfinancial_policy_flags(self):
        for value in (-1,1001,1.5,True,"10",None,10**100):
            with self.subTest(value=value), self.assertRaises(AppError):
                reservation_sql("user-a","market-a",value,"YES",3,"op",self.now)
        for value in (0,1,1000):
            self.assertTrue(reservation_sql("user-a","market-a",value,"YES",3,"op",self.now))
        policy = (await self.points.summary("user-a"))["policy"]
        for flag in ("purchasable","transferable","redeemable","reputationWeighted"):
            self.assertIs(policy[flag], False)
        self.assertEqual(policy["winReturnMultiplier"], 2)

    async def test_insufficient_balance_rolls_back_domain_vote_and_hold(self):
        await self.opened("market-a")
        await self.opened("market-b")
        await self.reserve("user-a","market-a",800)
        before = await self.db.first("SELECT snapshot FROM forecasts WHERE id='market-b'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "points_insufficient_balance"):
            await self.reserve("user-a","market-b",300)
        self.assertEqual((await self.db.first("SELECT snapshot FROM forecasts WHERE id='market-b'"))["snapshot"], before["snapshot"])
        self.assertEqual((await self.points.position("user-a","market-b"))["amount"], 0)
        self.assertIsNone(await self.db.first("SELECT user_id FROM user_forecasts WHERE forecast_id='market-b'"))
        await self.assert_conserved("user-a")

    async def test_different_forecast_race_cannot_spend_the_same_available_points(self):
        await self.opened("market-a")
        await self.opened("market-b")
        results = await asyncio.gather(self.reserve("user-a","market-a",700), self.reserve("user-a","market-b",700),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(result, sqlite3.IntegrityError) for result in results), 1)
        summary = await self.points.summary("user-a")
        self.assertEqual((summary["available"],summary["committed"]),(300,700))
        await self.assert_conserved("user-a")

    async def test_exact_reservation_retry_after_later_hold_does_not_restore_old_amount(self):
        await self.opened("market-a")
        revision, operation = await self.reserve("user-a","market-a",400)
        await self.reserve("user-a","market-a",200)
        before = await self.points.summary("user-a")
        await self.db.batch(reservation_sql("user-a","market-a",400,"YES",revision,operation,self.now+1))
        self.assertEqual(await self.points.summary("user-a"), before)
        with self.assertRaisesRegex(sqlite3.IntegrityError,"points_operation_conflict"):
            await self.db.batch(reservation_sql("user-a","market-a",401,"YES",revision,operation,self.now))

    async def test_reserved_outcome_must_match_actual_accepted_forecast(self):
        await self.opened("market-a")
        revision, operation = await self.reserve("user-a","market-a",0,outcome="YES")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"points_position_conflict"):
            await self.db.batch(reservation_sql("user-a","market-a",100,"NO",revision,"different-op",self.now))
        self.assertEqual((await self.points.summary("user-a"))["available"],1000)

    async def test_outer_failure_after_reservation_reverts_ledger_account_and_position(self):
        await self.opened("market-a")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.reserve("user-a","market-a",100,extra=(("INSERT INTO mutation_guards VALUES('fail',0)",()),))
        self.assertEqual((await self.points.summary("user-a"))["available"],1000)
        self.assertEqual((await self.points.position("user-a","market-a"))["amount"],0)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_write_guards"))["n"],0)

    async def test_correct_wrong_and_invalid_settlement_follow_locked_policy(self):
        for index, outcome, expected in ((0,Outcome.YES,1200),(1,Outcome.NO,800),(2,Outcome.INVALID,1000)):
            uid = ("user-a","user-b","user-c")[index]
            fid = "settle-"+str(index)
            await self.opened(fid)
            await self.reserve(uid,fid,200)
            await self.finalize(fid,outcome)
            await self.db.batch(settlement_sql(fid,self.now))
            summary = await self.points.summary(uid)
            self.assertEqual((summary["available"],summary["committed"]),(expected,0))
            position = await self.points.position(uid,fid)
            self.assertEqual(position["status"],"settled")
            self.assertEqual(position["returned"],400 if outcome==Outcome.YES else 0 if outcome==Outcome.NO else 200)
            self.assertEqual(position["amount"],200)
            await self.assert_conserved(uid)

    async def test_zero_practice_has_no_settlement_credit_and_legacy_positions_are_practice(self):
        await self.opened("practice")
        self.assertEqual((await self.points.position("user-a","practice"))["status"],"practice")
        await self.reserve("user-a","practice",0)
        await self.finalize("practice")
        await self.db.batch(settlement_sql("practice",self.now))
        self.assertEqual((await self.points.summary("user-a"))["available"],1000)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_ledger WHERE kind='settlement'"))["n"],0)

    async def test_pending_disputed_or_paused_forecast_never_pays_points(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        for state in ("OPEN","PROPOSED","CHALLENGE","DISPUTED","ESCALATED","PAUSED"):
            await self.db.execute("UPDATE forecasts SET state=? WHERE id='market-a'",(state,))
            await self.db.batch(settlement_sql("market-a",self.now))
            self.assertEqual((await self.points.summary("user-a"))["committed"],200)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_ledger WHERE kind='settlement'"))["n"],0)

    async def test_final_state_without_immutable_finalization_event_cannot_pay(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        await self.db.execute("UPDATE forecasts SET state='FINALIZED',finalized_outcome='YES' WHERE id='market-a'")
        await self.db.batch(settlement_sql("market-a",self.now))
        self.assertEqual((await self.points.summary("user-a"))["committed"],200)

    async def test_settlement_timestamp_cannot_precede_finalization(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        await self.finalize("market-a")
        await self.db.batch(settlement_sql("market-a",self.now-1))
        self.assertEqual((await self.points.summary("user-a"))["committed"],200)
        await self.db.batch(settlement_sql("market-a",self.now))
        self.assertEqual((await self.points.summary("user-a"))["available"],1200)

    async def test_stake_retry_after_settlement_and_archived_replay_never_credits_twice(self):
        await self.opened("market-a")
        revision, operation = await self.reserve("user-a","market-a",200)
        await self.finalize("market-a")
        await self.db.batch(settlement_sql("market-a",self.now))
        before = await self.points.summary("user-a")
        await self.db.batch(reservation_sql("user-a","market-a",200,"YES",revision,operation,self.now))
        await self.db.execute("UPDATE forecasts SET state='ARCHIVED' WHERE id='market-a'")
        await self.db.batch(settlement_sql("market-a",self.now+1))
        self.assertEqual(await self.points.summary("user-a"),before)

    async def test_settlement_balance_overflow_fails_without_consuming_committed_position(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        await self.finalize("market-a")
        # Inject the safe-integer boundary to verify the SQL adapter's overflow guard.
        await self.db.execute("UPDATE point_accounts SET available=? WHERE user_id='user-a'",(MAX_BALANCE-200,))
        with self.assertRaisesRegex(sqlite3.IntegrityError,"points_balance_overflow"):
            await self.db.batch(settlement_sql("market-a",self.now))
        self.assertEqual((await self.points.position("user-a","market-a"))["status"],"committed")
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_ledger WHERE kind='settlement'"))["n"],0)

    async def test_two_settlement_workers_and_replay_credit_each_position_exactly_once(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        await self.reserve("user-b","market-a",300,outcome="NO")
        await self.finalize("market-a")
        stale_a, stale_b = settlement_sql("market-a",self.now), settlement_sql("market-a",self.now)
        await asyncio.gather(self.db.batch(stale_a),self.db.batch(stale_b))
        await self.db.batch(settlement_sql("market-a",self.now+1))
        self.assertEqual((await self.points.summary("user-a"))["available"],1200)
        self.assertEqual((await self.points.summary("user-b"))["available"],700)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_ledger WHERE kind='settlement'"))["n"],2)
        await self.assert_conserved("user-a")
        await self.assert_conserved("user-b")

    async def test_settlement_outer_failure_rolls_back_credit_position_and_ledger(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        await self.finalize("market-a")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.batch((*settlement_sql("market-a",self.now),("INSERT INTO mutation_guards VALUES('fail',0)",())))
        self.assertEqual((await self.points.summary("user-a"))["available"],800)
        self.assertEqual((await self.points.position("user-a","market-a"))["status"],"committed")
        await self.db.batch(settlement_sql("market-a",self.now))
        await self.assert_conserved("user-a")

    async def test_ledger_policy_and_awards_are_immutable_and_integer_balance_is_bounded(self):
        for sql in ("UPDATE point_ledger SET available_delta=9999", "DELETE FROM point_ledger",
                    "UPDATE point_policies SET win_return_multiplier=3", "UPDATE point_awards SET amount=9999"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(sql)
        for value in (-1,1.5,MAX_BALANCE+1):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute("UPDATE point_accounts SET available=? WHERE user_id='user-a'",(value,))

    async def test_positions_bulk_is_scoped_uses_two_parameters_and_hides_other_users(self):
        await self.opened("market-a")
        await self.reserve("user-a","market-a",200)
        identifiers = ["market-a",*(f"missing-{n}" for n in range(99))]
        original_all = self.db.all
        seen = []
        async def inspect(sql, params=()):
            seen.append(len(params))
            return await original_all(sql,params)
        self.db.all = inspect
        positions = await self.points.positions("user-a",identifiers)
        self.assertEqual(seen,[2])
        self.assertEqual(positions["market-a"]["amount"],200)
        self.assertEqual((await self.points.position("user-b","market-a"))["amount"],0)
        with self.assertRaises(AppError):
            await self.points.positions("user-a",identifiers+["one-too-many"])


class PointsBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_profile_and_current_wallet_backfill_once_without_retroactive_stakes(self):
        connection = sqlite3.connect(":memory:")
        try:
            migrations = ROOT / "apps/web/migrations"
            for migration in sorted(migrations.glob("*.sql")):
                if migration.name < "0004":
                    connection.executescript(migration.read_text())
            connection.execute("INSERT INTO users VALUES('legacy','Legacy','legacy','legacy-hash',10)")
            connection.execute("INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at,used_at) "
                               "VALUES('legacy-challenge','legacy','legacy-wallet','https://forecast.example','link_forecast_profile',"
                               "'solana:devnet','verified legacy fixture',10,100,20)")
            connection.execute("INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) "
                               "VALUES('legacy','legacy-wallet','solana:devnet',20,'legacy-challenge',1)")
            connection.commit()
            connection.executescript((migrations / "0004_participation_points.sql").read_text())
            db = SQLiteDatabase(connection)
            points = PointsService(db)
            result = await points.summary("legacy")
            self.assertEqual((result["available"],result["committed"]),(1500,0))
            self.assertTrue(result["onboarding"]["wallet"]["completed"])
            self.assertEqual((await points.position("legacy","old-forecast"))["amount"],0)
            # Repeating idempotent backfill statements cannot mint a second award.
            await db.execute("INSERT OR IGNORE INTO point_awards SELECT * FROM point_awards")
            self.assertEqual((await points.summary("legacy"))["available"],1500)
            self.assertEqual((await db.first("SELECT COUNT(*) AS n FROM point_ledger"))["n"],2)
        finally:
            connection.close()


class PointsWranglerMigrationTests(unittest.TestCase):
    def test_remote_trigger_case_expressions_are_parenthesized(self):
        # D1's remote parser treats an unparenthesized CASE END as a trigger
        # terminator (workers-sdk issue 4727, reproduced on the live QA database).
        raw = (ROOT / "apps/web/migrations/0004_participation_points.sql").read_text()
        statements, current = [], ""
        for character in raw:
            current += character
            if character == ";" and sqlite3.complete_statement(current):
                statements.append(current)
                current = ""
        for statement in statements:
            unquoted = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/", " ", statement, flags=re.S)
            if not re.search(r"\bCREATE\s+TRIGGER\b", unquoted, re.I):
                continue
            depth = 0
            for token in re.findall(r"\bCASE\b|[()]", unquoted, re.I):
                if token == "(":
                    depth += 1
                elif token == ")":
                    depth -= 1
                else:
                    self.assertGreater(depth, 0, "Remote D1 cannot parse an unparenthesized CASE inside a trigger")

    def test_pinned_wrangler_splitter_preserves_complete_executable_triggers(self):
        node = shutil.which("node")
        packages = (ROOT / "tmp/cloudflare-tools/node_modules/wrangler", ROOT / "apps/web/node_modules/wrangler")
        wrangler = next((path for path in packages if (path / "package.json").is_file()), None)
        if node is None or wrangler is None:
            self.skipTest("Install the pinned Wrangler development dependency to verify its migration parser")
        script = "const fs=require('node:fs');const w=require(process.argv[1]);" \
                 "process.stdout.write(JSON.stringify(w.unstable_splitSqlQuery(fs.readFileSync(process.argv[2],'utf8'))));"
        output = subprocess.run([node, "-e", script, str(wrangler),
                                 str(ROOT / "apps/web/migrations/0004_participation_points.sql")],
                                check=True, capture_output=True, text=True,
                                env={**os.environ, "TMPDIR": str(ROOT / "tmp"), "WRANGLER_SEND_METRICS": "false"})
        statements = json.loads(output.stdout)
        connection = sqlite3.connect(":memory:")
        try:
            for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
                if migration.name < "0004":
                    connection.executescript(migration.read_text())
            for index, statement in enumerate(statements):
                self.assertTrue(sqlite3.complete_statement(statement.rstrip(";")+";"),
                                f"Wrangler split an incomplete SQL statement at index {index}: {statement[:90]}")
                connection.execute(statement)
            connection.execute("INSERT INTO users VALUES('wrangler-user','Name','wrangler-user','wrangler-hash',1)")
            self.assertEqual(connection.execute("SELECT available,committed FROM point_accounts WHERE user_id='wrangler-user'").fetchone(),
                             (1000,0))
        finally:
            connection.close()
