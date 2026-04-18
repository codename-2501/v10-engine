-- 022_artifact_file_manifest: 산출물 파일 매니페스트 — QA 직접 파일 읽기 지원
-- UP:

-- artifact_versions에 file_manifest 컬럼 추가
-- file_manifest: JSON 배열 ["src/app/globals.css", "src/components/ui/Button.tsx", ...]
-- NULL: 기존 산출물 (파일 매니페스트 없음) → 기존 방식(storage_path 내용) 유지
ALTER TABLE artifact_versions ADD COLUMN file_manifest TEXT DEFAULT NULL;

-- 인덱스: file_manifest가 있는 버전 빠른 조회 (QA 컨텍스트 로딩용)
CREATE INDEX IF NOT EXISTS idx_av_file_manifest
  ON artifact_versions(artifact_id)
  WHERE file_manifest IS NOT NULL;

-- DOWN:
-- SQLite는 컬럼 삭제를 직접 지원하지 않으므로 인덱스만 제거
-- DROP INDEX IF EXISTS idx_av_file_manifest;
-- (file_manifest 컬럼은 NULL 기본값이므로 롤백 시 무시해도 안전)
