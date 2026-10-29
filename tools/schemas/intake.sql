-- Generated from tools/intake/store.py (DDL); do not edit.
-- Schema version 1; structure only, not a data backup or restore procedure.
PRAGMA application_id=1279872596;
PRAGMA user_version=1;

CREATE TABLE requests (
 correlation_id TEXT PRIMARY KEY, correction_of TEXT REFERENCES requests(correlation_id),
 raw BLOB NOT NULL, request_sha256 TEXT NOT NULL, root TEXT NOT NULL);
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
