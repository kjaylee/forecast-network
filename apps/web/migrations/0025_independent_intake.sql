-- Additive mirror only: no forecast/global lifecycle trigger is installed here.
-- Root selects admission_guard_sql inside activated forecasts' existing CAS batch.
CREATE TABLE intake_bindings (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),
 program_id TEXT NOT NULL, genesis_hash TEXT NOT NULL,
 forecast_address TEXT NOT NULL UNIQUE, accumulator_address TEXT NOT NULL UNIQUE,
 specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 generation INTEGER NOT NULL CHECK(generation>0 AND generation<=9007199254740991),
 activated_at INTEGER NOT NULL CHECK(activated_at>=0)
);
CREATE TRIGGER intake_binding_identity BEFORE UPDATE ON intake_bindings
WHEN NEW.forecast_id!=OLD.forecast_id OR NEW.program_id!=OLD.program_id
 OR NEW.genesis_hash!=OLD.genesis_hash OR NEW.forecast_address!=OLD.forecast_address
 OR NEW.accumulator_address!=OLD.accumulator_address OR NEW.specification_hash!=OLD.specification_hash
 OR NEW.activated_at!=OLD.activated_at OR NEW.generation!=OLD.generation+1
BEGIN SELECT RAISE(ABORT,'intake_binding_immutable'); END;
CREATE TRIGGER intake_binding_no_delete BEFORE DELETE ON intake_bindings
BEGIN SELECT RAISE(ABORT,'intake_binding_immutable'); END;
CREATE TRIGGER intake_binding_spec BEFORE INSERT ON intake_bindings
WHEN NOT EXISTS(SELECT 1 FROM forecasts WHERE id=NEW.forecast_id AND specification_hash=NEW.specification_hash)
BEGIN SELECT RAISE(ABORT,'intake_spec_mismatch'); END;

CREATE TABLE intake_epoch_observations (
 forecast_id TEXT NOT NULL REFERENCES intake_bindings(forecast_id),
 epoch INTEGER NOT NULL CHECK(epoch>=0), revision INTEGER NOT NULL CHECK(revision>0),
 context_slot INTEGER NOT NULL CHECK(context_slot>0), commitment TEXT NOT NULL CHECK(length(commitment)=64),
 account_base64 TEXT NOT NULL CHECK(length(account_base64)=524),
 forecast_base64 TEXT NOT NULL CHECK(length(forecast_base64)=480),
 observed_at INTEGER NOT NULL CHECK(observed_at>=0),
 PRIMARY KEY(forecast_id,revision,context_slot)
);
CREATE TRIGGER intake_epochs_immutable BEFORE UPDATE ON intake_epoch_observations
BEGIN SELECT RAISE(ABORT,'intake_observation_immutable'); END;
CREATE TRIGGER intake_epochs_no_delete BEFORE DELETE ON intake_epoch_observations
BEGIN SELECT RAISE(ABORT,'intake_observation_immutable'); END;
CREATE TABLE intake_heads (
 forecast_id TEXT PRIMARY KEY REFERENCES intake_bindings(forecast_id),
 generation INTEGER NOT NULL CHECK(generation>0), epoch INTEGER NOT NULL CHECK(epoch>=0),
 revision INTEGER NOT NULL CHECK(revision>0), context_slot INTEGER NOT NULL CHECK(context_slot>0),
 commitment TEXT NOT NULL CHECK(length(commitment)=64),
 phase INTEGER NOT NULL CHECK(phase BETWEEN 0 AND 3),
 pending_count INTEGER NOT NULL CHECK(pending_count>=0),
 material_count INTEGER NOT NULL CHECK(material_count>=0),
 accepted_count INTEGER NOT NULL CHECK(accepted_count>=pending_count+material_count),
 deadline INTEGER NOT NULL CHECK(deadline>=0)
);

CREATE TABLE intake_receipts (
 address TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES intake_bindings(forecast_id),
 epoch INTEGER NOT NULL CHECK(epoch>0), proposal_revision INTEGER NOT NULL CHECK(proposal_revision>0),
 proposal_event_hash TEXT NOT NULL CHECK(length(proposal_event_hash)=64),
 resolution_hash TEXT NOT NULL CHECK(length(resolution_hash)=64),
 user_signer TEXT NOT NULL, nonce TEXT NOT NULL CHECK(length(nonce)=64),
 evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
 body_base64 TEXT NOT NULL CHECK(length(body_base64) BETWEEN 4 AND 43692),
 body_length INTEGER NOT NULL CHECK(body_length BETWEEN 1 AND 32768),
 accepted_at INTEGER NOT NULL CHECK(accepted_at>=0), accepted_slot INTEGER NOT NULL CHECK(accepted_slot>0),
 accepted_deadline INTEGER NOT NULL CHECK(accepted_deadline>=accepted_at),
 evidence_valid INTEGER NOT NULL CHECK(evidence_valid IN (0,1)),
 imported_at INTEGER NOT NULL CHECK(imported_at>=0),
 UNIQUE(forecast_id,epoch,user_signer)
);
CREATE TRIGGER intake_receipts_immutable BEFORE UPDATE ON intake_receipts
BEGIN SELECT RAISE(ABORT,'intake_receipt_immutable'); END;
CREATE TRIGGER intake_receipts_no_delete BEFORE DELETE ON intake_receipts
BEGIN SELECT RAISE(ABORT,'intake_receipt_immutable'); END;
CREATE TABLE intake_receipt_observations (
 address TEXT NOT NULL REFERENCES intake_receipts(address), commitment TEXT NOT NULL CHECK(length(commitment)=64),
 context_slot INTEGER NOT NULL CHECK(context_slot>0),
 account_base64 TEXT NOT NULL CHECK(length(account_base64) BETWEEN 568 AND 44256),
 native_status INTEGER NOT NULL CHECK(native_status BETWEEN 1 AND 3),
 review_hash TEXT NOT NULL CHECK(length(review_hash)=64), reviewer TEXT NOT NULL,
 reviewed_at INTEGER NOT NULL CHECK(reviewed_at>=0),
 PRIMARY KEY(address,commitment,context_slot)
);
CREATE TRIGGER intake_receipt_obs_immutable BEFORE UPDATE ON intake_receipt_observations
BEGIN SELECT RAISE(ABORT,'intake_observation_immutable'); END;
CREATE TRIGGER intake_receipt_obs_no_delete BEFORE DELETE ON intake_receipt_observations
BEGIN SELECT RAISE(ABORT,'intake_observation_immutable'); END;
CREATE TABLE intake_receipt_heads (
 address TEXT PRIMARY KEY REFERENCES intake_receipts(address),
 commitment TEXT NOT NULL CHECK(length(commitment)=64), context_slot INTEGER NOT NULL CHECK(context_slot>0),
 native_status INTEGER NOT NULL CHECK(native_status BETWEEN 1 AND 3)
);

-- Candidate preimage is the already-versioned native Advance encoding, not a
-- newly invented DomainV3 serialization/hash. Its leading tag2 is never sent.
CREATE TABLE intake_seals (
 forecast_id TEXT NOT NULL REFERENCES intake_bindings(forecast_id), epoch INTEGER NOT NULL CHECK(epoch>0),
 advance_base64 TEXT NOT NULL CHECK(length(advance_base64)=340),
 advance_hash TEXT NOT NULL CHECK(length(advance_hash)=64),
 predecessor_revision INTEGER NOT NULL CHECK(predecessor_revision>0),
 predecessor_event_hash TEXT NOT NULL CHECK(length(predecessor_event_hash)=64),
 candidate_revision INTEGER NOT NULL CHECK(candidate_revision=predecessor_revision+1),
 candidate_event_hash TEXT NOT NULL CHECK(length(candidate_event_hash)=64),
 candidate_snapshot_hash TEXT NOT NULL CHECK(length(candidate_snapshot_hash)=64),
 seal_commitment TEXT NOT NULL CHECK(length(seal_commitment)=64),
 account_base64 TEXT NOT NULL CHECK(length(account_base64)=524),
 sealed_at INTEGER NOT NULL CHECK(sealed_at>=0), sealed_slot INTEGER NOT NULL CHECK(sealed_slot>0),
 PRIMARY KEY(forecast_id,epoch)
);
CREATE TRIGGER intake_seals_immutable BEFORE UPDATE ON intake_seals
BEGIN SELECT RAISE(ABORT,'intake_seal_immutable'); END;
CREATE TRIGGER intake_seals_no_delete BEFORE DELETE ON intake_seals
BEGIN SELECT RAISE(ABORT,'intake_seal_immutable'); END;
CREATE TABLE intake_seal_admissions (
 forecast_id TEXT NOT NULL, epoch INTEGER NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
 context_slot INTEGER NOT NULL CHECK(context_slot>0),
 PRIMARY KEY(forecast_id,epoch,generation),
 FOREIGN KEY(forecast_id,epoch) REFERENCES intake_seals(forecast_id,epoch)
);
