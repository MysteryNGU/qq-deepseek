"""Durable runtime contracts shared by conversation, jobs, delivery and releases."""

SQL = """
CREATE TABLE IF NOT EXISTS policies(
 scope_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
 contact TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS work_jobs(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 due REAL NOT NULL, root_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
 lease_until REAL NOT NULL DEFAULT 0, forget_epoch INTEGER NOT NULL,
 policy_epoch INTEGER NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_due ON work_jobs(status,due);
CREATE TABLE IF NOT EXISTS resource_leases(
 resource TEXT PRIMARY KEY, job_id TEXT NOT NULL, token TEXT NOT NULL, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS profile_versions(
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, digest TEXT NOT NULL,
 spec TEXT NOT NULL, activated REAL NOT NULL, UNIQUE(agent_id,digest));
CREATE TABLE IF NOT EXISTS profile_current(agent_id TEXT PRIMARY KEY, version_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS persona_compilations(
 id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,digest TEXT NOT NULL,
 compiler_version TEXT NOT NULL,spec TEXT NOT NULL,activated REAL NOT NULL,
 UNIQUE(agent_id,digest,compiler_version));
CREATE TABLE IF NOT EXISTS state_journal(
 id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, event_id TEXT NOT NULL,
 appraisal TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL,
 rule_version TEXT NOT NULL, at REAL NOT NULL, UNIQUE(scope_id,event_id));
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 logical_key TEXT NOT NULL UNIQUE, segment INTEGER NOT NULL,
 recipient TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 token TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 1,
 context_revision INTEGER NOT NULL DEFAULT 1, forget_epoch INTEGER NOT NULL,
 policy_epoch INTEGER NOT NULL, receipt TEXT NOT NULL DEFAULT '{}',
 sources TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS delivery_scope ON deliveries(scope_id,status);
CREATE TABLE IF NOT EXISTS delivery_reviews(
 delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_revisions(
 scope_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS interests(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, topic TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', next_due REAL NOT NULL,
 UNIQUE(scope_id,topic));
CREATE TABLE IF NOT EXISTS wants(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, signature TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
 UNIQUE(scope_id,signature));
CREATE TABLE IF NOT EXISTS budget_ledger(
 root_id TEXT NOT NULL, day TEXT NOT NULL, category TEXT NOT NULL,
 used REAL NOT NULL DEFAULT 0, PRIMARY KEY(root_id,day,category));
CREATE TABLE IF NOT EXISTS share_ledger(
 scope_id TEXT NOT NULL, root_id TEXT NOT NULL, day TEXT NOT NULL,
 delivery_id TEXT NOT NULL, answered INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(scope_id,root_id));
CREATE TABLE IF NOT EXISTS grants(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 effects TEXT NOT NULL, resources TEXT NOT NULL, epoch INTEGER NOT NULL,
 expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tombstones(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 memory_ids TEXT NOT NULL, event_ids TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_evidence(
 card_id TEXT NOT NULL, kind TEXT NOT NULL, evidence_id TEXT NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(card_id,kind,evidence_id));
CREATE TABLE IF NOT EXISTS evaluations(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, baseline TEXT NOT NULL,
 candidate TEXT NOT NULL, verdict TEXT NOT NULL, report TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS release_journal(
 id TEXT PRIMARY KEY, previous_id TEXT NOT NULL, artifact TEXT NOT NULL,
 digest TEXT NOT NULL, phase TEXT NOT NULL, evaluation_id TEXT NOT NULL,
 payload TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS relationship_annotation_links(
 annotation_id TEXT PRIMARY KEY REFERENCES memory_annotations(id),
 agent_id TEXT NOT NULL,actor_id TEXT NOT NULL,root_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1),
 relation_kind TEXT NOT NULL CHECK(relation_kind IN ('preference','agreement','repair','expectation','role_description')),
 origin TEXT NOT NULL CHECK(origin IN ('explicit_statement','interpretation','legacy_unverified')),
 confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
 salience TEXT NOT NULL CHECK(salience IN ('low','normal','high')),
 interpretation_ref TEXT,supersedes_annotation_id TEXT REFERENCES memory_annotations(id),
 updated_at REAL NOT NULL,operation_hash TEXT NOT NULL DEFAULT '',UNIQUE(agent_id,root_id,revision));
CREATE INDEX IF NOT EXISTS ix_relation_notes_person ON relationship_annotation_links(agent_id,actor_id,relation_kind);
"""


def init(conn):
    conn.executescript(SQL)
