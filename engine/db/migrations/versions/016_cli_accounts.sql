-- 016_cli_accounts: Claude Code CLI 다계정 지원
-- UP:
CREATE TABLE IF NOT EXISTS cli_accounts (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,       -- 표시명 (예: "Pro", "Max")
  tier        TEXT NOT NULL DEFAULT 'pro' -- 'pro' | 'max'
    CHECK(tier IN ('pro', 'max')),
  config_dir  TEXT,                       -- CLAUDE_CONFIG_DIR (NULL = 기본 ~/.claude)
  is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  priority    INTEGER NOT NULL DEFAULT 0, -- 낮을수록 먼저 시도
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);
-- DOWN:
DROP TABLE IF EXISTS cli_accounts;
