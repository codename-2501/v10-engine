-- 027: Stage 21 Failure Classifier — nodes 테이블에 failure_class 컬럼 추가
-- UP:

ALTER TABLE nodes ADD COLUMN failure_class TEXT;
ALTER TABLE nodes ADD COLUMN failure_reclassified_at TEXT;
ALTER TABLE nodes ADD COLUMN auto_recovery_attempts INTEGER NOT NULL DEFAULT 0;

-- FAILED 상태 노드를 빠르게 조회 (watchdog 이 주기적으로 사용)
CREATE INDEX IF NOT EXISTS idx_nodes_failure_class
  ON nodes(state, failure_class)
  WHERE state='FAILED';

-- DOWN:
-- ALTER TABLE nodes DROP COLUMN failure_class;
-- ALTER TABLE nodes DROP COLUMN failure_reclassified_at;
-- ALTER TABLE nodes DROP COLUMN auto_recovery_attempts;
