-- Keep historical positive evidence and the unique genesis-mint claim when a
-- complete refresh no longer proves ownership. Revision guards overlapping RPC
-- refreshes even when their timestamps or slots are equal.
ALTER TABLE seeker_verifications ADD COLUMN invalidated_at INTEGER;
ALTER TABLE seeker_verifications ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0);
