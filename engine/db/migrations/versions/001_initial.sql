-- 001_initial: AI SI 플랫폼 핵심 스키마 (v1~v7 누적 기반)
-- UP:
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;
PRAGMA temp_store = MEMORY;
PRAGMA busy_timeout = 300;

CREATE TABLE IF NOT EXISTS users (
  id                    TEXT PRIMARY KEY,
  email                 TEXT NOT NULL UNIQUE,
  password_hash         TEXT NOT NULL,
  name                  TEXT NOT NULL,
  role                  TEXT NOT NULL DEFAULT 'DESIGNER'
    CHECK(role IN ('ADMIN', 'SENIOR_DESIGNER', 'DESIGNER')),
  is_active             INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  failed_login_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until          TEXT,
  last_login_at         TEXT,
  version               INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS constitution_versions (
  id           TEXT PRIMARY KEY,
  version      TEXT NOT NULL UNIQUE,
  file_path    TEXT NOT NULL,
  rules_hash   TEXT NOT NULL,
  changelog    TEXT,
  is_active    INTEGER NOT NULL DEFAULT 0,
  version_num  INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  activated_at TEXT
);
INSERT OR IGNORE INTO constitution_versions VALUES (
  'cv_v1_0', 'v1.0', 'prompts/v1/claude_init.md',
  'to_be_computed', '최초 9대 규칙 정의', 1, 0, datetime('now'), datetime('now')
);

CREATE TABLE IF NOT EXISTS templates (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  description   TEXT,
  project_type  TEXT NOT NULL,
  dag_structure TEXT NOT NULL,
  is_published  INTEGER NOT NULL DEFAULT 0,
  is_deprecated INTEGER NOT NULL DEFAULT 0,
  version       INTEGER NOT NULL DEFAULT 0,
  created_by    TEXT NOT NULL REFERENCES users(id),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intake_submissions (
  id                TEXT PRIMARY KEY,
  form_version      TEXT NOT NULL DEFAULT 'v1',
  raw_json          TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'RECEIVED'
    CHECK(status IN ('RECEIVED','VALIDATING','VALID','INVALID',
                     'CONVERTING','CONVERTED','FAILED')),
  engagement_id     TEXT,
  project_id        TEXT,
  validation_errors TEXT NOT NULL DEFAULT '[]',
  conversion_log    TEXT NOT NULL DEFAULT '[]',
  retry_count       INTEGER NOT NULL DEFAULT 0,
  version           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_submissions_status ON intake_submissions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_submissions_engagement ON intake_submissions(engagement_id) WHERE engagement_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_intake_submissions_project ON intake_submissions(project_id) WHERE project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS engagements (
  id                      TEXT PRIMARY KEY,
  name                    TEXT NOT NULL,
  client_name             TEXT NOT NULL,
  intake_submission_id    TEXT REFERENCES intake_submissions(id),
  status                  TEXT NOT NULL DEFAULT 'INTAKE'
    CHECK(status IN ('INTAKE','ACTIVE','PAUSED','COMPLETED','ARCHIVED','FORCE_CLOSED')),
  global_context          TEXT NOT NULL DEFAULT '{}',
  constitution_version_id TEXT REFERENCES constitution_versions(id),
  deadline                TEXT,
  priority                INTEGER NOT NULL DEFAULT 3,
  component_count         INTEGER NOT NULL DEFAULT 0,
  version                 INTEGER NOT NULL DEFAULT 0,
  created_by              TEXT NOT NULL REFERENCES users(id),
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engagements_status   ON engagements(status);
CREATE INDEX IF NOT EXISTS idx_engagements_priority ON engagements(priority, status);
CREATE INDEX IF NOT EXISTS idx_engagements_intake   ON engagements(intake_submission_id) WHERE intake_submission_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS projects (
  id                      TEXT PRIMARY KEY,
  name                    TEXT NOT NULL,
  client_name             TEXT NOT NULL,
  project_type            TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'INTAKE',
  global_context          TEXT NOT NULL DEFAULT '{}',
  constitution_version_id TEXT REFERENCES constitution_versions(id),
  template_id             TEXT REFERENCES templates(id),
  intake_submission_id    TEXT REFERENCES intake_submissions(id),
  engagement_id           TEXT REFERENCES engagements(id),
  component_type          TEXT,
  deadline                TEXT,
  priority                INTEGER NOT NULL DEFAULT 3,
  phase                   TEXT NOT NULL DEFAULT 'API_SERVER'
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  version                 INTEGER NOT NULL DEFAULT 0,
  created_by              TEXT NOT NULL REFERENCES users(id),
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_status     ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_priority   ON projects(priority, status);
CREATE INDEX IF NOT EXISTS idx_projects_phase      ON projects(phase, status);
CREATE INDEX IF NOT EXISTS idx_projects_intake     ON projects(intake_submission_id) WHERE intake_submission_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_projects_engagement ON projects(engagement_id) WHERE engagement_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS project_members (
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id     TEXT NOT NULL REFERENCES users(id),
  role        TEXT NOT NULL DEFAULT 'MEMBER',
  joined_at   TEXT NOT NULL,
  PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);

CREATE TABLE IF NOT EXISTS engagement_members (
  engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
  user_id       TEXT NOT NULL REFERENCES users(id),
  role          TEXT NOT NULL DEFAULT 'MEMBER',
  joined_at     TEXT NOT NULL,
  PRIMARY KEY (engagement_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_engagement_members_user ON engagement_members(user_id);

CREATE TABLE IF NOT EXISTS requirements (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL UNIQUE REFERENCES projects(id),
  current_version INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'DRAFT',
  has_conflict    INTEGER NOT NULL DEFAULT 0,
  approved_by     TEXT REFERENCES users(id),
  approved_at     TEXT,
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requirement_versions (
  id              TEXT PRIMARY KEY,
  requirement_id  TEXT NOT NULL REFERENCES requirements(id),
  version_num     INTEGER NOT NULL,
  content         TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  change_summary  TEXT,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE(requirement_id, version_num)
);
CREATE INDEX IF NOT EXISTS idx_req_versions_req ON requirement_versions(requirement_id, version_num DESC);

CREATE TABLE IF NOT EXISTS dags (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL UNIQUE REFERENCES projects(id),
  template_id      TEXT REFERENCES templates(id),
  status           TEXT NOT NULL DEFAULT 'INITIALIZING',
  total_nodes      INTEGER NOT NULL DEFAULT 0,
  completed_nodes  INTEGER NOT NULL DEFAULT 0,
  current_phase    TEXT NOT NULL DEFAULT 'API_SERVER'
    CHECK(current_phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  topo_order       TEXT,
  version          INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dags_status ON dags(status);

CREATE TABLE IF NOT EXISTS engagement_dags (
  id             TEXT PRIMARY KEY,
  engagement_id  TEXT NOT NULL UNIQUE REFERENCES engagements(id),
  status         TEXT NOT NULL DEFAULT 'INITIALIZING'
    CHECK(status IN ('INITIALIZING','VALID','EXECUTING','PAUSED','COMPLETED','INVALID','CORRUPTED')),
  topo_order     TEXT,
  version        INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engagement_dags_status ON engagement_dags(status);

CREATE TABLE IF NOT EXISTS engagement_edges (
  id                  TEXT PRIMARY KEY,
  engagement_dag_id   TEXT NOT NULL REFERENCES engagement_dags(id),
  from_project_id     TEXT NOT NULL REFERENCES projects(id),
  from_phase          TEXT NOT NULL CHECK(from_phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  to_project_id       TEXT NOT NULL REFERENCES projects(id),
  to_phase            TEXT NOT NULL CHECK(to_phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  gate_trigger_type   TEXT NOT NULL DEFAULT 'MANUAL_DESIGNER'
    CHECK(gate_trigger_type IN ('MANUAL_DESIGNER','AUTO','CLIENT_APPROVAL')),
  is_active           INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL,
  UNIQUE(from_project_id, from_phase, to_project_id, to_phase),
  CHECK(from_project_id != to_project_id)
);
CREATE INDEX IF NOT EXISTS idx_engagement_edges_dag  ON engagement_edges(engagement_dag_id);
CREATE INDEX IF NOT EXISTS idx_engagement_edges_from ON engagement_edges(from_project_id, from_phase) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_engagement_edges_to   ON engagement_edges(to_project_id, to_phase) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS nodes (
  id                      TEXT PRIMARY KEY,
  dag_id                  TEXT NOT NULL REFERENCES dags(id),
  project_id              TEXT NOT NULL REFERENCES projects(id),
  node_type               TEXT NOT NULL CHECK(node_type IN ('TASK','QA','GATE')),
  phase                   TEXT NOT NULL CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  name                    TEXT NOT NULL,
  description             TEXT,
  state                   TEXT NOT NULL DEFAULT 'NOT_STARTED'
    CHECK(state IN ('NOT_STARTED','READY','IN_PROGRESS','COMPLETED','BLOCKED','INVALID','AWAITING_APPROVAL','NEEDS_HUMAN','SUSPENDED','FAILED')),
  qa_pair_node_id         TEXT REFERENCES nodes(id),
  task_pair_node_id       TEXT REFERENCES nodes(id),
  gate_auto_approve       INTEGER NOT NULL DEFAULT 0,
  gate_trigger_type       TEXT NOT NULL DEFAULT 'MANUAL_DESIGNER'
    CHECK(gate_trigger_type IN ('MANUAL_DESIGNER','AUTO','CLIENT_APPROVAL')),
  assigned_model          TEXT,
  constitution_version_id TEXT REFERENCES constitution_versions(id),
  retry_count             INTEGER NOT NULL DEFAULT 0,
  max_retries             INTEGER NOT NULL DEFAULT 3,
  failure_reasons         TEXT DEFAULT '[]',
  stall_count             INTEGER NOT NULL DEFAULT 0,
  task_snapshot           TEXT DEFAULT '[]',
  last_heartbeat          TEXT,
  suspension_reason       TEXT,
  invalidation_pending    INTEGER NOT NULL DEFAULT 0,
  invalidation_source_id  TEXT,
  invalidation_queued_at  TEXT,
  estimated_tokens        INTEGER,
  actual_tokens           INTEGER DEFAULT 0,
  content_hash            TEXT,
  sandbox_path            TEXT,
  priority                INTEGER NOT NULL DEFAULT 3,
  version                 INTEGER NOT NULL DEFAULT 0,
  started_at              TEXT,
  completed_at            TEXT,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_dag             ON nodes(dag_id);
CREATE INDEX IF NOT EXISTS idx_nodes_project_state   ON nodes(project_id, state);
CREATE INDEX IF NOT EXISTS idx_nodes_state           ON nodes(state);
CREATE INDEX IF NOT EXISTS idx_nodes_type            ON nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_phase_state     ON nodes(phase, state);
CREATE INDEX IF NOT EXISTS idx_nodes_zombie_check    ON nodes(last_heartbeat) WHERE state = 'IN_PROGRESS';
CREATE INDEX IF NOT EXISTS idx_nodes_invalidation_pending ON nodes(invalidation_pending, invalidation_queued_at) WHERE invalidation_pending = 1;

CREATE TABLE IF NOT EXISTS edges (
  id           TEXT PRIMARY KEY,
  dag_id       TEXT NOT NULL REFERENCES dags(id),
  from_node_id TEXT NOT NULL REFERENCES nodes(id),
  to_node_id   TEXT NOT NULL REFERENCES nodes(id),
  edge_type    TEXT NOT NULL DEFAULT 'DEPENDS_ON',
  is_active    INTEGER NOT NULL DEFAULT 1,
  weight       INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  UNIQUE(from_node_id, to_node_id),
  CHECK(from_node_id != to_node_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node_id) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_node_id)   WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_edges_dag  ON edges(dag_id);

CREATE TABLE IF NOT EXISTS agent_runs (
  id                      TEXT PRIMARY KEY,
  node_id                 TEXT NOT NULL REFERENCES nodes(id),
  project_id              TEXT NOT NULL REFERENCES projects(id),
  agent_role              TEXT NOT NULL,
  model_used              TEXT,
  constitution_version_id TEXT REFERENCES constitution_versions(id),
  prompt_tokens           INTEGER DEFAULT 0,
  completion_tokens       INTEGER DEFAULT 0,
  total_tokens            INTEGER DEFAULT 0,
  cost_usd                REAL DEFAULT 0.0,
  status                  TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK(status IN ('QUEUED','RUNNING','SUCCESS','FAILED','TIMEOUT','CANCELLED','RATE_LIMITED')),
  attempt_number          INTEGER NOT NULL DEFAULT 1,
  retry_of_run_id         TEXT REFERENCES agent_runs(id),
  failure_reason          TEXT,
  enhanced_prompt_used    INTEGER NOT NULL DEFAULT 0,
  sandbox_path            TEXT,
  output_path             TEXT,
  version                 INTEGER NOT NULL DEFAULT 0,
  started_at              TEXT,
  ended_at                TEXT,
  created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_node    ON agent_runs(node_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status  ON agent_runs(status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS agent_processes (
  id              TEXT PRIMARY KEY,
  agent_run_id    TEXT NOT NULL REFERENCES agent_runs(id),
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  project_id      TEXT NOT NULL REFERENCES projects(id),
  pid             INTEGER,
  status          TEXT NOT NULL DEFAULT 'STARTING'
    CHECK(status IN ('STARTING','ALIVE','RETRYING','EXHAUSTED','TERMINATED')),
  last_heartbeat  TEXT,
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_processes_run    ON agent_processes(agent_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_processes_node   ON agent_processes(node_id);
CREATE INDEX IF NOT EXISTS idx_agent_processes_status ON agent_processes(status);
CREATE INDEX IF NOT EXISTS idx_agent_processes_alive  ON agent_processes(last_heartbeat) WHERE status IN ('ALIVE','RETRYING');

CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT PRIMARY KEY,
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  project_id      TEXT NOT NULL REFERENCES projects(id),
  artifact_type   TEXT NOT NULL,
  file_type       TEXT NOT NULL,
  current_version INTEGER NOT NULL DEFAULT 0,
  is_invalidated  INTEGER NOT NULL DEFAULT 0,
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_versions (
  id              TEXT PRIMARY KEY,
  artifact_id     TEXT NOT NULL REFERENCES artifacts(id),
  version_num     INTEGER NOT NULL,
  storage_path    TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  size_bytes      INTEGER DEFAULT 0,
  is_qa_approved  INTEGER NOT NULL DEFAULT 0,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE(artifact_id, version_num)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_node        ON artifacts(node_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_project     ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_art ON artifact_versions(artifact_id, version_num DESC);

CREATE TABLE IF NOT EXISTS deltas (
  id                TEXT PRIMARY KEY,
  artifact_id       TEXT NOT NULL REFERENCES artifacts(id),
  from_version_num  INTEGER NOT NULL,
  to_version_num    INTEGER NOT NULL,
  diff_strategy     TEXT NOT NULL,
  delta_content     TEXT,
  delta_size_bytes  INTEGER DEFAULT 0,
  is_empty          INTEGER NOT NULL DEFAULT 0,
  impacted_node_ids TEXT DEFAULT '[]',
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deltas_artifact ON deltas(artifact_id, to_version_num DESC);

CREATE TABLE IF NOT EXISTS revision_requests (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id),
  node_id      TEXT NOT NULL REFERENCES nodes(id),
  stage        TEXT NOT NULL,
  message      TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'OPEN'
    CHECK(status IN ('OPEN','IN_PROGRESS','RESOLVED','DISMISSED','DEFERRED')),
  revision_num INTEGER NOT NULL DEFAULT 1,
  created_by   TEXT NOT NULL REFERENCES users(id),
  resolved_at  TEXT,
  dismiss_reason TEXT,
  version      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revision_requests_node    ON revision_requests(node_id, status);
CREATE INDEX IF NOT EXISTS idx_revision_requests_project ON revision_requests(project_id, status);
CREATE INDEX IF NOT EXISTS idx_revision_requests_open    ON revision_requests(status) WHERE status IN ('OPEN','IN_PROGRESS');

CREATE TABLE IF NOT EXISTS project_env_vars (
  id               TEXT PRIMARY KEY,
  scope            TEXT NOT NULL CHECK(scope IN ('GLOBAL','ENGAGEMENT','PROJECT')),
  scope_id         TEXT NOT NULL,
  key              TEXT NOT NULL,
  value_encrypted  TEXT NOT NULL,
  is_secret        INTEGER NOT NULL DEFAULT 1 CHECK(is_secret IN (0, 1)),
  is_test_resource INTEGER NOT NULL DEFAULT 0 CHECK(is_test_resource IN (0, 1)),
  description      TEXT,
  created_by       TEXT NOT NULL REFERENCES users(id),
  created_at       TEXT NOT NULL,
  UNIQUE(scope, scope_id, key)
);
CREATE INDEX IF NOT EXISTS idx_env_vars_scope ON project_env_vars(scope, scope_id);

CREATE TABLE IF NOT EXISTS cost_tracking (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(id),
  date           TEXT NOT NULL,
  model          TEXT NOT NULL DEFAULT 'all',
  total_tokens   INTEGER NOT NULL DEFAULT 0,
  total_cost_usd REAL NOT NULL DEFAULT 0.0,
  run_count      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, date, model)
);
CREATE TABLE IF NOT EXISTS budget_limits (
  id                     TEXT PRIMARY KEY,
  project_id             TEXT NOT NULL UNIQUE REFERENCES projects(id),
  token_limit            INTEGER,
  cost_limit_usd         REAL,
  warn_threshold_pct     INTEGER NOT NULL DEFAULT 70,
  critical_threshold_pct INTEGER NOT NULL DEFAULT 90,
  version                INTEGER NOT NULL DEFAULT 0,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_tracking_project_date ON cost_tracking(project_id, date DESC);

CREATE TABLE IF NOT EXISTS rate_limit_state (
  id                    TEXT PRIMARY KEY DEFAULT 'singleton',
  max_concurrent_agents INTEGER NOT NULL DEFAULT 5,
  current_concurrent    INTEGER NOT NULL DEFAULT 0,
  consecutive_successes INTEGER NOT NULL DEFAULT 0,
  last_rate_limit_at    TEXT,
  total_rate_limit_hits INTEGER NOT NULL DEFAULT 0,
  updated_at            TEXT NOT NULL,
  CHECK (id = 'singleton')
);
INSERT OR IGNORE INTO rate_limit_state VALUES ('singleton', 5, 0, 0, NULL, 0, datetime('now'));

CREATE TABLE IF NOT EXISTS cross_project_patterns (
  id               TEXT PRIMARY KEY,
  pattern_type     TEXT NOT NULL,
  project_type     TEXT,
  phase            TEXT,
  description      TEXT NOT NULL,
  suggested_action TEXT,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  success_rate     REAL,
  is_validated     INTEGER NOT NULL DEFAULT 0,
  is_deprecated    INTEGER NOT NULL DEFAULT 0,
  version          INTEGER NOT NULL DEFAULT 0,
  last_seen_at     TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patterns_type    ON cross_project_patterns(pattern_type, is_deprecated);
CREATE INDEX IF NOT EXISTS idx_patterns_project ON cross_project_patterns(project_type, phase);

CREATE TABLE IF NOT EXISTS escalations (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES projects(id),
  node_id          TEXT REFERENCES nodes(id),
  escalation_type  TEXT NOT NULL,
  severity         TEXT NOT NULL DEFAULT 'MEDIUM',
  title            TEXT NOT NULL,
  description      TEXT,
  status           TEXT NOT NULL DEFAULT 'OPEN',
  assigned_to      TEXT REFERENCES users(id),
  resolution       TEXT,
  timeout_at       TEXT,
  version          INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_escalations_project  ON escalations(project_id, status);
CREATE INDEX IF NOT EXISTS idx_escalations_open     ON escalations(status, severity) WHERE status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS');
CREATE INDEX IF NOT EXISTS idx_escalations_assignee ON escalations(assigned_to, status);

CREATE TABLE IF NOT EXISTS event_store (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       TEXT NOT NULL UNIQUE,
  aggregate_type TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  payload        TEXT NOT NULL,
  checksum       TEXT NOT NULL,
  project_id     TEXT,
  engagement_id  TEXT,
  caused_by      TEXT,
  correlation_id TEXT,
  actor          TEXT NOT NULL DEFAULT 'SYSTEM',
  recorded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_aggregate   ON event_store(aggregate_type, aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_project     ON event_store(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_engagement  ON event_store(engagement_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_type        ON event_store(event_type);
CREATE INDEX IF NOT EXISTS idx_event_recorded    ON event_store(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_correlation ON event_store(correlation_id);
CREATE INDEX IF NOT EXISTS idx_event_caused_by   ON event_store(caused_by);
CREATE TRIGGER IF NOT EXISTS prevent_event_store_update
  BEFORE UPDATE ON event_store
BEGIN
  SELECT RAISE(ABORT, 'event_store is immutable. UPDATE is forbidden.');
END;
CREATE TRIGGER IF NOT EXISTS prevent_event_store_delete
  BEFORE DELETE ON event_store
BEGIN
  SELECT RAISE(ABORT, 'event_store is immutable. DELETE is forbidden.');
END;

CREATE TABLE IF NOT EXISTS outbox (
  id            TEXT PRIMARY KEY,
  event_id      TEXT NOT NULL REFERENCES event_store(event_id),
  event_type    TEXT NOT NULL,
  payload       TEXT NOT NULL,
  destination   TEXT NOT NULL
    CHECK(destination IN ('WEBSOCKET','AGENT_TRIGGER','NOTIFICATION','DASHBOARD')),
  status        TEXT NOT NULL DEFAULT 'PENDING'
    CHECK(status IN ('PENDING','PROCESSING','DELIVERED','FAILED','DEAD_LETTERED')),
  retry_count   INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 5,
  next_retry_at TEXT,
  error_message TEXT,
  created_at    TEXT NOT NULL,
  processed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(next_retry_at, status) WHERE status IN ('PENDING','FAILED');
CREATE INDEX IF NOT EXISTS idx_outbox_event   ON outbox(event_id);

CREATE TABLE IF NOT EXISTS aggregate_snapshots (
  id             TEXT PRIMARY KEY,
  aggregate_type TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  snapshot_seq   INTEGER NOT NULL,
  state          TEXT NOT NULL,
  state_hash     TEXT NOT NULL,
  version        INTEGER NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE(aggregate_type, aggregate_id, snapshot_seq)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_latest ON aggregate_snapshots(aggregate_type, aggregate_id, snapshot_seq DESC);

CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash         TEXT NOT NULL UNIQUE,
  refresh_token_hash TEXT UNIQUE,
  expires_at         TEXT NOT NULL,
  refresh_expires_at TEXT,
  ip_address         TEXT,
  user_agent         TEXT,
  is_revoked         INTEGER NOT NULL DEFAULT 0 CHECK(is_revoked IN (0, 1)),
  created_at         TEXT NOT NULL,
  last_used_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at) WHERE is_revoked = 0;

CREATE TABLE IF NOT EXISTS audit_logs (
  id            TEXT PRIMARY KEY,
  user_id       TEXT REFERENCES users(id) ON DELETE SET NULL,
  user_role     TEXT,
  action        TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id   TEXT,
  old_state     TEXT,
  new_state     TEXT,
  metadata      TEXT DEFAULT '{}',
  ip_address    TEXT,
  user_agent    TEXT,
  status        TEXT NOT NULL DEFAULT 'SUCCESS'
    CHECK(status IN ('SUCCESS','FAILED','DENIED')),
  created_at    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS prevent_audit_log_update
  BEFORE UPDATE ON audit_logs
BEGIN
  SELECT RAISE(ABORT, 'audit_logs is immutable. UPDATE is forbidden.');
END;
CREATE TRIGGER IF NOT EXISTS prevent_audit_log_delete
  BEFORE DELETE ON audit_logs
BEGIN
  SELECT RAISE(ABORT, 'audit_logs is immutable. DELETE is forbidden.');
END;
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id    ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource   ON audit_logs(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action     ON audit_logs(action, status);

CREATE TABLE IF NOT EXISTS provider_credentials (
  id                    TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  provider              TEXT NOT NULL CHECK(provider IN ('anthropic','openai','google')),
  auth_mode             TEXT NOT NULL DEFAULT 'api_key' CHECK(auth_mode IN ('api_key','oauth')),
  key_hash              TEXT UNIQUE,
  key_encrypted         TEXT,
  key_preview           TEXT,
  oauth_config_encrypted TEXT,
  is_active             INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  is_default            INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
  created_by            TEXT REFERENCES users(id) ON DELETE SET NULL,
  last_used_at          TEXT,
  token_expires_at      TEXT,
  usage_count           INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_creds_provider ON provider_credentials(provider, is_active);
CREATE INDEX IF NOT EXISTS idx_provider_creds_default  ON provider_credentials(provider, is_default) WHERE is_active = 1 AND is_default = 1;

CREATE TABLE IF NOT EXISTS artifact_qa_stamps (
  id                  TEXT PRIMARY KEY,
  artifact_id         TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  phase               TEXT NOT NULL CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  qa_node_id          TEXT NOT NULL REFERENCES nodes(id),
  verdict             TEXT NOT NULL DEFAULT 'PASS' CHECK(verdict IN ('PASS','CONDITIONAL_PASS')),
  stamped_at          TEXT NOT NULL,
  approved_by         TEXT REFERENCES users(id),
  verification_stage  TEXT,
  verification_passed INTEGER NOT NULL DEFAULT 0,
  verification_output TEXT,
  UNIQUE(artifact_id, phase)
);
CREATE INDEX IF NOT EXISTS idx_qa_stamps_artifact ON artifact_qa_stamps(artifact_id);
CREATE INDEX IF NOT EXISTS idx_qa_stamps_phase    ON artifact_qa_stamps(phase);

-- DOWN:
DROP TABLE IF EXISTS artifact_qa_stamps;
DROP TABLE IF EXISTS provider_credentials;
DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS aggregate_snapshots;
DROP TABLE IF EXISTS outbox;
DROP TABLE IF EXISTS event_store;
DROP TABLE IF EXISTS escalations;
DROP TABLE IF EXISTS cross_project_patterns;
DROP TABLE IF EXISTS rate_limit_state;
DROP TABLE IF EXISTS budget_limits;
DROP TABLE IF EXISTS cost_tracking;
DROP TABLE IF EXISTS project_env_vars;
DROP TABLE IF EXISTS revision_requests;
DROP TABLE IF EXISTS deltas;
DROP TABLE IF EXISTS artifact_versions;
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS agent_processes;
DROP TABLE IF EXISTS agent_runs;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS nodes;
DROP TABLE IF EXISTS engagement_edges;
DROP TABLE IF EXISTS engagement_dags;
DROP TABLE IF EXISTS dags;
DROP TABLE IF EXISTS requirement_versions;
DROP TABLE IF EXISTS requirements;
DROP TABLE IF EXISTS engagement_members;
DROP TABLE IF EXISTS project_members;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS engagements;
DROP TABLE IF EXISTS intake_submissions;
DROP TABLE IF EXISTS templates;
DROP TABLE IF EXISTS constitution_versions;
DROP TABLE IF EXISTS users;
