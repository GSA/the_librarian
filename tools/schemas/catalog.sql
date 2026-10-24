-- Generated from tools/catalog/catalog_store.py (SQL); do not edit.
-- Schema version 1; structure only, not a data backup or restore procedure.
PRAGMA application_id=1279476052;
PRAGMA user_version=1;

CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE revisions(
 revision INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 reason TEXT NOT NULL, committed_at TEXT NOT NULL, input_hash TEXT NOT NULL,
 record_count INTEGER NOT NULL CHECK(record_count>0), previous_hash TEXT NOT NULL,
 revision_hash TEXT NOT NULL);
CREATE TABLE versions(
 revision INTEGER NOT NULL REFERENCES revisions(revision), ordinal INTEGER NOT NULL,
 record_id TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 PRIMARY KEY(revision,ordinal), UNIQUE(revision,record_id));
CREATE TABLE current_records(
 record_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
 ordinal INTEGER NOT NULL,
 FOREIGN KEY(revision,ordinal) REFERENCES versions(revision,ordinal));
CREATE INDEX versions_record ON versions(record_id,revision);

CREATE TRIGGER immutable_metadata_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END;
CREATE TRIGGER immutable_metadata_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END;
CREATE TRIGGER immutable_revisions_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END;
CREATE TRIGGER immutable_revisions_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END;
CREATE TRIGGER immutable_versions_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END;
CREATE TRIGGER immutable_versions_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT,'immutable catalog history'); END;
