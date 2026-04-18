-- 020_fix_phase_checks: agent_token_usage phase CHECK 5단계로 변경
-- UP:

PRAGMA foreign_keys = OFF;

CREATE TABLE agent_token_usage_new (
  id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  node_id          TEXT NOT NULL REFERENCES nodes(id),
  agent_run_id     TEXT,
  engagement_id    TEXT NOT NULL,
  phase            TEXT NOT NULL
    CHECK(phase IN ('DEFINE','DESIGN','BUILD','VERIFY','DELIVER')),
  model_name       TEXT NOT NULL,
  input_tokens     INTEGER NOT NULL,
  output_tokens    INTEGER NOT NULL,
  estimated_input  INTEGER,
  recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO agent_token_usage_new
SELECT id, node_id, agent_run_id, engagement_id,
  CASE phase
    WHEN 'PLANNING' THEN 'DEFINE'
    WHEN 'API_SERVER' THEN 'DEFINE'
    WHEN 'DEVELOPMENT' THEN 'BUILD'
    WHEN 'TESTING' THEN 'VERIFY'
    WHEN 'INFRASTRUCTURE' THEN 'DELIVER'
    WHEN 'DELIVERY' THEN 'DELIVER'
    ELSE phase
  END,
  model_name, input_tokens, output_tokens, estimated_input, recorded_at
FROM agent_token_usage;

DROP TABLE agent_token_usage;
ALTER TABLE agent_token_usage_new RENAME TO agent_token_usage;

PRAGMA foreign_keys = ON;

-- DOWN:
-- 비가역
