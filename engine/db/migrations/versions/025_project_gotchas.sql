-- 025: project_gotchas — 프로젝트 레벨 실수 학습 DB (Gotchas Tracker)
-- UP:

CREATE TABLE IF NOT EXISTS project_gotchas (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  source_node_id TEXT,
  source_node_name TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gotchas_project ON project_gotchas(project_id);

-- DOWN:
-- DROP TABLE project_gotchas;
