-- 008_node_budget_overrides: TOKEN_BUDGET 노드별 오버라이드 테이블
-- UP:
CREATE TABLE IF NOT EXISTS node_budget_overrides (
  node_id               TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  max_input_override    INTEGER,
  max_output_override   INTEGER,
  phase_limit_override  INTEGER,
  reason                TEXT,
  created_by            TEXT NOT NULL REFERENCES users(id),
  created_at            TEXT NOT NULL
);
-- DOWN:
DROP TABLE IF EXISTS node_budget_overrides;
