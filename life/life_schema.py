"""Additive, rebuildable memory catalog and indexes (design sections 3.2/3.4)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS life_resources (
  resource_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
  kind TEXT NOT NULL, native_id TEXT NOT NULL, origin_scope TEXT NOT NULL,
  actor_key TEXT, occurred_at REAL NOT NULL, updated_at REAL NOT NULL,
  source_version INTEGER NOT NULL, content_digest TEXT NOT NULL,
  status TEXT NOT NULL, expires_at REAL,
  UNIQUE(agent_id,kind,native_id)
);
CREATE INDEX IF NOT EXISTS ix_lr_time ON life_resources(agent_id,occurred_at,resource_id);
CREATE INDEX IF NOT EXISTS ix_lr_actor ON life_resources(agent_id,actor_key,occurred_at);
CREATE TABLE IF NOT EXISTS life_acl (
  resource_id TEXT NOT NULL REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  purpose TEXT NOT NULL, audience_key TEXT NOT NULL,
  effect TEXT NOT NULL CHECK(effect IN ('allow','deny')),
  policy_revision INTEGER NOT NULL, expires_at REAL,
  PRIMARY KEY(resource_id,purpose,audience_key)
);
CREATE INDEX IF NOT EXISTS ix_la_lookup ON life_acl(purpose,audience_key,resource_id);
CREATE TABLE IF NOT EXISTS life_source_edges (
  child_id TEXT NOT NULL REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  parent_id TEXT NOT NULL REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  parent_version INTEGER NOT NULL, relation TEXT NOT NULL,
  PRIMARY KEY(child_id,parent_id,relation), CHECK(child_id <> parent_id)
);
CREATE INDEX IF NOT EXISTS ix_lse_parent ON life_source_edges(parent_id,child_id);
CREATE TABLE IF NOT EXISTS life_epochs (
  agent_id TEXT NOT NULL, domain TEXT NOT NULL, epoch INTEGER NOT NULL,
  PRIMARY KEY(agent_id,domain)
);
CREATE TABLE IF NOT EXISTS life_terms (
  agent_id TEXT NOT NULL, term TEXT NOT NULL,
  resource_id TEXT NOT NULL REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  source_version INTEGER NOT NULL, weight REAL NOT NULL,
  PRIMARY KEY(agent_id,term,resource_id)
);
CREATE INDEX IF NOT EXISTS ix_lt_resource ON life_terms(resource_id);
CREATE TABLE IF NOT EXISTS life_activity_index (
  resource_id TEXT PRIMARY KEY REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL, root_ref TEXT NOT NULL, phase TEXT NOT NULL,
  source_version INTEGER NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lai_active ON life_activity_index(agent_id,phase,updated_at);
CREATE TABLE IF NOT EXISTS person_facets (
  resource_id TEXT PRIMARY KEY REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL, actor_key TEXT NOT NULL, facet_key TEXT NOT NULL,
  material_kind TEXT NOT NULL, body TEXT NOT NULL, applicability TEXT NOT NULL,
  revision INTEGER NOT NULL, status TEXT NOT NULL, expires_at REAL
);
CREATE INDEX IF NOT EXISTS ix_pf_person ON person_facets(agent_id,actor_key,facet_key,status);
CREATE TABLE IF NOT EXISTS life_effect_claims (
  agent_id TEXT NOT NULL, stimulus_key TEXT NOT NULL,
  domain TEXT NOT NULL, subject_key TEXT NOT NULL, metric_key TEXT NOT NULL,
  review_id TEXT NOT NULL, journal_id INTEGER NOT NULL,
  revision INTEGER NOT NULL, source_digest TEXT NOT NULL, status TEXT NOT NULL,
  PRIMARY KEY(agent_id,stimulus_key,domain,subject_key,metric_key)
);
CREATE INDEX IF NOT EXISTS ix_lec_review ON life_effect_claims(agent_id,review_id);

CREATE TABLE IF NOT EXISTS life_semantic_index (
  agent_id TEXT NOT NULL,
  resource_id TEXT NOT NULL REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  source_version INTEGER NOT NULL, source_digest TEXT NOT NULL,
  chunker_version TEXT NOT NULL, byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL,
  chunk_digest TEXT NOT NULL, model_key TEXT NOT NULL, dimension INTEGER NOT NULL,
  vector BLOB, state TEXT NOT NULL CHECK(state IN ('pending','ready','failed')),
  source_epochs TEXT NOT NULL, policy_revision INTEGER NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(resource_id,source_version,chunker_version,byte_start,model_key),
  CHECK(byte_start >= 0 AND byte_end > byte_start), CHECK(dimension > 0),
  CHECK(state <> 'ready' OR (vector IS NOT NULL AND length(vector) = dimension * 4))
);
CREATE INDEX IF NOT EXISTS ix_lsi_lookup ON life_semantic_index(agent_id,model_key,state,resource_id);
CREATE TABLE IF NOT EXISTS life_entity_mentions (
  mention_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
  entity_key TEXT NOT NULL, entity_kind TEXT NOT NULL,
  resource_id TEXT NOT NULL REFERENCES life_resources(resource_id) ON DELETE CASCADE,
  source_version INTEGER NOT NULL, relation TEXT NOT NULL,
  evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('observed','stated','inferred')),
  byte_start INTEGER, byte_end INTEGER, extractor_version TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','candidate','invalid')),
  CHECK((byte_start IS NULL AND byte_end IS NULL) OR
        (byte_start IS NOT NULL AND byte_end IS NOT NULL AND byte_start >= 0 AND byte_end > byte_start))
);
CREATE INDEX IF NOT EXISTS ix_lem_entity ON life_entity_mentions(agent_id,entity_key,state,resource_id);
CREATE INDEX IF NOT EXISTS ix_lem_source ON life_entity_mentions(resource_id,source_version);
"""
