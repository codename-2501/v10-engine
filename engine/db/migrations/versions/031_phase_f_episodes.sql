-- 031: Phase F — Episode Memory (Vector 기반 실패 패턴 학습)
-- UP:

CREATE TABLE IF NOT EXISTS episodes (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL,
  node_id        TEXT,
  node_name      TEXT,
  episode_type   TEXT NOT NULL
    CHECK(episode_type IN ('gotcha', 'success', 'pattern')),
  content        TEXT NOT NULL,
  metadata_json  TEXT NOT NULL DEFAULT '{}',
  embedding_json TEXT,
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_project
  ON episodes(project_id, episode_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_episodes_embed
  ON episodes(project_id)
  WHERE embedding_json IS NOT NULL;

-- DOWN:
-- DROP INDEX IF EXISTS idx_episodes_embed;
-- DROP INDEX IF EXISTS idx_episodes_project;
-- DROP TABLE IF EXISTS episodes;
