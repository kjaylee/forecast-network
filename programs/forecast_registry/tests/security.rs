use forecast_registry::{
    process_instruction, Advance, Config, Error, Forecast, CONFIG_LEN, FORECAST_LEN,
    MIN_CHALLENGE_MS,
};
use solana_program::{account_info::AccountInfo, program_error::ProgramError, pubkey::Pubkey};
fn open() -> Forecast {
    Forecast {
        id: [1; 32],
        creator: [2; 32],
        specification: [3; 32],
        open_at: 1000,
        close_at: 2000,
        revision: 3,
        occurred_at: 1000,
        state: 2,
        outcome: 0,
        paused_from: 0,
        event: [4; 32],
        snapshot: [5; 32],
        resolution: [0; 32],
        disputes: [0; 32],
        reputation: [0; 32],
        trigger: [0; 32],
        challenge_until: 0,
        chain_not_before: 0,
        paused_at: 0,
        pending: 0,
        material: 0,
    }
}
fn next(f: &Forecast, state: u8, time: i64) -> Advance {
    let mut event = f.event;
    event[0] += 1;
    Advance {
        revision: f.revision + 1,
        occurred_at: time,
        previous: f.event,
        event,
        snapshot: [8; 32],
        state,
        outcome: f.outcome,
        resolution: f.resolution,
        disputes: f.disputes,
        reputation: f.reputation,
        trigger: f.trigger,
        challenge_until: f.challenge_until,
        pending: f.pending,
        material: f.material,
    }
}
fn challenge() -> Forecast {
    let f = open();
    let f = f.advance(&next(&f, 3, 2000), 2000).unwrap();
    let f = f.advance(&next(&f, 4, 2001), 2001).unwrap();
    let mut a = next(&f, 5, 2002);
    a.outcome = 1;
    a.resolution = [9; 32];
    let f = f.advance(&a, 2002).unwrap();
    let mut a = next(&f, 6, 2003);
    a.challenge_until = 3000;
    f.advance(&a, 2003).unwrap()
}
fn rejects(f: &Forecast, a: &Advance, now: i64, error: Error) {
    let before = f.encode();
    assert_eq!(f.advance(a, now), Err(ProgramError::Custom(error as u32)));
    assert_eq!(before, f.encode());
}
#[test]
fn wire_roundtrip_and_exact_size() {
    let f = open();
    assert_eq!(f.encode().len(), FORECAST_LEN);
    assert_eq!(Forecast::decode(&f.encode()).unwrap(), f);
    let f = challenge();
    assert_eq!(Forecast::decode(&f.encode()).unwrap(), f);
    let c = Config {
        admin: Pubkey::new_unique(),
        relayer: Pubkey::new_unique(),
        pending_admin: Pubkey::default(),
    };
    assert_eq!(c.encode().len(), CONFIG_LEN);
    assert_eq!(Config::decode(&c.encode()).unwrap(), c);
}
#[test]
fn truncated_and_trailing_snapshots_rejected() {
    let bytes = open().encode();
    for len in 0..bytes.len() {
        assert!(Forecast::decode(&bytes[..len]).is_err());
    }
    let mut extra = bytes;
    extra.push(0);
    assert!(Forecast::decode(&extra).is_err());
    for len in 0..254 {
        assert!(Advance::decode(&vec![0; len]).is_err());
    }
    assert!(Advance::decode(&[0; 255]).is_err());
}
#[test]
fn no_skipped_revision_or_forked_history() {
    let f = open();
    let mut a = next(&f, 2, 1001);
    a.revision += 1;
    rejects(&f, &a, 1001, Error::Revision);
    a.revision -= 1;
    a.previous = [99; 32];
    rejects(&f, &a, 1001, Error::Event);
    a.previous = f.event;
    a.event = f.event;
    rejects(&f, &a, 1001, Error::Event);
}
#[test]
fn all_illegal_state_edges_rejected() {
    let f = open();
    for state in [0, 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 255] {
        rejects(&f, &next(&f, state, 2000), 2000, Error::Transition);
    }
    let f = challenge();
    for state in [0, 1, 2, 3, 4, 5, 6, 8, 11, 12] {
        rejects(&f, &next(&f, state, 3000), 3000, Error::Transition);
    }
}
#[test]
fn no_clock_forgery_and_no_early_lock_without_trigger() {
    let f = open();
    rejects(&f, &next(&f, 2, 999), 2000, Error::Time);
    rejects(&f, &next(&f, 2, 1001), 1000, Error::Time);
    rejects(&f, &next(&f, 3, 1500), 1500, Error::Time);
    rejects(&f, &next(&f, 2, 2000), 2000, Error::Time);
}
#[test]
fn early_trigger_is_immutable_and_positive_only() {
    let f = open();
    let mut a = next(&f, 3, 1500);
    a.trigger = [10; 32];
    let f = f.advance(&a, 1500).unwrap();
    let mut a = next(&f, 4, 1501);
    a.trigger = [11; 32];
    rejects(&f, &a, 1501, Error::Immutable);
    a.trigger = f.trigger;
    let f = f.advance(&a, 1501).unwrap();
    let mut a = next(&f, 5, 1502);
    a.resolution = [9; 32];
    a.outcome = 2;
    rejects(&f, &a, 1502, Error::Transition);
    a.outcome = 1;
    assert!(f.advance(&a, 1502).is_ok());
}
#[test]
fn finalization_uses_chain_time_not_old_application_deadline() {
    let f = challenge();
    assert_eq!(f.chain_not_before, 2003 + MIN_CHALLENGE_MS);
    rejects(&f, &next(&f, 10, 3000), 3000, Error::NotReady);
    let mut a = next(&f, 10, f.chain_not_before);
    a.reputation = [12; 32];
    let final_f = f.advance(&a, f.chain_not_before).unwrap();
    let archived = final_f
        .advance(&next(&final_f, 11, f.chain_not_before), f.chain_not_before)
        .unwrap();
    assert_eq!(archived.outcome, 1);
    rejects(
        &archived,
        &next(&archived, 11, f.chain_not_before),
        f.chain_not_before,
        Error::Transition,
    );
}
#[test]
fn final_outcome_resolution_and_dispute_hash_cannot_change() {
    let f = challenge();
    for field in 0..3 {
        let mut a = next(&f, 10, f.chain_not_before);
        if field == 0 {
            a.outcome = 2;
        } else if field == 1 {
            a.resolution = [20; 32];
        } else {
            a.disputes = [20; 32];
        }
        rejects(
            &f,
            &a,
            f.chain_not_before,
            if field == 2 {
                Error::Dispute
            } else {
                Error::Immutable
            },
        );
    }
}
#[test]
fn unresolved_disputes_and_material_conflict_block_finalization() {
    let f = challenge();
    let mut a = next(&f, 7, 2500);
    a.pending = 1;
    a.disputes = [10; 32];
    let f = f.advance(&a, 2500).unwrap();
    rejects(
        &f,
        &next(&f, 10, f.chain_not_before),
        f.chain_not_before,
        Error::Transition,
    );
    rejects(&f, &next(&f, 6, 2600), 2600, Error::Dispute);
    let mut a = next(&f, 7, 2600);
    a.pending = 0;
    a.material = 1;
    let f = f.advance(&a, 2600).unwrap();
    rejects(&f, &next(&f, 6, 2700), 2700, Error::Dispute);
    let f = f.advance(&next(&f, 8, 2700), 2700).unwrap();
    rejects(
        &f,
        &next(&f, 10, f.chain_not_before),
        f.chain_not_before,
        Error::Transition,
    );
}
#[test]
fn adjudication_requires_new_proposal_and_fresh_window() {
    let f = challenge();
    let mut a = next(&f, 7, 2500);
    a.material = 1;
    a.disputes = [10; 32];
    let f = f.advance(&a, 2500).unwrap();
    let f = f.advance(&next(&f, 8, 2600), 2600).unwrap();
    let mut a = next(&f, 5, 2700);
    a.resolution = [11; 32];
    a.outcome = 3;
    a.pending = 0;
    a.material = 0;
    a.challenge_until = 0;
    a.disputes = [0; 32];
    let f = f.advance(&a, 2700).unwrap();
    assert_eq!(f.chain_not_before, 2700 + MIN_CHALLENGE_MS);
    let mut a = next(&f, 6, 2800);
    a.challenge_until = 5000;
    let f = f.advance(&a, 2800).unwrap();
    assert_eq!(f.chain_not_before, 2800 + MIN_CHALLENGE_MS);
}
#[test]
fn pause_can_only_restore_previous_state_and_extends_chain_window() {
    let f = challenge();
    let before = f.chain_not_before;
    let paused = f.advance(&next(&f, 9, 2500), 2500).unwrap();
    rejects(
        &paused,
        &next(&paused, 10, before),
        before,
        Error::Transition,
    );
    let mut a = next(&paused, 6, 3500);
    a.challenge_until += 1000;
    let resumed = paused.advance(&a, 3500).unwrap();
    assert_eq!(resumed.chain_not_before, before + 1000);
}
#[test]
fn snapshot_semantic_guards_reject_corruption() {
    for field in 0..10 {
        let mut f = challenge();
        match field {
            0 => f.specification = [0; 32],
            1 => f.outcome = 0,
            2 => f.resolution = [0; 32],
            3 => f.pending = 1,
            4 => f.material = 1,
            5 => f.challenge_until = 0,
            6 => f.chain_not_before = 0,
            7 => f.paused_from = 6,
            8 => f.revision = 0,
            _ => f.close_at = f.open_at,
        };
        assert!(Forecast::decode(&f.encode()).is_err());
    }
}
#[test]
fn historical_registration_requires_published_open_snapshot() {
    let f = open();
    let mut bytes = f.encode()[8..136].to_vec();
    bytes.extend(f.event);
    bytes.extend(f.snapshot);
    assert_eq!(bytes.len(), 192);
    assert_eq!(Forecast::register(&bytes, 5000).unwrap(), f);
    let mut invalid = bytes.clone();
    invalid[120..128].copy_from_slice(&2000_i64.to_le_bytes());
    assert!(Forecast::register(&invalid, 5000).is_err());
    let mut extra = bytes;
    extra.push(0);
    assert!(Forecast::register(&extra, 5000).is_err());
}
#[test]
fn processor_rejects_missing_accounts_unknown_tags_and_non_signer() {
    let program = Pubkey::new_unique();
    for tag in 0..=6 {
        assert!(process_instruction(&program, &[], &[tag]).is_err());
    }
    let key = Pubkey::new_unique();
    let owner = Pubkey::new_unique();
    let mut lamports = 1;
    let mut data = [];
    let a = AccountInfo::new(
        &key,
        false,
        true,
        &mut lamports,
        &mut data,
        &owner,
        false,
        0,
    );
    assert_eq!(
        process_instruction(
            &program,
            &[a.clone(), a.clone(), a],
            &[vec![0], vec![0; 32]].concat()
        ),
        Err(Error::Unauthorized.into())
    );
}
#[test]
fn processor_rejects_foreign_config_owner_and_address() {
    let program = Pubkey::new_unique();
    let relay = Pubkey::new_unique();
    let (conf, _) = Pubkey::find_program_address(&[b"config"], &program);
    let owner = Pubkey::new_unique();
    let mut l1 = 1;
    let mut l2 = 1;
    let mut d1 = [];
    let mut d2 = Config {
        admin: Pubkey::new_unique(),
        relayer: relay,
        pending_admin: Pubkey::default(),
    }
    .encode();
    let a = AccountInfo::new(&relay, true, true, &mut l1, &mut d1, &owner, false, 0);
    let c = AccountInfo::new(&conf, false, false, &mut l2, &mut d2, &owner, false, 0);
    assert_eq!(
        process_instruction(&program, &[a.clone(), c, a], &[2]),
        Err(Error::Owner.into())
    );
}

fn config_instruction(
    c: &mut Config,
    caller: Pubkey,
    signed: bool,
    writable: bool,
    tag: u8,
    payload: &[u8],
) -> Result<(), ProgramError> {
    let program = Pubkey::new_unique();
    let (key, _) = Pubkey::find_program_address(&[b"config"], &program);
    let system = solana_program::system_program::id();
    let mut signer_lamports = 1;
    let mut config_lamports = 1;
    let mut signer_data = [];
    let mut data = c.encode();
    let authority = AccountInfo::new(
        &caller,
        signed,
        false,
        &mut signer_lamports,
        &mut signer_data,
        &system,
        false,
        0,
    );
    let target = AccountInfo::new(
        &key,
        false,
        writable,
        &mut config_lamports,
        &mut data,
        &program,
        false,
        0,
    );
    let result = process_instruction(&program, &[authority, target], &[&[tag], payload].concat());
    *c = Config::decode(&data).unwrap();
    result
}
#[test]
fn hot_relayer_cannot_rotate_or_accept_admin() {
    let mut c = Config {
        admin: Pubkey::new_unique(),
        relayer: Pubkey::new_unique(),
        pending_admin: Pubkey::default(),
    };
    let original = c.clone();
    let relay = c.relayer;
    for tag in [3, 4] {
        assert_eq!(
            config_instruction(
                &mut c,
                relay,
                true,
                true,
                tag,
                &Pubkey::new_unique().to_bytes()
            ),
            Err(Error::Unauthorized.into())
        );
        assert_eq!(c, original);
    }
    assert_eq!(
        config_instruction(&mut c, relay, true, true, 5, &[]),
        Err(Error::Unauthorized.into())
    );
}
#[test]
fn admin_transfer_is_two_step_and_blocks_relayer_aliasing() {
    let mut c = Config {
        admin: Pubkey::new_unique(),
        relayer: Pubkey::new_unique(),
        pending_admin: Pubkey::default(),
    };
    let old = c.admin;
    let new = Pubkey::new_unique();
    let relay = c.relayer;
    assert_eq!(
        config_instruction(&mut c, old, true, true, 4, &relay.to_bytes()),
        Err(Error::Invalid.into())
    );
    config_instruction(&mut c, old, true, true, 4, &new.to_bytes()).unwrap();
    assert_eq!(c.admin, old);
    assert_eq!(
        config_instruction(&mut c, old, true, true, 3, &new.to_bytes()),
        Err(Error::Invalid.into())
    );
    assert_eq!(
        config_instruction(&mut c, old, true, true, 5, &[]),
        Err(Error::Unauthorized.into())
    );
    assert_eq!(
        config_instruction(&mut c, new, false, true, 5, &[]),
        Err(Error::Unauthorized.into())
    );
    config_instruction(&mut c, new, true, true, 5, &[]).unwrap();
    assert_eq!(c.admin, new);
    assert_eq!(c.pending_admin, Pubkey::default());
    assert_eq!(
        config_instruction(&mut c, old, true, true, 3, &Pubkey::new_unique().to_bytes()),
        Err(Error::Unauthorized.into())
    );
    let next = Pubkey::new_unique();
    config_instruction(&mut c, new, true, true, 3, &next.to_bytes()).unwrap();
    assert_eq!(c.relayer, next);
}
#[test]
fn admin_writes_require_exact_payload_and_writable_account() {
    let mut c = Config {
        admin: Pubkey::new_unique(),
        relayer: Pubkey::new_unique(),
        pending_admin: Pubkey::default(),
    };
    let admin = c.admin;
    assert_eq!(
        config_instruction(
            &mut c,
            admin,
            true,
            false,
            3,
            &Pubkey::new_unique().to_bytes()
        ),
        Err(Error::Unauthorized.into())
    );
    for len in [0, 1, 31, 33, 64] {
        assert_eq!(
            config_instruction(&mut c, admin, true, true, 3, &vec![1; len]),
            Err(Error::Invalid.into())
        );
    }
    assert_eq!(
        config_instruction(&mut c, admin, true, true, 3, &[0; 32]),
        Err(Error::Invalid.into())
    );
}
#[test]
fn aggregate_dispute_limit_and_terminal_snapshot_deadline() {
    let mut f = challenge();
    f.state = 7;
    f.disputes = [14; 32];
    f.pending = 200;
    f.material = 100;
    assert!(f.validate().is_err());
    let mut f = challenge();
    f.state = 10;
    f.occurred_at = f.challenge_until - 1;
    assert!(f.validate().is_err());
    let mut f = challenge();
    f.chain_not_before = f.challenge_until - 1;
    assert!(f.validate().is_err());
}

#[test]
fn native_processor_commits_full_lifecycle_and_rejects_replays_atomically() {
    use solana_program::{
        clock::Clock,
        program_stubs::{set_syscall_stubs, SyscallStubs},
    };
    use std::sync::{
        atomic::{AtomicI64, Ordering},
        Arc,
    };
    struct ChainClock(Arc<AtomicI64>);
    impl SyscallStubs for ChainClock {
        fn sol_get_clock_sysvar(&self, target: *mut u8) -> u64 {
            let clock = Clock {
                unix_timestamp: self.0.load(Ordering::SeqCst),
                ..Clock::default()
            };
            // SAFETY: Solana's host Sysvar::get shim supplies a live, aligned
            // Clock destination. This test substitutes only that trusted syscall;
            // production account decoding has no unsafe memory operations.
            unsafe {
                (target as *mut Clock).write(clock);
            }
            0
        }
    }
    struct Restore(Option<Box<dyn SyscallStubs>>);
    impl Drop for Restore {
        fn drop(&mut self) {
            set_syscall_stubs(self.0.take().unwrap());
        }
    }
    let seconds = Arc::new(AtomicI64::new(4));
    let _restore = Restore(Some(set_syscall_stubs(Box::new(ChainClock(
        seconds.clone(),
    )))));
    let program = Pubkey::new_unique();
    let relay = Pubkey::new_unique();
    let admin = Pubkey::new_unique();
    let (config_key, _) = Pubkey::find_program_address(&[b"config"], &program);
    let mut forecast = open();
    let (forecast_key, _) = Pubkey::find_program_address(&[b"forecast", &forecast.id], &program);
    let mut forecast_data = forecast.encode();
    let mut config_data = Config {
        admin,
        relayer: relay,
        pending_admin: Pubkey::default(),
    }
    .encode();
    let system = solana_program::system_program::id();
    let mut relay_lamports = 1;
    let mut config_lamports = 1;
    let mut forecast_lamports = 1;
    let mut relay_data = [];
    let mut apply = |a: &Advance| {
        let mut payload = vec![2];
        payload.extend(a.revision.to_le_bytes());
        payload.extend(a.occurred_at.to_le_bytes());
        payload.extend(a.previous);
        payload.extend(a.event);
        payload.extend(a.snapshot);
        payload.extend([a.state, a.outcome]);
        for h in [a.resolution, a.disputes, a.reputation, a.trigger] {
            payload.extend(h);
        }
        payload.extend(a.challenge_until.to_le_bytes());
        payload.extend(a.pending.to_le_bytes());
        payload.extend(a.material.to_le_bytes());
        assert_eq!(payload.len(), 255);
        let accounts = [
            AccountInfo::new(
                &relay,
                true,
                false,
                &mut relay_lamports,
                &mut relay_data,
                &system,
                false,
                0,
            ),
            AccountInfo::new(
                &config_key,
                false,
                false,
                &mut config_lamports,
                &mut config_data,
                &program,
                false,
                0,
            ),
            AccountInfo::new(
                &forecast_key,
                false,
                true,
                &mut forecast_lamports,
                &mut forecast_data,
                &program,
                false,
                0,
            ),
        ];
        let before = accounts[2].try_borrow_data().unwrap().to_vec();
        let result = process_instruction(&program, &accounts, &payload);
        if result.is_err() {
            assert_eq!(accounts[2].try_borrow_data().unwrap().to_vec(), before);
        }
        let stored = Forecast::decode(&accounts[2].try_borrow_data().unwrap()).unwrap();
        (result, stored)
    };
    for state in [3, 4, 5, 6] {
        let mut a = next(&forecast, state, 2000 + state as i64);
        if state == 5 {
            a.resolution = [9; 32];
            a.outcome = 1;
        }
        if state == 6 {
            a.challenge_until = 5000;
        }
        let (result, stored) = apply(&a);
        result.unwrap();
        forecast = stored;
        assert_eq!(apply(&a).0, Err(Error::Revision.into()));
    }
    let a = next(&forecast, 10, 6000);
    assert_eq!(apply(&a).0, Err(Error::Time.into()));
    seconds.store(6, Ordering::SeqCst);
    assert_eq!(apply(&a).0, Err(Error::NotReady.into()));
    seconds.store((forecast.chain_not_before + 999) / 1000, Ordering::SeqCst);
    let mut a = next(&forecast, 10, forecast.chain_not_before);
    a.reputation = [20; 32];
    let (result, stored) = apply(&a);
    result.unwrap();
    forecast = stored;
    let a = next(&forecast, 11, forecast.occurred_at);
    let (result, stored) = apply(&a);
    result.unwrap();
    assert_eq!(stored.state, 11);
    assert_eq!(stored.specification, open().specification);
}

#[test]
fn instruction_and_account_decoders_never_panic_on_bounded_arbitrary_bytes() {
    let mut seed = 0x3ac71_u64;
    for length in 0..=512 {
        let mut bytes = Vec::with_capacity(length);
        for _ in 0..length {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            bytes.push((seed >> 32) as u8);
        }
        let _ = Advance::decode(&bytes);
        let _ = Forecast::decode(&bytes);
        let _ = Forecast::register(&bytes, 5000);
        let _ = Config::decode(&bytes);
        assert!(process_instruction(&Pubkey::new_unique(), &[], &bytes).is_err());
    }
}
#[test]
fn revision_and_clock_overflow_fail_closed() {
    let mut f = open();
    f.revision = 9_007_199_254_740_991;
    let a = next(&f, 2, 1001);
    assert!(f.advance(&a, 1001).is_err());
    let f = open();
    let f = f.advance(&next(&f, 3, 2000), 2000).unwrap();
    let f = f.advance(&next(&f, 4, 2001), 2001).unwrap();
    let mut a = next(&f, 5, 2002);
    a.resolution = [9; 32];
    a.outcome = 1;
    assert!(f.advance(&a, 9_007_199_254_740_991).is_err());
    let mut f = challenge();
    f.state = 9;
    f.paused_from = 6;
    f.paused_at = 2500;
    let mut a = next(&f, 6, 9_007_199_254_740_991);
    a.challenge_until = 9_007_199_254_740_991;
    assert!(f.advance(&a, 9_007_199_254_740_991).is_err());
}

#[test]
fn finalized_reputation_requires_real_collection_commitment_even_when_empty() {
    let f = challenge();
    let mut a = next(&f, 10, f.chain_not_before);
    rejects(&f, &a, f.chain_not_before, Error::Commitment);
    a.reputation = [42; 32];
    let finalized = f.advance(&a, f.chain_not_before).unwrap();
    assert_eq!(finalized.reputation, a.reputation);
    let mut corrupt = finalized.clone();
    corrupt.reputation = [0; 32];
    assert!(Forecast::decode(&corrupt.encode()).is_err());
    let mut archive = next(&finalized, 11, finalized.occurred_at);
    archive.reputation = [43; 32];
    rejects(
        &finalized,
        &archive,
        finalized.occurred_at,
        Error::Immutable,
    );
}
