-- 026: atomic_state + content_cache — Stage 3 (StateStore) + Stage 8 (Content Hash Cache)
-- UP:

-- Stage 3: Map-Reduce 된 task 단위 idempotent 상태 저장
CREATE TABLE IF NOT EXISTS atomic_state (
  engagement_id   TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  item_key        TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'PENDING'
    CHECK(status IN ('PENDING','RESERVED','COMPLETE','FAILED','NEEDS_HUMAN','SKIPPED')),
  retry_count     INTEGER NOT NULL DEFAULT 0,
  artifact_hash   TEXT,
  reason          TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (engagement_id, node_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_atomic_state_node ON atomic_state(node_id, status);
CREATE INDEX IF NOT EXISTS idx_atomic_state_engagement ON atomic_state(engagement_id, status);

-- Stage 4: Coverage 검증 리포트
CREATE TABLE IF NOT EXISTS coverage_report (
  engagement_id   TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  expected_count  INTEGER NOT NULL DEFAULT 0,
  produced_count  INTEGER NOT NULL DEFAULT 0,
  missing_items   TEXT,  -- JSON array
  retry_attempts  INTEGER NOT NULL DEFAULT 0,
  verified_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (engagement_id, node_id)
);

-- Stage 8: Content Hash Cache (input hash 기반 artifact 재사용)
CREATE TABLE IF NOT EXISTS content_cache (
  cache_key       TEXT PRIMARY KEY,          -- sha256(namespace + input_blocks)
  namespace       TEXT NOT NULL,             -- engagement_id:node_type
  node_type       TEXT NOT NULL,
  input_hash      TEXT NOT NULL,
  content         TEXT NOT NULL,             -- 재사용할 artifact content
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  hit_count       INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  last_hit_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_cache_ns ON content_cache(namespace, input_hash);
CREATE INDEX IF NOT EXISTS idx_content_cache_type ON content_cache(node_type, input_hash);

-- DOWN:
-- DROP TABLE atomic_state;
-- DROP TABLE coverage_report;
-- DROP TABLE content_cache;
