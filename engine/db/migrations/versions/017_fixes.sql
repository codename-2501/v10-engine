-- 017_fixes: outbox.engagement_id 추가 + agent_token_usage TESTING phase 허용
-- UP:

-- 1. outbox에 engagement_id 컬럼 추가 (WebSocket 폴링 fix)
ALTER TABLE outbox ADD COLUMN engagement_id TEXT;
CREATE INDEX IF NOT EXISTS idx_outbox_engagement ON outbox(engagement_id)
  WHERE engagement_id IS NOT NULL;

-- 2. agent_token_usage: TESTING phase 허용
--    SQLite는 CHECK 제약 직접 변경 불가 → 재생성
DROP VIEW IF EXISTS v_phase_token_summary;
ALTER TABLE agent_token_usage RENAME TO agent_token_usage_bak_017;
CREATE TABLE agent_token_usage (
  id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  node_id          TEXT NOT NULL REFERENCES nodes(id),
  agent_run_id     TEXT REFERENCES agent_runs(id),
  engagement_id    TEXT NOT NULL,
  phase            TEXT NOT NULL
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','TESTING','INFRASTRUCTURE','DELIVERY')),
  model_name       TEXT NOT NULL,
  input_tokens     INTEGER NOT NULL,
  output_tokens    INTEGER NOT NULL,
  estimated_input  INTEGER,
  recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_token_usage_node       ON agent_token_usage(node_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_engagement ON agent_token_usage(engagement_id, phase);
CREATE INDEX IF NOT EXISTS idx_token_usage_recorded   ON agent_token_usage(recorded_at DESC);
INSERT INTO agent_token_usage SELECT * FROM agent_token_usage_bak_017;
DROP TABLE agent_token_usage_bak_017;
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
-- Note: SQLite는 DROP COLUMN 미지원 → outbox.engagement_id 제거 불가
