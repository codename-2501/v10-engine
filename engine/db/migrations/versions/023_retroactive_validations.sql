-- 023: retroactive_validations 테이블 — 소급 검증 기록 저장
-- UP:

CREATE TABLE IF NOT EXISTS retroactive_validations (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    check_name      TEXT NOT NULL,
    result          TEXT NOT NULL,  -- 'PASS' or 'FAIL'
    failures        TEXT,           -- JSON array of failure messages
    invalidated_nodes TEXT,         -- JSON array of node IDs set to INVALID
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retro_project ON retroactive_validations(project_id);

-- DOWN:
-- DROP TABLE IF EXISTS retroactive_validations;
