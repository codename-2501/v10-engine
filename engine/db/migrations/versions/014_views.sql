-- 014_views: Read Model 보조 뷰 (v_ready_nodes, v_zombie_nodes, v_project_summary 등)
-- UP:
CREATE VIEW IF NOT EXISTS v_ready_nodes AS
SELECT n.id, n.project_id, n.dag_id, n.node_type, n.phase,
       n.name, n.priority, n.assigned_model, n.estimated_tokens, n.created_at
FROM nodes n
WHERE n.state = 'READY'
ORDER BY n.priority ASC, n.created_at ASC;

CREATE VIEW IF NOT EXISTS v_zombie_nodes AS
SELECT n.id, n.project_id, n.name, n.last_heartbeat,
  ROUND((julianday('now') - julianday(n.last_heartbeat)) * 24 * 60) AS minutes_since_heartbeat
FROM nodes n
WHERE n.state = 'IN_PROGRESS'
  AND (n.last_heartbeat IS NULL
       OR julianday('now') - julianday(n.last_heartbeat) > 30.0 / 1440.0);

CREATE VIEW IF NOT EXISTS v_project_summary AS
SELECT
  p.id, p.name, p.status, p.phase, p.priority,
  p.engagement_id, p.component_type, p.intake_submission_id,
  COUNT(n.id) AS total_nodes,
  SUM(CASE WHEN n.state = 'COMPLETED'   THEN 1 ELSE 0 END) AS completed,
  SUM(CASE WHEN n.state = 'IN_PROGRESS' THEN 1 ELSE 0 END) AS in_progress,
  SUM(CASE WHEN n.state = 'INVALID'     THEN 1 ELSE 0 END) AS invalid,
  SUM(CASE WHEN n.state = 'NEEDS_HUMAN' THEN 1 ELSE 0 END) AS needs_human,
  SUM(CASE WHEN n.state = 'SUSPENDED'   THEN 1 ELSE 0 END) AS suspended,
  SUM(CASE WHEN n.state = 'BLOCKED'     THEN 1 ELSE 0 END) AS blocked,
  ROUND(100.0 * SUM(CASE WHEN n.state = 'COMPLETED' THEN 1 ELSE 0 END) / NULLIF(COUNT(n.id), 0), 1) AS completion_pct,
  COALESCE(ct.total_cost_usd, 0) AS total_cost_usd,
  COALESCE(bl.cost_limit_usd, 0) AS budget_limit_usd
FROM projects p
LEFT JOIN dags d ON d.project_id = p.id
LEFT JOIN nodes n ON n.dag_id = d.id
LEFT JOIN (SELECT project_id, SUM(total_cost_usd) AS total_cost_usd FROM cost_tracking GROUP BY project_id) ct ON ct.project_id = p.id
LEFT JOIN budget_limits bl ON bl.project_id = p.id
GROUP BY p.id;

CREATE VIEW IF NOT EXISTS v_today_costs AS
SELECT p.name AS project_name, ct.model, ct.total_tokens, ct.total_cost_usd, ct.run_count
FROM cost_tracking ct
JOIN projects p ON p.id = ct.project_id
WHERE ct.date = date('now')
ORDER BY ct.total_cost_usd DESC;

CREATE VIEW IF NOT EXISTS v_pending_escalations AS
SELECT e.id, e.project_id, p.name AS project_name,
  e.severity, e.escalation_type, e.title, e.status, e.assigned_to,
  ROUND((julianday('now') - julianday(e.created_at)) * 24, 1) AS hours_open
FROM escalations e
JOIN projects p ON p.id = e.project_id
WHERE e.status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS')
ORDER BY CASE e.severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LOW' THEN 4 END, e.created_at ASC;

CREATE VIEW IF NOT EXISTS v_artifact_badges AS
SELECT a.id AS artifact_id, a.node_id, a.project_id,
  a.artifact_type, a.file_type, av_latest.storage_path,
  json_group_array(json_object(
    'phase', s.phase, 'verdict', s.verdict, 'stamped_at', s.stamped_at,
    'approved_by', s.approved_by, 'verification_stage', s.verification_stage,
    'verification_passed', s.verification_passed
  )) FILTER (WHERE s.id IS NOT NULL) AS qa_badges,
  COUNT(s.id) AS badge_count
FROM artifacts a
LEFT JOIN artifact_qa_stamps s ON a.id = s.artifact_id
LEFT JOIN artifact_versions av_latest ON av_latest.artifact_id = a.id AND av_latest.version_num = a.current_version
GROUP BY a.id;

CREATE VIEW IF NOT EXISTS v_intake_pending AS
SELECT s.id, s.form_version, s.status, s.retry_count, s.created_at,
  ROUND((julianday('now') - julianday(s.created_at)) * 24 * 60, 1) AS minutes_waiting,
  s.engagement_id, s.project_id
FROM intake_submissions s
WHERE s.status IN ('RECEIVED','VALID','FAILED') AND s.engagement_id IS NULL AND s.retry_count < 3
ORDER BY s.created_at ASC;

CREATE VIEW IF NOT EXISTS v_engagement_summary AS
SELECT
  e.id AS engagement_id, e.name AS engagement_name, e.client_name,
  e.status AS engagement_status, e.priority, e.deadline, e.component_count,
  COUNT(p.id) AS project_count,
  SUM(CASE WHEN p.status = 'COMPLETED' THEN 1 ELSE 0 END) AS projects_completed,
  SUM(CASE WHEN p.status = 'ACTIVE'    THEN 1 ELSE 0 END) AS projects_active,
  SUM(CASE WHEN p.status = 'PAUSED'    THEN 1 ELSE 0 END) AS projects_paused,
  COUNT(n.id) AS total_nodes,
  SUM(CASE WHEN n.state = 'COMPLETED' THEN 1 ELSE 0 END) AS nodes_completed,
  ROUND(100.0 * SUM(CASE WHEN n.state = 'COMPLETED' THEN 1 ELSE 0 END) / NULLIF(COUNT(n.id), 0), 1) AS overall_completion_pct,
  COALESCE(SUM(ct.total_cost_usd), 0) AS total_cost_usd,
  e.created_at, e.updated_at
FROM engagements e
LEFT JOIN projects p ON p.engagement_id = e.id
LEFT JOIN dags d ON d.project_id = p.id
LEFT JOIN nodes n ON n.dag_id = d.id
LEFT JOIN cost_tracking ct ON ct.project_id = p.id
GROUP BY e.id;
-- DOWN:
DROP VIEW IF EXISTS v_engagement_summary;
DROP VIEW IF EXISTS v_intake_pending;
DROP VIEW IF EXISTS v_artifact_badges;
DROP VIEW IF EXISTS v_pending_escalations;
DROP VIEW IF EXISTS v_today_costs;
DROP VIEW IF EXISTS v_project_summary;
DROP VIEW IF EXISTS v_zombie_nodes;
DROP VIEW IF EXISTS v_ready_nodes;
