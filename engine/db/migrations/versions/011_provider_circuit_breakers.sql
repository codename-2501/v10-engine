-- 011_provider_circuit_breakers: DB 기반 CB 상태 (멀티 프로세스 대응)
-- UP:
CREATE TABLE IF NOT EXISTS provider_circuit_breakers (
  provider_name   TEXT PRIMARY KEY,
  state           TEXT NOT NULL DEFAULT 'CLOSED'
    CHECK(state IN ('CLOSED','OPEN','HALF_OPEN')),
  failure_count   INTEGER NOT NULL DEFAULT 0,
  success_count   INTEGER NOT NULL DEFAULT 0,
  opened_at       TEXT,
  last_attempt_at TEXT,
  version         INTEGER NOT NULL DEFAULT 1
);
INSERT OR IGNORE INTO provider_circuit_breakers (provider_name) VALUES ('anthropic');
INSERT OR IGNORE INTO provider_circuit_breakers (provider_name) VALUES ('openai');
INSERT OR IGNORE INTO provider_circuit_breakers (provider_name) VALUES ('google');
-- DOWN:
DROP TABLE IF EXISTS provider_circuit_breakers;
