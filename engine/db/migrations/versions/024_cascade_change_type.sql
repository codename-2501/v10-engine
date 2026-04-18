-- 024: cascade_change_type — Partial/Contextual Cascade 변경 전파 타입 컬럼 추가
-- UP:

ALTER TABLE nodes ADD COLUMN invalidation_change_type TEXT DEFAULT NULL;
ALTER TABLE nodes ADD COLUMN invalidation_affected_sections TEXT DEFAULT NULL;

-- DOWN:
-- ALTER TABLE nodes DROP COLUMN invalidation_change_type;
-- ALTER TABLE nodes DROP COLUMN invalidation_affected_sections;
