//! Independent intake v1: the exact native ABI, and the commitments over it.
//!
//! Pure codecs. Decoding bytes proves nothing about them — an inclusion proof needs finalized
//! accounts from the configured program — so nothing here authenticates anything. What it does is
//! make the bytes exact, because a codec that is one byte out does not produce a slightly
//! different transaction: it produces a rejected one, or a *different valid instruction*.
//!
//! Three things carry that weight:
//!
//!   - **The reserved field and the genesis binding.** Every account is compared against `GENESIS`
//!     and a reserved run of seven NULs — the reference's `bytes(7)`, which is the count of zero
//!     bytes rather than a fill value, and which is easy to read the other way.
//!   - **The phase invariants.** A dormant accumulator is zero in six specific places, an open one
//!     has an epoch and a resolution, and a sealed one has a payload hash and a slot. The seam
//!     between "we might still assemble this" and "we have committed to it" is the type's whole
//!     purpose, so the invariants are the encoder's.
//!   - **The seal chain.** `encode_finalize` accepts a candidate only if it equals the sealed
//!     payload hash, revision, event and snapshot, which is what makes the seal a commitment to
//!     one candidate rather than to the act of sealing.
//!
//! The publication side lives in `crate::solana`: the config and forecast account decoders and
//! `encode_register`. Not yet ported from `solana_wire`: `assemble_transaction`, which needs the
//! signature map a signer owns.

use std::sync::OnceLock;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::solana::{self, raw, shortvec};

/// `(1 << 53) - 1`.
pub const MAX: i64 = 9_007_199_254_740_991;
pub const GATE_SEED: &[u8] = b"intake-v1";
pub const RECEIPT_SEED: &[u8] = b"dispute-v1";
pub const REVIEWER_SEED: &[u8] = b"intake-review-v1";
pub const GATE_SIZE: usize = 392;
pub const RECEIPT_HEADER: usize = 424;
pub const REVIEWER_SIZE: usize = 48;
pub const MAX_BODY: usize = 32_768;
pub const CHUNK: usize = 512;
pub const ZERO: [u8; 32] = [0u8; 32];
/// `SYSTEM`: the all-zero program id, which is how a system instruction is addressed.
pub const SYSTEM: [u8; 32] = ZERO;
/// Seven zero bytes: the reference's `bytes(7)`, which is a run of NULs rather than of
/// sevens. Reading it as `[7; 7]` produced a plausible-looking account that no validator accepts.
pub const RESERVED: [u8; 7] = [0u8; 7];

const GENESIS_TEXT: &str = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
/// `ComputeBudget111111111111111111111111111111`.
pub const COMPUTE_BUDGET_TEXT: &str = "ComputeBudget111111111111111111111111111111";

/// `GENESIS`, decoded once. A panic here is a build mistake, not input the caller could avoid.
pub fn genesis() -> &'static [u8; 32] {
    static GENESIS: OnceLock<[u8; 32]> = OnceLock::new();
    GENESIS.get_or_init(|| {
        let decoded = solana::base58_decode(GENESIS_TEXT, 32).expect("the devnet genesis decodes");
        let mut key = [0u8; 32];
        key.copy_from_slice(&decoded);
        key
    })
}

pub fn compute_budget() -> [u8; 32] {
    let decoded = solana::base58_decode(COMPUTE_BUDGET_TEXT, 32).expect("the compute budget id decodes");
    let mut key = [0u8; 32];
    key.copy_from_slice(&decoded);
    key
}

fn number(value: i64, maximum: i64) -> Result<i64, String> {
    if (0..=maximum).contains(&value) {
        Ok(value)
    } else {
        Err("invalid integer".to_string())
    }
}

fn key(value: &[u8], nonzero: bool) -> Result<Vec<u8>, String> {
    raw(value, 32, "hash/key", nonzero)
}

fn fixed(value: &[u8], size: usize) -> Result<Vec<u8>, String> {
    raw(value, size, "wire length", false)
}

/// Lowercase hex, which is how every commitment in this ABI is written on the wire and in
/// the vector.
pub fn to_hex(value: &[u8]) -> String {
    value.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn hash(domain: &[u8], data: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    hasher.update(data);
    hasher.finalize().into()
}

fn zeroed(value: &[u8]) -> bool {
    value.iter().all(|byte| *byte == 0)
}

/// `evidence_hash`.
pub fn evidence_hash(body: &[u8]) -> Result<[u8; 32], String> {
    if body.is_empty() || body.len() > MAX_BODY {
        return Err("invalid evidence length".to_string());
    }
    Ok(hash(b"forecast-intake-v1:evidence:", body))
}

fn array(value: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(value);
    out
}

pub fn gate_address(program: &[u8], forecast: &[u8]) -> Result<([u8; 32], u8), String> {
    solana::find_program_address(&key(program, true)?, &[GATE_SEED.to_vec(), key(forecast, true)?])
}

pub fn receipt_address(program: &[u8], forecast: &[u8], epoch: i64, user: &[u8]) -> Result<([u8; 32], u8), String> {
    if number(epoch, MAX)? <= 0 {
        return Err("invalid epoch".to_string());
    }
    solana::find_program_address(
        &key(program, true)?,
        &[
            RECEIPT_SEED.to_vec(),
            key(forecast, true)?,
            (epoch as u64).to_le_bytes().to_vec(),
            key(user, true)?,
        ],
    )
}

pub fn reviewer_address(program: &[u8]) -> Result<([u8; 32], u8), String> {
    solana::find_program_address(&key(program, true)?, &[REVIEWER_SEED.to_vec()])
}

fn put_u64(out: &mut Vec<u8>, value: u64) {
    out.extend(value.to_le_bytes());
}

fn put_i64(out: &mut Vec<u8>, value: i64) {
    out.extend(value.to_le_bytes());
}

fn put_u32(out: &mut Vec<u8>, value: u32) {
    out.extend(value.to_le_bytes());
}

fn put_u16(out: &mut Vec<u8>, value: u16) {
    out.extend(value.to_le_bytes());
}

fn read_u64(raw: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(raw[at..at + 8].try_into().unwrap_or([0; 8]))
}

fn read_u32(raw: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(raw[at..at + 4].try_into().unwrap_or([0; 4]))
}

fn read_i64(raw: &[u8], at: usize) -> i64 {
    i64::from_le_bytes(raw[at..at + 8].try_into().unwrap_or([0; 8]))
}

/// `Reviewer`: who may review, and at which revision of the reviewer set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reviewer {
    pub key: [u8; 32],
    pub revision: i64,
}

impl Reviewer {
    pub fn new(key_bytes: &[u8], revision: i64) -> Result<Self, String> {
        Ok(Self {
            key: array(&key(key_bytes, true)?),
            revision: {
                if number(revision, MAX)? <= 0 {
                    return Err("invalid reviewer revision".to_string());
                }
                revision
            },
        })
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(REVIEWER_SIZE);
        out.extend(b"FNJUDG01");
        out.extend(self.key);
        put_u64(&mut out, self.revision as u64);
        out
    }

    pub fn decode(raw_bytes: &[u8]) -> Result<Self, String> {
        let raw = fixed(raw_bytes, REVIEWER_SIZE)?;
        if &raw[0..8] != b"FNJUDG01" {
            return Err("invalid reviewer discriminator".to_string());
        }
        Self::new(&raw[8..40], read_u64(&raw, 40) as i64)
    }

    pub fn commitment(&self) -> [u8; 32] {
        hash(b"forecast-intake-v1:reviewer:", &self.encode())
    }
}

/// `Accumulator`: the per-forecast intake gate, in one of three phases.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accumulator {
    pub forecast: [u8; 32],
    pub specification: [u8; 32],
    pub epoch: i64,
    pub revision: i64,
    pub proposal_revision: i64,
    pub resolution: [u8; 32],
    pub proposal_event: [u8; 32],
    pub opened: i64,
    pub deadline: i64,
    pub pending: i64,
    pub material: i64,
    pub accepted: i64,
    pub head: [u8; 32],
    pub phase: i64,
    pub sealed_revision: i64,
    pub sealed_event: [u8; 32],
    pub sealed_snapshot: [u8; 32],
    pub sealed_payload: [u8; 32],
    pub sealed_at: i64,
    pub sealed_slot: i64,
}

pub struct AccumulatorParts<'a> {
    pub forecast: &'a [u8],
    pub specification: &'a [u8],
    pub epoch: i64,
    pub revision: i64,
    pub proposal_revision: i64,
    pub resolution: &'a [u8],
    pub proposal_event: &'a [u8],
    pub opened: i64,
    pub deadline: i64,
    pub pending: i64,
    pub material: i64,
    pub accepted: i64,
    pub head: &'a [u8],
    pub phase: i64,
}

impl Accumulator {
    /// The dormant accumulator: an epoch that has not opened.
    pub fn dormant(forecast: &[u8], specification: &[u8], head: &[u8]) -> Result<Self, String> {
        Self::new(AccumulatorParts {
            forecast,
            specification,
            epoch: 0,
            revision: 1,
            proposal_revision: 0,
            resolution: &ZERO,
            proposal_event: &ZERO,
            opened: 0,
            deadline: 0,
            pending: 0,
            material: 0,
            accepted: 0,
            head,
            phase: 0,
        })
    }

    #[allow(clippy::too_many_lines)]
    pub fn new(parts: AccumulatorParts<'_>) -> Result<Self, String> {
        let value = Self {
            forecast: array(&key(parts.forecast, true)?),
            specification: array(&key(parts.specification, true)?),
            head: array(&key(parts.head, true)?),
            resolution: array(&key(parts.resolution, false)?),
            proposal_event: array(&key(parts.proposal_event, false)?),
            epoch: number(parts.epoch, MAX)?,
            revision: number(parts.revision, MAX)?,
            proposal_revision: number(parts.proposal_revision, MAX)?,
            opened: number(parts.opened, MAX)?,
            deadline: number(parts.deadline, MAX)?,
            pending: number(parts.pending, MAX)?,
            material: number(parts.material, MAX)?,
            accepted: number(parts.accepted, MAX)?,
            phase: number(parts.phase, 3)?,
            sealed_revision: 0,
            sealed_event: ZERO,
            sealed_snapshot: ZERO,
            sealed_payload: ZERO,
            sealed_at: 0,
            sealed_slot: 0,
        };
        value.invariants()?;
        Ok(value)
    }

    /// The phase invariants. Everything that distinguishes "still assembling" from "committed".
    fn invariants(&self) -> Result<(), String> {
        if self.revision <= 0 || self.pending + self.material > self.accepted {
            return Err("invalid accumulator counters".to_string());
        }
        if self.phase == 0 {
            let dormant = (
                self.epoch,
                self.proposal_revision,
                self.opened,
                self.deadline,
                self.accepted,
            ) == (0, 0, 0, 0, 0)
                && zeroed(&self.resolution)
                && zeroed(&self.proposal_event);
            if !dormant {
                return Err("invalid dormant accumulator".to_string());
            }
        } else {
            let open = self.epoch > 0
                && self.proposal_revision > 0
                && !zeroed(&self.resolution)
                && !zeroed(&self.proposal_event)
                && self.opened < self.deadline;
            if !open {
                return Err("invalid epoch".to_string());
            }
        }
        if self.phase < 2 {
            let premature = (self.sealed_revision, self.sealed_at, self.sealed_slot) == (0, 0, 0)
                && zeroed(&self.sealed_event)
                && zeroed(&self.sealed_snapshot)
                && zeroed(&self.sealed_payload);
            if !premature {
                return Err("premature seal".to_string());
            }
        } else {
            let sealed = self.pending == 0
                && self.material == 0
                && self.sealed_revision > self.proposal_revision
                && !zeroed(&self.sealed_event)
                && !zeroed(&self.sealed_snapshot)
                && !zeroed(&self.sealed_payload)
                && self.sealed_at > self.deadline
                && self.sealed_slot > 0;
            if !sealed {
                return Err("invalid seal".to_string());
            }
        }
        Ok(())
    }

    /// `seal`: bind this accumulator to one candidate. The caller supplies the candidate's own
    /// revision, event, snapshot and payload hash, which `encode_finalize` later re-checks.
    pub fn sealed(
        &self,
        revision: i64,
        event: &[u8],
        snapshot: &[u8],
        payload: &[u8],
        at: i64,
        slot: i64,
    ) -> Result<Self, String> {
        let mut value = self.clone();
        value.phase = 2;
        value.pending = 0;
        value.material = 0;
        value.sealed_revision = number(revision, MAX)?;
        value.sealed_event = array(&key(event, true)?);
        value.sealed_snapshot = array(&key(snapshot, true)?);
        value.sealed_payload = array(&key(payload, true)?);
        value.sealed_at = number(at, MAX)?;
        value.sealed_slot = number(slot, MAX)?;
        value.invariants()?;
        Ok(value)
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(GATE_SIZE);
        out.extend(b"FNINTK01");
        out.extend(self.forecast);
        out.extend(self.specification);
        out.extend(genesis());
        put_u64(&mut out, self.epoch as u64);
        put_u64(&mut out, self.revision as u64);
        put_u64(&mut out, self.proposal_revision as u64);
        out.extend(self.resolution);
        out.extend(self.proposal_event);
        put_i64(&mut out, self.opened);
        put_i64(&mut out, self.deadline);
        put_u64(&mut out, self.pending as u64);
        put_u64(&mut out, self.material as u64);
        put_u64(&mut out, self.accepted as u64);
        out.extend(self.head);
        out.push(self.phase as u8);
        out.extend(RESERVED);
        put_u64(&mut out, self.sealed_revision as u64);
        out.extend(self.sealed_event);
        out.extend(self.sealed_snapshot);
        out.extend(self.sealed_payload);
        put_i64(&mut out, self.sealed_at);
        put_u64(&mut out, self.sealed_slot as u64);
        out
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, String> {
        let raw = fixed(bytes, GATE_SIZE)?;
        let legitimate = &raw[0..8] == b"FNINTK01" && raw[72..104] == *genesis() && raw[265..272] == RESERVED;
        if !legitimate {
            return Err("invalid gate scope/reserved".to_string());
        }
        let value = Self {
            forecast: array(&raw[8..40]),
            specification: array(&raw[40..72]),
            epoch: read_u64(&raw, 104) as i64,
            revision: read_u64(&raw, 112) as i64,
            proposal_revision: read_u64(&raw, 120) as i64,
            resolution: array(&raw[128..160]),
            proposal_event: array(&raw[160..192]),
            opened: read_i64(&raw, 192),
            deadline: read_i64(&raw, 200),
            pending: read_u64(&raw, 208) as i64,
            material: read_u64(&raw, 216) as i64,
            accepted: read_u64(&raw, 224) as i64,
            head: array(&raw[232..264]),
            phase: raw[264] as i64,
            sealed_revision: read_u64(&raw, 272) as i64,
            sealed_event: array(&raw[280..312]),
            sealed_snapshot: array(&raw[312..344]),
            sealed_payload: array(&raw[344..376]),
            sealed_at: read_i64(&raw, 376),
            sealed_slot: read_u64(&raw, 384) as i64,
        };
        value.invariants()?;
        Ok(value)
    }

    pub fn commitment(&self) -> [u8; 32] {
        hash(b"forecast-intake-v1:accumulator:", &self.encode())
    }
}

/// `Receipt`: one dispute, from draft through acceptance to review.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Receipt {
    pub forecast: [u8; 32],
    pub specification: [u8; 32],
    pub program: [u8; 32],
    pub epoch: i64,
    pub proposal_revision: i64,
    pub resolution: [u8; 32],
    pub proposal_event: [u8; 32],
    pub user: [u8; 32],
    pub nonce: [u8; 32],
    pub evidence: [u8; 32],
    pub body_length: i64,
    pub body: Vec<u8>,
    pub status: i64,
    pub accepted_at: i64,
    pub accepted_slot: i64,
    pub deadline: i64,
    pub review: [u8; 32],
    pub reviewer: [u8; 32],
    pub reviewed_at: i64,
}

pub struct ReceiptParts<'a> {
    pub forecast: &'a [u8],
    pub specification: &'a [u8],
    pub program: &'a [u8],
    pub epoch: i64,
    pub proposal_revision: i64,
    pub resolution: &'a [u8],
    pub proposal_event: &'a [u8],
    pub user: &'a [u8],
    pub nonce: &'a [u8],
    pub body: &'a [u8],
}

impl Receipt {
    pub fn new(parts: ReceiptParts<'_>) -> Result<Self, String> {
        let value = Self {
            forecast: array(&key(parts.forecast, true)?),
            specification: array(&key(parts.specification, true)?),
            program: array(&key(parts.program, true)?),
            resolution: array(&key(parts.resolution, true)?),
            proposal_event: array(&key(parts.proposal_event, true)?),
            user: array(&key(parts.user, true)?),
            nonce: array(&key(parts.nonce, true)?),
            evidence: evidence_hash(parts.body)?,
            body_length: parts.body.len() as i64,
            body: parts.body.to_vec(),
            epoch: number(parts.epoch, MAX)?,
            proposal_revision: number(parts.proposal_revision, MAX)?,
            status: 0,
            accepted_at: 0,
            accepted_slot: 0,
            deadline: 0,
            review: ZERO,
            reviewer: ZERO,
            reviewed_at: 0,
        };
        value.invariants()?;
        Ok(value)
    }

    fn invariants(&self) -> Result<(), String> {
        number(self.epoch, MAX)?;
        number(self.proposal_revision, MAX)?;
        number(self.accepted_at, MAX)?;
        number(self.accepted_slot, MAX)?;
        number(self.deadline, MAX)?;
        number(self.reviewed_at, MAX)?;
        number(self.status, 3)?;
        let scoped = self.epoch > 0
            && self.proposal_revision > 0
            && number(self.body_length, MAX_BODY as i64)? > 0
            && self.body.len() <= self.body_length as usize;
        if !scoped {
            return Err("invalid receipt body/scope".to_string());
        }
        if self.status == 0 {
            if (self.accepted_at, self.accepted_slot, self.deadline) != (0, 0, 0) {
                return Err("unsubmitted receipt time".to_string());
            }
        } else {
            let accepted = self.body.len() == self.body_length as usize
                && evidence_hash(&self.body).ok() == Some(self.evidence)
                && self.accepted_slot > 0
                && self.accepted_at <= self.deadline;
            if !accepted {
                return Err("invalid accepted receipt".to_string());
            }
        }
        if self.status < 2 {
            let premature = zeroed(&self.review) && zeroed(&self.reviewer) && self.reviewed_at == 0;
            if !premature {
                return Err("premature review".to_string());
            }
        } else {
            let reviewed = !zeroed(&self.review) && !zeroed(&self.reviewer) && self.reviewed_at >= self.accepted_at;
            if !reviewed {
                return Err("invalid review".to_string());
            }
        }
        Ok(())
    }

    /// `accept`: the transition from draft to a submitted receipt.
    pub fn accepted(&self, at: i64, slot: i64, deadline: i64) -> Result<Self, String> {
        let mut value = self.clone();
        value.status = 1;
        value.accepted_at = number(at, MAX)?;
        value.accepted_slot = number(slot, MAX)?;
        value.deadline = number(deadline, MAX)?;
        value.invariants()?;
        Ok(value)
    }

    /// `review`: the disposition a reviewer recorded, and who recorded it.
    pub fn reviewed(&self, review: &[u8], reviewer: &[u8], at: i64) -> Result<Self, String> {
        let mut value = self.clone();
        value.status = 2;
        value.review = array(&key(review, true)?);
        value.reviewer = array(&key(reviewer, true)?);
        value.reviewed_at = number(at, MAX)?;
        value.invariants()?;
        Ok(value)
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(RECEIPT_HEADER + self.body.len());
        out.extend(b"FNDRCP01");
        out.extend(self.forecast);
        out.extend(self.specification);
        out.extend(genesis());
        out.extend(self.program);
        put_u64(&mut out, self.epoch as u64);
        put_u64(&mut out, self.proposal_revision as u64);
        out.extend(self.resolution);
        out.extend(self.proposal_event);
        out.extend(self.user);
        out.extend(self.nonce);
        out.extend(self.evidence);
        put_u32(&mut out, self.body_length as u32);
        put_u32(&mut out, self.body.len() as u32);
        out.push(self.status as u8);
        out.extend(RESERVED);
        put_i64(&mut out, self.accepted_at);
        put_u64(&mut out, self.accepted_slot as u64);
        put_i64(&mut out, self.deadline);
        out.extend(self.review);
        out.extend(self.reviewer);
        put_i64(&mut out, self.reviewed_at);
        out.extend(&self.body);
        out
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, String> {
        if bytes.len() < RECEIPT_HEADER || bytes.len() > RECEIPT_HEADER + MAX_BODY {
            return Err("receipt length".to_string());
        }
        let raw = bytes;
        let legitimate = &raw[0..8] == b"FNDRCP01"
            && raw[72..104] == *genesis()
            && raw[321..328] == RESERVED
            && read_u32(raw, 316) as usize == raw.len() - RECEIPT_HEADER;
        if !legitimate {
            return Err("invalid receipt scope/length/reserved".to_string());
        }
        let value = Self {
            forecast: array(&raw[8..40]),
            specification: array(&raw[40..72]),
            program: array(&raw[104..136]),
            epoch: read_u64(raw, 136) as i64,
            proposal_revision: read_u64(raw, 144) as i64,
            resolution: array(&raw[152..184]),
            proposal_event: array(&raw[184..216]),
            user: array(&raw[216..248]),
            nonce: array(&raw[248..280]),
            evidence: array(&raw[280..312]),
            body_length: read_u32(raw, 312) as i64,
            body: raw[RECEIPT_HEADER..].to_vec(),
            status: raw[320] as i64,
            accepted_at: read_i64(raw, 328),
            accepted_slot: read_u64(raw, 336) as i64,
            deadline: read_i64(raw, 344),
            review: array(&raw[352..384]),
            reviewer: array(&raw[384..416]),
            reviewed_at: read_i64(raw, 416),
        };
        value.invariants()?;
        Ok(value)
    }

    pub fn commitment(&self) -> [u8; 32] {
        hash(b"forecast-intake-v1:receipt:", &self.encode())
    }
}

/// `encode_reviewer`.
pub fn encode_reviewer(key_bytes: &[u8], previous: Option<&Reviewer>) -> Result<Vec<u8>, String> {
    let mut out = vec![0x0e];
    put_u64(&mut out, previous.map_or(0, |value| value.revision as u64));
    out.extend(previous.map_or(ZERO, Reviewer::commitment));
    out.extend(key(key_bytes, true)?);
    Ok(out)
}

/// `encode_activate`.
pub fn encode_activate(revision: i64, event_hash: &[u8], specification_hash: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = vec![0x06];
    put_u64(&mut out, number(revision, MAX)? as u64);
    out.extend(key(event_hash, true)?);
    out.extend(key(specification_hash, true)?);
    Ok(out)
}

/// `_advance`: the original 254-byte advance body, without the 7 it was rebranded with.
fn advance_body(data: &[u8]) -> Result<Vec<u8>, String> {
    fixed(data, 255)?;
    if data[0] != 2 {
        return Err("expected original Advance encoding".to_string());
    }
    Ok(data[1..].to_vec())
}

/// `encode_advance`: an advance, with the extension that says which phase it belongs to.
pub fn encode_advance(data: &[u8], mode: i64, artifact: &[u8], head: &[u8]) -> Result<Vec<u8>, String> {
    let mode = number(mode, 2)?;
    let body = advance_body(data)?;
    if body[112] == 10 {
        return Err("finalization requires a seal".to_string());
    }
    let mut out = vec![7, mode as u8];
    out.extend(&body);
    match mode {
        1 => {
            out.extend(key(artifact, true)?);
            out.extend(key(head, true)?);
        }
        2 => out.extend(key(artifact, true)?),
        _ => {
            if !zeroed(artifact) || !zeroed(head) {
                return Err("unexpected advance extension".to_string());
            }
        }
    }
    Ok(out)
}

/// `encode_draft`.
pub fn encode_draft(gate: &Accumulator, nonce: &[u8], body: &[u8]) -> Result<Vec<u8>, String> {
    if gate.phase != 1 {
        return Err("epoch not open".to_string());
    }
    let mut out = vec![0x08];
    put_u64(&mut out, gate.epoch as u64);
    put_u64(&mut out, gate.proposal_revision as u64);
    out.extend(gate.specification);
    out.extend(gate.resolution);
    out.extend(gate.proposal_event);
    out.extend(key(nonce, true)?);
    out.extend(evidence_hash(body)?);
    put_u32(&mut out, body.len() as u32);
    Ok(out)
}

/// `encode_append`: one chunk of an evidence body, at an offset.
pub fn encode_append(offset: i64, chunk: &[u8]) -> Result<Vec<u8>, String> {
    let offset = number(offset, MAX_BODY as i64)?;
    if chunk.is_empty() || chunk.len() > CHUNK || offset as usize + chunk.len() > MAX_BODY {
        return Err("invalid chunk".to_string());
    }
    let mut out = vec![0x09];
    put_u32(&mut out, offset as u32);
    put_u16(&mut out, chunk.len() as u16);
    out.extend(chunk);
    Ok(out)
}

/// `encode_submit`.
pub fn encode_submit(receipt: &Receipt) -> Result<Vec<u8>, String> {
    let complete = receipt.status == 0
        && receipt.body.len() == receipt.body_length as usize
        && evidence_hash(&receipt.body).ok() == Some(receipt.evidence);
    if !complete {
        return Err("incomplete or already accepted receipt".to_string());
    }
    let mut out = vec![0x0a];
    put_u64(&mut out, receipt.epoch as u64);
    out.extend(receipt.nonce);
    out.extend(receipt.evidence);
    Ok(out)
}

/// `encode_review`: the disposition, bound to the receipt it reviews.
pub fn encode_review(receipt: &Receipt, artifact: &[u8], disposition: i64) -> Result<Vec<u8>, String> {
    if receipt.status != 1 || !(disposition == 2 || disposition == 3) {
        return Err("invalid review".to_string());
    }
    let mut out = vec![0x0b];
    put_u64(&mut out, receipt.epoch as u64);
    out.extend(receipt.commitment());
    out.extend(key(artifact, true)?);
    out.push(disposition as u8);
    Ok(out)
}

/// `advance_hash`: the commitment a seal records.
pub fn advance_hash(data: &[u8]) -> Result<[u8; 32], String> {
    Ok(hash(b"forecast-intake-v1:advance:", &advance_body(data)?))
}

/// `encode_seal`: bind an open epoch to one candidate.
pub fn encode_seal(gate: &Accumulator, data: &[u8]) -> Result<Vec<u8>, String> {
    let body = advance_body(data)?;
    if !(gate.phase == 1 && gate.pending == 0 && gate.material == 0 && body[112] == 10) {
        return Err("cannot seal".to_string());
    }
    let mut out = vec![0x0c];
    out.extend(&body);
    put_u64(&mut out, gate.revision as u64);
    out.extend(gate.commitment());
    Ok(out)
}

/// `encode_finalize`: publish the candidate the seal committed to, and only that one.
pub fn encode_finalize(gate: &Accumulator, data: &[u8]) -> Result<Vec<u8>, String> {
    let body = advance_body(data)?;
    let matches = gate.phase == 2
        && advance_hash(data).ok() == Some(gate.sealed_payload)
        && body[112] == 10
        && read_u64(&body, 0) as i64 == gate.sealed_revision
        && body[48..80] == gate.sealed_event
        && body[80..112] == gate.sealed_snapshot;
    if !matches {
        return Err("candidate differs from seal".to_string());
    }
    let mut out = vec![0x0d];
    out.extend(&body);
    out.extend(gate.commitment());
    Ok(out)
}

/// A shortvec of `value`, exposed here because the instruction encoders are where it is used.
pub fn compact(value: i64) -> Result<Vec<u8>, String> {
    shortvec(value)
}

#[cfg(test)]
mod tests_support {
    pub use super::*;
    pub use serde_json::Value;

    pub fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/solana-wire-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("solana golden")).expect("json")
    }

    pub fn from_hex(text: &str) -> Vec<u8> {
        (0..text.len() / 2)
            .map(|index| u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).expect("hex"))
            .collect()
    }

    pub use super::to_hex;

    pub const PROGRAM: [u8; 32] = [10u8; 32];
    pub const FORECAST: [u8; 32] = [7u8; 32];
    pub const SPECIFICATION: [u8; 32] = [3u8; 32];
    pub const RESOLUTION: [u8; 32] = [6u8; 32];
    pub const EVENT: [u8; 32] = [8u8; 32];
    pub const USER: [u8; 32] = [11u8; 32];
    pub const NONCE: [u8; 32] = [12u8; 32];
    pub const ARTIFACT: [u8; 32] = [12u8; 32];
    pub const HEAD: [u8; 32] = [9u8; 32];
    pub const BODY: &[u8] = b"forecast evidence bodyforecast evidence bodyforecast evidence bodyforecast evidence body";

    pub fn open_gate() -> Accumulator {
        Accumulator::new(AccumulatorParts {
            forecast: &FORECAST,
            specification: &SPECIFICATION,
            epoch: 1,
            revision: 1,
            proposal_revision: 3,
            resolution: &RESOLUTION,
            proposal_event: &EVENT,
            opened: 30,
            deadline: 100,
            pending: 0,
            material: 0,
            accepted: 0,
            head: &HEAD,
            phase: 1,
        })
        .expect("an open gate")
    }

    /// The advance body the reference builds for its fixtures.
    pub fn advance_bytes(state: i64, revision: i64) -> Vec<u8> {
        let mut out = vec![2u8];
        put_u64(&mut out, revision as u64);
        put_i64(&mut out, 101);
        out.extend([4u8; 32]);
        out.extend([13u8; 32]);
        out.extend([14u8; 32]);
        out.push(state as u8);
        out.push(1);
        out.extend(RESOLUTION);
        out.extend(ZERO);
        out.extend([15u8; 32]);
        out.extend(ZERO);
        put_i64(&mut out, 100);
        put_u16(&mut out, 0);
        put_u16(&mut out, 0);
        out
    }

    pub fn sealed_gate() -> Accumulator {
        let payload = advance_hash(&advance_bytes(10, 5)).expect("a payload hash");
        open_gate()
            .sealed(5, &[13u8; 32], &[14u8; 32], &payload, 101, 42)
            .expect("a sealed gate")
    }

    pub fn receipt(body: &[u8], status: i64) -> Receipt {
        let draft = Receipt::new(ReceiptParts {
            forecast: &FORECAST,
            specification: &SPECIFICATION,
            program: &PROGRAM,
            epoch: 1,
            proposal_revision: 3,
            resolution: &RESOLUTION,
            proposal_event: &EVENT,
            user: &USER,
            nonce: &NONCE,
            body,
        })
        .expect("a draft receipt");
        match status {
            0 => draft,
            1 => draft.accepted(50, 9, 100).expect("an accepted receipt"),
            _ => draft
                .accepted(50, 9, 100)
                .and_then(|value| value.reviewed(&[17u8; 32], &[11u8; 32], 60))
                .expect("a reviewed receipt"),
        }
    }

    #[test]
    fn the_published_commitments_are_reproduced() {
        // These four are what `tests/test_dispute_wire.py` already claims about the on-chain ABI.
        // A port that disagrees with them is wrong regardless of anything else in this vector.
        let document = golden();
        let expected = &document["pinned"]["expected"];
        assert_eq!(to_hex(&open_gate().commitment()), expected["gate"].as_str().unwrap());
        assert_eq!(
            to_hex(&receipt(b"*", 0).commitment()),
            expected["receipt"].as_str().unwrap()
        );
        assert_eq!(
            to_hex(&Reviewer::new(&[11u8; 32], 1).unwrap().commitment()),
            expected["reviewer"].as_str().unwrap()
        );
        assert_eq!(
            to_hex(&advance_hash(&advance_bytes(10, 5)).unwrap()),
            expected["advance"].as_str().unwrap()
        );
    }

    #[test]
    fn every_account_layout_encodes_and_round_trips() {
        let document = golden();
        let encoded = &document["encoded"];
        for (name, value) in [
            ("open", open_gate()),
            ("sealed", sealed_gate()),
            (
                "dormant",
                Accumulator::dormant(&FORECAST, &SPECIFICATION, &HEAD).unwrap(),
            ),
        ] {
            let bytes = value.encode();
            let expected = &encoded["gate"][name];
            assert_eq!(to_hex(&bytes), expected["bytes"].as_str().unwrap(), "gate {name} bytes");
            assert_eq!(
                to_hex(&value.commitment()),
                expected["commitment"].as_str().unwrap(),
                "gate {name} commitment"
            );
            assert_eq!(
                bytes.len(),
                expected["length"].as_u64().unwrap() as usize,
                "gate {name} length"
            );
            assert_eq!(
                to_hex(&Accumulator::decode(&bytes).expect("a decode").encode()),
                to_hex(&bytes),
                "gate {name} round-trip"
            );
        }
        for (name, status) in [("draft", 0), ("accepted", 1), ("reviewed", 2)] {
            let value = receipt(BODY, status);
            let bytes = value.encode();
            let expected = &encoded["receipt"][name];
            assert_eq!(
                to_hex(&bytes),
                expected["bytes"].as_str().unwrap(),
                "receipt {name} bytes"
            );
            assert_eq!(
                to_hex(&value.commitment()),
                expected["commitment"].as_str().unwrap(),
                "receipt {name} commitment"
            );
            assert_eq!(
                to_hex(&Receipt::decode(&bytes).expect("a decode").encode()),
                to_hex(&bytes),
                "receipt {name} round-trip"
            );
        }
        let reviewer = Reviewer::new(&[11u8; 32], 1).unwrap();
        assert_eq!(
            to_hex(&reviewer.encode()),
            encoded["reviewer"]["bytes"].as_str().unwrap(),
            "reviewer bytes"
        );
        assert_eq!(
            to_hex(&Reviewer::decode(&reviewer.encode()).unwrap().encode()),
            to_hex(&reviewer.encode()),
            "reviewer round-trip"
        );
    }

    #[test]
    fn every_address_and_instruction_matches_the_reference() {
        let document = golden();
        let addresses = &document["addresses"];
        for (name, pair) in [
            ("gate", gate_address(&PROGRAM, &FORECAST).unwrap()),
            ("receipt", receipt_address(&PROGRAM, &FORECAST, 1, &USER).unwrap()),
            ("reviewer", reviewer_address(&PROGRAM).unwrap()),
            ("config", solana::config_address(&PROGRAM).unwrap()),
            ("forecast", solana::forecast_address(&PROGRAM, &FORECAST).unwrap()),
        ] {
            assert_eq!(
                to_hex(&pair.0),
                addresses[name]["address"].as_str().unwrap(),
                "{name} address"
            );
            assert_eq!(pair.1 as u64, addresses[name]["bump"].as_u64().unwrap(), "{name} bump");
        }

        let gate = open_gate();
        let mut instructions = std::collections::BTreeMap::new();
        instructions.insert(
            "reviewer",
            encode_reviewer(&[11u8; 32], Some(&Reviewer::new(&[11u8; 32], 1).unwrap())),
        );
        instructions.insert("reviewer_first", encode_reviewer(&[11u8; 32], None));
        instructions.insert("activate", encode_activate(5, &[13u8; 32], &SPECIFICATION));
        instructions.insert("advance", encode_advance(&advance_bytes(9, 5), 0, &ZERO, &ZERO));
        instructions.insert(
            "advance_mode1",
            encode_advance(&advance_bytes(9, 5), 1, &ARTIFACT, &HEAD),
        );
        instructions.insert(
            "advance_mode2",
            encode_advance(&advance_bytes(9, 5), 2, &ARTIFACT, &ZERO),
        );
        instructions.insert("draft", encode_draft(&gate, &NONCE, BODY));
        instructions.insert("append", encode_append(0, b"chunk"));
        instructions.insert("submit", encode_submit(&receipt(BODY, 0)));
        instructions.insert("review", encode_review(&receipt(BODY, 1), &ARTIFACT, 2));
        instructions.insert("seal", encode_seal(&gate, &advance_bytes(10, 5)));
        instructions.insert("finalize", encode_finalize(&sealed_gate(), &advance_bytes(10, 5)));
        for (name, produced) in instructions {
            assert_eq!(
                to_hex(&produced.expect("an instruction")),
                document["instructions"][name].as_str().unwrap(),
                "{name}"
            );
        }
    }

    #[test]
    fn the_seal_chain_only_finalizes_the_candidate_it_sealed() {
        // The seal is a commitment to one candidate, not to the act of sealing: a candidate that
        // differs in revision, event or state is refused by name.
        let document = golden();
        let sealed = sealed_gate();
        let candidates = [
            ("exact", advance_bytes(10, 5)),
            ("different_revision", advance_bytes(10, 7)),
            ("not_final", advance_bytes(9, 5)),
        ];
        for (name, data) in candidates {
            let expected = document["sealChain"]
                .as_array()
                .unwrap()
                .iter()
                .find(|case| case["name"] == format!("finalize:{name}"))
                .unwrap();
            match encode_finalize(&sealed, &data) {
                Ok(bytes) => assert_eq!(to_hex(&bytes), expected["result"].as_str().unwrap(), "{name}"),
                Err(error) => assert_eq!(Some(error.as_str()), expected["error"].as_str(), "{name}"),
            }
        }
    }

    #[test]
    fn every_invariant_refuses_what_the_reference_refuses() {
        let document = golden();
        let cases = document["refusals"].as_array().expect("refusals");
        assert!(cases.len() >= 20, "the corpus lost its breadth");
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let produced: Result<Vec<u8>, String> = match name {
                "gate:dormant_with_resolution" => {
                    let mut gate = open_gate();
                    gate.phase = 0;
                    Accumulator::new(AccumulatorParts {
                        forecast: &gate.forecast,
                        specification: &gate.specification,
                        epoch: gate.epoch,
                        revision: gate.revision,
                        proposal_revision: gate.proposal_revision,
                        resolution: &gate.resolution,
                        proposal_event: &gate.proposal_event,
                        opened: gate.opened,
                        deadline: gate.deadline,
                        pending: gate.pending,
                        material: gate.material,
                        accepted: gate.accepted,
                        head: &gate.head,
                        phase: 0,
                    })
                    .map(|value| value.encode())
                }
                // The reference mutates one field and re-runs the type's own validation, which is
                // the point: the invariant is the encoder's, not the constructor's.
                "gate:premature_seal" => {
                    let mut gate = open_gate();
                    gate.sealed_revision = 6;
                    gate.invariants().map(|()| gate.encode())
                }
                "gate:sealed_without_payload" => {
                    let mut gate = sealed_gate();
                    gate.sealed_payload = ZERO;
                    gate.invariants().map(|()| gate.encode())
                }
                "receipt:unsubmitted_time" => {
                    let mut value = receipt(BODY, 0);
                    value.accepted_at = 5;
                    value.invariants().map(|()| value.encode())
                }
                "receipt:evidence_mismatch" => {
                    let mut value = receipt(BODY, 1);
                    value.evidence = [1u8; 32];
                    value.invariants().map(|()| value.encode())
                }
                "receipt:premature_review" => {
                    let mut value = receipt(BODY, 1);
                    value.review = [1u8; 32];
                    value.invariants().map(|()| value.encode())
                }
                "evidence:empty" => evidence_hash(b"").map(|value| value.to_vec()),
                "evidence:oversized" => evidence_hash(&vec![b'x'; MAX_BODY + 1]).map(|value| value.to_vec()),
                "append:oversized_chunk" => encode_append(0, &vec![b'x'; CHUNK + 1]),
                "append:past_the_end" => encode_append((MAX_BODY - 1) as i64, b"xx"),
                "review:disposition" => encode_review(&receipt(BODY, 1), &ARTIFACT, 4),
                "submit:already_accepted" => encode_submit(&receipt(BODY, 1)),
                "advance:final_without_seal" => encode_advance(&advance_bytes(10, 5), 0, &ZERO, &ZERO),
                "advance:unexpected_extension" => encode_advance(&advance_bytes(9, 5), 0, &ARTIFACT, &ZERO),
                "draft:epoch_not_open" => encode_draft(&sealed_gate(), &NONCE, BODY),
                "seal:cannot_seal" => encode_seal(&sealed_gate(), &advance_bytes(10, 5)),
                "pda:too_many_seeds" => {
                    solana::find_program_address(&PROGRAM, &vec![b"s".to_vec(); 16]).map(|(a, b)| {
                        let mut out = a.to_vec();
                        out.push(b);
                        out
                    })
                }
                "pda:seed_too_long" => solana::find_program_address(&PROGRAM, &[vec![b's'; 33]]).map(|(a, b)| {
                    let mut out = a.to_vec();
                    out.push(b);
                    out
                }),
                "base58:invalid_length" => solana::base58_decode("1", 0),
                "base58:bad_character" => solana::base58_decode("0OIl", 32),
                other => panic!("unhandled refusal case {other}"),
            };
            assert!(produced.is_err(), "{name}: accepted where the reference refused");
            assert_eq!(
                produced.unwrap_err(),
                case["error"].as_str().unwrap(),
                "{name}: a different refusal"
            );
        }
    }

    #[test]
    fn the_wire_primitives_match_the_reference() {
        let document = golden();
        for entry in document["base58"].as_array().expect("base58") {
            let raw = from_hex(entry["bytes"].as_str().unwrap());
            let text = solana::base58_encode(&raw);
            assert_eq!(text, entry["text"].as_str().unwrap(), "base58 encode");
            assert_eq!(
                to_hex(&solana::base58_decode(&text, raw.len()).unwrap()),
                entry["roundtrip"].as_str().unwrap(),
                "base58 decode"
            );
        }
        for entry in document["shortvecs"].as_array().expect("shortvecs") {
            assert_eq!(
                to_hex(&solana::shortvec(entry["value"].as_i64().unwrap()).unwrap()),
                entry["bytes"].as_str().unwrap(),
                "shortvec {}",
                entry["value"]
            );
        }
        for entry in document["shortvecRefusals"].as_array().expect("shortvec refusals") {
            let value = entry["name"]
                .as_str()
                .unwrap()
                .trim_start_matches("shortvec:")
                .parse()
                .unwrap();
            assert_eq!(
                solana::shortvec(value).unwrap_err(),
                entry["error"].as_str().unwrap(),
                "{}",
                entry["name"]
            );
        }
        for entry in document["points"].as_array().expect("points") {
            let raw = from_hex(entry["bytes"].as_str().unwrap());
            assert_eq!(
                solana::is_edwards_point(&raw).unwrap(),
                entry["on_curve"].as_bool().unwrap(),
                "point {}",
                entry["bytes"]
            );
        }
        assert_eq!(to_hex(genesis()), document["genesis"].as_str().unwrap());
    }

    #[test]
    fn a_message_is_compiled_in_the_order_the_runtime_requires() {
        let document = golden();
        let submit = encode_submit(&receipt(BODY, 0)).expect("a submit");
        let instructions = vec![
            solana::Instruction::new(
                &PROGRAM,
                vec![
                    solana::AccountMeta::new(&USER, true, true).unwrap(),
                    solana::AccountMeta::new(&FORECAST, false, true).unwrap(),
                ],
                submit,
            )
            .unwrap(),
            solana::Instruction::new(
                &SYSTEM,
                vec![
                    solana::AccountMeta::new(&USER, true, true).unwrap(),
                    solana::AccountMeta::new(&FORECAST, false, true).unwrap(),
                ],
                vec![2],
            )
            .unwrap(),
        ];
        let compiled = solana::compile_message(&USER, &HEAD, &instructions).expect("a message");
        assert_eq!(
            to_hex(&compiled.data),
            document["message"]["data"].as_str().unwrap(),
            "message data"
        );
        assert_eq!(
            compiled
                .signer_keys
                .iter()
                .map(|key| to_hex(key))
                .collect::<Vec<String>>(),
            document["message"]["signer_keys"]
                .as_array()
                .unwrap()
                .iter()
                .map(|key| key.as_str().unwrap().to_string())
                .collect::<Vec<String>>(),
            "signer keys"
        );
        assert_eq!(
            compiled.account_keys.len(),
            document["message"]["account_keys"].as_array().unwrap().len()
        );
    }
}

#[cfg(test)]
mod publication {
    use super::tests_support::*;
    use crate::solana;

    #[test]
    fn every_publication_encoding_matches_the_reference() {
        let document = golden();
        let publication = &document["publication"];
        let produced = [
            ("initialize", solana::encode_initialize(&[7u8; 32]).unwrap()),
            ("set_relayer", solana::encode_set_relayer(&[8u8; 32]).unwrap()),
            ("propose_admin", solana::encode_propose_admin(&[9u8; 32]).unwrap()),
            ("accept_admin", solana::encode_accept_admin()),
            (
                "register",
                solana::encode_register(solana::RegisterParts {
                    forecast_id_hash: &[7u8; 32],
                    creator_hash: &[5u8; 32],
                    specification_hash: &[3u8; 32],
                    open_at_ms: 1,
                    close_at_ms: 100,
                    revision: 1,
                    occurred_at_ms: 50,
                    event_hash: &[13u8; 32],
                    snapshot_hash: &[14u8; 32],
                })
                .unwrap(),
            ),
        ];
        for (name, bytes) in produced {
            assert_eq!(to_hex(&bytes), publication[name].as_str().unwrap(), "{name}");
        }
    }

    #[test]
    fn the_config_account_reads_back_and_refuses_a_broken_separation() {
        let document = golden();
        let fixture = from_hex(document["accounts"]["config"].as_str().unwrap());
        assert_eq!(to_hex(&fixture), document["accounts"]["config"].as_str().unwrap());
        let config = solana::decode_config(&fixture).expect("a config");
        assert_eq!(
            to_hex(&config.administrator),
            document["decoded"]["config"]["administrator"].as_str().unwrap()
        );
        assert_eq!(
            to_hex(&config.relayer),
            document["decoded"]["config"]["relayer"].as_str().unwrap()
        );
        for case in document["accountRefusals"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            if !name.starts_with("config:") {
                continue;
            }
            let produced = match name {
                "config:zero_administrator" => {
                    let mut broken = fixture.clone();
                    broken[8..40].fill(0);
                    solana::decode_config(&broken)
                }
                "config:same_authority" => {
                    let mut broken = fixture.clone();
                    let administrator = broken[8..40].to_vec();
                    broken[40..72].copy_from_slice(&administrator);
                    solana::decode_config(&broken)
                }
                _ => {
                    let mut broken = fixture.clone();
                    let administrator = broken[8..40].to_vec();
                    broken[72..104].copy_from_slice(&administrator);
                    solana::decode_config(&broken)
                }
            };
            assert_eq!(produced.unwrap_err(), case["error"].as_str().unwrap(), "{name}");
        }
    }

    #[test]
    fn the_forecast_account_reads_back_and_upholds_its_cross_field_rules() {
        // An inclusion proof is only worth what the decoder that accepted the account is worth.
        let document = golden();
        let fixture = from_hex(document["accounts"]["forecast"].as_str().unwrap());
        let forecast = solana::decode_forecast(&fixture).expect("a forecast");
        assert_eq!(
            forecast.state,
            document["decoded"]["forecast"]["state"].as_i64().unwrap()
        );
        assert_eq!(
            forecast.close_at_ms,
            document["decoded"]["forecast"]["close_at_ms"].as_i64().unwrap()
        );
        assert_eq!(
            to_hex(&forecast.event_hash),
            document["decoded"]["forecast"]["event_hash"].as_str().unwrap()
        );
        for case in document["accountRefusals"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            if !name.starts_with("forecast:") {
                continue;
            }
            let produced = match name {
                "forecast:bad_discriminator" => {
                    let mut broken = fixture.clone();
                    broken[0..5].copy_from_slice(b"XXXXX");
                    solana::decode_forecast(&broken)
                }
                "forecast:window" => {
                    let mut broken = fixture.clone();
                    broken[104..112].copy_from_slice(&100i64.to_le_bytes());
                    broken[112..120].copy_from_slice(&1i64.to_le_bytes());
                    solana::decode_forecast(&broken)
                }
                _ => {
                    let mut broken = fixture.clone();
                    broken[340..348].copy_from_slice(&5i64.to_le_bytes());
                    solana::decode_forecast(&broken)
                }
            };
            assert_eq!(produced.unwrap_err(), case["error"].as_str().unwrap(), "{name}");
        }
        // And the register instruction refuses a publication that closes before it opens.
        assert_eq!(
            solana::encode_register(solana::RegisterParts {
                forecast_id_hash: &[7u8; 32],
                creator_hash: &[5u8; 32],
                specification_hash: &[3u8; 32],
                open_at_ms: 1,
                close_at_ms: 100,
                revision: 1,
                occurred_at_ms: 200,
                event_hash: &[13u8; 32],
                snapshot_hash: &[14u8; 32],
            })
            .unwrap_err(),
            "publication must precede close"
        );
    }
}

// ---------------------------------------------------------------- the ABI decoder

/// The fixed length of each opcode's payload, where it is fixed.
fn fixed_operand(tag: u8) -> Option<usize> {
    Some(match tag {
        6 => 73,
        8 => 181,
        10 => 73,
        11 => 74,
        12 => 295,
        13 => 287,
        14 => 73,
        _ => return None,
    })
}

/// The advance body of a tag 7, 12 or 13 instruction: 254 bytes at `start`, with the extension
/// after it.
fn decode_advance_body(data: &[u8], tag: u8) -> Result<(Value, Vec<u8>), String> {
    let start = if tag == 7 { 2 } else { 1 };
    let body = data
        .get(start..start + 254)
        .ok_or_else(|| "noncanonical advance".to_string())?
        .to_vec();
    let revision = u64::from_le_bytes(body[0..8].try_into().unwrap_or([0; 8])) as i64;
    let occurred = i64::from_le_bytes(body[8..16].try_into().unwrap_or([0; 8]));
    let challenge = i64::from_le_bytes(body[242..250].try_into().unwrap_or([0; 8]));
    let pending = u16::from_le_bytes(body[250..252].try_into().unwrap_or([0; 2]));
    let material = u16::from_le_bytes(body[252..254].try_into().unwrap_or([0; 2]));
    let fields = json!({
        "revision": revision, "occurred_at_ms": occurred,
        "previous_event_hash": to_hex(&body[16..48]), "event_hash": to_hex(&body[48..80]),
        "snapshot_hash": to_hex(&body[80..112]), "state": body[112], "outcome": body[113],
        "resolution_hash": to_hex(&body[114..146]), "dispute_hash": to_hex(&body[146..178]),
        "reputation_hash": to_hex(&body[178..210]), "trigger_hash": to_hex(&body[210..242]),
        "challenge_until_ms": challenge, "pending_disputes": pending, "material_disputes": material,
    });
    // Canonicality is proved by re-encoding: an advance whose bytes say one thing and whose
    // fields say another is not an instruction anyone signed, and a state the program would never
    // write cannot be smuggled in as a payload that merely parses.
    let re_encoded = solana::encode_advance(&solana::AdvanceFields {
        revision,
        occurred_at_ms: occurred,
        previous_event_hash: &body[16..48],
        event_hash: &body[48..80],
        snapshot_hash: &body[80..112],
        state: body[112] as i64,
        outcome: body[113] as i64,
        resolution_hash: &body[114..146],
        dispute_hash: &body[146..178],
        reputation_hash: &body[178..210],
        trigger_hash: &body[210..242],
        challenge_until_ms: challenge,
        pending_disputes: pending as i64,
        material_disputes: material as i64,
    })
    .map_err(|_| "noncanonical advance".to_string())?;
    let mut canonical = vec![2u8];
    canonical.extend(&body);
    if re_encoded != canonical {
        return Err("noncanonical advance".to_string());
    }
    if (body[112] == 10) != (tag == 12 || tag == 13) {
        return Err("wrong finalization opcode".to_string());
    }
    Ok((fields, data[start + 254..].to_vec()))
}

/// `decode_instruction`: a strict structural decoder. State-dependent authorization is native.
pub fn decode_instruction(data: &[u8]) -> Result<Value, String> {
    if data.is_empty() {
        return Err("missing instruction".to_string());
    }
    let tag = data[0];
    if let Some(size) = fixed_operand(tag) {
        fixed(data, size)?;
    } else if tag == 7 {
        if data.len() < 2 || data[1] > 2 {
            return Err("unknown advance mode".to_string());
        }
        fixed(
            data,
            match data[1] {
                0 => 256,
                1 => 320,
                _ => 288,
            },
        )?;
    } else if tag == 9 {
        if data.len() < 8 {
            return Err("empty evidence chunk".to_string());
        }
        let offset = read_u32(data, 1) as i64;
        let size = u16::from_le_bytes(data[5..7].try_into().unwrap_or([0; 2])) as usize;
        if encode_append(offset, &data[7..])? != data || size != data.len() - 7 {
            return Err("invalid append payload".to_string());
        }
        return Ok(json!({"tag": tag, "offset": offset, "chunk": to_hex(&data[7..])}));
    } else {
        return Err("unsupported intake opcode".to_string());
    }

    if matches!(tag, 7 | 12 | 13) {
        let (advance, extension) = decode_advance_body(data, tag)?;
        if tag == 12 {
            number(read_u64(data, 255) as i64, MAX)?;
        }
        return Ok(json!({
            "tag": tag, "mode": if tag == 7 { json!(data[1]) } else { Value::Null },
            "advance": advance, "extension": to_hex(&extension),
        }));
    }
    number(read_u64(data, 1) as i64, MAX)?;
    if tag == 8 {
        number(read_u64(data, 9) as i64, MAX)?;
        let size = read_u32(data, 177) as i64;
        if size <= 0 || size > MAX_BODY as i64 {
            return Err("invalid declared evidence length".to_string());
        }
    }
    if tag == 11 && !(data[73] == 2 || data[73] == 3) {
        return Err("invalid review disposition".to_string());
    }
    Ok(json!({"tag": tag, "payload": to_hex(&data[1..])}))
}

/// One account an instruction needs, as the caller must declare it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Meta {
    pub pubkey: [u8; 32],
    pub signer: bool,
    pub writable: bool,
}

impl Meta {
    /// An account the instruction names. The reference does not require it to be non-zero: the
    /// system program is the all-zero id, and several instructions address it.
    fn new(pubkey: &[u8], signer: bool, writable: bool) -> Result<Self, String> {
        Ok(Self {
            pubkey: array(&key(pubkey, false)?),
            signer,
            writable,
        })
    }

    /// An account the reference marks non-zero: a receipt or an adjudicator that cannot be
    /// absent, because the instruction is addressed to it.
    fn required(pubkey: &[u8], signer: bool, writable: bool) -> Result<Self, String> {
        Ok(Self {
            pubkey: array(&key(pubkey, true)?),
            signer,
            writable,
        })
    }
}

/// `instruction`: the exact account metas each opcode requires.
///
/// The set is not a suggestion. An instruction with the wrong writability is rejected by the
/// runtime, and one with the right accounts in the wrong privileges is a different instruction.
pub fn instruction(
    program: &[u8],
    data: &[u8],
    actor: &[u8],
    forecast: &[u8],
    receipt: &[u8],
    adjudicator: &[u8],
) -> Result<Vec<Meta>, String> {
    key(program, true)?;
    key(actor, true)?;
    decode_instruction(data)?;
    let tag = data[0];
    let config = solana::config_address(program)?.0;
    let judge = reviewer_address(program)?.0;
    let gate = if forecast != ZERO {
        gate_address(program, forecast)?.0
    } else {
        ZERO
    };
    let metas = match tag {
        14 => vec![
            Meta::new(actor, true, true)?,
            Meta::new(&config, false, false)?,
            Meta::new(&judge, false, true)?,
            Meta::new(&SYSTEM, false, false)?,
        ],
        6 => vec![
            Meta::new(actor, true, true)?,
            Meta::new(&config, false, false)?,
            Meta::new(forecast, false, false)?,
            Meta::new(&gate, true == false, true)?,
            Meta::new(&SYSTEM, false, false)?,
        ],
        7 => {
            let mut metas = vec![
                Meta::new(actor, true, false)?,
                Meta::new(&config, false, false)?,
                Meta::new(forecast, false, true)?,
                Meta::new(&gate, false, true)?,
            ];
            match data.get(1) {
                Some(1) => {
                    // An adjudicator signs; it does not need write access to be named.
                    metas.push(Meta::required(adjudicator, true, false)?);
                    metas.push(Meta::new(&judge, false, false)?);
                }
                Some(2) => metas.push(Meta::required(receipt, false, false)?),
                _ => {}
            }
            metas
        }
        8 => vec![
            Meta::new(actor, true, true)?,
            Meta::new(forecast, false, false)?,
            Meta::new(&gate, false, false)?,
            Meta::required(receipt, false, true)?,
            Meta::new(&SYSTEM, false, false)?,
        ],
        9 => vec![
            Meta::new(actor, true, true)?,
            Meta::required(receipt, false, true)?,
            Meta::new(&SYSTEM, false, false)?,
        ],
        10 => vec![
            Meta::new(actor, true, false)?,
            Meta::new(forecast, false, false)?,
            Meta::new(&gate, false, true)?,
            Meta::required(receipt, false, true)?,
        ],
        11 => vec![
            Meta::new(actor, true, false)?,
            Meta::new(&config, false, false)?,
            Meta::new(&judge, false, false)?,
            Meta::new(forecast, false, false)?,
            Meta::new(&gate, false, true)?,
            Meta::required(receipt, false, true)?,
        ],
        _ => vec![
            Meta::new(actor, true, false)?,
            Meta::new(&config, false, false)?,
            Meta::new(forecast, false, true)?,
            Meta::new(&gate, false, true)?,
        ],
    };
    let unique: std::collections::BTreeSet<[u8; 32]> = metas.iter().map(|meta| meta.pubkey).collect();
    if unique.len() != metas.len() {
        return Err("aliased accounts".to_string());
    }
    Ok(metas)
}

/// `heap_frame`: the compute-budget instruction that has to appear once per transaction.
pub fn heap_frame() -> ([u8; 32], Vec<u8>) {
    let mut data = vec![1u8];
    data.extend(262_144u32.to_le_bytes());
    (compute_budget(), data)
}

/// The fields a source may carry, and nothing else.
const SOURCE_FIELDS: [&str; 4] = ["url", "body_base64", "sha256", "captured_at_ms"];
const EVIDENCE_FIELDS: [&str; 5] = ["version", "claim", "rule_clause_id", "explanation", "sources"];
const EVIDENCE_VERSION: &str = "forecast-dispute-evidence-v1";
const MAX_SOURCES: usize = 8;

/// Whether a URL is an acceptable evidence source: https, a real host, no credentials, no
/// fragment. Deliberately *not* the registry check — evidence may cite any public https page, and
/// it is the resolution path's job to decide whether a source is authoritative, not this one's.
fn https_source(url: &str) -> bool {
    if url.len() > 2000 || !url.starts_with("https://") {
        return false;
    }
    let rest = &url["https://".len()..];
    if rest.is_empty() || rest.starts_with('/') {
        return false;
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty() || authority.contains('@') {
        return false;
    }
    !url.contains('#')
}

/// `decode_evidence`: the retained evidence envelope.
///
/// The format belongs off chain and inclusion does not establish validity — what this proves is
/// that the bytes are the ones the hash covers. So it re-encodes canonically and compares, which
/// is the only way to know a document is its own canonical form rather than something that parses
/// to the same value.
pub fn decode_evidence(body: &[u8]) -> Result<Value, String> {
    evidence_hash(body)?;
    let text = std::str::from_utf8(body).map_err(|_| "invalid evidence encoding".to_string())?;
    // A duplicated field is its own refusal: the reference's `object_pairs_hook` raises it before
    // any schema check runs, and it means something different from a malformed document.
    let value = match parse_without_duplicates(text) {
        Ok(value) => value,
        Err(ParseFailure::Duplicate) => return Err("duplicate evidence field".to_string()),
        Err(ParseFailure::Other) => return Err("invalid evidence encoding".to_string()),
    };
    let Some(fields) = value.as_object() else {
        return Err("invalid evidence schema".to_string());
    };
    let exact = fields.len() == EVIDENCE_FIELDS.len() && EVIDENCE_FIELDS.iter().all(|name| fields.contains_key(*name));
    if !exact || fields["version"].as_str() != Some(EVIDENCE_VERSION) {
        return Err("invalid evidence schema".to_string());
    }
    for (name, limit) in [("claim", 1000usize), ("rule_clause_id", 128), ("explanation", 3000)] {
        let text = fields[name].as_str().unwrap_or_default();
        if text.is_empty() || text.chars().count() > limit {
            return Err("invalid evidence text".to_string());
        }
    }
    let Some(sources) = fields["sources"].as_array() else {
        return Err("invalid evidence sources".to_string());
    };
    if sources.is_empty() || sources.len() > MAX_SOURCES {
        return Err("invalid evidence sources".to_string());
    }
    for source in sources {
        let Some(entry) = source.as_object() else {
            return Err("invalid source".to_string());
        };
        if entry.len() != SOURCE_FIELDS.len() || !SOURCE_FIELDS.iter().all(|name| entry.contains_key(*name)) {
            return Err("invalid source".to_string());
        }
        let url = entry["url"].as_str().unwrap_or_default();
        if !https_source(url) {
            return Err("invalid source URL".to_string());
        }
        number(entry["captured_at_ms"].as_i64().unwrap_or(-1), MAX)?;
        let Some(encoded) = entry["body_base64"].as_str() else {
            return Err("invalid retained source".to_string());
        };
        let decoded = base64_decode(encoded).ok_or_else(|| "invalid retained source".to_string())?;
        let digest = to_hex(&evidence_body_digest(&decoded));
        if decoded.is_empty() || digest != entry["sha256"].as_str().unwrap_or_default() {
            return Err("source hash mismatch".to_string());
        }
    }
    let canonical = String::from_utf8(crate::translations::canonical_bytes_public(&value))
        .map_err(|_| "invalid evidence encoding".to_string())?;
    if canonical.as_bytes() != body {
        return Err("noncanonical evidence JSON".to_string());
    }
    Ok(value)
}

fn evidence_body_digest(body: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(body);
    hasher.finalize().into()
}

fn base64_decode(text: &str) -> Option<Vec<u8>> {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.decode(text).ok()
}

/// A JSON parse that refuses a duplicate key, which `serde_json::Value` would otherwise resolve
/// silently by keeping the last one. The number handling is deliberately permissive: the
/// reference rejects duplicates here and accepts any number.
enum ParseFailure {
    Duplicate,
    Other,
}

fn parse_without_duplicates(text: &str) -> Result<Value, ParseFailure> {
    use serde::de::{Deserialize, Deserializer, Error as DeError, MapAccess, SeqAccess, Visitor};
    use std::fmt;

    struct Plain(Value);
    impl<'de> Deserialize<'de> for Plain {
        fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            deserializer.deserialize_any(PlainVisitor).map(Plain)
        }
    }
    struct PlainVisitor;
    impl<'de> Visitor<'de> for PlainVisitor {
        type Value = Value;
        fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
            formatter.write_str("a JSON value with no duplicate keys")
        }
        fn visit_bool<E: DeError>(self, value: bool) -> Result<Value, E> {
            Ok(Value::Bool(value))
        }
        fn visit_i64<E: DeError>(self, value: i64) -> Result<Value, E> {
            Ok(json!(value))
        }
        fn visit_u64<E: DeError>(self, value: u64) -> Result<Value, E> {
            Ok(json!(value))
        }
        fn visit_f64<E: DeError>(self, value: f64) -> Result<Value, E> {
            Ok(json!(value))
        }
        fn visit_str<E: DeError>(self, value: &str) -> Result<Value, E> {
            Ok(Value::String(value.to_string()))
        }
        fn visit_none<E: DeError>(self) -> Result<Value, E> {
            Ok(Value::Null)
        }
        fn visit_unit<E: DeError>(self) -> Result<Value, E> {
            Ok(Value::Null)
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut access: A) -> Result<Value, A::Error> {
            let mut items = Vec::new();
            while let Some(Plain(item)) = access.next_element::<Plain>()? {
                items.push(item);
            }
            Ok(Value::Array(items))
        }
        fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Value, A::Error> {
            let mut fields = serde_json::Map::new();
            while let Some(key) = access.next_key::<String>()? {
                let Plain(value) = access.next_value::<Plain>()?;
                if fields.contains_key(&key) {
                    return Err(DeError::custom("duplicate key"));
                }
                fields.insert(key, value);
            }
            Ok(Value::Object(fields))
        }
    }

    let mut deserializer = serde_json::Deserializer::from_str(text);
    let value = Plain::deserialize(&mut deserializer).map_err(|error| {
        if error.to_string().contains("duplicate key") {
            ParseFailure::Duplicate
        } else {
            ParseFailure::Other
        }
    })?;
    deserializer.end().map_err(|_| ParseFailure::Other)?;
    Ok(value.0)
}

#[cfg(test)]
mod decoder {
    use super::tests_support::*;
    use super::*;

    fn fixtures() -> Vec<(&'static str, Vec<u8>)> {
        let reviewer = Reviewer::new(&[11u8; 32], 1).unwrap();
        let mut data = Vec::new();
        data.push(6u8);
        data.extend(5u64.to_le_bytes());
        data.extend([13u8; 32]);
        data.extend(SPECIFICATION);
        vec![
            ("reviewer", encode_reviewer(&[11u8; 32], Some(&reviewer)).unwrap()),
            ("activate", data),
            (
                "advance_mode0",
                encode_advance(&advance_bytes(9, 5), 0, &ZERO, &ZERO).unwrap(),
            ),
            (
                "advance_mode1",
                encode_advance(&advance_bytes(9, 5), 1, &ARTIFACT, &HEAD).unwrap(),
            ),
            (
                "advance_mode2",
                encode_advance(&advance_bytes(9, 5), 2, &ARTIFACT, &ZERO).unwrap(),
            ),
            ("draft", encode_draft(&open_gate(), &NONCE, BODY).unwrap()),
            ("append", encode_append(0, b"chunk").unwrap()),
            ("submit", encode_submit(&receipt(BODY, 0)).unwrap()),
            ("review", encode_review(&receipt(BODY, 1), &ARTIFACT, 2).unwrap()),
            ("seal", encode_seal(&open_gate(), &advance_bytes(10, 5)).unwrap()),
            (
                "finalize",
                encode_finalize(&sealed_gate(), &advance_bytes(10, 5)).unwrap(),
            ),
        ]
    }

    #[test]
    fn every_opcode_decodes_to_what_the_reference_decoded() {
        let document = golden();
        for (name, data) in fixtures() {
            let produced = decode_instruction(&data).unwrap_or_else(|error| panic!("{name}: {error}"));
            assert_eq!(produced, document["decodedInstructions"][name], "{name}");
        }
    }

    #[test]
    fn every_opcode_requires_the_accounts_the_reference_requires() {
        // An instruction with the wrong writability is rejected by the runtime; one with the
        // right accounts in the wrong privileges is a different instruction.
        let document = golden();
        let adjudicator = [2u8; 32];
        let receipt_key = [1u8; 32];
        for (name, data) in fixtures() {
            let metas = instruction(&PROGRAM, &data, &USER, &FORECAST, &receipt_key, &adjudicator)
                .unwrap_or_else(|error| panic!("{name}: {error}"));
            let produced: Vec<Value> = metas
                .iter()
                .map(|meta| {
                    serde_json::json!({
                        "pubkey": to_hex(&meta.pubkey), "signer": meta.signer, "writable": meta.writable,
                    })
                })
                .collect();
            let expected = document["instructionMetas"][name]
                .as_array()
                .unwrap()
                .iter()
                .map(|meta| {
                    serde_json::json!({
                        "pubkey": meta["pubkey"].as_str().unwrap(), "signer": meta["signer"],
                        "writable": meta["writable"],
                    })
                })
                .collect::<Vec<Value>>();
            assert_eq!(produced, expected, "{name}");
        }
        let (program, data) = heap_frame();
        assert_eq!(to_hex(&data), document["heapFrame"]["bytes"].as_str().unwrap());
        assert_eq!(to_hex(&program), document["heapFrame"]["program"].as_str().unwrap());
    }

    #[test]
    fn the_evidence_envelope_is_read_back_and_its_hash_verified() {
        let document = golden();
        let body = from_hex(document["evidence"]["body"].as_str().unwrap());
        let decoded = decode_evidence(&body).expect("the reference evidence");
        assert_eq!(decoded, document["evidence"]["decoded"]);
    }

    #[test]
    fn every_evidence_refusal_matches_the_reference() {
        let document = golden();
        for case in document["evidenceRefusals"].as_array().expect("refusals") {
            let name = case["name"].as_str().unwrap();
            let body = match name {
                "evidence:noncanonical" => {
                    // The same document, indented: it parses to the same value and is not its own
                    // canonical form, which is what the re-encode catches.
                    let canonical = from_hex(document["evidence"]["body"].as_str().unwrap());
                    let value: Value = serde_json::from_slice(&canonical).unwrap();
                    serde_json::to_vec_pretty(&value).unwrap()
                }
                "evidence:duplicate_field" => {
                    b"{\"version\":\"forecast-dispute-evidence-v1\",\"claim\":\"a\",\"claim\":\"b\",\"rule_clause_id\":\"r\",\"explanation\":\"e\",\"sources\":[]}".to_vec()
                }
                _ => {
                    let canonical = from_hex(document["evidence"]["body"].as_str().unwrap());
                    let mut value: Value = serde_json::from_slice(&canonical).unwrap();
                    match name {
                        "evidence:bad_hash" => value["sources"][0]["sha256"] = serde_json::json!("0".repeat(64)),
                        "evidence:wrong_version" => value["version"] = serde_json::json!("v2"),
                        "evidence:empty_sources" => value["sources"] = serde_json::json!([]),
                        _ => value["sources"][0]["url"] = serde_json::json!("http://www.apple.com/x"),
                    }
                    String::from_utf8(crate::translations::canonical_bytes_public(&value))
                        .unwrap()
                        .into_bytes()
                }
            };
            let produced = decode_evidence(&body);
            assert!(produced.is_err(), "{name}: accepted where the reference refused");
            assert_eq!(
                produced.unwrap_err(),
                case["error"].as_str().unwrap(),
                "{name}: a different refusal"
            );
        }
    }
}
