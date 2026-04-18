-- 012_notification_subscriptions: Outbox 재사용 알림 구독 관리
-- UP:
CREATE TABLE IF NOT EXISTS notification_subscriptions (
  id                  TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  engagement_id       TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
  event_type          TEXT NOT NULL,
  channel_type        TEXT NOT NULL CHECK(channel_type IN ('WEBHOOK','EMAIL','LOG_ONLY')),
  encrypted_endpoint  TEXT,
  encrypted_recipient TEXT,
  is_active           INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(engagement_id, event_type, channel_type)
);
CREATE INDEX IF NOT EXISTS idx_notification_subs_engagement ON notification_subscriptions(engagement_id, is_active);
CREATE INDEX IF NOT EXISTS idx_notification_subs_event_type ON notification_subscriptions(event_type, is_active);
-- DOWN:
DROP TABLE IF EXISTS notification_subscriptions;
