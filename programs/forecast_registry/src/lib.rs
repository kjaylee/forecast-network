//! Authority-attested commitments. This program verifies authorization, ordering,
//! immutable identity and lifecycle/time gates; it does not independently judge news.
#![allow(unexpected_cfgs)]
use solana_program::{
    account_info::AccountInfo,
    clock::Clock,
    entrypoint::ProgramResult,
    program::{invoke, invoke_signed},
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    system_instruction, system_program,
    sysvar::Sysvar,
};
mod initial_admin;
// Direct intake clients request a 256-KiB heap frame. The existing Cargo feature
// suppresses the SDK's fixed 32-KiB allocator; SBF intake builds enable it.
#[cfg(all(target_os = "solana", feature = "custom-heap"))]
#[global_allocator]
static INTAKE_HEAP: solana_program::entrypoint::BumpAllocator =
    solana_program::entrypoint::BumpAllocator {
        start: solana_program::entrypoint::HEAP_START_ADDRESS as usize,
        len: 262_144,
    };
#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

pub const CONFIG_LEN: usize = 104;
pub const FORECAST_LEN: usize = 360;
pub const MIN_CHALLENGE_MS: i64 = 172_800_000;
const MAX_TIME: i64 = 9_007_199_254_740_991;
const ZERO: [u8; 32] = [0; 32];
const CONFIG_MAGIC: &[u8; 8] = b"FNCONF01";
const FORECAST_MAGIC: &[u8; 8] = b"FNFORE01";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum Error {
    Invalid = 1,
    Unauthorized,
    Address,
    Owner,
    Revision,
    Event,
    Transition,
    Time,
    Commitment,
    Dispute,
    NotReady,
    Immutable,
    LegacyAdvanceDisabled,
}
impl From<Error> for ProgramError {
    fn from(e: Error) -> Self {
        Self::Custom(e as u32)
    }
}
fn require(ok: bool, error: Error) -> ProgramResult {
    if ok {
        Ok(())
    } else {
        Err(error.into())
    }
}

struct Reader<'a> {
    bytes: &'a [u8],
    cursor: usize,
}
impl<'a> Reader<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, cursor: 0 }
    }
    fn take<const N: usize>(&mut self) -> Result<[u8; N], ProgramError> {
        let end = self.cursor.checked_add(N).ok_or(Error::Invalid)?;
        let result = self
            .bytes
            .get(self.cursor..end)
            .ok_or(Error::Invalid)?
            .try_into()
            .map_err(|_| Error::Invalid)?;
        self.cursor = end;
        Ok(result)
    }
    fn byte(&mut self) -> Result<u8, ProgramError> {
        Ok(self.take::<1>()?[0])
    }
    fn u16(&mut self) -> Result<u16, ProgramError> {
        Ok(u16::from_le_bytes(self.take()?))
    }
    fn u64(&mut self) -> Result<u64, ProgramError> {
        Ok(u64::from_le_bytes(self.take()?))
    }
    fn time(&mut self) -> Result<i64, ProgramError> {
        Ok(i64::from_le_bytes(self.take()?))
    }
    fn end(&self) -> ProgramResult {
        require(self.cursor == self.bytes.len(), Error::Invalid)
    }
}
fn valid_time(t: i64) -> bool {
    (0..=MAX_TIME).contains(&t)
}
fn now_ms() -> Result<i64, ProgramError> {
    let time = Clock::get()?
        .unix_timestamp
        .checked_mul(1000)
        .ok_or(Error::Time)?;
    require(valid_time(time), Error::Time)?;
    Ok(time)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub admin: Pubkey,
    pub relayer: Pubkey,
    pub pending_admin: Pubkey,
}
impl Config {
    pub fn decode(bytes: &[u8]) -> Result<Self, ProgramError> {
        let mut r = Reader::new(bytes);
        require(&r.take::<8>()? == CONFIG_MAGIC, Error::Invalid)?;
        let value = Self {
            admin: Pubkey::new_from_array(r.take()?),
            relayer: Pubkey::new_from_array(r.take()?),
            pending_admin: Pubkey::new_from_array(r.take()?),
        };
        r.end()?;
        require(
            value.admin != Pubkey::default()
                && value.relayer != Pubkey::default()
                && value.admin != value.relayer
                && (value.pending_admin == Pubkey::default()
                    || (value.pending_admin != value.admin
                        && value.pending_admin != value.relayer)),
            Error::Invalid,
        )?;
        Ok(value)
    }
    pub fn encode(&self) -> Vec<u8> {
        let mut b = CONFIG_MAGIC.to_vec();
        b.extend(self.admin.to_bytes());
        b.extend(self.relayer.to_bytes());
        b.extend(self.pending_admin.to_bytes());
        b
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Forecast {
    pub id: [u8; 32],
    pub creator: [u8; 32],
    pub specification: [u8; 32],
    pub open_at: i64,
    pub close_at: i64,
    pub revision: u64,
    pub occurred_at: i64,
    pub state: u8,
    pub outcome: u8,
    pub paused_from: u8,
    pub event: [u8; 32],
    pub snapshot: [u8; 32],
    pub resolution: [u8; 32],
    pub disputes: [u8; 32],
    pub reputation: [u8; 32],
    pub trigger: [u8; 32],
    pub challenge_until: i64,
    pub chain_not_before: i64,
    pub paused_at: i64,
    pub pending: u16,
    pub material: u16,
}
impl Forecast {
    pub fn decode(bytes: &[u8]) -> Result<Self, ProgramError> {
        let mut r = Reader::new(bytes);
        require(&r.take::<8>()? == FORECAST_MAGIC, Error::Invalid)?;
        let mut f = Self::register_read(&mut r)?;
        f.state = r.byte()?;
        f.outcome = r.byte()?;
        f.paused_from = r.byte()?;
        require(r.byte()? == 0, Error::Invalid)?;
        f.event = r.take()?;
        f.snapshot = r.take()?;
        f.resolution = r.take()?;
        f.disputes = r.take()?;
        f.reputation = r.take()?;
        f.trigger = r.take()?;
        f.challenge_until = r.time()?;
        f.chain_not_before = r.time()?;
        f.paused_at = r.time()?;
        f.pending = r.u16()?;
        f.material = r.u16()?;
        r.end()?;
        f.validate()?;
        Ok(f)
    }
    fn register_read(r: &mut Reader) -> Result<Self, ProgramError> {
        Ok(Self {
            id: r.take()?,
            creator: r.take()?,
            specification: r.take()?,
            open_at: r.time()?,
            close_at: r.time()?,
            revision: r.u64()?,
            occurred_at: r.time()?,
            state: 2,
            outcome: 0,
            paused_from: 0,
            event: ZERO,
            snapshot: ZERO,
            resolution: ZERO,
            disputes: ZERO,
            reputation: ZERO,
            trigger: ZERO,
            challenge_until: 0,
            chain_not_before: 0,
            paused_at: 0,
            pending: 0,
            material: 0,
        })
    }
    pub fn register(bytes: &[u8], now: i64) -> Result<Self, ProgramError> {
        let mut r = Reader::new(bytes);
        let mut f = Self::register_read(&mut r)?;
        f.event = r.take()?;
        f.snapshot = r.take()?;
        r.end()?;
        f.validate()?;
        require(
            f.occurred_at < f.close_at && f.occurred_at <= now,
            Error::Time,
        )?;
        Ok(f)
    }
    pub fn encode(&self) -> Vec<u8> {
        let mut b = FORECAST_MAGIC.to_vec();
        for h in [&self.id, &self.creator, &self.specification] {
            b.extend(h);
        }
        b.extend(self.open_at.to_le_bytes());
        b.extend(self.close_at.to_le_bytes());
        b.extend(self.revision.to_le_bytes());
        b.extend(self.occurred_at.to_le_bytes());
        b.extend([self.state, self.outcome, self.paused_from, 0]);
        for h in [
            &self.event,
            &self.snapshot,
            &self.resolution,
            &self.disputes,
            &self.reputation,
            &self.trigger,
        ] {
            b.extend(h);
        }
        b.extend(self.challenge_until.to_le_bytes());
        b.extend(self.chain_not_before.to_le_bytes());
        b.extend(self.paused_at.to_le_bytes());
        b.extend(self.pending.to_le_bytes());
        b.extend(self.material.to_le_bytes());
        b
    }
    pub fn validate(&self) -> ProgramResult {
        require(
            self.id != ZERO
                && self.creator != ZERO
                && self.specification != ZERO
                && self.event != ZERO
                && self.snapshot != ZERO,
            Error::Commitment,
        )?;
        require(
            valid_time(self.open_at)
                && valid_time(self.close_at)
                && valid_time(self.occurred_at)
                && self.open_at < self.close_at,
            Error::Time,
        )?;
        require(
            self.revision > 0 && self.revision <= MAX_TIME as u64 && (2..=11).contains(&self.state),
            Error::Invalid,
        )?;
        let effective = if self.state == 9 {
            self.paused_from
        } else {
            self.state
        };
        require(
            (self.state == 9 && (4..=8).contains(&self.paused_from) && self.paused_at > 0)
                || (self.state != 9 && self.paused_from == 0 && self.paused_at == 0),
            Error::Transition,
        )?;
        let resolved = effective >= 5;
        require(
            if resolved {
                self.resolution != ZERO
                    && (1..=3).contains(&self.outcome)
                    && self.chain_not_before > 0
            } else {
                self.resolution == ZERO && self.outcome == 0 && self.chain_not_before == 0
            },
            Error::Commitment,
        )?;
        let challenged = effective >= 6;
        require(
            if challenged {
                self.challenge_until > 0
            } else {
                self.challenge_until == 0 && self.pending == 0 && self.material == 0
            },
            Error::Time,
        )?;
        require(
            valid_time(self.challenge_until)
                && valid_time(self.chain_not_before)
                && valid_time(self.paused_at),
            Error::Time,
        )?;
        require(
            u32::from(self.pending) + u32::from(self.material) <= 256,
            Error::Dispute,
        )?;
        require(
            self.chain_not_before >= self.challenge_until
                && (![10, 11].contains(&effective) || self.occurred_at >= self.challenge_until),
            Error::Time,
        )?;
        require(
            ![6, 10, 11].contains(&effective) || (self.pending == 0 && self.material == 0),
            Error::Dispute,
        )?;
        require(
            effective != 8 || (self.pending == 0 && self.material > 0),
            Error::Dispute,
        )?;
        require(
            (self.pending == 0 && self.material == 0) || self.disputes != ZERO,
            Error::Commitment,
        )?;
        require(
            if effective >= 10 {
                self.reputation != ZERO
            } else {
                self.reputation == ZERO
            },
            Error::Commitment,
        )?;
        require(
            self.state != 2 || (self.trigger == ZERO && self.occurred_at < self.close_at),
            Error::Time,
        )?;
        require(
            self.state == 2 || self.occurred_at >= self.close_at || self.trigger != ZERO,
            Error::Time,
        )?;
        require(
            self.occurred_at >= self.close_at || !resolved || self.outcome == 1,
            Error::Transition,
        )
    }
    pub fn advance(&self, a: &Advance, now: i64) -> Result<Self, ProgramError> {
        self.advance_intake(a, now, false)
    }
    fn advance_intake(&self, a: &Advance, now: i64, imported: bool) -> Result<Self, ProgramError> {
        self.validate()?;
        require(valid_time(now), Error::Time)?;
        require(
            a.revision == self.revision.checked_add(1).ok_or(Error::Revision)?,
            Error::Revision,
        )?;
        require(
            a.previous == self.event
                && a.event != self.event
                && a.event != ZERO
                && a.snapshot != ZERO,
            Error::Event,
        )?;
        require(
            a.occurred_at >= self.occurred_at && a.occurred_at <= now,
            Error::Time,
        )?;
        let legal = match self.state {
            2 => matches!(a.state, 2 | 3),
            3 => a.state == 4,
            4 => matches!(a.state, 5 | 9),
            5 => matches!(a.state, 6 | 9),
            6 => matches!(a.state, 7 | 9 | 10),
            7 => matches!(a.state, 6..=9),
            8 => matches!(a.state, 5 | 9) || (imported && a.state == 7),
            9 => a.state == self.paused_from,
            10 => a.state == 11,
            _ => false,
        };
        require(legal, Error::Transition)?;
        require(
            a.trigger == self.trigger
                || (self.state == 2
                    && a.state == 3
                    && self.trigger == ZERO
                    && a.trigger != ZERO
                    && a.occurred_at < self.close_at),
            Error::Immutable,
        )?;
        let new_proposal = (self.state == 4 || self.state == 8) && a.state == 5;
        require(
            new_proposal || (a.resolution == self.resolution && a.outcome == self.outcome),
            Error::Immutable,
        )?;
        require(
            !new_proposal
                || (a.resolution != ZERO
                    && a.resolution != self.resolution
                    && a.challenge_until == 0
                    && a.pending == 0
                    && a.material == 0),
            Error::Commitment,
        )?;
        require(
            a.reputation == self.reputation || a.state == 10,
            Error::Immutable,
        )?;
        if self.state == 10 {
            require(
                a.disputes == self.disputes && a.challenge_until == self.challenge_until,
                Error::Immutable,
            )?;
        }
        let mut f = self.clone();
        f.revision = a.revision;
        f.occurred_at = a.occurred_at;
        f.state = a.state;
        f.outcome = a.outcome;
        f.event = a.event;
        f.snapshot = a.snapshot;
        f.resolution = a.resolution;
        f.disputes = a.disputes;
        f.reputation = a.reputation;
        f.trigger = a.trigger;
        f.challenge_until = a.challenge_until;
        f.pending = a.pending;
        f.material = a.material;
        if new_proposal {
            f.chain_not_before = now.checked_add(MIN_CHALLENGE_MS).ok_or(Error::Time)?;
        }
        if self.state == 5 && a.state == 6 {
            require(a.challenge_until > a.occurred_at, Error::Time)?;
            f.chain_not_before = f
                .chain_not_before
                .max(now.checked_add(MIN_CHALLENGE_MS).ok_or(Error::Time)?)
                .max(a.challenge_until);
        } else if !new_proposal {
            require(a.challenge_until >= self.challenge_until, Error::Time)?;
        }
        if a.state == 9 {
            require(
                a.resolution == self.resolution
                    && a.disputes == self.disputes
                    && a.pending == self.pending
                    && a.material == self.material
                    && a.challenge_until == self.challenge_until,
                Error::Immutable,
            )?;
            f.paused_from = self.state;
            f.paused_at = now;
        }
        if self.state == 9 {
            let elapsed = now.checked_sub(self.paused_at).ok_or(Error::Time)?;
            require(elapsed >= 0, Error::Time)?;
            if self.chain_not_before > 0 {
                f.chain_not_before = self
                    .chain_not_before
                    .checked_add(elapsed)
                    .ok_or(Error::Time)?
                    .max(a.challenge_until);
            }
            require(
                a.disputes == self.disputes
                    && a.pending == self.pending
                    && a.material == self.material,
                Error::Immutable,
            )?;
            f.paused_at = 0;
            f.paused_from = 0;
        }
        if a.state == 10 {
            require(
                self.pending == 0
                    && self.material == 0
                    && a.pending == 0
                    && a.material == 0
                    && a.disputes == self.disputes
                    && a.challenge_until == self.challenge_until,
                Error::Dispute,
            )?;
            require(
                now >= self.chain_not_before
                    && now >= self.challenge_until
                    && a.occurred_at >= self.challenge_until,
                Error::NotReady,
            )?;
        }
        f.chain_not_before = f.chain_not_before.max(f.challenge_until);
        f.validate()?;
        Ok(f)
    }
}
#[derive(Clone, Debug)]
pub struct Advance {
    pub revision: u64,
    pub occurred_at: i64,
    pub previous: [u8; 32],
    pub event: [u8; 32],
    pub snapshot: [u8; 32],
    pub state: u8,
    pub outcome: u8,
    pub resolution: [u8; 32],
    pub disputes: [u8; 32],
    pub reputation: [u8; 32],
    pub trigger: [u8; 32],
    pub challenge_until: i64,
    pub pending: u16,
    pub material: u16,
}
impl Advance {
    pub fn decode(bytes: &[u8]) -> Result<Self, ProgramError> {
        let mut r = Reader::new(bytes);
        let a = Self {
            revision: r.u64()?,
            occurred_at: r.time()?,
            previous: r.take()?,
            event: r.take()?,
            snapshot: r.take()?,
            state: r.byte()?,
            outcome: r.byte()?,
            resolution: r.take()?,
            disputes: r.take()?,
            reputation: r.take()?,
            trigger: r.take()?,
            challenge_until: r.time()?,
            pending: r.u16()?,
            material: r.u16()?,
        };
        r.end()?;
        Ok(a)
    }
}
fn signer(a: &AccountInfo) -> ProgramResult {
    require(a.is_signer, Error::Unauthorized)
}
fn writable(a: &AccountInfo) -> ProgramResult {
    require(a.is_writable, Error::Unauthorized)
}
fn owned(a: &AccountInfo, program: &Pubkey, len: usize) -> ProgramResult {
    require(a.owner == program, Error::Owner)?;
    require(a.data_len() == len && !a.executable, Error::Invalid)
}
fn address(a: &AccountInfo, program: &Pubkey, seeds: &[&[u8]]) -> Result<u8, ProgramError> {
    let (key, bump) = Pubkey::find_program_address(seeds, program);
    require(*a.key == key, Error::Address)?;
    Ok(bump)
}
fn config(a: &AccountInfo, program: &Pubkey) -> Result<Config, ProgramError> {
    address(a, program, &[b"config"])?;
    owned(a, program, CONFIG_LEN)?;
    Config::decode(&a.try_borrow_data()?)
}
fn store(a: &AccountInfo, bytes: &[u8]) -> ProgramResult {
    writable(a)?;
    let mut data = a.try_borrow_mut_data()?;
    require(data.len() == bytes.len(), Error::Invalid)?;
    data.copy_from_slice(bytes);
    Ok(())
}
fn create<'a>(
    payer: &AccountInfo<'a>,
    target: &AccountInfo<'a>,
    system: &AccountInfo<'a>,
    program: &Pubkey,
    seeds: &[&[u8]],
    len: usize,
) -> ProgramResult {
    signer(payer)?;
    writable(payer)?;
    writable(target)?;
    require(
        *system.key == system_program::id() && system.executable,
        Error::Address,
    )?;
    require(
        target.owner == &system_program::id() && target.data_is_empty() && !target.executable,
        Error::Owner,
    )?;
    require(payer.key != target.key, Error::Address)?;
    let needed = Rent::get()?
        .minimum_balance(len)
        .saturating_sub(target.lamports());
    if needed > 0 {
        invoke(
            &system_instruction::transfer(payer.key, target.key, needed),
            &[payer.clone(), target.clone(), system.clone()],
        )?;
    }
    invoke_signed(
        &system_instruction::allocate(target.key, len as u64),
        &[target.clone(), system.clone()],
        &[seeds],
    )?;
    invoke_signed(
        &system_instruction::assign(target.key, program),
        &[target.clone(), system.clone()],
        &[seeds],
    )
}

/// Versioned independent intake. Evidence bytes are retained on chain; their JSON
/// semantics and adjudication provenance are validated by the independent importer.
pub mod intake {
    use super::*;
    use solana_program::hash::hashv;
    pub const GATE_LEN: usize = 392;
    pub const RECEIPT_HEADER: usize = 424;
    pub const REVIEWER_LEN: usize = 48;
    pub const MAX_BODY: usize = 32768;
    pub const CHUNK: usize = 512;
    pub const GATE_SEED: &[u8] = b"intake-v1";
    pub const RECEIPT_SEED: &[u8] = b"dispute-v1";
    pub const REVIEWER_SEED: &[u8] = b"intake-review-v1";
    pub const GENESIS: Pubkey =
        solana_program::pubkey!("EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG");
    fn hash(domain: &[u8], data: &[u8]) -> [u8; 32] {
        hashv(&[domain, data]).to_bytes()
    }
    fn count(n: u64) -> ProgramResult {
        require(n <= MAX_TIME as u64, Error::Invalid)
    }
    fn inc(n: u64) -> Result<u64, ProgramError> {
        let n = n.checked_add(1).ok_or(Error::Invalid)?;
        count(n)?;
        Ok(n)
    }
    fn later(time: i64, delta: i64) -> Result<i64, ProgramError> {
        let n = time.checked_add(delta).ok_or(Error::Time)?;
        require(valid_time(n) && delta >= 0, Error::Time)?;
        Ok(n)
    }
    fn unique(a: &[AccountInfo]) -> ProgramResult {
        for (i, key) in a.iter().enumerate() {
            require(
                !a[..i].iter().any(|other| key.key == other.key),
                Error::Address,
            )?;
        }
        Ok(())
    }
    fn forecast(a: &AccountInfo, p: &Pubkey) -> Result<Forecast, ProgramError> {
        owned(a, p, FORECAST_LEN)?;
        let f = Forecast::decode(&a.try_borrow_data()?)?;
        address(a, p, &[b"forecast", &f.id])?;
        Ok(f)
    }
    fn relay(a: &AccountInfo, conf: &AccountInfo, p: &Pubkey) -> ProgramResult {
        signer(a)?;
        require(*a.key == config(conf, p)?.relayer, Error::Unauthorized)
    }
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct Reviewer {
        pub key: [u8; 32],
        pub revision: u64,
    }
    impl Reviewer {
        pub fn encode(&self) -> Vec<u8> {
            let mut b = b"FNJUDG01".to_vec();
            b.extend(self.key);
            b.extend(self.revision.to_le_bytes());
            b
        }
        pub fn decode(b: &[u8]) -> Result<Self, ProgramError> {
            let mut r = Reader::new(b);
            require(r.take::<8>()? == *b"FNJUDG01", Error::Invalid)?;
            let v = Self {
                key: r.take()?,
                revision: r.u64()?,
            };
            r.end()?;
            require(v.key != ZERO && v.revision > 0, Error::Invalid)?;
            count(v.revision)?;
            Ok(v)
        }
        pub fn commitment(&self) -> [u8; 32] {
            hash(b"forecast-intake-v1:reviewer:", &self.encode())
        }
    }
    fn reviewer(
        a: &AccountInfo,
        conf: &AccountInfo,
        policy: &AccountInfo,
        p: &Pubkey,
    ) -> ProgramResult {
        signer(a)?;
        address(policy, p, &[REVIEWER_SEED])?;
        owned(policy, p, REVIEWER_LEN)?;
        let role = Reviewer::decode(&policy.try_borrow_data()?)?;
        let c = config(conf, p)?;
        require(
            role.key == a.key.to_bytes() && *a.key != c.admin && *a.key != c.relayer,
            Error::Unauthorized,
        )
    }
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct Gate {
        pub forecast: [u8; 32],
        pub specification: [u8; 32],
        pub epoch: u64,
        pub revision: u64,
        pub proposal_revision: u64,
        pub resolution: [u8; 32],
        pub proposal_event: [u8; 32],
        pub opened: i64,
        pub deadline: i64,
        pub pending: u64,
        pub material: u64,
        pub accepted: u64,
        pub head: [u8; 32],
        pub phase: u8,
        pub sealed_revision: u64,
        pub sealed_event: [u8; 32],
        pub sealed_snapshot: [u8; 32],
        pub sealed_payload: [u8; 32],
        pub sealed_at: i64,
        pub sealed_slot: u64,
    }
    impl Gate {
        pub fn encode(&self) -> Vec<u8> {
            let mut b = b"FNINTK01".to_vec();
            for x in [self.forecast, self.specification, GENESIS.to_bytes()] {
                b.extend(x);
            }
            for x in [self.epoch, self.revision, self.proposal_revision] {
                b.extend(x.to_le_bytes());
            }
            b.extend(self.resolution);
            b.extend(self.proposal_event);
            b.extend(self.opened.to_le_bytes());
            b.extend(self.deadline.to_le_bytes());
            for x in [self.pending, self.material, self.accepted] {
                b.extend(x.to_le_bytes());
            }
            b.extend(self.head);
            b.push(self.phase);
            b.extend([0; 7]);
            b.extend(self.sealed_revision.to_le_bytes());
            for x in [self.sealed_event, self.sealed_snapshot, self.sealed_payload] {
                b.extend(x);
            }
            b.extend(self.sealed_at.to_le_bytes());
            b.extend(self.sealed_slot.to_le_bytes());
            b
        }
        pub fn decode(b: &[u8]) -> Result<Self, ProgramError> {
            let mut r = Reader::new(b);
            require(r.take::<8>()? == *b"FNINTK01", Error::Invalid)?;
            let forecast = r.take()?;
            let specification = r.take()?;
            require(r.take::<32>()? == GENESIS.to_bytes(), Error::Invalid)?;
            let mut g = Self {
                forecast,
                specification,
                epoch: r.u64()?,
                revision: r.u64()?,
                proposal_revision: r.u64()?,
                resolution: r.take()?,
                proposal_event: r.take()?,
                opened: r.time()?,
                deadline: r.time()?,
                pending: r.u64()?,
                material: r.u64()?,
                accepted: r.u64()?,
                head: r.take()?,
                phase: r.byte()?,
                sealed_revision: 0,
                sealed_event: ZERO,
                sealed_snapshot: ZERO,
                sealed_payload: ZERO,
                sealed_at: 0,
                sealed_slot: 0,
            };
            require(r.take::<7>()? == [0; 7], Error::Invalid)?;
            g.sealed_revision = r.u64()?;
            g.sealed_event = r.take()?;
            g.sealed_snapshot = r.take()?;
            g.sealed_payload = r.take()?;
            g.sealed_at = r.time()?;
            g.sealed_slot = r.u64()?;
            r.end()?;
            g.validate()?;
            Ok(g)
        }
        fn validate(&self) -> ProgramResult {
            for n in [
                self.epoch,
                self.revision,
                self.proposal_revision,
                self.pending,
                self.material,
                self.accepted,
                self.sealed_revision,
                self.sealed_slot,
            ] {
                count(n)?;
            }
            require(
                self.forecast != ZERO
                    && self.specification != ZERO
                    && self.head != ZERO
                    && self.revision > 0
                    && self.phase <= 3,
                Error::Invalid,
            )?;
            require(
                valid_time(self.opened) && valid_time(self.deadline) && valid_time(self.sealed_at),
                Error::Time,
            )?;
            require(
                self.pending
                    .checked_add(self.material)
                    .ok_or(Error::Invalid)?
                    <= self.accepted,
                Error::Dispute,
            )?;
            if self.phase == 0 {
                require(
                    self.epoch == 0
                        && self.proposal_revision == 0
                        && self.resolution == ZERO
                        && self.proposal_event == ZERO
                        && self.opened == 0
                        && self.deadline == 0
                        && self.accepted == 0,
                    Error::Invalid,
                )?;
            } else {
                require(
                    self.epoch > 0
                        && self.proposal_revision > 0
                        && self.resolution != ZERO
                        && self.proposal_event != ZERO
                        && self.opened < self.deadline,
                    Error::Invalid,
                )?;
            }
            if self.phase < 2 {
                require(
                    self.sealed_revision == 0
                        && self.sealed_event == ZERO
                        && self.sealed_snapshot == ZERO
                        && self.sealed_payload == ZERO
                        && self.sealed_at == 0
                        && self.sealed_slot == 0,
                    Error::Invalid,
                )
            } else {
                require(
                    self.pending == 0
                        && self.material == 0
                        && self.sealed_revision > self.proposal_revision
                        && self.sealed_event != ZERO
                        && self.sealed_snapshot != ZERO
                        && self.sealed_payload != ZERO
                        && self.sealed_at > self.deadline
                        && self.sealed_slot > 0,
                    Error::Invalid,
                )
            }
        }
        pub fn commitment(&self) -> [u8; 32] {
            hash(b"forecast-intake-v1:accumulator:", &self.encode())
        }
        fn bump(&mut self, tag: u8, material: &[u8]) -> ProgramResult {
            self.revision = inc(self.revision)?;
            self.head =
                hashv(&[b"forecast-intake-v1:event:", &self.head, &[tag], material]).to_bytes();
            self.validate()
        }
        fn same(&self, f: &Forecast, key: &Pubkey) -> ProgramResult {
            require(
                self.forecast == key.to_bytes() && self.specification == f.specification,
                Error::Commitment,
            )?;
            if self.phase != 0 {
                require(self.resolution == f.resolution, Error::Commitment)?;
            }
            Ok(())
        }
        fn effective_deadline(&self, f: &Forecast, now: i64) -> Result<i64, ProgramError> {
            if f.state == 9 {
                later(
                    self.deadline,
                    now.checked_sub(f.paused_at).ok_or(Error::Time)?,
                )
            } else {
                Ok(self.deadline)
            }
        }
        fn new_epoch(&mut self, f: &Forecast, now: i64) -> ProgramResult {
            require(self.pending == 0, Error::Dispute)?;
            self.epoch = inc(self.epoch)?;
            self.proposal_revision = f.revision;
            self.resolution = f.resolution;
            self.proposal_event = f.event;
            self.opened = now;
            self.deadline = later(now, MIN_CHALLENGE_MS)?
                .max(f.chain_not_before)
                .max(f.challenge_until);
            self.pending = 0;
            self.material = 0;
            self.accepted = 0;
            self.phase = 1;
            Ok(())
        }
    }
    fn gate(
        a: &AccountInfo,
        f: &Forecast,
        forecast_key: &Pubkey,
        p: &Pubkey,
    ) -> Result<Gate, ProgramError> {
        address(a, p, &[GATE_SEED, forecast_key.as_ref()])?;
        owned(a, p, GATE_LEN)?;
        let g = Gate::decode(&a.try_borrow_data()?)?;
        g.same(f, forecast_key)?;
        Ok(g)
    }
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct Receipt {
        pub forecast: [u8; 32],
        pub specification: [u8; 32],
        pub program: [u8; 32],
        pub epoch: u64,
        pub proposal_revision: u64,
        pub resolution: [u8; 32],
        pub proposal_event: [u8; 32],
        pub user: [u8; 32],
        pub nonce: [u8; 32],
        pub evidence: [u8; 32],
        pub body_length: u32,
        pub body: Vec<u8>,
        pub status: u8,
        pub accepted_at: i64,
        pub accepted_slot: u64,
        pub deadline: i64,
        pub review: [u8; 32],
        pub reviewer: [u8; 32],
        pub reviewed_at: i64,
    }
    impl Receipt {
        pub fn encode(&self) -> Vec<u8> {
            let mut b = b"FNDRCP01".to_vec();
            for x in [
                self.forecast,
                self.specification,
                GENESIS.to_bytes(),
                self.program,
            ] {
                b.extend(x);
            }
            b.extend(self.epoch.to_le_bytes());
            b.extend(self.proposal_revision.to_le_bytes());
            for x in [
                self.resolution,
                self.proposal_event,
                self.user,
                self.nonce,
                self.evidence,
            ] {
                b.extend(x);
            }
            b.extend(self.body_length.to_le_bytes());
            b.extend((self.body.len() as u32).to_le_bytes());
            b.push(self.status);
            b.extend([0; 7]);
            b.extend(self.accepted_at.to_le_bytes());
            b.extend(self.accepted_slot.to_le_bytes());
            b.extend(self.deadline.to_le_bytes());
            b.extend(self.review);
            b.extend(self.reviewer);
            b.extend(self.reviewed_at.to_le_bytes());
            b.extend(&self.body);
            b
        }
        pub fn decode(b: &[u8]) -> Result<Self, ProgramError> {
            require(
                b.len() >= RECEIPT_HEADER && b.len() <= RECEIPT_HEADER + MAX_BODY,
                Error::Invalid,
            )?;
            let mut r = Reader::new(&b[..RECEIPT_HEADER]);
            require(r.take::<8>()? == *b"FNDRCP01", Error::Invalid)?;
            let forecast = r.take()?;
            let specification = r.take()?;
            require(r.take::<32>()? == GENESIS.to_bytes(), Error::Invalid)?;
            let program = r.take()?;
            let epoch = r.u64()?;
            let proposal_revision = r.u64()?;
            let resolution = r.take()?;
            let proposal_event = r.take()?;
            let user = r.take()?;
            let nonce = r.take()?;
            let evidence = r.take()?;
            let body_length = u32::from_le_bytes(r.take()?);
            let written = u32::from_le_bytes(r.take()?);
            let status = r.byte()?;
            require(r.take::<7>()? == [0; 7], Error::Invalid)?;
            let v = Self {
                forecast,
                specification,
                program,
                epoch,
                proposal_revision,
                resolution,
                proposal_event,
                user,
                nonce,
                evidence,
                body_length,
                body: b[RECEIPT_HEADER..].to_vec(),
                status,
                accepted_at: r.time()?,
                accepted_slot: r.u64()?,
                deadline: r.time()?,
                review: r.take()?,
                reviewer: r.take()?,
                reviewed_at: r.time()?,
            };
            r.end()?;
            require(written as usize == v.body.len(), Error::Invalid)?;
            v.validate()?;
            Ok(v)
        }
        fn validate(&self) -> ProgramResult {
            for x in [
                self.forecast,
                self.specification,
                self.program,
                self.resolution,
                self.proposal_event,
                self.user,
                self.nonce,
                self.evidence,
            ] {
                require(x != ZERO, Error::Invalid)?;
            }
            count(self.epoch)?;
            count(self.proposal_revision)?;
            count(self.accepted_slot)?;
            require(
                self.epoch > 0
                    && self.proposal_revision > 0
                    && self.body_length > 0
                    && (self.body_length as usize) <= MAX_BODY
                    && self.body.len() <= self.body_length as usize
                    && self.status <= 3,
                Error::Invalid,
            )?;
            for t in [self.accepted_at, self.deadline, self.reviewed_at] {
                require(valid_time(t), Error::Time)?;
            }
            if self.status == 0 {
                require(
                    self.accepted_at == 0 && self.accepted_slot == 0 && self.deadline == 0,
                    Error::Invalid,
                )?;
            } else {
                require(
                    self.body.len() == self.body_length as usize
                        && self.accepted_slot > 0
                        && self.accepted_at <= self.deadline
                        && hash(b"forecast-intake-v1:evidence:", &self.body) == self.evidence,
                    Error::Commitment,
                )?;
            }
            if self.status < 2 {
                require(
                    self.review == ZERO && self.reviewer == ZERO && self.reviewed_at == 0,
                    Error::Invalid,
                )
            } else {
                require(
                    self.review != ZERO
                        && self.reviewer != ZERO
                        && self.reviewed_at >= self.accepted_at,
                    Error::Invalid,
                )
            }
        }
        pub fn commitment(&self) -> [u8; 32] {
            hash(b"forecast-intake-v1:receipt:", &self.encode())
        }
        fn same(&self, g: &Gate) -> ProgramResult {
            require(
                self.forecast == g.forecast
                    && self.specification == g.specification
                    && self.epoch == g.epoch
                    && self.proposal_revision == g.proposal_revision
                    && self.resolution == g.resolution
                    && self.proposal_event == g.proposal_event,
                Error::Commitment,
            )
        }
    }
    fn receipt(a: &AccountInfo, p: &Pubkey) -> Result<Receipt, ProgramError> {
        require(a.owner == p && !a.executable, Error::Owner)?;
        let r = Receipt::decode(&a.try_borrow_data()?)?;
        require(r.program == p.to_bytes(), Error::Commitment)?;
        address(
            a,
            p,
            &[RECEIPT_SEED, &r.forecast, &r.epoch.to_le_bytes(), &r.user],
        )?;
        Ok(r)
    }
    pub fn submit(
        g: &Gate,
        r: &Receipt,
        f: &Forecast,
        now: i64,
        slot: u64,
    ) -> Result<(Gate, Receipt), ProgramError> {
        g.validate()?;
        r.validate()?;
        r.same(g)?;
        require(valid_time(now) && slot > 0, Error::Time)?;
        count(slot)?;
        let state = if f.state == 9 { f.paused_from } else { f.state };
        let deadline = g.effective_deadline(f, now)?;
        require(
            g.phase == 1 && (6..=8).contains(&state) && now >= g.opened && now <= deadline,
            Error::NotReady,
        )?;
        require(
            r.status == 0
                && r.body.len() == r.body_length as usize
                && hash(b"forecast-intake-v1:evidence:", &r.body) == r.evidence,
            Error::Commitment,
        )?;
        let mut next = g.clone();
        let mut accepted = r.clone();
        next.pending = inc(next.pending)?;
        next.accepted = inc(next.accepted)?;
        accepted.status = 1;
        accepted.accepted_at = now;
        accepted.accepted_slot = slot;
        accepted.deadline = deadline;
        accepted.validate()?;
        next.bump(10, &accepted.commitment())?;
        Ok((next, accepted))
    }
    pub fn review(
        g: &Gate,
        r: &Receipt,
        review_hash: [u8; 32],
        authority: [u8; 32],
        disposition: u8,
        now: i64,
    ) -> Result<(Gate, Receipt), ProgramError> {
        g.validate()?;
        r.validate()?;
        r.same(g)?;
        require(
            g.phase == 1
                && r.status == 1
                && g.pending > 0
                && (disposition == 2 || disposition == 3)
                && review_hash != ZERO
                && authority != ZERO
                && now >= r.accepted_at
                && valid_time(now),
            Error::Dispute,
        )?;
        let mut next = g.clone();
        let mut reviewed = r.clone();
        next.pending -= 1;
        if disposition == 3 {
            next.material = inc(next.material)?;
        }
        reviewed.status = disposition;
        reviewed.review = review_hash;
        reviewed.reviewer = authority;
        reviewed.reviewed_at = now;
        reviewed.validate()?;
        next.bump(11, &reviewed.commitment())?;
        Ok((next, reviewed))
    }
    pub fn seal(
        g: &Gate,
        f: &Forecast,
        payload: &[u8],
        now: i64,
        slot: u64,
    ) -> Result<Gate, ProgramError> {
        g.validate()?;
        count(slot)?;
        require(
            g.phase == 1
                && g.pending == 0
                && g.material == 0
                && f.state == 6
                && now > g.deadline
                && slot > 0,
            Error::NotReady,
        )?;
        let a = Advance::decode(payload)?;
        require(a.state == 10, Error::Transition)?;
        f.advance(&a, now)?;
        let mut next = g.clone();
        next.phase = 2;
        next.sealed_revision = a.revision;
        next.sealed_event = a.event;
        next.sealed_snapshot = a.snapshot;
        next.sealed_payload = hash(b"forecast-intake-v1:advance:", payload);
        next.sealed_at = now;
        next.sealed_slot = slot;
        next.bump(12, &next.sealed_payload.clone())?;
        Ok(next)
    }
    pub fn process(p: &Pubkey, a: &[AccountInfo], tag: u8, data: &[u8]) -> ProgramResult {
        unique(a)?;
        let now = now_ms()?;
        if tag == 14 {
            require(a.len() == 4 && data.len() == 72, Error::Invalid)?;
            signer(&a[0])?;
            writable(&a[0])?;
            require(
                *a[3].key == system_program::id() && a[3].executable,
                Error::Address,
            )?;
            let c = config(&a[1], p)?;
            require(*a[0].key == c.admin, Error::Unauthorized)?;
            let mut rd = Reader::new(data);
            let revision = rd.u64()?;
            let commitment = rd.take::<32>()?;
            let key = rd.take::<32>()?;
            rd.end()?;
            require(
                key != ZERO && key != c.admin.to_bytes() && key != c.relayer.to_bytes(),
                Error::Unauthorized,
            )?;
            let bump = address(&a[2], p, &[REVIEWER_SEED])?;
            let new_revision = if a[2].owner == &system_program::id() && a[2].data_is_empty() {
                require(revision == 0 && commitment == ZERO, Error::Revision)?;
                create(
                    &a[0],
                    &a[2],
                    &a[3],
                    p,
                    &[REVIEWER_SEED, &[bump]],
                    REVIEWER_LEN,
                )?;
                1
            } else {
                owned(&a[2], p, REVIEWER_LEN)?;
                let old = Reviewer::decode(&a[2].try_borrow_data()?)?;
                require(
                    old.revision == revision && old.commitment() == commitment,
                    Error::Revision,
                )?;
                inc(revision)?
            };
            return store(
                &a[2],
                &Reviewer {
                    key,
                    revision: new_revision,
                }
                .encode(),
            );
        }
        if tag == 9 {
            require(
                a.len() == 3 && data.len() >= 7 && data.len() <= 6 + CHUNK,
                Error::Invalid,
            )?;
            signer(&a[0])?;
            let mut r = receipt(&a[1], p)?;
            require(
                r.user == a[0].key.to_bytes() && r.status == 0,
                Error::Unauthorized,
            )?;
            let offset =
                u32::from_le_bytes(data[..4].try_into().map_err(|_| Error::Invalid)?) as usize;
            let length =
                u16::from_le_bytes(data[4..6].try_into().map_err(|_| Error::Invalid)?) as usize;
            require(
                length > 0
                    && length <= CHUNK
                    && data.len() == 6 + length
                    && offset == r.body.len()
                    && offset + length <= r.body_length as usize,
                Error::Invalid,
            )?;
            writable(&a[0])?;
            writable(&a[1])?;
            require(
                *a[2].key == system_program::id() && a[2].executable,
                Error::Address,
            )?;
            let next_len = RECEIPT_HEADER + offset + length;
            let needed = Rent::get()?
                .minimum_balance(next_len)
                .saturating_sub(a[1].lamports());
            if needed > 0 {
                invoke(
                    &system_instruction::transfer(a[0].key, a[1].key, needed),
                    &[a[0].clone(), a[1].clone(), a[2].clone()],
                )?;
            }
            require(next_len - a[1].data_len() <= 10240, Error::Invalid)?;
            a[1].realloc(next_len, false)?;
            r.body.extend(&data[6..]);
            r.validate()?;
            return store(&a[1], &r.encode());
        }
        if tag == 8 || tag == 10 {
            require(a.len() == if tag == 8 { 5 } else { 4 }, Error::Invalid)?;
            signer(&a[0])?;
            let f = forecast(&a[1], p)?;
            let g = gate(&a[2], &f, a[1].key, p)?;
            if tag == 8 {
                require(data.len() == 180 && g.phase == 1, Error::Invalid)?;
                let mut rd = Reader::new(data);
                let epoch = rd.u64()?;
                let proposal_revision = rd.u64()?;
                let specification = rd.take()?;
                let resolution = rd.take()?;
                let proposal_event = rd.take()?;
                let nonce = rd.take()?;
                let evidence = rd.take()?;
                let body_length = u32::from_le_bytes(rd.take()?);
                rd.end()?;
                let r = Receipt {
                    forecast: a[1].key.to_bytes(),
                    specification,
                    program: p.to_bytes(),
                    epoch,
                    proposal_revision,
                    resolution,
                    proposal_event,
                    user: a[0].key.to_bytes(),
                    nonce,
                    evidence,
                    body_length,
                    body: vec![],
                    status: 0,
                    accepted_at: 0,
                    accepted_slot: 0,
                    deadline: 0,
                    review: ZERO,
                    reviewer: ZERO,
                    reviewed_at: 0,
                };
                r.validate()?;
                r.same(&g)?;
                let bump = address(
                    &a[3],
                    p,
                    &[RECEIPT_SEED, &r.forecast, &epoch.to_le_bytes(), &r.user],
                )?;
                create(
                    &a[0],
                    &a[3],
                    &a[4],
                    p,
                    &[
                        RECEIPT_SEED,
                        &r.forecast,
                        &epoch.to_le_bytes(),
                        &r.user,
                        &[bump],
                    ],
                    RECEIPT_HEADER,
                )?;
                return store(&a[3], &r.encode());
            }
            require(data.len() == 72, Error::Invalid)?;
            let r = receipt(&a[3], p)?;
            let mut rd = Reader::new(data);
            require(
                rd.u64()? == r.epoch
                    && rd.take::<32>()? == r.nonce
                    && rd.take::<32>()? == r.evidence,
                Error::Commitment,
            )?;
            rd.end()?;
            require(r.user == a[0].key.to_bytes(), Error::Unauthorized)?;
            let (ng, nr) = submit(&g, &r, &f, now, Clock::get()?.slot)?;
            store(&a[2], &ng.encode())?;
            return store(&a[3], &nr.encode());
        }
        if tag == 11 {
            require(a.len() == 6 && data.len() == 73, Error::Invalid)?;
            reviewer(&a[0], &a[1], &a[2], p)?;
            let f = forecast(&a[3], p)?;
            let g = gate(&a[4], &f, a[3].key, p)?;
            let r = receipt(&a[5], p)?;
            let mut rd = Reader::new(data);
            require(
                rd.u64()? == g.epoch && rd.take::<32>()? == r.commitment(),
                Error::Commitment,
            )?;
            let artifact = rd.take()?;
            let disposition = rd.byte()?;
            rd.end()?;
            let (ng, nr) = review(&g, &r, artifact, a[0].key.to_bytes(), disposition, now)?;
            store(&a[4], &ng.encode())?;
            return store(&a[5], &nr.encode());
        }
        require(
            (tag == 6 && a.len() == 5)
                || (tag == 7 && [4, 5, 6].contains(&a.len()))
                || ((tag == 12 || tag == 13) && a.len() == 4),
            Error::Invalid,
        )?;
        relay(&a[0], &a[1], p)?;
        let f = forecast(&a[2], p)?;
        if tag == 6 {
            require(data.len() == 72 && f.state < 10, Error::Invalid)?;
            let mut rd = Reader::new(data);
            require(
                rd.u64()? == f.revision
                    && rd.take::<32>()? == f.event
                    && rd.take::<32>()? == f.specification,
                Error::Commitment,
            )?;
            rd.end()?;
            let bump = address(&a[3], p, &[GATE_SEED, a[2].key.as_ref()])?;
            let mut g = Gate {
                forecast: a[2].key.to_bytes(),
                specification: f.specification,
                epoch: 0,
                revision: 1,
                proposal_revision: 0,
                resolution: ZERO,
                proposal_event: ZERO,
                opened: 0,
                deadline: 0,
                pending: 0,
                material: 0,
                accepted: 0,
                head: hashv(&[
                    b"forecast-intake-v1:genesis:",
                    p.as_ref(),
                    a[2].key.as_ref(),
                    &f.specification,
                ])
                .to_bytes(),
                phase: 0,
                sealed_revision: 0,
                sealed_event: ZERO,
                sealed_snapshot: ZERO,
                sealed_payload: ZERO,
                sealed_at: 0,
                sealed_slot: 0,
            };
            if f.resolution != ZERO {
                g.new_epoch(&f, now)?;
            }
            g.validate()?;
            create(
                &a[0],
                &a[3],
                &a[4],
                p,
                &[GATE_SEED, a[2].key.as_ref(), &[bump]],
                GATE_LEN,
            )?;
            return store(&a[3], &g.encode());
        }
        if tag == 7 {
            require(!data.is_empty(), Error::Invalid)?;
            let mode = data[0];
            require(
                (mode == 0 && data.len() == 255 && a.len() == 4)
                    || (mode == 1 && data.len() == 319 && a.len() == 6)
                    || (mode == 2 && data.len() == 287 && a.len() == 5),
                Error::Invalid,
            )?;
            let adv = Advance::decode(&data[1..255])?;
            require(adv.state != 10, Error::Transition)?;
            address(&a[3], p, &[GATE_SEED, a[2].key.as_ref()])?;
            if a[3].owner == &system_program::id() && a[3].data_is_empty() {
                require(mode == 0, Error::Invalid)?;
                return store(&a[2], &f.advance(&adv, now)?.encode());
            }
            let mut g = gate(&a[3], &f, a[2].key, p)?;
            if g.phase == 3 {
                require(
                    mode == 0 && f.state == 10 && adv.state == 11,
                    Error::Transition,
                )?;
                let updated = f.advance(&adv, now)?;
                g.bump(7, data)?;
                store(&a[2], &updated.encode())?;
                return store(&a[3], &g.encode());
            }
            require(g.phase < 2, Error::NotReady)?;
            let replacement = f.state == 8 && adv.state == 5;
            require((mode == 1) == replacement, Error::Transition)?;
            if replacement {
                reviewer(&a[4], &a[1], &a[5], p)?;
                require(
                    g.pending == 0 && data[255..287] != ZERO && data[287..319] == g.head,
                    Error::Dispute,
                )?;
            }
            if mode == 2 {
                let r = receipt(&a[4], p)?;
                r.same(&g)?;
                require(
                    f.state == 8
                        && adv.state == 7
                        && r.status == 1
                        && r.commitment() == data[255..287]
                        && g.pending > 0,
                    Error::Dispute,
                )?;
            }
            let updated = f.advance_intake(&adv, now, mode == 2)?;
            if (f.state == 4 || f.state == 8) && updated.state == 5 {
                g.new_epoch(&updated, now)?;
            } else if g.phase == 1 {
                if f.state == 9 {
                    g.deadline =
                        later(g.deadline, now.checked_sub(f.paused_at).ok_or(Error::Time)?)?;
                }
                g.deadline = g
                    .deadline
                    .max(updated.chain_not_before)
                    .max(updated.challenge_until);
            }
            g.bump(7, data)?;
            store(&a[2], &updated.encode())?;
            return store(&a[3], &g.encode());
        }
        writable(&a[2])?;
        let g = gate(&a[3], &f, a[2].key, p)?;
        if tag == 12 {
            require(data.len() == 294, Error::Invalid)?;
            let mut rd = Reader::new(&data[254..]);
            require(
                rd.u64()? == g.revision && rd.take::<32>()? == g.commitment(),
                Error::Revision,
            )?;
            rd.end()?;
            let ng = seal(&g, &f, &data[..254], now, Clock::get()?.slot)?;
            return store(&a[3], &ng.encode());
        }
        require(
            tag == 13 && data.len() == 286 && g.phase == 2 && data[254..] == g.commitment(),
            Error::NotReady,
        )?;
        require(
            hash(b"forecast-intake-v1:advance:", &data[..254]) == g.sealed_payload
                && now > g.deadline,
            Error::Commitment,
        )?;
        let adv = Advance::decode(&data[..254])?;
        require(
            adv.state == 10
                && adv.revision == g.sealed_revision
                && adv.event == g.sealed_event
                && adv.snapshot == g.sealed_snapshot,
            Error::Commitment,
        )?;
        let updated = f.advance(&adv, now)?;
        let mut ng = g.clone();
        ng.phase = 3;
        ng.bump(13, &g.sealed_payload)?;
        store(&a[2], &updated.encode())?;
        store(&a[3], &ng.encode())
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        fn forecast() -> Forecast {
            Forecast {
                id: [1; 32],
                creator: [2; 32],
                specification: [3; 32],
                open_at: 10,
                close_at: 20,
                revision: 4,
                occurred_at: 30,
                state: 6,
                outcome: 1,
                paused_from: 0,
                event: [4; 32],
                snapshot: [5; 32],
                resolution: [6; 32],
                disputes: ZERO,
                reputation: ZERO,
                trigger: ZERO,
                challenge_until: 100,
                chain_not_before: 100,
                paused_at: 0,
                pending: 0,
                material: 0,
            }
        }
        fn gate() -> Gate {
            Gate {
                forecast: [7; 32],
                specification: [3; 32],
                epoch: 1,
                revision: 1,
                proposal_revision: 3,
                resolution: [6; 32],
                proposal_event: [8; 32],
                opened: 30,
                deadline: 100,
                pending: 0,
                material: 0,
                accepted: 0,
                head: [9; 32],
                phase: 1,
                sealed_revision: 0,
                sealed_event: ZERO,
                sealed_snapshot: ZERO,
                sealed_payload: ZERO,
                sealed_at: 0,
                sealed_slot: 0,
            }
        }
        fn receipt(body: Vec<u8>) -> Receipt {
            Receipt {
                forecast: [7; 32],
                specification: [3; 32],
                program: [10; 32],
                epoch: 1,
                proposal_revision: 3,
                resolution: [6; 32],
                proposal_event: [8; 32],
                user: [11; 32],
                nonce: [12; 32],
                evidence: hash(b"forecast-intake-v1:evidence:", &body),
                body_length: body.len() as u32,
                body,
                status: 0,
                accepted_at: 0,
                accepted_slot: 0,
                deadline: 0,
                review: ZERO,
                reviewer: ZERO,
                reviewed_at: 0,
            }
        }
        fn final_payload(f: &Forecast) -> Vec<u8> {
            let mut b = vec![];
            b.extend((f.revision + 1).to_le_bytes());
            b.extend(101i64.to_le_bytes());
            b.extend(f.event);
            b.extend([13; 32]);
            b.extend([14; 32]);
            b.extend([10, 1]);
            b.extend(f.resolution);
            b.extend(f.disputes);
            b.extend([15; 32]);
            b.extend(f.trigger);
            b.extend(f.challenge_until.to_le_bytes());
            b.extend([0; 4]);
            b
        }
        #[test]
        fn exact_layout_roundtrip_and_corruptions() {
            let g = gate();
            assert_eq!(g.encode().len(), 392);
            assert_eq!(Gate::decode(&g.encode()).unwrap(), g);
            for n in 0..392 {
                assert!(Gate::decode(&g.encode()[..n]).is_err());
            }
            for (offset, value) in [(0, 0), (72, 0), (264, 4), (265, 1), (272, 1)] {
                let mut b = g.encode();
                b[offset] = value;
                assert!(Gate::decode(&b).is_err());
            }
            let r = receipt(vec![42; MAX_BODY]);
            assert_eq!(r.encode().len(), 424 + MAX_BODY);
            assert_eq!(Receipt::decode(&r.encode()).unwrap(), r);
            let mut b = r.encode();
            b[317] = 0;
            assert!(Receipt::decode(&b).is_err());
            let mut b = r.encode();
            b[321] = 1;
            assert!(Receipt::decode(&b).is_err());
            let role = Reviewer {
                key: [11; 32],
                revision: 1,
            };
            assert_eq!(role.encode().len(), 48);
            assert_eq!(Reviewer::decode(&role.encode()).unwrap(), role);
        }
        #[test]
        fn python_rust_golden_commitments_match() {
            fn hex(value: [u8; 32]) -> String {
                value.iter().map(|byte| format!("{byte:02x}")).collect()
            }
            assert_eq!(
                hex(gate().commitment()),
                "27194d29a1041f5d1a7e4dec8b6001560b4f269fe5ee413dfc87e9964555ebed"
            );
            assert_eq!(
                hex(receipt(vec![42]).commitment()),
                "bb91319fb3146d0653cf42b84726fa5c51c0b9aa1d30276b01e9c7e85e76db6d"
            );
            assert_eq!(
                hex(Reviewer {
                    key: [11; 32],
                    revision: 1
                }
                .commitment()),
                "b633e1b6358a9eb49d9758b46e3da9fe2f92cfe1d4db0a400125d3745bd29e01"
            );
            assert_eq!(
                hex(hash(
                    b"forecast-intake-v1:advance:",
                    &final_payload(&forecast())
                )),
                "cbc73db439e88aa792bee35cf094ec142913bbe37efbbba6ca120bb75a46e972"
            );
        }
        #[test]
        fn timely_submission_and_seal_both_orderings() {
            let (g, r, f) = (gate(), receipt(vec![42]), forecast());
            let payload = final_payload(&f);
            let (pending, accepted) = submit(&g, &r, &f, 100, 1).unwrap();
            assert_eq!(
                (pending.pending, pending.accepted, accepted.accepted_at),
                (1, 1, 100)
            );
            assert!(seal(&pending, &f, &payload, 101, 2).is_err());
            assert!(seal(&g, &f, &payload, 100, 1).is_err());
            let sealed = seal(&g, &f, &payload, 101, 2).unwrap();
            assert!(submit(&sealed, &r, &f, 101, 2).is_err());
            assert!(submit(&g, &r, &f, 101, 2).is_err());
            assert!(submit(&pending, &accepted, &f, 100, 2).is_err());
            assert_eq!(g, gate());
            assert_eq!(r, receipt(vec![42]));
        }
        #[test]
        fn incomplete_wrong_evidence_epoch_and_overflow_never_mutate() {
            let (g, r, f) = (gate(), receipt(vec![42]), forecast());
            for variant in 0..4 {
                let mut wrong = r.clone();
                match variant {
                    0 => wrong.body.clear(),
                    1 => wrong.evidence = [1; 32],
                    2 => wrong.epoch = 2,
                    _ => wrong.proposal_revision = 4,
                };
                assert!(submit(&g, &wrong, &f, 99, 1).is_err());
            }
            let mut full = g.clone();
            full.pending = MAX_TIME as u64;
            full.accepted = full.pending;
            assert!(submit(&full, &r, &f, 99, 1).is_err());
        }
        #[test]
        fn rejection_clears_once_material_blocks_replacement_until_reviewed() {
            let (g, r) = submit(&gate(), &receipt(vec![42]), &forecast(), 99, 1).unwrap();
            let (clear, rejected) = review(&g, &r, [16; 32], [17; 32], 2, 101).unwrap();
            assert_eq!((clear.pending, clear.material), (0, 0));
            assert!(seal(&clear, &forecast(), &final_payload(&forecast()), 101, 2).is_ok());
            assert!(review(&clear, &rejected, [16; 32], [17; 32], 2, 102).is_err());
            let (material, _) = review(&g, &r, [16; 32], [17; 32], 3, 101).unwrap();
            assert!(seal(&material, &forecast(), &final_payload(&forecast()), 101, 2).is_err());
            let mut pending = g.clone();
            assert!(pending.new_epoch(&forecast(), 101).is_err());
            assert_eq!(pending, g);
        }
        #[test]
        fn pause_extends_inclusion_and_disallows_seal() {
            let mut f = forecast();
            f.state = 9;
            f.paused_from = 6;
            f.paused_at = 90;
            let (_, r) = submit(&gate(), &receipt(vec![42]), &f, 200, 1).unwrap();
            assert_eq!(r.deadline, 210);
            assert!(seal(&gate(), &f, &final_payload(&f), 200, 1).is_err());
        }
        #[test]
        fn legacy_opcode_cannot_bypass_sidecar() {
            assert_eq!(
                super::super::process_instruction(&Pubkey::new_unique(), &[], &[2]),
                Err(Error::LegacyAdvanceDisabled.into())
            );
        }
    }
}

pub fn process_instruction(
    program: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let (&tag, payload) = data.split_first().ok_or(Error::Invalid)?;
    if (6..=14).contains(&tag) {
        return intake::process(program, accounts, tag, payload);
    }
    match tag {
        0 => {
            require(accounts.len() == 3 && payload.len() == 32, Error::Invalid)?;
            let admin = &accounts[0];
            let target = &accounts[1];
            let system = &accounts[2];
            signer(admin)?;
            require(
                admin.key.to_bytes() == initial_admin::INITIAL_ADMIN,
                Error::Unauthorized,
            )?;
            let relayer = Pubkey::new_from_array(payload.try_into().map_err(|_| Error::Invalid)?);
            require(
                relayer != Pubkey::default() && relayer != *admin.key,
                Error::Invalid,
            )?;
            let bump = address(target, program, &[b"config"])?;
            create(
                admin,
                target,
                system,
                program,
                &[b"config", &[bump]],
                CONFIG_LEN,
            )?;
            store(
                target,
                &Config {
                    admin: *admin.key,
                    relayer,
                    pending_admin: Pubkey::default(),
                }
                .encode(),
            )
        }
        1 => {
            require(accounts.len() == 4, Error::Invalid)?;
            let relay = &accounts[0];
            let conf = &accounts[1];
            let target = &accounts[2];
            let system = &accounts[3];
            signer(relay)?;
            require(
                config(conf, program)?.relayer == *relay.key,
                Error::Unauthorized,
            )?;
            let f = Forecast::register(payload, now_ms()?)?;
            let bump = address(target, program, &[b"forecast", &f.id])?;
            create(
                relay,
                target,
                system,
                program,
                &[b"forecast", &f.id, &[bump]],
                FORECAST_LEN,
            )?;
            store(target, &f.encode())?;
            solana_program::msg!("forecast_registered");
            Ok(())
        }
        2 => {
            // Its historical three-account ABI cannot observe the intake sidecar.
            Err(Error::LegacyAdvanceDisabled.into())
        }
        3..=5 => {
            require(accounts.len() == 2, Error::Invalid)?;
            let authority = &accounts[0];
            let target = &accounts[1];
            signer(authority)?;
            writable(target)?;
            let mut c = config(target, program)?;
            if tag == 5 {
                require(payload.is_empty(), Error::Invalid)?;
                require(
                    c.pending_admin != Pubkey::default() && *authority.key == c.pending_admin,
                    Error::Unauthorized,
                )?;
                c.admin = c.pending_admin;
                c.pending_admin = Pubkey::default();
            } else {
                require(*authority.key == c.admin, Error::Unauthorized)?;
                require(payload.len() == 32, Error::Invalid)?;
                let key = Pubkey::new_from_array(payload.try_into().map_err(|_| Error::Invalid)?);
                require(key != Pubkey::default() && key != c.admin, Error::Invalid)?;
                if tag == 3 {
                    require(key != c.pending_admin, Error::Invalid)?;
                    c.relayer = key;
                } else {
                    require(key != c.relayer, Error::Invalid)?;
                    c.pending_admin = key;
                }
            }
            store(target, &c.encode())
        }
        _ => Err(Error::Invalid.into()),
    }
}
