-- 018_skipped_state: SKIPPED 상태 추가 + 누락 컬럼 복원
-- UP:

PRAGMA foreign_keys = OFF;

CREATE TABLE nodes_new (
  id                      TEXT PRIMARY KEY,
  dag_id                  TEXT NOT NULL REFERENCES dags(id),
  project_id              TEXT NOT NULL REFERENCES projects(id),
  node_type               TEXT NOT NULL CHECK(node_type IN ('TASK','QA','GATE')),
  phase                   TEXT NOT NULL,
  name                    TEXT NOT NULL,
  description             TEXT,
  state                   TEXT NOT NULL DEFAULT 'NOT_STARTED'
    CHECK(state IN ('NOT_STARTED','READY','IN_PROGRESS','COMPLETED','BLOCKED',
                    'INVALID','AWAITING_APPROVAL','NEEDS_HUMAN','SUSPENDED',
                    'FAILED','SKIPPED')),
  qa_pair_node_id         TEXT REFERENCES nodes(id),
  task_pair_node_id       TEXT REFERENCES nodes(id),
  gate_auto_approve       INTEGER NOT NULL DEFAULT 0,
  gate_trigger_type       TEXT NOT NULL DEFAULT 'MANUAL_DESIGNER',
  assigned_model          TEXT,
  constitution_version_id TEXT,
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
  created_at              TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 현재 테이블의 컬럼만 복사 (누락 컬럼은 DEFAULT 처리)
INSERT INTO nodes_new (
  id, dag_id, project_id, node_type, phase, name, state, priority,
  assigned_model, retry_count, max_retries, qa_pair_node_id, task_pair_node_id,
  gate_auto_approve, suspension_reason, last_heartbeat, completed_at,
  version, created_at, updated_at
)
SELECT
  id, dag_id, project_id, node_type, phase, name, state, priority,
  assigned_model, retry_count, max_retries, qa_pair_node_id, task_pair_node_id,
  gate_auto_approve, suspension_reason, last_heartbeat, completed_at,
  version, created_at, updated_at
FROM nodes;

DROP TABLE nodes;
ALTER TABLE nodes_new RENAME TO nodes;

CREATE INDEX IF NOT EXISTS idx_nodes_dag           ON nodes(dag_id, state);
CREATE INDEX IF NOT EXISTS idx_nodes_project_state ON nodes(project_id, state);
CREATE INDEX IF NOT EXISTS idx_nodes_state         ON nodes(state);
CREATE INDEX IF NOT EXISTS idx_nodes_zombie        ON nodes(last_heartbeat) WHERE state = 'IN_PROGRESS';

PRAGMA foreign_keys = ON;

-- DOWN:
-- 비가역
