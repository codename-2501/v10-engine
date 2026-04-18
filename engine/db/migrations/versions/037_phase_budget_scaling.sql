-- 037: V10 — Phase 예산 동적 스케일링 (Level 1 Pre-scale + Level 2 Runtime Realloc)
-- UP:

-- engagements 단위 phase 예산 override (JSON: {"DEFINE": 600000, "DESIGN": 2200000, ...})
-- Level 1: intake 접수 시 size_estimator + budget_scaler 가 계산해 저장
-- Level 2: PhaseBudgetExceededError 발생 시 executor 가 다른 Phase 여유 차용해 갱신
ALTER TABLE engagements ADD COLUMN phase_budget_override TEXT;

-- Level 2 realloc 이력 추적 (무한 루프 감지용 — max 2회/engagement)
CREATE TABLE IF NOT EXISTS budget_realloc_log (
  id              TEXT PRIMARY KEY,
  engagement_id   TEXT NOT NULL,
  from_phase      TEXT NOT NULL,
  to_phase        TEXT NOT NULL,
  transferred     INTEGER NOT NULL,
  reason          TEXT,
  created_at      TEXT NOT NULL,
  FOREIGN KEY (engagement_id) REFERENCES engagements(id)
);

CREATE INDEX IF NOT EXISTS idx_budget_realloc_engagement
  ON budget_realloc_log(engagement_id, created_at DESC);

-- DOWN:
-- DROP INDEX IF EXISTS idx_budget_realloc_engagement;
-- DROP TABLE IF EXISTS budget_realloc_log;
-- (ALTER TABLE DROP COLUMN 불가 — phase_budget_override 제거 불가)
