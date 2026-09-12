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
            8 => matches!(a.state, 5 | 9),
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
pub fn process_instruction(
    program: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let (&tag, payload) = data.split_first().ok_or(Error::Invalid)?;
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
            require(accounts.len() == 3, Error::Invalid)?;
            let relay = &accounts[0];
            let conf = &accounts[1];
            let target = &accounts[2];
            signer(relay)?;
            require(
                config(conf, program)?.relayer == *relay.key,
                Error::Unauthorized,
            )?;
            writable(target)?;
            owned(target, program, FORECAST_LEN)?;
            let f = Forecast::decode(&target.try_borrow_data()?)?;
            address(target, program, &[b"forecast", &f.id])?;
            let updated = f.advance(&Advance::decode(payload)?, now_ms()?)?;
            store(target, &updated.encode())?;
            solana_program::msg!("forecast_advanced");
            Ok(())
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
