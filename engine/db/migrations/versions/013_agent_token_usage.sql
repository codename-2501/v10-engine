-- 013_agent_token_usage: Phase별 실시간 토큰 사용량 추적 + 요약 뷰
-- UP:
CREATE TABLE IF NOT EXISTS agent_token_usage (
  id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  node_id          TEXT NOT NULL REFERENCES nodes(id),
  agent_run_id     TEXT REFERENCES agent_runs(id),
  engagement_id    TEXT NOT NULL,
  phase            TEXT NOT NULL
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  model_name       TEXT NOT NULL,
  input_tokens     INTEGER NOT NULL,
  output_tokens    INTEGER NOT NULL,
  estimated_input  INTEGER,
  recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_token_usage_node       ON agent_token_usage(node_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_engagement ON agent_token_usage(engagement_id, phase);
CREATE INDEX IF NOT EXISTS idx_token_usage_recorded   ON agent_token_usage(recorded_at DESC);

CREATE VIEW IF NOT EXISTS v_phase_token_summary AS
SELECT
  engagement_id,
  phase,
  SUM(input_tokens)  AS total_input,
  SUM(output_tokens) AS total_output,
  SUM(input_tokens + output_tokens) AS total_tokens,
  COUNT(*)           AS call_count
FROM agent_token_usage
GROUP BY engagement_id, phase;
-- DOWN:
DROP VIEW IF EXISTS v_phase_token_summary;
DROP TABLE IF EXISTS agent_token_usage;
