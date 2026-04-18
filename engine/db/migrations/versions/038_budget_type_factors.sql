-- 038: V10 — TYPE_FACTOR 외부화 + 실측 기반 자동 재튜닝 지원
-- UP:

-- 프로젝트 타입 × Phase 별 스케일 팩터.
-- budget_scaler.scale_engagement_budget() 이 이 테이블을 우선 조회.
-- recalibrate_type_factor.py 가 주기적으로 agent_token_usage 실측으로 갱신.
CREATE TABLE IF NOT EXISTS budget_type_factors (
  project_type       TEXT NOT NULL CHECK(project_type IN ('app','si','mlops','data','mixed')),
  phase              TEXT NOT NULL CHECK(phase IN ('DEFINE','DESIGN','BUILD','VERIFY','DELIVER')),
  factor             REAL NOT NULL,
  sample_size        INTEGER NOT NULL DEFAULT 0,
  source             TEXT NOT NULL DEFAULT 'seed'
    CHECK(source IN ('seed','measured','manual','ml_predicted')),
  last_calibrated_at TEXT NOT NULL,
  note               TEXT,
  PRIMARY KEY (project_type, phase)
);

-- 초기 seed 값 (budget_scaler.py 하드코딩 복사본)
-- 이후 recalibrate 실행으로 source='measured' 로 전환됨
INSERT INTO budget_type_factors (project_type, phase, factor, source, last_calibrated_at) VALUES
  ('app',   'DEFINE',  0.8, 'seed', datetime('now')),
  ('app',   'DESIGN',  1.8, 'seed', datetime('now')),
  ('app',   'BUILD',   1.5, 'seed', datetime('now')),
  ('app',   'VERIFY',  1.2, 'seed', datetime('now')),
  ('app',   'DELIVER', 0.4, 'seed', datetime('now')),
  ('si',    'DEFINE',  1.3, 'seed', datetime('now')),
  ('si',    'DESIGN',  1.2, 'seed', datetime('now')),
  ('si',    'BUILD',   2.5, 'seed', datetime('now')),
  ('si',    'VERIFY',  1.8, 'seed', datetime('now')),
  ('si',    'DELIVER', 0.6, 'seed', datetime('now')),
  ('mlops', 'DEFINE',  1.0, 'seed', datetime('now')),
  ('mlops', 'DESIGN',  1.5, 'seed', datetime('now')),
  ('mlops', 'BUILD',   2.8, 'seed', datetime('now')),
  ('mlops', 'VERIFY',  2.0, 'seed', datetime('now')),
  ('mlops', 'DELIVER', 0.5, 'seed', datetime('now')),
  ('data',  'DEFINE',  1.0, 'seed', datetime('now')),
  ('data',  'DESIGN',  1.0, 'seed', datetime('now')),
  ('data',  'BUILD',   2.0, 'seed', datetime('now')),
  ('data',  'VERIFY',  1.3, 'seed', datetime('now')),
  ('data',  'DELIVER', 0.4, 'seed', datetime('now')),
  ('mixed', 'DEFINE',  1.1, 'seed', datetime('now')),
  ('mixed', 'DESIGN',  1.4, 'seed', datetime('now')),
  ('mixed', 'BUILD',   1.8, 'seed', datetime('now')),
  ('mixed', 'VERIFY',  1.4, 'seed', datetime('now')),
  ('mixed', 'DELIVER', 0.5, 'seed', datetime('now'));

-- 재튜닝 이력 (감사 추적)
CREATE TABLE IF NOT EXISTS budget_factor_calibrations (
  id                 TEXT PRIMARY KEY,
  project_type       TEXT NOT NULL,
  phase              TEXT NOT NULL,
  old_factor         REAL NOT NULL,
  new_factor         REAL NOT NULL,
  sample_size        INTEGER NOT NULL,
  median_actual      INTEGER,      -- 중앙값 토큰 소비
  base_budget        INTEGER,      -- BASE_BUDGET[phase]
  triggered_by       TEXT,         -- 'cli' | 'api' | 'scheduled'
  created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calibrations_time
  ON budget_factor_calibrations(created_at DESC);

-- DOWN:
-- DROP INDEX IF EXISTS idx_calibrations_time;
-- DROP TABLE IF EXISTS budget_factor_calibrations;
-- DROP TABLE IF EXISTS budget_type_factors;
