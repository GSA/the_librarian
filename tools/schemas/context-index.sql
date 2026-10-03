-- Generated from tools/context-index/context_index.py (DDL); do not edit.
-- Schema version 1; structure only, not a data backup or restore procedure.
PRAGMA application_id=1178817624;
PRAGMA user_version=1;

CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
    path TEXT PRIMARY KEY, hash TEXT NOT NULL, role TEXT NOT NULL,
    introduction TEXT NOT NULL, reference_definitions TEXT NOT NULL
);
CREATE TABLE sections (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES documents(path),
    locator TEXT NOT NULL UNIQUE, title TEXT NOT NULL, kind TEXT NOT NULL,
    line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
    parent_id INTEGER REFERENCES sections(id), lead_end INTEGER NOT NULL,
    text TEXT NOT NULL, lead_text TEXT NOT NULL, table_header TEXT
);
CREATE TABLE aliases (locator TEXT PRIMARY KEY, section_id INTEGER REFERENCES sections(id));
CREATE TABLE rendition_documents (path TEXT PRIMARY KEY REFERENCES documents(path), payload TEXT NOT NULL);
CREATE TABLE rendition_locations (locator TEXT PRIMARY KEY REFERENCES sections(locator), payload TEXT NOT NULL);
CREATE VIRTUAL TABLE search_fts USING fts5(title, text, tokenize='unicode61');
