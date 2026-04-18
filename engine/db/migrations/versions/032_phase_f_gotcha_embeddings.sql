-- 032: Phase F — project_gotchas 임베딩 컬럼 추가
-- UP:

ALTER TABLE project_gotchas ADD COLUMN embedding_json TEXT;

CREATE INDEX IF NOT EXISTS idx_gotchas_embed
  ON project_gotchas(project_id)
  WHERE embedding_json IS NOT NULL;

-- DOWN:
-- DROP INDEX IF EXISTS idx_gotchas_embed;
-- (embedding_json 컬럼은 SQLite ALTER TABLE DROP COLUMN 제약으로 제거 불가)
