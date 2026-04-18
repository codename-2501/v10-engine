-- 034: Habit Tracker — 중복 기록 방지 UNIQUE 인덱스
-- UP:

-- 같은 날짜에 같은 습관 중복 기록 방지
-- (SQLite ALTER TABLE ADD CONSTRAINT 불가 → UNIQUE INDEX로 우회)
CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_logs_unique_day
  ON habit_logs(habit_id, logged_at);

-- DOWN:
-- DROP INDEX IF EXISTS idx_habit_logs_unique_day;
