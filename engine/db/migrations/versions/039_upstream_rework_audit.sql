-- 039: V10 — 거시 진단(QA root cause) 감사 추적 테이블
-- UP:

-- QA FAIL 거시 진단 결과 기록.
-- recalibrate_upstream_keywords.py 가 주기적으로 분석하여
-- false positive 키워드 보정 + 미감지 사례 새 키워드 후보 도출.
CREATE TABLE IF NOT EXISTS upstream_rework_audit (
  id                    TEXT PRIMARY KEY,
  qa_node_id            TEXT NOT NULL,
  detected_categories   TEXT NOT NULL,   -- JSON 배열 (예: ["DESIGN","API"])
  invalidated_node_ids  TEXT NOT NULL,   -- JSON 배열 (영향받은 상위 TASK id)
  outcome               TEXT NOT NULL DEFAULT 'pending'
    CHECK(outcome IN ('pending','success','false_positive','timeout','no_effect')),
  method                TEXT NOT NULL    -- 'keyword' | 'ai' | 'dry-run'
    CHECK(method IN ('keyword','ai','ai_empty','dry-run')),
  notes                 TEXT,            -- recalibrate 도구가 채우는 분석 노트
  created_at            TEXT NOT NULL,
  resolved_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_rework_audit_outcome
  ON upstream_rework_audit(outcome, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rework_audit_method
  ON upstream_rework_audit(method, created_at DESC);

-- DOWN:
-- DROP INDEX IF EXISTS idx_rework_audit_method;
-- DROP INDEX IF EXISTS idx_rework_audit_outcome;
-- DROP TABLE IF EXISTS upstream_rework_audit;
