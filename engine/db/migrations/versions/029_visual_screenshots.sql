-- 029: Stage 25 Visual Regression — viewport 별 스크린샷 저장
-- UP:

CREATE TABLE IF NOT EXISTS visual_screenshots (
  engagement_id   TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  item_key        TEXT NOT NULL,
  viewport        TEXT NOT NULL,        -- mobile | tablet | desktop
  screenshot_blob BLOB,
  issues          TEXT,                 -- JSON array (blank_page, overflow, z_index_war 등)
  baseline_diff_pct REAL,               -- 선택 — baseline 과 픽셀 diff %
  captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (engagement_id, node_id, item_key, viewport)
);

CREATE INDEX IF NOT EXISTS idx_visual_screenshots_node
  ON visual_screenshots(engagement_id, node_id);

-- DOWN:
-- DROP TABLE visual_screenshots;
