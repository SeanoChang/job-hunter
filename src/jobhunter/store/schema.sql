-- provenance --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_attempts (
  attempt_id        TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  source            TEXT NOT NULL,
  board             TEXT NOT NULL,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ NOT NULL,
  http_status       INTEGER,
  transport         TEXT NOT NULL,
  health            TEXT NOT NULL,
  blob_sha256       TEXT,
  payload_bytes     INTEGER,
  observed_count    INTEGER NOT NULL DEFAULT 0,
  parsed_count      INTEGER NOT NULL DEFAULT 0,
  failed_count      INTEGER NOT NULL DEFAULT 0,
  unidentifiable_count INTEGER NOT NULL DEFAULT 0,
  prev_observed_count INTEGER,
  adapter_version   TEXT NOT NULL,
  registry_revision TEXT NOT NULL,
  cli_version       TEXT NOT NULL,
  warnings          JSONB,
  error             TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempts_board_time ON fetch_attempts (source, board, started_at);
CREATE INDEX IF NOT EXISTS ix_attempts_run ON fetch_attempts (run_id);

CREATE TABLE IF NOT EXISTS posting_versions (
  version_hash      TEXT NOT NULL,              -- content identity, may be shared by postings
  version_hash_v    INTEGER NOT NULL,
  uid               TEXT NOT NULL,
  source            TEXT NOT NULL,
  board             TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  title             TEXT NOT NULL,
  company           TEXT NOT NULL,
  locations         JSONB NOT NULL,
  workplace_type    TEXT,
  is_remote         BOOLEAN,
  department        TEXT,
  team              TEXT,
  employment_type   TEXT,
  compensation      JSONB,
  url               TEXT,
  apply_url         TEXT,
  source_created_at TIMESTAMPTZ,
  first_seen_attempt TEXT NOT NULL REFERENCES fetch_attempts (attempt_id),
  PRIMARY KEY (uid, version_hash)               -- one row per posting per content version
);
CREATE INDEX IF NOT EXISTS ix_versions_hash ON posting_versions (version_hash);

CREATE TABLE IF NOT EXISTS documents (
  version_hash       TEXT NOT NULL,             -- content identity of the source version
  normalizer_version TEXT NOT NULL,
  document_hash      TEXT NOT NULL,             -- sha256(markdown); shared when texts coincide
  markdown           TEXT NOT NULL,
  PRIMARY KEY (version_hash, normalizer_version)
);
CREATE INDEX IF NOT EXISTS ix_documents_hash ON documents (document_hash);

-- derived -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS presence (
  uid            TEXT NOT NULL,
  version_hash   TEXT,
  parse_status   TEXT NOT NULL,
  first_attempt  TEXT NOT NULL,
  last_attempt   TEXT NOT NULL,
  first_at       TIMESTAMPTZ NOT NULL,
  last_at        TIMESTAMPTZ NOT NULL,
  runs           INTEGER NOT NULL,
  PRIMARY KEY (uid, first_attempt)
);
CREATE INDEX IF NOT EXISTS ix_presence_last ON presence (last_attempt);
CREATE INDEX IF NOT EXISTS ix_presence_uid_last ON presence (uid, last_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  started_at     TIMESTAMPTZ NOT NULL,
  finished_at    TIMESTAMPTZ NOT NULL,
  cli_version    TEXT NOT NULL,
  boards_total   INTEGER NOT NULL,
  boards_ok      INTEGER NOT NULL,
  boards_suspect INTEGER NOT NULL,
  boards_error   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS panel (
  source            TEXT NOT NULL,
  board             TEXT NOT NULL,
  company           TEXT NOT NULL,
  added_at          TIMESTAMPTZ NOT NULL,
  removed_at        TIMESTAMPTZ,
  registry_revision TEXT NOT NULL,
  PRIMARY KEY (source, board, added_at)
);

CREATE TABLE IF NOT EXISTS postings (
  uid                  TEXT PRIMARY KEY,
  source               TEXT NOT NULL,
  board                TEXT NOT NULL,
  source_id            TEXT NOT NULL,
  status               TEXT NOT NULL,
  current_version_hash TEXT,
  version_count        INTEGER NOT NULL DEFAULT 0,
  reopen_count         INTEGER NOT NULL DEFAULT 0,
  first_seen_attempt   TEXT NOT NULL,
  first_seen_at        TIMESTAMPTZ NOT NULL,
  last_seen_attempt    TEXT NOT NULL,
  last_seen_at         TIMESTAMPTZ NOT NULL,
  closed_lower_at      TIMESTAMPTZ,
  closed_upper_at      TIMESTAMPTZ,
  closed_by_attempt    TEXT,
  source_updated_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_postings_board_status ON postings (source, board, status);

CREATE TABLE IF NOT EXISTS posting_events (
  event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  uid             TEXT NOT NULL,
  kind            TEXT NOT NULL,
  attempt_id      TEXT NOT NULL,
  at              TIMESTAMPTZ NOT NULL,
  from_version    TEXT,
  to_version      TEXT,
  closed_lower_at TIMESTAMPTZ,
  closed_upper_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_events_uid ON posting_events (uid, event_id);
CREATE INDEX IF NOT EXISTS ix_events_time ON posting_events (at);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- extraction surface (harness spec §4.7); writer: store/extraction.py under
-- EXTRACT_LOCK_KEY. Provenance insert-only; extractions derived (replayable).
CREATE TABLE IF NOT EXISTS extraction_attempts (
  attempt_key        TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL,
  document_hash      TEXT NOT NULL,
  normalizer_version TEXT NOT NULL,
  sample_slot        INTEGER NOT NULL,
  attempt_no         INTEGER NOT NULL,
  requested_engine   TEXT NOT NULL,
  requested_model    TEXT NOT NULL,
  observed_model     TEXT,
  prompt_version     TEXT NOT NULL,
  schema_version     TEXT NOT NULL,
  validator_version  TEXT NOT NULL,
  outcome            TEXT NOT NULL,
  ladder_exhausted   BOOLEAN NOT NULL,
  error_detail       JSONB,
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cost_usd           NUMERIC(9,5),
  started_at         TIMESTAMPTZ NOT NULL,
  finished_at        TIMESTAMPTZ NOT NULL,
  cli_version        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_xattempts_doc ON extraction_attempts (document_hash, started_at);
CREATE INDEX IF NOT EXISTS ix_xattempts_time ON extraction_attempts (started_at);

CREATE TABLE IF NOT EXISTS extraction_reviews (
  review_key        TEXT PRIMARY KEY,
  document_hash     TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  schema_version    TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  verb              TEXT NOT NULL,
  payload           JSONB,
  actor             TEXT NOT NULL,
  at                TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS extractions (
  document_hash     TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  schema_version    TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  status            TEXT NOT NULL,
  chosen_attempt    TEXT REFERENCES extraction_attempts (attempt_key),
  k                 INTEGER NOT NULL DEFAULT 1,
  agreement         JSONB,
  profile           JSONB,
  flags             JSONB,
  reviewed_by       TEXT,
  updated_at        TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (document_hash, model, prompt_version, schema_version, validator_version)
);
CREATE INDEX IF NOT EXISTS ix_extractions_doc ON extractions (document_hash);
CREATE INDEX IF NOT EXISTS ix_extractions_status ON extractions (status);

-- derived from extractions.profile by store/extraction.upsert_state; rebuildable.
-- Validated rows only, homogeneous in the engine tuple like every aggregate
-- (2026-08-26 ruling): a mention here is something the corpus asserts.
CREATE TABLE IF NOT EXISTS profile_mentions (
  document_hash     TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  schema_version    TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  mention           TEXT NOT NULL,
  area_kind         TEXT NOT NULL,
  importance        TEXT NOT NULL,
  PRIMARY KEY (document_hash, model, prompt_version, schema_version,
               validator_version, mention, area_kind, importance)
);
CREATE INDEX IF NOT EXISTS ix_mentions_mention ON profile_mentions (mention, importance);
