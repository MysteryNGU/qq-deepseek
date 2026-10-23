#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""life.db 的表结构。

只放设计文档明确要求的约束：外部消息唯一、输入只归属一个轮次、
同一输入同一效果只应用一次、投影批次幂等、来源唯一、遗忘屏障。
"""

from __future__ import annotations

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id                TEXT PRIMARY KEY,
  agent_id          TEXT NOT NULL,
  conversation_id   TEXT NOT NULL,
  participant_id    TEXT NOT NULL DEFAULT '',
  audience_policy_id TEXT NOT NULL DEFAULT '',
  kind              TEXT NOT NULL,
  source_kind       TEXT NOT NULL,
  external_id       TEXT,
  occurred_at       REAL NOT NULL,
  received_at       REAL NOT NULL,
  ingest_seq        INTEGER NOT NULL,
  actor_id          TEXT NOT NULL DEFAULT '',
  payload           TEXT NOT NULL DEFAULT '{}',
  source_event_ids  TEXT NOT NULL DEFAULT '[]',
  schema_version    INTEGER NOT NULL DEFAULT 1,
  redacted_at       REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_external
  ON events(agent_id, conversation_id, external_id)
  WHERE external_id IS NOT NULL AND external_id <> '';
CREATE INDEX IF NOT EXISTS ix_events_scope_seq
  ON events(agent_id, conversation_id, ingest_seq);

CREATE TABLE IF NOT EXISTS turns (
  id                TEXT PRIMARY KEY,
  agent_id          TEXT NOT NULL,
  conversation_id   TEXT NOT NULL,
  participant_id    TEXT NOT NULL DEFAULT '',
  origin            TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'queued',
  generation        INTEGER NOT NULL DEFAULT 1,
  lease_owner       TEXT,
  lease_until       REAL,
  context_revision  INTEGER NOT NULL DEFAULT 1,
  policy_epoch      INTEGER NOT NULL DEFAULT 0,
  forget_epoch      INTEGER NOT NULL DEFAULT 0,
  created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_scope ON turns(agent_id, conversation_id, created_at);

CREATE TABLE IF NOT EXISTS turn_inputs (
  turn_id   TEXT NOT NULL,
  event_id  TEXT NOT NULL,
  added_at  REAL NOT NULL,
  PRIMARY KEY(turn_id, event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_turn_inputs_event ON turn_inputs(event_id);

CREATE TABLE IF NOT EXISTS applied_effects (
  input_event_id TEXT NOT NULL,
  effect_kind    TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  applied_at     REAL NOT NULL,
  PRIMARY KEY(input_event_id, effect_kind, aggregate_id)
);

CREATE TABLE IF NOT EXISTS state_snapshots (
  aggregate_id      TEXT NOT NULL,
  version           INTEGER NOT NULL,
  last_applied_seq  INTEGER NOT NULL DEFAULT 0,
  payload           TEXT NOT NULL,
  updated_at        REAL NOT NULL,
  PRIMARY KEY(aggregate_id, version)
);

CREATE TABLE IF NOT EXISTS memories (
  id                  TEXT PRIMARY KEY,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  audience_policy_id  TEXT NOT NULL DEFAULT '',
  kind                TEXT NOT NULL,
  subject_id          TEXT NOT NULL DEFAULT '',
  predicate           TEXT NOT NULL DEFAULT '',
  normalized_value    TEXT NOT NULL DEFAULT '',
  summary             TEXT NOT NULL,
  event_group_id      TEXT NOT NULL DEFAULT '',
  topic_tags          TEXT NOT NULL DEFAULT '[]',
  source_kind         TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'candidate',
  valid_from          REAL,
  valid_until         REAL,
  time_precision      TEXT NOT NULL DEFAULT 'exact',
  recorded_at         REAL NOT NULL,
  updated_at          REAL NOT NULL,
  version             INTEGER NOT NULL DEFAULT 1,
  salience            REAL NOT NULL DEFAULT 0.5,
  source_quality      REAL NOT NULL DEFAULT 0.5,
  uncertainty_reason  TEXT NOT NULL DEFAULT '',
  supersedes_id       TEXT NOT NULL DEFAULT '',
  derived_from        TEXT NOT NULL DEFAULT '[]',
  last_mentioned_at   REAL,
  mention_count_window INTEGER NOT NULL DEFAULT 0,
  retention_policy_id TEXT NOT NULL DEFAULT 'default',
  forget_epoch        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_memories_scope_status
  ON memories(agent_id, conversation_id, status, kind);
CREATE INDEX IF NOT EXISTS ix_memories_fact_key
  ON memories(agent_id, conversation_id, subject_id, predicate, status);

CREATE TABLE IF NOT EXISTS memory_sources (
  memory_id    TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  relation     TEXT NOT NULL DEFAULT 'supports',
  quote_start  INTEGER,
  quote_end    INTEGER,
  PRIMARY KEY(memory_id, event_id, relation, quote_start, quote_end)
);
CREATE INDEX IF NOT EXISTS ix_memory_sources_event ON memory_sources(event_id);

CREATE TABLE IF NOT EXISTS memory_terms (
  memory_id TEXT NOT NULL,
  term      TEXT NOT NULL,
  PRIMARY KEY(memory_id, term)
);
CREATE INDEX IF NOT EXISTS ix_memory_terms_term ON memory_terms(term);

CREATE TABLE IF NOT EXISTS open_loops (
  id                  TEXT PRIMARY KEY,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  kind                TEXT NOT NULL,
  summary             TEXT NOT NULL,
  source_event_ids    TEXT NOT NULL DEFAULT '[]',
  expected_outcome    TEXT NOT NULL DEFAULT '',
  acceptance_checks   TEXT NOT NULL DEFAULT '[]',
  status              TEXT NOT NULL DEFAULT 'proposed',
  next_step           TEXT NOT NULL DEFAULT '',
  checkpoint          TEXT NOT NULL DEFAULT '',
  waiting_for         TEXT NOT NULL DEFAULT '',
  due_at              REAL,
  next_review_at      REAL,
  retry_after         REAL,
  permission_scope_id TEXT NOT NULL DEFAULT '',
  result_evidence_ids TEXT NOT NULL DEFAULT '[]',
  version             INTEGER NOT NULL DEFAULT 1,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_open_loops_scope_status
  ON open_loops(agent_id, conversation_id, status, next_review_at);

CREATE TABLE IF NOT EXISTS decision_records (
  id                  TEXT PRIMARY KEY,
  turn_id             TEXT NOT NULL,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  input_event_ids     TEXT NOT NULL DEFAULT '[]',
  memory_ids          TEXT NOT NULL DEFAULT '[]',
  appraisal_id        TEXT NOT NULL DEFAULT '',
  state_version_before INTEGER NOT NULL DEFAULT 0,
  state_version_after  INTEGER NOT NULL DEFAULT 0,
  behavior_plan       TEXT NOT NULL DEFAULT '{}',
  action_id           TEXT NOT NULL DEFAULT '',
  outcome             TEXT NOT NULL DEFAULT '',
  reason_codes        TEXT NOT NULL DEFAULT '[]',
  policy_version      TEXT NOT NULL DEFAULT 'life-1',
  prompt_version      TEXT NOT NULL DEFAULT '',
  model_id            TEXT NOT NULL DEFAULT '',
  tokens              INTEGER NOT NULL DEFAULT 0,
  duration_ms         INTEGER NOT NULL DEFAULT 0,
  created_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_batches (
  scope_id       TEXT NOT NULL,
  projector_kind TEXT NOT NULL,
  batch_id       TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'committed',
  committed_at   REAL NOT NULL,
  PRIMARY KEY(scope_id, projector_kind, batch_id)
);

CREATE TABLE IF NOT EXISTS daily_jobs (
  scope_id   TEXT NOT NULL,
  local_date TEXT NOT NULL,
  job_kind   TEXT NOT NULL,
  version    INTEGER NOT NULL DEFAULT 1,
  status     TEXT NOT NULL DEFAULT 'pending',
  due_at     REAL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  PRIMARY KEY(scope_id, local_date, job_kind, version)
);

CREATE TABLE IF NOT EXISTS forget_epochs (
  scope_id TEXT PRIMARY KEY,
  epoch    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS legacy_mappings (
  source_database_id TEXT NOT NULL,
  table_name         TEXT NOT NULL,
  source_primary_key TEXT NOT NULL,
  target_kind        TEXT NOT NULL,
  target_id          TEXT NOT NULL,
  migrated_at        REAL NOT NULL,
  PRIMARY KEY(source_database_id, table_name, source_primary_key)
);

CREATE TABLE IF NOT EXISTS knowledge_cards (
  id                  TEXT PRIMARY KEY,
  memory_id           TEXT NOT NULL DEFAULT '',
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  subject             TEXT NOT NULL DEFAULT '',
  question            TEXT NOT NULL DEFAULT '',
  claims              TEXT NOT NULL DEFAULT '[]',
  explanation         TEXT NOT NULL DEFAULT '',
  applicability       TEXT NOT NULL DEFAULT '',
  counterexamples     TEXT NOT NULL DEFAULT '[]',
  limitations         TEXT NOT NULL DEFAULT '[]',
  support_state       TEXT NOT NULL DEFAULT 'source_asserted',
  source_event_ids    TEXT NOT NULL DEFAULT '[]',
  source_metadata     TEXT NOT NULL DEFAULT '[]',
  acquired_at         REAL NOT NULL,
  valid_as_of         REAL,
  review_after        REAL,
  understanding_check_ids TEXT NOT NULL DEFAULT '[]',
  application_evidence_ids TEXT NOT NULL DEFAULT '[]',
  supersedes_id       TEXT NOT NULL DEFAULT '',
  version             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS capability_gaps (
  id                  TEXT PRIMARY KEY,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  normalized_need     TEXT NOT NULL,
  environment         TEXT NOT NULL DEFAULT '{}',
  linked_open_loops   TEXT NOT NULL DEFAULT '[]',
  evidence_ids        TEXT NOT NULL DEFAULT '[]',
  trigger_kind        TEXT NOT NULL DEFAULT 'observed',
  expected_outcome    TEXT NOT NULL DEFAULT '',
  acceptance_checks   TEXT NOT NULL DEFAULT '[]',
  status              TEXT NOT NULL DEFAULT 'observed',
  attempt_budget      INTEGER NOT NULL DEFAULT 2,
  attempts            INTEGER NOT NULL DEFAULT 0,
  next_review_at      REAL,
  failure_signature   TEXT NOT NULL DEFAULT '',
  version             INTEGER NOT NULL DEFAULT 1,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_capability_gap
  ON capability_gaps(agent_id, conversation_id, normalized_need, failure_signature);

CREATE TABLE IF NOT EXISTS evolution_proposals (
  id                  TEXT PRIMARY KEY,
  gap_id              TEXT NOT NULL,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  kind                TEXT NOT NULL,
  hypothesis          TEXT NOT NULL DEFAULT '',
  baseline_evidence   TEXT NOT NULL DEFAULT '[]',
  expected_gain       TEXT NOT NULL DEFAULT '',
  acceptance_checks   TEXT NOT NULL DEFAULT '[]',
  requested_effects   TEXT NOT NULL DEFAULT '[]',
  experiment_budget   REAL NOT NULL DEFAULT 0,
  permission_scope_id TEXT NOT NULL DEFAULT '',
  status              TEXT NOT NULL DEFAULT 'proposed',
  candidate_artifact  TEXT NOT NULL DEFAULT '',
  evaluation_id       TEXT NOT NULL DEFAULT '',
  release_id          TEXT NOT NULL DEFAULT '',
  version             INTEGER NOT NULL DEFAULT 1,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS study_threads (
  id                  TEXT PRIMARY KEY,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  topic               TEXT NOT NULL,
  specific_question   TEXT NOT NULL DEFAULT '',
  origin_want_id      TEXT NOT NULL DEFAULT '',
  why_evidence_ids    TEXT NOT NULL DEFAULT '[]',
  learning_checks     TEXT NOT NULL DEFAULT '[]',
  known_card_ids      TEXT NOT NULL DEFAULT '[]',
  unresolved_questions TEXT NOT NULL DEFAULT '[]',
  next_step           TEXT NOT NULL DEFAULT '',
  status              TEXT NOT NULL DEFAULT 'planned',
  next_review_at      REAL,
  budget              TEXT NOT NULL DEFAULT '{}',
  version             INTEGER NOT NULL DEFAULT 1,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_settlements (
  id           TEXT PRIMARY KEY,
  scope_id     TEXT NOT NULL,
  root_id      TEXT NOT NULL,
  dimension    TEXT NOT NULL,
  effect_kind  TEXT NOT NULL,
  cycle_id     TEXT NOT NULL DEFAULT '',
  before_value REAL NOT NULL,
  after_value  REAL NOT NULL,
  source_ids   TEXT NOT NULL DEFAULT '[]',
  payload      TEXT NOT NULL DEFAULT '{}',
  rule_version TEXT NOT NULL,
  created_at   REAL NOT NULL,
  revoked_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_drive_active_effect
  ON drive_settlements(scope_id, root_id, dimension, effect_kind, cycle_id)
  WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_drive_settlements_scope
  ON drive_settlements(scope_id, created_at);

CREATE TABLE IF NOT EXISTS action_attempts (
  id                  TEXT PRIMARY KEY,
  action_id           TEXT NOT NULL,
  agent_id            TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  canonical_spec      TEXT NOT NULL DEFAULT '{}',
  spec_hash           TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'prepared',
  lease_generation    INTEGER NOT NULL DEFAULT 1,
  started_at          REAL,
  result              TEXT NOT NULL DEFAULT '{}',
  reconciliation_due_at REAL,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL,
  UNIQUE(action_id, spec_hash)
);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)
    from .system_schema import init as init_runtime
    init_runtime(conn)
    from .recollection import SCHEMA as recollection_schema
    conn.executescript(recollection_schema)
    annotation_columns = {r[1] for r in conn.execute('PRAGMA table_info(memory_annotations)')}
    for name, declaration in [('always_load', 'INTEGER NOT NULL DEFAULT 0'), ('skill_refs', "TEXT NOT NULL DEFAULT '[]'")]:
        if name not in annotation_columns:
            conn.execute(f'ALTER TABLE memory_annotations ADD COLUMN {name} {declaration}')
    from .text import terms as annotation_terms
    for row in conn.execute('SELECT id,text,cues FROM memory_annotations WHERE id NOT IN (SELECT annotation_id FROM annotation_terms)'):
        for term in set(annotation_terms(row[1] + ' ' + row[2])):
            conn.execute('INSERT OR IGNORE INTO annotation_terms VALUES(?,?)', (row[0], term))
    job_columns = {r[1] for r in conn.execute("PRAGMA table_info(daily_jobs)")}
    if "lease_token" not in job_columns:
        conn.execute("ALTER TABLE daily_jobs ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_cards)")}
        if "memory_id" not in cols:
            conn.execute("ALTER TABLE knowledge_cards ADD COLUMN memory_id TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    conn.commit()
