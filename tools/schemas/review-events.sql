-- Generated from tools/review-events/event_store.py (SQL); do not edit.
-- Schema version 1; structure only, not a data backup or restore procedure.
PRAGMA application_id=1280460118;
PRAGMA user_version=1;

CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches(
 revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 committed_at TEXT NOT NULL, input_hash TEXT NOT NULL, event_count INTEGER NOT NULL CHECK(event_count>0),
 previous_hash TEXT NOT NULL, batch_hash TEXT NOT NULL);
CREATE TABLE events(
 sequence INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL REFERENCES batches(revision),
 origin_key TEXT UNIQUE, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE TABLE corrections(
 predecessor TEXT PRIMARY KEY REFERENCES events(event_id),
 successor TEXT NOT NULL UNIQUE REFERENCES events(event_id));
CREATE INDEX events_revision ON events(revision);

CREATE TRIGGER immutable_metadata_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_metadata_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_batches_update BEFORE UPDATE ON batches BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_batches_delete BEFORE DELETE ON batches BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_corrections_update BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT,'append-only store'); END;
CREATE TRIGGER immutable_corrections_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'append-only store'); END;
