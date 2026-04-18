-- 030: Stage 26 PRD Clarifier — 모호성 자동 감지 + 사용자 질문·답변 기록
-- UP:

CREATE TABLE IF NOT EXISTS prd_clarifications (
  engagement_id  TEXT NOT NULL,
  question_id    TEXT NOT NULL,
  question       TEXT NOT NULL,
  options        TEXT,                    -- JSON array (선택지)
  severity       TEXT NOT NULL
    CHECK(severity IN ('blocking','advisory')),
  category       TEXT,                    -- quantity_vague / role_undefined / ...
  answer         TEXT,
  answered_at    TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (engagement_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_prd_clar_pending
  ON prd_clarifications(engagement_id, answer)
  WHERE answer IS NULL;

-- DOWN:
-- DROP TABLE prd_clarifications;
