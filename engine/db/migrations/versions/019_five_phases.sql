-- 019_five_phases: 5단계 구조 전환 + dags PK 복원
-- UP:

PRAGMA foreign_keys = OFF;

-- dags 테이블 재생성 (PK + CHECK 제거한 새 스키마)
CREATE TABLE dags_new (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL UNIQUE REFERENCES projects(id),
  template_id      TEXT,
  status           TEXT NOT NULL DEFAULT 'INITIALIZING',
  total_nodes      INTEGER NOT NULL DEFAULT 0,
  completed_nodes  INTEGER NOT NULL DEFAULT 0,
  current_phase    TEXT NOT NULL DEFAULT 'DEFINE',
  topo_order       TEXT,
  version          INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO dags_new (id, project_id, template_id, status, total_nodes, completed_nodes,
  current_phase, topo_order, version, created_at, updated_at)
SELECT id, project_id, template_id, COALESCE(status,'INITIALIZING'),
  COALESCE(total_nodes,0), COALESCE(completed_nodes,0),
  COALESCE(current_phase,'DEFINE'), topo_order, COALESCE(version,0),
  COALESCE(created_at,datetime('now')), COALESCE(updated_at,datetime('now'))
FROM dags;
DROP TABLE dags;
ALTER TABLE dags_new RENAME TO dags;
CREATE INDEX IF NOT EXISTS idx_dags_status ON dags(status);

-- phase 값 변환 (기존 데이터)
UPDATE nodes SET phase = 'DEFINE' WHERE phase IN ('PLANNING', 'API_SERVER');
UPDATE nodes SET phase = 'BUILD' WHERE phase = 'DEVELOPMENT';
UPDATE nodes SET phase = 'VERIFY' WHERE phase = 'TESTING';
UPDATE nodes SET phase = 'DELIVER' WHERE phase IN ('INFRASTRUCTURE', 'DELIVERY');

UPDATE dags SET current_phase = 'DEFINE' WHERE current_phase IN ('PLANNING', 'API_SERVER');
UPDATE dags SET current_phase = 'BUILD' WHERE current_phase = 'DEVELOPMENT';
UPDATE dags SET current_phase = 'VERIFY' WHERE current_phase = 'TESTING';
UPDATE dags SET current_phase = 'DELIVER' WHERE current_phase IN ('INFRASTRUCTURE', 'DELIVERY');

PRAGMA foreign_keys = ON;

-- DOWN:
-- 비가역
