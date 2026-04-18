-- 028: Stage 23 Shared Context Ledger — chunk 간 공통 결정사항 기록
-- UP:

CREATE TABLE IF NOT EXISTS shared_context (
  engagement_id   TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  context_key     TEXT NOT NULL,        -- nav_menu / footer / route_map / brand_header / common_actions
  value           TEXT NOT NULL,        -- JSON 또는 HTML 조각
  origin_item     TEXT NOT NULL,        -- 이 값을 생성한 item_key
  version         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (engagement_id, node_id, context_key)
);

CREATE INDEX IF NOT EXISTS idx_shared_context_node
  ON shared_context(engagement_id, node_id);

-- DOWN:
-- DROP TABLE shared_context;
