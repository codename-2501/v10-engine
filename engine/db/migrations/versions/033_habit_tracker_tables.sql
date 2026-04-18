-- 033: Habit Tracker (Personal Habit Tracking + AI Analysis)
-- UP:

CREATE TABLE IF NOT EXISTS habits (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL,
  name           TEXT NOT NULL,
  category       TEXT NOT NULL
    CHECK(category IN ('health', 'learning', 'productivity', 'wellness', 'other')),
  target_days    INTEGER DEFAULT 3,
  created_at     TEXT NOT NULL,
  metadata_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_habits_user
  ON habits(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS habit_logs (
  id             TEXT PRIMARY KEY,
  habit_id       TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  logged_at      TEXT NOT NULL,
  notes          TEXT,
  created_at     TEXT NOT NULL,
  FOREIGN KEY(habit_id) REFERENCES habits(id)
);

CREATE INDEX IF NOT EXISTS idx_habit_logs_habit
  ON habit_logs(habit_id, logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_habit_logs_user
  ON habit_logs(user_id, logged_at DESC);

-- DOWN:
-- DROP INDEX IF EXISTS idx_habit_logs_user;
-- DROP INDEX IF EXISTS idx_habit_logs_habit;
-- DROP TABLE IF EXISTS habit_logs;
-- DROP INDEX IF EXISTS idx_habits_user;
-- DROP TABLE IF EXISTS habits;
