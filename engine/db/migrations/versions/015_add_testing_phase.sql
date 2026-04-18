-- 015_add_testing_phase.sql
-- TESTING phase 추가: nodes, dags, phase_transitions, node_snapshots 테이블
-- SQLite는 CHECK 제약 ALTER 불가 → 재생성 방식

PRAGMA foreign_keys=OFF;

-- ── nodes ──────────────────────────────────────────────────────────────────
ALTER TABLE nodes RENAME TO nodes_bak_015;
CREATE TABLE nodes (
  id                      TEXT PRIMARY KEY,
  dag_id                  TEXT NOT NULL REFERENCES dags(id),
  project_id              TEXT NOT NULL REFERENCES projects(id),
  node_type               TEXT NOT NULL CHECK(node_type IN ('TASK','QA','GATE')),
  phase                   TEXT NOT NULL CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','TESTING','INFRASTRUCTURE','DELIVERY')),
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
INSERT INTO nodes SELECT * FROM nodes_bak_015;
DROP TABLE nodes_bak_015;

-- ── dags (current_phase) ───────────────────────────────────────────────────
ALTER TABLE dags RENAME TO dags_bak_015;
CREATE TABLE dags (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id),
  status            TEXT NOT NULL DEFAULT 'PENDING'
    CHECK(status IN ('PENDING','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED')),
  current_phase     TEXT CHECK(current_phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','TESTING','INFRASTRUCTURE','DELIVERY')),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
INSERT INTO dags SELECT * FROM dags_bak_015;
DROP TABLE dags_bak_015;

PRAGMA foreign_keys=ON;
