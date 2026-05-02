-- UP:
-- Screen Registry — 화면 목록 정의서에서 추출한 ID↔이름 매핑의 구조화 저장소.
-- Tier 2-B/C: 후속 design skill (UI 시안 / 화면 설계서 / 페이지 레시피) 이 artifact
-- content regex 파싱 대신 본 테이블에서 조회. 선행 노드 state / artifact 구조 변화 /
-- 타입 시그니처 혼용 등 fragility 원천 제거.
CREATE TABLE IF NOT EXISTS screen_registry (
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  screen_id       TEXT NOT NULL,
  screen_name     TEXT NOT NULL,
  domain          TEXT,
  priority        TEXT,
  intent          TEXT,
  source_version  INTEGER,
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (project_id, screen_id)
);
CREATE INDEX IF NOT EXISTS idx_screen_registry_project ON screen_registry(project_id);

-- DOWN:
DROP INDEX IF EXISTS idx_screen_registry_project;
DROP TABLE IF EXISTS screen_registry;
