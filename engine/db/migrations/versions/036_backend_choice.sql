-- 036: 백엔드 선택지 구조화 — Phase F-4 (InstantDB 생태계 흡수)
-- UP:

ALTER TABLE engagements ADD COLUMN backend_choice TEXT DEFAULT 'sql';
ALTER TABLE projects    ADD COLUMN backend_choice TEXT DEFAULT 'sql';

CREATE INDEX IF NOT EXISTS idx_engagements_backend
  ON engagements(backend_choice);
CREATE INDEX IF NOT EXISTS idx_projects_backend
  ON projects(backend_choice);

-- 값 규약 (enum 유사, 코드에서 화이트리스트 검증):
--   'sql'       : 기본값 — generic SQL 백엔드 (PostgreSQL/MySQL/SQLite 등)
--   'instantdb' : InstantDB — Triple Store + 실시간 동기화 + 오프라인
--   'firebase'  : Google Firebase — Firestore + Auth + Functions
--   'supabase'  : Supabase — Postgres + Auth + Realtime + Edge Functions
--   'custom'    : 기타 (techNotes 에 상세)
--
-- 이 값에 따라 executor.py 에서 backend_requirement 가 명시된 스킬만 로드.

-- DOWN:
-- DROP INDEX IF EXISTS idx_projects_backend;
-- DROP INDEX IF EXISTS idx_engagements_backend;
-- (SQLite ALTER TABLE DROP COLUMN 제약으로 컬럼 제거 불가)
