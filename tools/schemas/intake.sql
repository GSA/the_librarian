-- Generated from tools/intake/store.py (DDL); do not edit.
-- Schema version 1; structure only, not a data backup or restore procedure.
PRAGMA application_id=1279872596;
PRAGMA user_version=1;

CREATE TABLE requests (
 correlation_id TEXT PRIMARY KEY, correction_of TEXT REFERENCES requests(correlation_id),
 raw BLOB NOT NULL, request_sha256 TEXT NOT NULL, root TEXT NOT NULL);
-- Kata 5m1h: every "who corrects this request" lookup (partial: most requests correct nothing).
CREATE INDEX IF NOT EXISTS requests_correction ON requests(correction_of) WHERE correction_of IS NOT NULL;
CREATE TABLE stages (
 correlation_id TEXT NOT NULL REFERENCES requests(correlation_id), stage_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, status TEXT NOT NULL, cycle INTEGER NOT NULL DEFAULT 0,
 intent TEXT, intent_sha256 TEXT, evidence TEXT, evidence_sha256 TEXT,
 PRIMARY KEY(correlation_id, stage_id), UNIQUE(correlation_id, ordinal));
CREATE TABLE attempts (
 correlation_id TEXT NOT NULL, stage_id TEXT NOT NULL, cycle INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, started REAL NOT NULL, ended REAL,
 outcome TEXT, category TEXT, code TEXT, eligible_at REAL,
 PRIMARY KEY(correlation_id, stage_id, cycle, ordinal),
 FOREIGN KEY(correlation_id,stage_id) REFERENCES stages(correlation_id,stage_id));
CREATE TABLE replays (
 correlation_id TEXT NOT NULL, stage_id TEXT NOT NULL, cycle INTEGER NOT NULL,
 authorized_at REAL NOT NULL, PRIMARY KEY(correlation_id,stage_id,cycle),
 FOREIGN KEY(correlation_id,stage_id) REFERENCES stages(correlation_id,stage_id));
CREATE TABLE acknowledgments (
 correlation_id TEXT NOT NULL, stage_id TEXT NOT NULL, payload TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, PRIMARY KEY(correlation_id,stage_id),
 FOREIGN KEY(correlation_id,stage_id) REFERENCES stages(correlation_id,stage_id));
-- Derived (Kata e22r): one row per cohort document, recomputable from the requests above.
CREATE TABLE docs (
 system TEXT NOT NULL, path TEXT NOT NULL, format TEXT NOT NULL,
 submission_id TEXT NOT NULL, suffix TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL,
 receipt_id TEXT NOT NULL, original TEXT NOT NULL, state TEXT NOT NULL,
 conflicts INTEGER NOT NULL DEFAULT 0, rendition TEXT, rendition_sha256 TEXT,
 extract_head TEXT, register_head TEXT,
 dev INTEGER, ino INTEGER, size INTEGER, mtime_ns INTEGER, ctime_ns INTEGER, last_run REAL,
 PRIMARY KEY(system, path)) WITHOUT ROWID;
CREATE INDEX docs_state ON docs(state);
CREATE INDEX docs_upload ON docs(submission_id);
CREATE INDEX docs_extract ON docs(extract_head);
CREATE INDEX docs_register ON docs(register_head);
