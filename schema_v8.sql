-- ============================================================
-- AI SI 매뉴팩처링 플랫폼 — DB 스키마 v8 (최종 확정본)
-- 변경 이력:
--   v1 → v2 (003_intake_bridge): intake_submissions 추가, phase CHECK 확장
--   v2 → v3 (004_engagement_layer): engagements, engagement_dags, engagement_edges,
--            revision_requests, agent_processes, project_env_vars
--   v3 → v4 (005_v4_additions): project_env_vars GLOBAL scope, artifact_qa_stamps
--   v4 → v5 (007_v5_improvements): 10-state nodes, provider_credentials, OAuth
--   v5 → v8 (008~013):
--     008: node_budget_overrides — TOKEN_BUDGET 노드별 오버라이드 (전역 변이 버그 수정)
--     009: nodes.invalidation_pending/source/queued_at — Cascade 2-Phase 프로토콜
--     010: artifact_qa_stamps 검증 컬럼 — CodeVerificationSandbox 3단계 기록
--     011: provider_circuit_breakers — DB 기반 CB 상태 (멀티 프로세스 대응)
--     012: notification_subscriptions — Outbox 재사용 알림 구독 관리
--     013: agent_token_usage + v_phase_token_summary — Phase별 토큰 예산 추적
--
-- 설계 원칙:
--   1. Event Logging: event_store(불변) + projection tables(Read) 완전 분리
--   2. 낙관적 잠금: 모든 집합체 테이블에 version 컬럼
--   3. Outbox 패턴: DB 변경 + 이벤트 발행 원자성 보장
--   4. 집합체 스냅샷: 장기 실행 시 이벤트 재생 성능 보장
--   5. SQLite 우선: DatabaseAdapter 추상화로 PostgreSQL 전환 가능
--      (전환 도구: pgloader — 방언 자동 번역 없음, 각 Adapter가 직접 구현)
--   6. UUID TEXT: 향후 분산 환경 대응
--   7. ISO 8601 TEXT: 타임스탬프 통일
--   8. Engagement Layer: engagements(계약) > projects(서브프로젝트) 2계층
--   9. SQLite 동시성: WAL 모드 + busy_timeout=300ms (db_retry보다 짧게)
--      멀티 프로세스 환경 = PostgreSQL 필수. SQLite는 workers=1 전용.
--  10. Circuit Breaker 원자성: SELECT FOR UPDATE 불가 → BEGIN IMMEDIATE 트랜잭션
-- ============================================================

PRAGMA journal_mode = WAL;          -- 동시 읽기 성능
PRAGMA foreign_keys = ON;           -- 참조 무결성 강제
PRAGMA synchronous = NORMAL;        -- WAL 모드에서 안전 + 성능
PRAGMA cache_size = -64000;         -- 64MB 캐시
PRAGMA temp_store = MEMORY;         -- 임시 테이블 메모리 사용
PRAGMA busy_timeout = 300;          -- v8: 300ms (db_retry 간격보다 짧게)


-- ============================================================
-- 0. 스키마 버전 관리
-- ============================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,   -- '001_initial', '002_rate_limit' 등
  description TEXT NOT NULL,
  checksum    TEXT NOT NULL,       -- SHA-256(마이그레이션 파일 내용)
  applied_at  TEXT NOT NULL        -- ISO 8601
);

INSERT INTO schema_migrations VALUES (
  '001_initial',
  'AI SI 플랫폼 초기 스키마',
  'placeholder_checksum',
  datetime('now')
);


-- ============================================================
-- 1. 사용자 (RBAC 기반)
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
  id                    TEXT PRIMARY KEY,     -- UUID v4
  email                 TEXT NOT NULL UNIQUE,
  password_hash         TEXT NOT NULL,        -- bcrypt(cost=12), 절대 평문 저장 금지
  name                  TEXT NOT NULL,
  role                  TEXT NOT NULL DEFAULT 'DESIGNER'
    CHECK(role IN ('ADMIN', 'SENIOR_DESIGNER', 'DESIGNER')),
    -- CLIENT는 시스템 직접 접근 없음 → 역할 3개로 단순화
  is_active             INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  failed_login_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until          TEXT,                 -- ISO 8601, NULL = 잠금 없음
  last_login_at         TEXT,
  version               INTEGER NOT NULL DEFAULT 0,  -- 낙관적 잠금
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);


-- ============================================================
-- 2. 헌법 버전 (claude_init.md 버전 관리)
-- ============================================================

CREATE TABLE IF NOT EXISTS constitution_versions (
  id           TEXT PRIMARY KEY,
  version      TEXT NOT NULL UNIQUE,    -- 'v1.0' | 'v1.1' | 'v2.0'
  file_path    TEXT NOT NULL,           -- 'prompts/v1/claude_init.md'
  rules_hash   TEXT NOT NULL,           -- SHA-256(파일 내용), 변조 감지
  changelog    TEXT,
  is_active    INTEGER NOT NULL DEFAULT 0,  -- BOOLEAN: 신규 프로젝트 적용 버전
  version_num  INTEGER NOT NULL DEFAULT 0,  -- 낙관적 잠금
  created_at   TEXT NOT NULL,
  activated_at TEXT
);

INSERT INTO constitution_versions VALUES (
  'cv_v1_0',
  'v1.0',
  'prompts/v1/claude_init.md',
  'to_be_computed',
  '최초 9대 규칙 정의',
  1,
  0,
  datetime('now'),
  datetime('now')
);


-- ============================================================
-- 3. DAG 템플릿
-- ============================================================

CREATE TABLE IF NOT EXISTS templates (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  description   TEXT,
  project_type  TEXT NOT NULL,
    -- 'web_app' | 'mobile_app' | 'api_service' | 'custom'
  dag_structure TEXT NOT NULL,      -- JSON: {nodes: [...], edges: [...]}
  is_published  INTEGER NOT NULL DEFAULT 0,   -- BOOLEAN
  is_deprecated INTEGER NOT NULL DEFAULT 0,   -- BOOLEAN
  version       INTEGER NOT NULL DEFAULT 0,   -- 낙관적 잠금
  created_by    TEXT NOT NULL REFERENCES users(id),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);


-- ============================================================
-- 4. 인테이크 제출 (v2 신규 — IntakeProcessor 입력 원천)
-- ============================================================

CREATE TABLE IF NOT EXISTS intake_submissions (
  id                TEXT PRIMARY KEY,        -- UUID v4
  form_version      TEXT NOT NULL DEFAULT 'v1',
  raw_json          TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'RECEIVED'
    CHECK(status IN ('RECEIVED','VALIDATING','VALID','INVALID',
                     'CONVERTING','CONVERTED','FAILED')),
  engagement_id     TEXT,
  project_id        TEXT,
  validation_errors TEXT NOT NULL DEFAULT '[]',
  conversion_log    TEXT NOT NULL DEFAULT '[]',
  retry_count       INTEGER NOT NULL DEFAULT 0,
  version           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intake_submissions_status
  ON intake_submissions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_submissions_engagement
  ON intake_submissions(engagement_id)
  WHERE engagement_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_intake_submissions_project
  ON intake_submissions(project_id)
  WHERE project_id IS NOT NULL;


-- ============================================================
-- 5. 인게이지먼트 (v3 신규 — 클라이언트 계약 단위)
-- ============================================================

CREATE TABLE IF NOT EXISTS engagements (
  id                      TEXT PRIMARY KEY,
  name                    TEXT NOT NULL,
  client_name             TEXT NOT NULL,
  intake_submission_id    TEXT REFERENCES intake_submissions(id),
  status                  TEXT NOT NULL DEFAULT 'INTAKE'
    CHECK(status IN (
      'INTAKE', 'ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED', 'FORCE_CLOSED'
    )),
  global_context          TEXT NOT NULL DEFAULT '{}',
  constitution_version_id TEXT REFERENCES constitution_versions(id),
  deadline                TEXT,
  priority                INTEGER NOT NULL DEFAULT 3,
  component_count         INTEGER NOT NULL DEFAULT 0,
  version                 INTEGER NOT NULL DEFAULT 0,
  created_by              TEXT NOT NULL REFERENCES users(id),
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engagements_status   ON engagements(status);
CREATE INDEX IF NOT EXISTS idx_engagements_priority ON engagements(priority, status);
CREATE INDEX IF NOT EXISTS idx_engagements_intake   ON engagements(intake_submission_id)
  WHERE intake_submission_id IS NOT NULL;


-- ============================================================
-- 6. 프로젝트 (Read Model)
-- ============================================================

CREATE TABLE IF NOT EXISTS projects (
  id                      TEXT PRIMARY KEY,
  name                    TEXT NOT NULL,
  client_name             TEXT NOT NULL,
  project_type            TEXT NOT NULL,
    -- 'web_app' | 'mobile_app' | 'api_service' | 'custom'
  status                  TEXT NOT NULL DEFAULT 'INTAKE',
  global_context          TEXT NOT NULL DEFAULT '{}',
  constitution_version_id TEXT REFERENCES constitution_versions(id),
  template_id             TEXT REFERENCES templates(id),
  intake_submission_id    TEXT REFERENCES intake_submissions(id),
  engagement_id           TEXT REFERENCES engagements(id),
  component_type          TEXT,
    -- 'WEB' | 'MOBILE' | 'API' | 'ADMIN' | 'INFRA' | 'MASTER' | NULL
  deadline                TEXT,
  priority                INTEGER NOT NULL DEFAULT 3,
  phase                   TEXT NOT NULL DEFAULT 'API_SERVER'
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN',
                    'DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  version                 INTEGER NOT NULL DEFAULT 0,
  created_by              TEXT NOT NULL REFERENCES users(id),
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_status      ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_priority    ON projects(priority, status);
CREATE INDEX IF NOT EXISTS idx_projects_phase       ON projects(phase, status);
CREATE INDEX IF NOT EXISTS idx_projects_intake      ON projects(intake_submission_id)
  WHERE intake_submission_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_projects_engagement  ON projects(engagement_id)
  WHERE engagement_id IS NOT NULL;


-- ============================================================
-- 7. 프로젝트 멤버 (RBAC)
-- ============================================================

CREATE TABLE IF NOT EXISTS project_members (
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id     TEXT NOT NULL REFERENCES users(id),
  role        TEXT NOT NULL DEFAULT 'MEMBER',
    -- 'OWNER' | 'SENIOR' | 'MEMBER' | 'CLIENT'
  joined_at   TEXT NOT NULL,
  PRIMARY KEY (project_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);


-- ============================================================
-- 8. 인게이지먼트 멤버 (v3 신규)
-- ============================================================

CREATE TABLE IF NOT EXISTS engagement_members (
  engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
  user_id       TEXT NOT NULL REFERENCES users(id),
  role          TEXT NOT NULL DEFAULT 'MEMBER',
  joined_at     TEXT NOT NULL,
  PRIMARY KEY (engagement_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_engagement_members_user ON engagement_members(user_id);


-- ============================================================
-- 9. 요구사항
-- ============================================================

CREATE TABLE IF NOT EXISTS requirements (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL UNIQUE REFERENCES projects(id),
  current_version INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'DRAFT',
  has_conflict    INTEGER NOT NULL DEFAULT 0,
  approved_by     TEXT REFERENCES users(id),
  approved_at     TEXT,
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_versions (
  id              TEXT PRIMARY KEY,
  requirement_id  TEXT NOT NULL REFERENCES requirements(id),
  version_num     INTEGER NOT NULL,
  content         TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  change_summary  TEXT,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE(requirement_id, version_num)
);

CREATE INDEX IF NOT EXISTS idx_req_versions_req ON requirement_versions(requirement_id, version_num DESC);


-- ============================================================
-- 10. DAG (Read Model)
-- ============================================================

CREATE TABLE IF NOT EXISTS dags (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL UNIQUE REFERENCES projects(id),
  template_id      TEXT REFERENCES templates(id),
  status           TEXT NOT NULL DEFAULT 'INITIALIZING',
    -- INITIALIZING | VALID | EXECUTING | PAUSED | COMPLETED | INVALID | CORRUPTED
  total_nodes      INTEGER NOT NULL DEFAULT 0,
  completed_nodes  INTEGER NOT NULL DEFAULT 0,
  current_phase    TEXT NOT NULL DEFAULT 'API_SERVER'
    CHECK(current_phase IN ('API_SERVER','PLANNING','DESIGN',
                            'DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  topo_order       TEXT,
  version          INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dags_status ON dags(status);


-- ============================================================
-- 11. 인게이지먼트 DAG (v3 신규)
-- ============================================================

CREATE TABLE IF NOT EXISTS engagement_dags (
  id             TEXT PRIMARY KEY,
  engagement_id  TEXT NOT NULL UNIQUE REFERENCES engagements(id),
  status         TEXT NOT NULL DEFAULT 'INITIALIZING'
    CHECK(status IN (
      'INITIALIZING', 'VALID', 'EXECUTING', 'PAUSED',
      'COMPLETED', 'INVALID', 'CORRUPTED'
    )),
  topo_order     TEXT,
  version        INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engagement_dags_status ON engagement_dags(status);


-- ============================================================
-- 12. 인게이지먼트 엣지 (v3 신규 — 크로스 프로젝트 Gate 의존성)
-- ============================================================

CREATE TABLE IF NOT EXISTS engagement_edges (
  id                  TEXT PRIMARY KEY,
  engagement_dag_id   TEXT NOT NULL REFERENCES engagement_dags(id),
  from_project_id     TEXT NOT NULL REFERENCES projects(id),
  from_phase          TEXT NOT NULL
    CHECK(from_phase IN ('API_SERVER','PLANNING','DESIGN',
                         'DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  to_project_id       TEXT NOT NULL REFERENCES projects(id),
  to_phase            TEXT NOT NULL
    CHECK(to_phase IN ('API_SERVER','PLANNING','DESIGN',
                       'DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  gate_trigger_type   TEXT NOT NULL DEFAULT 'MANUAL_DESIGNER'
    CHECK(gate_trigger_type IN ('MANUAL_DESIGNER', 'AUTO', 'CLIENT_APPROVAL')),
  is_active           INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL,
  UNIQUE(from_project_id, from_phase, to_project_id, to_phase),
  CHECK(from_project_id != to_project_id)
);

CREATE INDEX IF NOT EXISTS idx_engagement_edges_dag  ON engagement_edges(engagement_dag_id);
CREATE INDEX IF NOT EXISTS idx_engagement_edges_from ON engagement_edges(from_project_id, from_phase)
  WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_engagement_edges_to   ON engagement_edges(to_project_id, to_phase)
  WHERE is_active = 1;


-- ============================================================
-- 13. 노드 (Read Model — 핵심 집합체)
--     v8 추가: invalidation_pending, invalidation_source_id, invalidation_queued_at
--              (Cascade Invalidation 2-Phase 프로토콜 — migration 009)
-- ============================================================

CREATE TABLE IF NOT EXISTS nodes (
  id                      TEXT PRIMARY KEY,
  dag_id                  TEXT NOT NULL REFERENCES dags(id),
  project_id              TEXT NOT NULL REFERENCES projects(id),
  node_type               TEXT NOT NULL CHECK(node_type IN ('TASK', 'QA', 'GATE')),
  phase                   TEXT NOT NULL
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN',
                    'DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),

  name                    TEXT NOT NULL,
  description             TEXT,

  -- 상태 머신 (10-state)
  state                   TEXT NOT NULL DEFAULT 'NOT_STARTED'
    CHECK(state IN (
      'NOT_STARTED','READY','IN_PROGRESS','COMPLETED',
      'BLOCKED','INVALID','AWAITING_APPROVAL','NEEDS_HUMAN',
      'SUSPENDED','FAILED'
    )),

  -- QA 페어링 (Rule 9)
  qa_pair_node_id         TEXT REFERENCES nodes(id),
  task_pair_node_id       TEXT REFERENCES nodes(id),

  -- Gate 설정
  gate_auto_approve       INTEGER NOT NULL DEFAULT 0,
  gate_trigger_type       TEXT NOT NULL DEFAULT 'MANUAL_DESIGNER'
    CHECK(gate_trigger_type IN ('MANUAL_DESIGNER', 'AUTO', 'CLIENT_APPROVAL')),

  -- 에이전트 할당
  assigned_model          TEXT,
    -- 'claude-opus-4-6' | 'claude-sonnet-4-6' | 'claude-haiku-4-5-20251001'
  constitution_version_id TEXT REFERENCES constitution_versions(id),

  -- 재시도
  retry_count             INTEGER NOT NULL DEFAULT 0,
  max_retries             INTEGER NOT NULL DEFAULT 3,
  failure_reasons         TEXT DEFAULT '[]',  -- JSON: [{attempt, reason, at}]

  -- Stall Detection
  stall_count             INTEGER NOT NULL DEFAULT 0,
  task_snapshot           TEXT DEFAULT '[]',

  -- 생존 감지
  last_heartbeat          TEXT,
  suspension_reason       TEXT,
    -- 'BUDGET_EXCEEDED' | 'CIRCUIT_BREAKER' | 'SHUTDOWN_DRAIN' | NULL

  -- v8 신규: Cascade Invalidation 2-Phase (migration 009)
  invalidation_pending    INTEGER NOT NULL DEFAULT 0,
    -- 1 = Phase 1 완료 (invalidation_source_id 설정됨), Phase 2 대기 중
  invalidation_source_id  TEXT,
    -- 이 노드를 무효화시킨 upstream 노드 ID
  invalidation_queued_at  TEXT,
    -- Phase 1 완료 시각 (Phase 2 타임아웃 감지용)

  -- 비용 추적
  estimated_tokens        INTEGER,
  actual_tokens           INTEGER DEFAULT 0,

  -- 산출물 추적
  content_hash            TEXT,
  sandbox_path            TEXT,

  -- 타임스탬프
  priority                INTEGER NOT NULL DEFAULT 3,
  version                 INTEGER NOT NULL DEFAULT 0,
  started_at              TEXT,
  completed_at            TEXT,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_dag             ON nodes(dag_id);
CREATE INDEX IF NOT EXISTS idx_nodes_project_state   ON nodes(project_id, state);
CREATE INDEX IF NOT EXISTS idx_nodes_state           ON nodes(state);
CREATE INDEX IF NOT EXISTS idx_nodes_type            ON nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_phase_state     ON nodes(phase, state);
CREATE INDEX IF NOT EXISTS idx_nodes_zombie_check
  ON nodes(last_heartbeat)
  WHERE state = 'IN_PROGRESS';
CREATE INDEX IF NOT EXISTS idx_nodes_invalidation_pending
  ON nodes(invalidation_pending, invalidation_queued_at)
  WHERE invalidation_pending = 1;


-- ============================================================
-- 14. 엣지 (의존성 그래프 — 프로젝트 내부)
-- ============================================================

CREATE TABLE IF NOT EXISTS edges (
  id           TEXT PRIMARY KEY,
  dag_id       TEXT NOT NULL REFERENCES dags(id),
  from_node_id TEXT NOT NULL REFERENCES nodes(id),
  to_node_id   TEXT NOT NULL REFERENCES nodes(id),
  edge_type    TEXT NOT NULL DEFAULT 'DEPENDS_ON',
    -- 'DEPENDS_ON' | 'QA_PAIR' | 'GATE_ENTRY'
  is_active    INTEGER NOT NULL DEFAULT 1,
  weight       INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  UNIQUE(from_node_id, to_node_id),
  CHECK(from_node_id != to_node_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node_id) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_node_id)   WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_edges_dag  ON edges(dag_id);


-- ============================================================
-- 15. 에이전트 실행 기록
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_runs (
  id                      TEXT PRIMARY KEY,
  node_id                 TEXT NOT NULL REFERENCES nodes(id),
  project_id              TEXT NOT NULL REFERENCES projects(id),

  agent_role              TEXT NOT NULL,
    -- 'WATCHER' | 'ANALYZER' | 'EDITOR' | 'QA' | 'RED_TEAM'
  model_used              TEXT,
  constitution_version_id TEXT REFERENCES constitution_versions(id),

  prompt_tokens           INTEGER DEFAULT 0,
  completion_tokens       INTEGER DEFAULT 0,
  total_tokens            INTEGER DEFAULT 0,
  cost_usd                REAL DEFAULT 0.0,

  status                  TEXT NOT NULL DEFAULT 'QUEUED',
    -- QUEUED | RUNNING | SUCCESS | FAILED | TIMEOUT | CANCELLED | RATE_LIMITED
  attempt_number          INTEGER NOT NULL DEFAULT 1,
  retry_of_run_id         TEXT REFERENCES agent_runs(id),
  failure_reason          TEXT,
  enhanced_prompt_used    INTEGER NOT NULL DEFAULT 0,

  sandbox_path            TEXT,
  output_path             TEXT,

  version                 INTEGER NOT NULL DEFAULT 0,
  started_at              TEXT,
  ended_at                TEXT,
  created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_node    ON agent_runs(node_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status  ON agent_runs(status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);


-- ============================================================
-- 16. 에이전트 프로세스 (v3 신규 — PID 추적 + 생존 감지)
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_processes (
  id              TEXT PRIMARY KEY,
  agent_run_id    TEXT NOT NULL REFERENCES agent_runs(id),
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  project_id      TEXT NOT NULL REFERENCES projects(id),
  pid             INTEGER,
  status          TEXT NOT NULL DEFAULT 'STARTING'
    CHECK(status IN ('STARTING', 'ALIVE', 'RETRYING', 'EXHAUSTED', 'TERMINATED')),
  last_heartbeat  TEXT,
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_processes_run     ON agent_processes(agent_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_processes_node    ON agent_processes(node_id);
CREATE INDEX IF NOT EXISTS idx_agent_processes_status  ON agent_processes(status);
CREATE INDEX IF NOT EXISTS idx_agent_processes_alive
  ON agent_processes(last_heartbeat)
  WHERE status IN ('ALIVE', 'RETRYING');


-- ============================================================
-- 17. 산출물 (Artifact)
-- ============================================================

CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT PRIMARY KEY,
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  project_id      TEXT NOT NULL REFERENCES projects(id),
  artifact_type   TEXT NOT NULL,
    -- 'PRD' | 'DESIGN_SPEC' | 'CODE' | 'TEST' | 'API_SPEC' | 'REPORT'
  file_type       TEXT NOT NULL,
    -- 'md' | 'json' | 'py' | 'ts' | 'png' | 'pdf' | 'txt'
  current_version INTEGER NOT NULL DEFAULT 0,
  is_invalidated  INTEGER NOT NULL DEFAULT 0,
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_versions (
  id              TEXT PRIMARY KEY,
  artifact_id     TEXT NOT NULL REFERENCES artifacts(id),
  version_num     INTEGER NOT NULL,
  storage_path    TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  size_bytes      INTEGER DEFAULT 0,
  is_qa_approved  INTEGER NOT NULL DEFAULT 0,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE(artifact_id, version_num)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_node        ON artifacts(node_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_project     ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_art ON artifact_versions(artifact_id, version_num DESC);


-- ============================================================
-- 18. 변경분 (Delta — ContextAssembler Upstream Delta Layer)
-- ============================================================

CREATE TABLE IF NOT EXISTS deltas (
  id                TEXT PRIMARY KEY,
  artifact_id       TEXT NOT NULL REFERENCES artifacts(id),
  from_version_num  INTEGER NOT NULL,
  to_version_num    INTEGER NOT NULL,
  diff_strategy     TEXT NOT NULL,
    -- 'difflib' | 'ast' | 'json_patch' | 'hash_only'
  delta_content     TEXT,
  delta_size_bytes  INTEGER DEFAULT 0,
  is_empty          INTEGER NOT NULL DEFAULT 0,
  impacted_node_ids TEXT DEFAULT '[]',
  created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deltas_artifact ON deltas(artifact_id, to_version_num DESC);


-- ============================================================
-- 19. 수정 요청 (v3 신규)
-- ============================================================

CREATE TABLE IF NOT EXISTS revision_requests (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id),
  node_id      TEXT NOT NULL REFERENCES nodes(id),
  stage        TEXT NOT NULL,
    -- 'QA_CODE' | 'QA_DESIGN' | 'CLIENT_REVIEW' | 'MANUAL'
  message      TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'OPEN'
    CHECK(status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'DISMISSED', 'DEFERRED')),
  revision_num INTEGER NOT NULL DEFAULT 1,
  created_by   TEXT NOT NULL REFERENCES users(id),
  resolved_at  TEXT,
  dismiss_reason TEXT,
  version      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revision_requests_node    ON revision_requests(node_id, status);
CREATE INDEX IF NOT EXISTS idx_revision_requests_project ON revision_requests(project_id, status);
CREATE INDEX IF NOT EXISTS idx_revision_requests_open    ON revision_requests(status)
  WHERE status IN ('OPEN', 'IN_PROGRESS');


-- ============================================================
-- 20. 환경변수 암호화 저장 (v3 신규, AES-256-GCM)
-- ============================================================

CREATE TABLE IF NOT EXISTS project_env_vars (
  id              TEXT PRIMARY KEY,
  scope           TEXT NOT NULL
    CHECK(scope IN ('GLOBAL', 'ENGAGEMENT', 'PROJECT')),
  scope_id        TEXT NOT NULL,
    -- GLOBAL → 'GLOBAL', ENGAGEMENT → engagements.id, PROJECT → projects.id
  key             TEXT NOT NULL,
  value_encrypted TEXT NOT NULL,    -- AES-256-GCM 암호화
  is_secret       INTEGER NOT NULL DEFAULT 1 CHECK(is_secret IN (0, 1)),
  is_test_resource INTEGER NOT NULL DEFAULT 0 CHECK(is_test_resource IN (0, 1)),
  description     TEXT,
  created_by      TEXT NOT NULL REFERENCES users(id),
  created_at      TEXT NOT NULL,
  UNIQUE(scope, scope_id, key)
);

CREATE INDEX IF NOT EXISTS idx_env_vars_scope ON project_env_vars(scope, scope_id);


-- ============================================================
-- 21. 비용 추적 (Read Model)
-- ============================================================

CREATE TABLE IF NOT EXISTS cost_tracking (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(id),
  date           TEXT NOT NULL,
  model          TEXT NOT NULL DEFAULT 'all',
  total_tokens   INTEGER NOT NULL DEFAULT 0,
  total_cost_usd REAL NOT NULL DEFAULT 0.0,
  run_count      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, date, model)
);

CREATE TABLE IF NOT EXISTS budget_limits (
  id                     TEXT PRIMARY KEY,
  project_id             TEXT NOT NULL UNIQUE REFERENCES projects(id),
  token_limit            INTEGER,
  cost_limit_usd         REAL,
  warn_threshold_pct     INTEGER NOT NULL DEFAULT 70,
  critical_threshold_pct INTEGER NOT NULL DEFAULT 90,
  version                INTEGER NOT NULL DEFAULT 0,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cost_tracking_project_date ON cost_tracking(project_id, date DESC);


-- ============================================================
-- 22. Rate Limit 동적 조절 상태 (싱글턴)
-- ============================================================

CREATE TABLE IF NOT EXISTS rate_limit_state (
  id                    TEXT PRIMARY KEY DEFAULT 'singleton',
  max_concurrent_agents INTEGER NOT NULL DEFAULT 5,
  current_concurrent    INTEGER NOT NULL DEFAULT 0,
  consecutive_successes INTEGER NOT NULL DEFAULT 0,
  last_rate_limit_at    TEXT,
  total_rate_limit_hits INTEGER NOT NULL DEFAULT 0,
  updated_at            TEXT NOT NULL,
  CHECK (id = 'singleton')
);

INSERT OR IGNORE INTO rate_limit_state VALUES (
  'singleton', 5, 0, 0, NULL, 0, datetime('now')
);


-- ============================================================
-- 23. 크로스 프로젝트 패턴 (학습 데이터)
-- ============================================================

CREATE TABLE IF NOT EXISTS cross_project_patterns (
  id               TEXT PRIMARY KEY,
  pattern_type     TEXT NOT NULL,
    -- 'FAILURE_PATTERN' | 'SUCCESS_PATTERN' | 'RISK_PATTERN'
  project_type     TEXT,
  phase            TEXT,
  description      TEXT NOT NULL,
  suggested_action TEXT,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  success_rate     REAL,
  is_validated     INTEGER NOT NULL DEFAULT 0,
  is_deprecated    INTEGER NOT NULL DEFAULT 0,
  version          INTEGER NOT NULL DEFAULT 0,
  last_seen_at     TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patterns_type    ON cross_project_patterns(pattern_type, is_deprecated);
CREATE INDEX IF NOT EXISTS idx_patterns_project ON cross_project_patterns(project_type, phase);


-- ============================================================
-- 24. 에스컬레이션
-- ============================================================

CREATE TABLE IF NOT EXISTS escalations (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES projects(id),
  node_id          TEXT REFERENCES nodes(id),
  escalation_type  TEXT NOT NULL,
    -- 'MAX_RETRIES' | 'BUDGET' | 'GATE_TIMEOUT' | 'CONSTITUTION' | 'SYSTEM' | 'MISSING_RESOURCE'
  severity         TEXT NOT NULL DEFAULT 'MEDIUM',
    -- 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
  title            TEXT NOT NULL,
  description      TEXT,
  status           TEXT NOT NULL DEFAULT 'OPEN',
    -- OPEN | ACKNOWLEDGED | IN_PROGRESS | RESOLVED | CLOSED | AUTO_RESOLVED
  assigned_to      TEXT REFERENCES users(id),
  resolution       TEXT,
  timeout_at       TEXT,
  version          INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  resolved_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_escalations_project  ON escalations(project_id, status);
CREATE INDEX IF NOT EXISTS idx_escalations_open     ON escalations(status, severity)
  WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS');
CREATE INDEX IF NOT EXISTS idx_escalations_assignee ON escalations(assigned_to, status);


-- ============================================================
-- 25. EVENT STORE ← 단일 진실 원천 (WRITE SIDE, 불변)
--     절대 UPDATE / DELETE 금지
-- ============================================================

CREATE TABLE IF NOT EXISTS event_store (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       TEXT NOT NULL UNIQUE,
  aggregate_type TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  payload        TEXT NOT NULL,    -- JSON
  checksum       TEXT NOT NULL,    -- SHA-256(event_id||event_type||payload)
  project_id     TEXT,
  engagement_id  TEXT,
  caused_by      TEXT,
  correlation_id TEXT,
  actor          TEXT NOT NULL DEFAULT 'SYSTEM',
  recorded_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_aggregate   ON event_store(aggregate_type, aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_project     ON event_store(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_engagement  ON event_store(engagement_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_type        ON event_store(event_type);
CREATE INDEX IF NOT EXISTS idx_event_recorded    ON event_store(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_correlation ON event_store(correlation_id);
CREATE INDEX IF NOT EXISTS idx_event_caused_by   ON event_store(caused_by);

CREATE TRIGGER IF NOT EXISTS prevent_event_store_update
  BEFORE UPDATE ON event_store
BEGIN
  SELECT RAISE(ABORT, 'event_store is immutable. UPDATE is forbidden.');
END;

CREATE TRIGGER IF NOT EXISTS prevent_event_store_delete
  BEFORE DELETE ON event_store
BEGIN
  SELECT RAISE(ABORT, 'event_store is immutable. DELETE is forbidden.');
END;


-- ============================================================
-- 26. OUTBOX ← 이벤트 발행 원자성 보장
--     v8: destination='NOTIFICATION' → notification_subscriptions 테이블 참조
--         NotificationWorker가 지수 백오프(30/60/120초)로 재시도
-- ============================================================

CREATE TABLE IF NOT EXISTS outbox (
  id            TEXT PRIMARY KEY,
  event_id      TEXT NOT NULL REFERENCES event_store(event_id),
  event_type    TEXT NOT NULL,
  payload       TEXT NOT NULL,
  destination   TEXT NOT NULL,
    -- 'WEBSOCKET' | 'AGENT_TRIGGER' | 'NOTIFICATION' | 'DASHBOARD'
  status        TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING | PROCESSING | DELIVERED | FAILED | DEAD_LETTERED
  retry_count   INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 5,
  next_retry_at TEXT,
  error_message TEXT,
  created_at    TEXT NOT NULL,
  processed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
  ON outbox(next_retry_at, status)
  WHERE status IN ('PENDING', 'FAILED');
CREATE INDEX IF NOT EXISTS idx_outbox_event ON outbox(event_id);


-- ============================================================
-- 27. 집합체 스냅샷
-- ============================================================

CREATE TABLE IF NOT EXISTS aggregate_snapshots (
  id             TEXT PRIMARY KEY,
  aggregate_type TEXT NOT NULL,
  aggregate_id   TEXT NOT NULL,
  snapshot_seq   INTEGER NOT NULL,
  state          TEXT NOT NULL,
  state_hash     TEXT NOT NULL,
  version        INTEGER NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE(aggregate_type, aggregate_id, snapshot_seq)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_latest
  ON aggregate_snapshots(aggregate_type, aggregate_id, snapshot_seq DESC);


-- ============================================================
-- 28. 세션 (JWT 서버사이드 무효화)
-- ============================================================

CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash         TEXT NOT NULL UNIQUE,
  refresh_token_hash TEXT UNIQUE,
  expires_at         TEXT NOT NULL,
  refresh_expires_at TEXT,
  ip_address         TEXT,
  user_agent         TEXT,
  is_revoked         INTEGER NOT NULL DEFAULT 0 CHECK(is_revoked IN (0, 1)),
  created_at         TEXT NOT NULL,
  last_used_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)
  WHERE is_revoked = 0;


-- ============================================================
-- 29. 감사 로그 (불변 — UPDATE/DELETE 금지)
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_logs (
  id            TEXT PRIMARY KEY,
  user_id       TEXT REFERENCES users(id) ON DELETE SET NULL,
  user_role     TEXT,
  action        TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id   TEXT,
  old_state     TEXT,
  new_state     TEXT,
  metadata      TEXT DEFAULT '{}',
  ip_address    TEXT,
  user_agent    TEXT,
  status        TEXT NOT NULL DEFAULT 'SUCCESS'
    CHECK(status IN ('SUCCESS', 'FAILED', 'DENIED')),
  created_at    TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS prevent_audit_log_update
  BEFORE UPDATE ON audit_logs
BEGIN
  SELECT RAISE(ABORT, 'audit_logs is immutable. UPDATE is forbidden.');
END;

CREATE TRIGGER IF NOT EXISTS prevent_audit_log_delete
  BEFORE DELETE ON audit_logs
BEGIN
  SELECT RAISE(ABORT, 'audit_logs is immutable. DELETE is forbidden.');
END;

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id    ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource   ON audit_logs(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action     ON audit_logs(action, status);


-- ============================================================
-- 30. Provider Credentials (v5: api_keys → provider_credentials, OAuth 지원)
-- ============================================================

CREATE TABLE IF NOT EXISTS provider_credentials (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  provider        TEXT NOT NULL
    CHECK(provider IN ('anthropic', 'openai', 'google')),
  auth_mode       TEXT NOT NULL DEFAULT 'api_key'
    CHECK(auth_mode IN ('api_key', 'oauth')),
  key_hash        TEXT UNIQUE,
  key_encrypted   TEXT,          -- AES-256-GCM
  key_preview     TEXT,
  oauth_config_encrypted TEXT,   -- AES-256-GCM
  is_active       INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
  is_default      INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
  created_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
  last_used_at    TEXT,
  token_expires_at TEXT,
  usage_count     INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provider_creds_provider ON provider_credentials(provider, is_active);
CREATE INDEX IF NOT EXISTS idx_provider_creds_default
  ON provider_credentials(provider, is_default)
  WHERE is_active = 1 AND is_default = 1;


-- ============================================================
-- 34. QA 뱃지 스탬프 (v4 신규)
--     v8 추가: verification_stage, verification_passed, verification_output
--              (CodeVerificationSandbox 3단계 결과 기록 — migration 010)
-- ============================================================

CREATE TABLE IF NOT EXISTS artifact_qa_stamps (
  id                  TEXT PRIMARY KEY,
  artifact_id         TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  phase               TEXT NOT NULL
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN','DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  qa_node_id          TEXT NOT NULL REFERENCES nodes(id),
  verdict             TEXT NOT NULL DEFAULT 'PASS'
    CHECK(verdict IN ('PASS', 'CONDITIONAL_PASS')),
  stamped_at          TEXT NOT NULL,
  approved_by         TEXT REFERENCES users(id),

  -- v8 신규: CodeVerificationSandbox 3단계 검증 결과 (migration 010)
  verification_stage  TEXT,
    -- 'AST' | 'STATIC' | 'DOCKER' | NULL (비코드 산출물은 NULL)
  verification_passed INTEGER NOT NULL DEFAULT 0,
    -- 1 = 검증 통과, 0 = 검증 안 함 또는 실패
  verification_output TEXT,
    -- JSON: {stage, risk_score, blocked_patterns, docker_exit_code}

  UNIQUE(artifact_id, phase)
);

CREATE INDEX IF NOT EXISTS idx_qa_stamps_artifact ON artifact_qa_stamps(artifact_id);
CREATE INDEX IF NOT EXISTS idx_qa_stamps_phase    ON artifact_qa_stamps(phase);


-- ============================================================
-- v8 신규 테이블 (Migration 008~013)
-- ============================================================

-- ============================================================
-- 35. 노드 예산 오버라이드 (v8 — migration 008)
--     TOKEN_BUDGET는 MappingProxyType으로 불변 선언.
--     노드별 예산 변경은 이 테이블에 별도 기록. 전역 변이 금지.
-- ============================================================

CREATE TABLE IF NOT EXISTS node_budget_overrides (
  node_id               TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  max_input_override    INTEGER,     -- None = 글로벌 TOKEN_BUDGET 사용
  max_output_override   INTEGER,
  phase_limit_override  INTEGER,     -- 해당 Phase 누적 예산 오버라이드
  reason                TEXT,        -- 오버라이드 사유 (감사 추적용)
  created_by            TEXT NOT NULL REFERENCES users(id),
  created_at            TEXT NOT NULL
);


-- ============================================================
-- 36. Provider Circuit Breaker 상태 (v8 — migration 011)
--     DB 기반 상태 저장 → 멀티 프로세스 환경에서 공유 가능
--     원자적 카운터 증가: BEGIN IMMEDIATE (SELECT FOR UPDATE SQLite 미지원)
-- ============================================================

CREATE TABLE IF NOT EXISTS provider_circuit_breakers (
  provider_name  TEXT PRIMARY KEY,
    -- 'anthropic' | 'openai' | 'google'
  state          TEXT NOT NULL DEFAULT 'CLOSED'
    CHECK(state IN ('CLOSED', 'OPEN', 'HALF_OPEN')),
  failure_count  INTEGER NOT NULL DEFAULT 0,
  success_count  INTEGER NOT NULL DEFAULT 0,
  opened_at      TEXT,    -- OPEN 전환 시각 (HALF_OPEN 복귀 시간 계산용)
  last_attempt_at TEXT,   -- 마지막 시도 시각
  version        INTEGER NOT NULL DEFAULT 1  -- 낙관적 잠금 (업데이트 충돌 감지)
);

-- 초기 데이터 (3개 provider 사전 등록)
INSERT OR IGNORE INTO provider_circuit_breakers (provider_name) VALUES ('anthropic');
INSERT OR IGNORE INTO provider_circuit_breakers (provider_name) VALUES ('openai');
INSERT OR IGNORE INTO provider_circuit_breakers (provider_name) VALUES ('google');


-- ============================================================
-- 37. 알림 구독 (v8 — migration 012)
--     알림 발송: Outbox 테이블 재사용 (destination='NOTIFICATION')
--     NotificationWorker가 이 테이블 참조 → 지수 백오프 재시도(30/60/120초)
-- ============================================================

CREATE TABLE IF NOT EXISTS notification_subscriptions (
  id                  TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  engagement_id       TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
  event_type          TEXT NOT NULL,
    -- 예: 'GateTriggered' | 'NodeFailed' | 'BudgetWarning' | 'EscalationCreated'
  channel_type        TEXT NOT NULL
    CHECK(channel_type IN ('WEBHOOK', 'EMAIL', 'LOG_ONLY')),
  encrypted_endpoint  TEXT,  -- WEBHOOK URL (AES-256-GCM)
  encrypted_recipient TEXT,  -- EMAIL 주소 (AES-256-GCM)
  is_active           INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(engagement_id, event_type, channel_type)
);

CREATE INDEX IF NOT EXISTS idx_notification_subs_engagement
  ON notification_subscriptions(engagement_id, is_active);
CREATE INDEX IF NOT EXISTS idx_notification_subs_event_type
  ON notification_subscriptions(event_type, is_active);


-- ============================================================
-- 38. 에이전트 토큰 사용량 상세 (v8 — migration 013)
--     BudgetEnforcer L3: Phase별 누적 예산 추적
--     일별 집계는 기존 cost_tracking 유지, 이 테이블은 실시간 Phase 추적용
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_token_usage (
  id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  node_id          TEXT NOT NULL REFERENCES nodes(id),
  agent_run_id     TEXT REFERENCES agent_runs(id),
  engagement_id    TEXT NOT NULL,
  phase            TEXT NOT NULL
    CHECK(phase IN ('API_SERVER','PLANNING','DESIGN',
                    'DEVELOPMENT','INFRASTRUCTURE','DELIVERY')),
  model_name       TEXT NOT NULL,
  input_tokens     INTEGER NOT NULL,
  output_tokens    INTEGER NOT NULL,
  estimated_input  INTEGER,   -- BudgetEnforcer L1 추정값 (정확도 측정용)
  recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_token_usage_node        ON agent_token_usage(node_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_engagement  ON agent_token_usage(engagement_id, phase);
CREATE INDEX IF NOT EXISTS idx_token_usage_recorded    ON agent_token_usage(recorded_at DESC);


-- ============================================================
-- 뷰 (Read Model 보조)
-- ============================================================

-- 현재 READY 상태 노드
CREATE VIEW IF NOT EXISTS v_ready_nodes AS
SELECT
  n.id, n.project_id, n.dag_id, n.node_type, n.phase,
  n.name, n.priority, n.assigned_model, n.estimated_tokens, n.created_at
FROM nodes n
WHERE n.state = 'READY'
ORDER BY n.priority ASC, n.created_at ASC;

-- 좀비 노드 감지 (30분 이상 heartbeat 없음)
CREATE VIEW IF NOT EXISTS v_zombie_nodes AS
SELECT
  n.id, n.project_id, n.name, n.last_heartbeat,
  ROUND((julianday('now') - julianday(n.last_heartbeat)) * 24 * 60) AS minutes_since_heartbeat
FROM nodes n
WHERE n.state = 'IN_PROGRESS'
  AND (
    n.last_heartbeat IS NULL
    OR julianday('now') - julianday(n.last_heartbeat) > 30.0 / 1440.0
  );

-- 프로젝트별 현황 요약
CREATE VIEW IF NOT EXISTS v_project_summary AS
SELECT
  p.id, p.name, p.status, p.phase, p.priority,
  p.engagement_id, p.component_type, p.intake_submission_id,
  COUNT(n.id)                                                  AS total_nodes,
  SUM(CASE WHEN n.state = 'COMPLETED'   THEN 1 ELSE 0 END)    AS completed,
  SUM(CASE WHEN n.state = 'IN_PROGRESS' THEN 1 ELSE 0 END)    AS in_progress,
  SUM(CASE WHEN n.state = 'INVALID'     THEN 1 ELSE 0 END)    AS invalid,
  SUM(CASE WHEN n.state = 'NEEDS_HUMAN' THEN 1 ELSE 0 END)    AS needs_human,
  SUM(CASE WHEN n.state = 'SUSPENDED'   THEN 1 ELSE 0 END)    AS suspended,
  SUM(CASE WHEN n.state = 'BLOCKED'     THEN 1 ELSE 0 END)    AS blocked,
  ROUND(
    100.0 * SUM(CASE WHEN n.state = 'COMPLETED' THEN 1 ELSE 0 END)
    / NULLIF(COUNT(n.id), 0), 1
  )                                                            AS completion_pct,
  COALESCE(ct.total_cost_usd, 0)                              AS total_cost_usd,
  COALESCE(bl.cost_limit_usd, 0)                              AS budget_limit_usd
FROM projects p
LEFT JOIN dags d ON d.project_id = p.id
LEFT JOIN nodes n ON n.dag_id = d.id
LEFT JOIN (
  SELECT project_id, SUM(total_cost_usd) AS total_cost_usd
  FROM cost_tracking
  GROUP BY project_id
) ct ON ct.project_id = p.id
LEFT JOIN budget_limits bl ON bl.project_id = p.id
GROUP BY p.id;

-- 오늘의 비용 현황
CREATE VIEW IF NOT EXISTS v_today_costs AS
SELECT
  p.name AS project_name, ct.model, ct.total_tokens,
  ct.total_cost_usd, ct.run_count
FROM cost_tracking ct
JOIN projects p ON p.id = ct.project_id
WHERE ct.date = date('now')
ORDER BY ct.total_cost_usd DESC;

-- 에스컬레이션 대기 현황
CREATE VIEW IF NOT EXISTS v_pending_escalations AS
SELECT
  e.id, e.project_id, p.name AS project_name,
  e.severity, e.escalation_type, e.title, e.status, e.assigned_to,
  ROUND((julianday('now') - julianday(e.created_at)) * 24, 1) AS hours_open
FROM escalations e
JOIN projects p ON p.id = e.project_id
WHERE e.status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS')
ORDER BY
  CASE e.severity
    WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
    WHEN 'MEDIUM'   THEN 3 WHEN 'LOW'  THEN 4
  END,
  e.created_at ASC;

-- 산출물 QA 뱃지 현황 (대시보드 카드용)
CREATE VIEW IF NOT EXISTS v_artifact_badges AS
SELECT
  a.id AS artifact_id, a.node_id, a.project_id,
  a.artifact_type, a.file_type, av_latest.storage_path,
  json_group_array(
    json_object(
      'phase',       s.phase,
      'verdict',     s.verdict,
      'stamped_at',  s.stamped_at,
      'approved_by', s.approved_by,
      'verification_stage',  s.verification_stage,
      'verification_passed', s.verification_passed
    )
  ) FILTER (WHERE s.id IS NOT NULL) AS qa_badges,
  COUNT(s.id) AS badge_count
FROM artifacts a
LEFT JOIN artifact_qa_stamps s ON a.id = s.artifact_id
LEFT JOIN artifact_versions av_latest
  ON av_latest.artifact_id = a.id
  AND av_latest.version_num = a.current_version
GROUP BY a.id;

-- 인테이크 처리 대기 현황
CREATE VIEW IF NOT EXISTS v_intake_pending AS
SELECT
  s.id, s.form_version, s.status, s.retry_count, s.created_at,
  ROUND((julianday('now') - julianday(s.created_at)) * 24 * 60, 1) AS minutes_waiting,
  s.engagement_id, s.project_id
FROM intake_submissions s
WHERE s.status IN ('RECEIVED', 'VALID', 'FAILED')
  AND s.engagement_id IS NULL
  AND s.retry_count < 3
ORDER BY s.created_at ASC;

-- 인게이지먼트 전체 현황 요약
CREATE VIEW IF NOT EXISTS v_engagement_summary AS
SELECT
  e.id AS engagement_id, e.name AS engagement_name, e.client_name,
  e.status AS engagement_status, e.priority, e.deadline, e.component_count,
  COUNT(p.id)                                                  AS project_count,
  SUM(CASE WHEN p.status = 'COMPLETED' THEN 1 ELSE 0 END)     AS projects_completed,
  SUM(CASE WHEN p.status = 'ACTIVE'    THEN 1 ELSE 0 END)     AS projects_active,
  SUM(CASE WHEN p.status = 'PAUSED'    THEN 1 ELSE 0 END)     AS projects_paused,
  COUNT(n.id)                                                  AS total_nodes,
  SUM(CASE WHEN n.state = 'COMPLETED' THEN 1 ELSE 0 END)      AS nodes_completed,
  ROUND(
    100.0 * SUM(CASE WHEN n.state = 'COMPLETED' THEN 1 ELSE 0 END)
    / NULLIF(COUNT(n.id), 0), 1
  )                                                            AS overall_completion_pct,
  COALESCE(SUM(ct.total_cost_usd), 0)                         AS total_cost_usd,
  e.created_at, e.updated_at
FROM engagements e
LEFT JOIN projects p    ON p.engagement_id = e.id
LEFT JOIN dags d        ON d.project_id = p.id
LEFT JOIN nodes n       ON n.dag_id = d.id
LEFT JOIN cost_tracking ct ON ct.project_id = p.id
GROUP BY e.id;

-- 크로스 프로젝트 게이트 대기 현황
CREATE VIEW IF NOT EXISTS v_cross_project_gates AS
SELECT
  ee.id AS edge_id, e.name AS engagement_name,
  fp.name AS from_project, fp.component_type AS from_component, ee.from_phase,
  tp.name AS to_project, tp.component_type AS to_component, ee.to_phase,
  ee.gate_trigger_type,
  CASE WHEN NOT EXISTS (
    SELECT 1 FROM nodes n
    JOIN dags d ON d.id = n.dag_id
    WHERE d.project_id = fp.id
      AND n.phase = ee.from_phase
      AND n.state NOT IN ('COMPLETED')
      AND n.node_type IN ('TASK', 'QA')
  ) THEN 1 ELSE 0 END AS from_phase_completed,
  CASE WHEN tp.status NOT IN ('COMPLETED','ARCHIVED','FORCE_CLOSED')
       THEN 1 ELSE 0 END AS to_project_active
FROM engagement_edges ee
JOIN engagement_dags ed ON ed.id = ee.engagement_dag_id
JOIN engagements e      ON e.id = ed.engagement_id
JOIN projects fp        ON fp.id = ee.from_project_id
JOIN projects tp        ON tp.id = ee.to_project_id
WHERE ee.is_active = 1
ORDER BY e.priority ASC, ee.from_phase ASC;

-- v8 신규: Phase별 토큰 예산 현황 (BudgetEnforcer L3 참조)
CREATE VIEW IF NOT EXISTS v_phase_token_summary AS
SELECT
  engagement_id,
  phase,
  SUM(input_tokens)                       AS total_input_tokens,
  SUM(output_tokens)                      AS total_output_tokens,
  SUM(input_tokens + output_tokens)       AS total_tokens,
  COUNT(*)                                AS api_call_count,
  AVG(CASE WHEN estimated_input > 0
    THEN CAST(input_tokens AS REAL) / estimated_input
    ELSE NULL END)                        AS avg_estimation_accuracy,
  MAX(recorded_at)                        AS last_call_at
FROM agent_token_usage
GROUP BY engagement_id, phase;


-- ============================================================
-- 스키마 마이그레이션 기록 (001~013)
-- ============================================================

INSERT OR IGNORE INTO schema_migrations VALUES (
  '002_rbac',
  'RBAC 보안 모델: users 컬럼 확장 + sessions + audit_logs + provider_credentials',
  'placeholder_checksum_002', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '003_intake_bridge',
  '인테이크 연동: intake_submissions 테이블 + phase enum 확장',
  'placeholder_checksum_003', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '004_engagement_layer',
  'Engagement Layer: engagements + engagement_dags + engagement_edges + revision_requests + agent_processes + project_env_vars',
  'placeholder_checksum_004', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '005_error_recovery',
  '에러 복구: nodes.stall_count + nodes.task_snapshot (Stall Detection)',
  'placeholder_checksum_005', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '006_v4_additions',
  'v4: project_env_vars GLOBAL scope + is_test_resource / artifact_qa_stamps + v_artifact_badges',
  'placeholder_checksum_006', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '007_v5_improvements',
  'v5: nodes 10-state + suspension_reason / provider_credentials OAuth / v_artifact_badges 수정',
  'placeholder_checksum_007', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '008_token_budget_override',
  'v8: node_budget_overrides 테이블 — TOKEN_BUDGET 전역 변이 버그 수정. 노드별 예산은 이 테이블에 별도 기록.',
  'placeholder_checksum_008', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '009_invalidation_pending',
  'v8: nodes.invalidation_pending + invalidation_source_id + invalidation_queued_at — Cascade Invalidation 2-Phase 프로토콜 지원',
  'placeholder_checksum_009', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '010_qa_verification_columns',
  'v8: artifact_qa_stamps.verification_stage + verification_passed + verification_output — CodeVerificationSandbox 3단계 결과 기록',
  'placeholder_checksum_010', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '011_circuit_breaker_db',
  'v8: provider_circuit_breakers 테이블 — DB 기반 CB 상태 공유 (멀티 프로세스 대응). BEGIN IMMEDIATE 원자적 잠금 사용.',
  'placeholder_checksum_011', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '012_notification_subscriptions',
  'v8: notification_subscriptions 테이블 — Outbox 재사용 알림 구독 관리 (별도 notification_deliveries 테이블 불필요)',
  'placeholder_checksum_012', datetime('now')
);
INSERT OR IGNORE INTO schema_migrations VALUES (
  '013_agent_token_usage',
  'v8: agent_token_usage 테이블 + v_phase_token_summary 뷰 — BudgetEnforcer L3 Phase별 누적 예산 추적',
  'placeholder_checksum_013', datetime('now')
);
