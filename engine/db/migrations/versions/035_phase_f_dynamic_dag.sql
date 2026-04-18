-- 035: Phase F-2 — DynamicDAGExtension (런타임 노드 주입)
-- UP:

-- 주입된 노드의 부모(주입 트리거) 추적 — 루프 감지용
ALTER TABLE nodes ADD COLUMN injected_by TEXT;

CREATE INDEX IF NOT EXISTS idx_nodes_injected
  ON nodes(injected_by)
  WHERE injected_by IS NOT NULL;

-- DOWN:
-- DROP INDEX IF EXISTS idx_nodes_injected;
-- (injected_by 컬럼은 SQLite ALTER TABLE DROP COLUMN 제약으로 제거 불가)
