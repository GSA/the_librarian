-- Generated from tools/review_events/corrections.py (DDL); do not edit.
-- Schema version 1; structure only, not a data backup or restore procedure.
PRAGMA application_id=1280459599;
PRAGMA user_version=1;

CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches(
 revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 committed_at TEXT NOT NULL, input_hash TEXT NOT NULL, record_count INTEGER NOT NULL CHECK(record_count>0),
 previous_hash TEXT NOT NULL, batch_hash TEXT NOT NULL);
CREATE TABLE corrections(
 sequence INTEGER PRIMARY KEY, correction_id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL REFERENCES batches(revision), payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE INDEX corrections_revision ON corrections(revision);
CREATE TRIGGER immutable_metadata_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_metadata_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_batches_update BEFORE UPDATE ON batches BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_batches_delete BEFORE DELETE ON batches BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_corrections_update BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_corrections_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'append-only store'); END;
