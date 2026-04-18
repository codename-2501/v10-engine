# AI SI 매뉴팩처링 플랫폼 v8 — 구현 진행 현황

> 기준: DESIGN_SPEC_v8.md (설계 완료 99%)
> 최초 작성: 2026-03-24

---

## 모듈 구현 상태

| # | 모듈 | 경로 | 상태 | 완료일 |
|---|------|------|------|--------|
| M01 | DatabaseAdapter (SQLite/PostgreSQL 추상화) | `engine/db/adapter.py` | ✅ 완료 | 2026-03-24 |
| M02 | 도메인 이벤트 + 상태 머신 (10-state) | `engine/core/state_machine.py` | ✅ 완료 | 2026-03-24 |
| M03 | DAGAdvancer (직렬화 Queue 기반) | `engine/core/dag_advancer.py` | ✅ 완료 | 2026-03-24 |
| M04 | DB 스키마 마이그레이션 (001~014) | `engine/db/migrations/` | ✅ 완료 | 2026-03-24 |
| M05 | PathGuard (경로 순회 방어) | `engine/security/path_guard.py` | ✅ 완료 | 2026-03-24 |
| M06 | CodeVerificationSandbox (AST→Static→Docker) | `engine/security/code_sandbox.py` | ✅ 완료 | 2026-03-24 |
| M07 | AES-256-GCM 암호화 (환경변수/API 키) | `engine/security/crypto.py` | ✅ 완료 | 2026-03-24 |
| M08 | RBAC (역할 3계층) | `engine/security/rbac.py` | ✅ 완료 | 2026-03-24 |
| M09 | Circuit Breaker (BEGIN IMMEDIATE) | `engine/core/circuit_breaker.py` | ✅ 완료 | 2026-03-24 |
| M10 | TOKEN_BUDGET + BudgetEnforcer (L1/L2/L3) | `engine/core/budget_enforcer.py` | ✅ 완료 | 2026-03-24 |
| M11 | Model Provider Adapter (Claude Opus/Sonnet) | `engine/ai/model_adapter.py` | ✅ 완료 | 2026-03-24 |
| M12 | ContextAssembler (5-Layer Delta) | `engine/ai/context_assembler.py` | ✅ 완료 | 2026-03-24 |
| M13 | Cascade Invalidation 2-Phase | `engine/core/cascade.py` | ✅ 완료 | 2026-03-24 |
| M14 | ResourceResolver (GLOBAL/ENGAGEMENT/PROJECT) | `engine/core/resource_resolver.py` | ✅ 완료 | 2026-03-24 |
| M15 | IntakeProcessor (1→N 프로젝트 생성) | `engine/intake/processor.py` | ✅ 완료 | 2026-03-24 |
| M16 | GateNotificationService (Outbox + 지수 백오프) | `engine/core/gate_notification.py` | ✅ 완료 | 2026-03-24 |
| M17 | Observability (structlog + prometheus_client + AlertRules) | `engine/observability/` | ✅ 완료 | 2026-03-24 |
| M18 | ShutdownManager (SIGTERM → SUSPENDED) | `engine/lifecycle/shutdown.py` | ✅ 완료 | 2026-03-24 |
| M19 | Startup Recovery | `engine/lifecycle/startup.py` | ✅ 완료 | 2026-03-24 |
| M20 | SleepGuard (크로스 플랫폼) | `engine/platform/sleep_guard.py` | ✅ 완료 | 2026-03-24 |
| M21 | ValidationGateway (C1~C9) | `engine/core/validation_gateway.py` | ✅ 완료 | 2026-03-25 |
| M22 | OAuth 3사 (aiohttp) | `engine/auth/oauth.py` | ✅ 완료 | 2026-03-25 |
| M23 | FastAPI 서버 + API 엔드포인트 | `api/server.py` | ✅ 완료 | 2026-03-25 |
| M24 | WebSocket 실시간 통신 | `api/websocket.py` | ✅ 완료 | 2026-03-25 |
| M25 | Dashboard (Jinja2 + HTMX + D3.js) | `frontend/` | ✅ 완료 | 2026-03-25 |
| M26 | SQLite 백업 전략 | `engine/db/backup.py` | ✅ 완료 | 2026-03-25 |
| M27 | 테스트 (Hypothesis property-based) | `tests/` | ✅ 완료 | 2026-03-25 |
| M28 | launchd plist (macOS 자동 재시작) | `infra/` | ✅ 완료 | 2026-03-25 |

---

## 진행률

- 완료: **28 / 28** (100%) 🎉 + 보완 완료
- 진행 중: 0
- 미시작: 0

### 2026-03-25 추가 완성 (감사 보완)
- ImpactAnalyzer (C4): BFS downstream 분석 + Cascade Phase1 자동 예약
- ConflictDetector (C5): 헌법 규칙 diff + 활성 노드 충돌 감지 + C8 단계적 전환
- ValidationGateway C4/C5/C7/C8 메서드 추가 (C1~C9 완전 구현)
- requirements.txt + pyproject.toml + .env.example 추가
- frontend/__init__.py + tools/__init__.py 추가
- frontend/router.py → api/server.py 통합 (대시보드 라우트 등록)
- WebSocket Outbox 워커 lifespan 통합
- 누락 API 엔드포인트 추가: engagement CRUD/pause/resume/force-close, members, env-vars, projects, DAG gates, node detail/suspend/resume, artifacts, credentials, resources, alerts
- frontend/static/htmx.min.js (48KB) + d3.min.js (279KB) self-hosted 다운로드
- run.py 메인 엔트리포인트 추가
- tests/test_validation_gateway.py (C1~C9 전체 테스트) 추가

---

## 변경 이력

| 날짜 | 완료 모듈 | 비고 |
|------|-----------|------|
| 2026-03-24 | - | 초기 progress.md 생성, 설계 완료 기준 |
| 2026-03-24 | M01 | DatabaseAdapter: SQLiteAdapter(WAL+BEGIN IMMEDIATE) + PostgreSQLAdapter(asyncpg+SERIALIZABLE) + create_adapter factory |
| 2026-03-24 | M02 | StateMachine: NodeState(10) + VALID_TRANSITIONS + Phase + NodeType + SuspensionReason |
| 2026-03-24 | M03 | DAGAdvancer: asyncio.Queue 단일 소비자, Kahn's topological sort, Gate auto/manual, 낙관적 잠금 전이 |
| 2026-03-24 | M04 | MigrationRunner (UP/DOWN 파싱, SHA-256 체크섬) + SQL 파일 001/008/011/012/013/014 |
| 2026-03-24 | M05 | PathGuard: ALLOWED_ROOTS 기반 경로 순회 방어, configure_roots/validate/safe_join |
| 2026-03-24 | M06 | CodeVerificationSandbox: Stage1(AST) + Stage2(정적분석) + Stage3(Docker격리) |
| 2026-03-24 | M07 | AES256GCM: nonce+ciphertext base64, generate_key, key_from_env |
| 2026-03-24 | M08 | RBAC: Role(3) + Permission enum + _ROLE_PERMISSIONS 불변 매핑 + require/has_permission |
| 2026-03-24 | M09 | CircuitBreaker: CLOSED→OPEN→HALF_OPEN, BEGIN IMMEDIATE, 낙관적 잠금, recovery_timeout |
| 2026-03-24 | M10 | TOKEN_BUDGET(MappingProxyType 불변) + BudgetEnforcer L1(추정)/L2(max_tokens)/L3(Phase누적) |
| 2026-03-24 | M11 | ModelAdapter: 단일 Messages API 호출, CredentialProvider(APIKey/OAuth), RateLimitError |
| 2026-03-24 | M12 | ContextAssembler: 5-Layer(헌법/글로벌/Phase/Delta/실패이력), Delta-First, 예산 트림 |
| 2026-03-24 | M13 | CascadeInvalidator: Phase1(빠른 마킹) + Phase2(INVALID 전이) + BFS downstream + 배치 처리 |
| 2026-03-24 | M14 | ResourceResolver: PROJECT→ENGAGEMENT→GLOBAL 폴백, AES 복호화, validate_required_for_phase |
| 2026-03-24 | M15 | IntakeProcessor: Engagement+N프로젝트+DAG+노드 자동생성, Rule9 QA쌍, Phase Gate 삽입 |
| 2026-03-24 | M16 | GateNotificationService(Outbox패턴) + NotificationWorker(5단계 지수백오프 30~600초) |
| 2026-03-24 | M17 | Observability: structlog(JSON/콘솔) + prometheus_client(6개 메트릭) + AlertManager(30초 체크) |
| 2026-03-24 | M18 | ShutdownManager: SIGTERM→5단계(차단/25초대기/SUSPENDED/5초drain/WAL체크포인트) |
| 2026-03-24 | M19 | StartupRecovery: 좀비SUSPENDED+SHUTDOWN_DRAIN→READY + BackupScheduler(매일03:00) |
| 2026-03-24 | M20 | SleepGuard: macOS(caffeinate)/Linux(systemd-inhibit)/Windows(SetThreadExecutionState) |
| 2026-03-25 | M21 | ValidationGateway: C1(DFS순환)/C2(위상정렬)/C3(INVALID→BLOCKED)/C6(낙관적잠금)/C9(수동재실행) |
| 2026-03-25 | M22 | OAuth: Google(ServiceAccount RS256) + Anthropic(code/refresh) + OpenAI(refresh) via aiohttp |
| 2026-03-25 | M23 | FastAPI: lifespan(migration+recovery+shutdown) + JWT Bearer인증 + RBAC 엔드포인트 11개 |
| 2026-03-25 | M24 | WebSocket: ConnectionManager(engagement별 브로드캐스트) + OutboxWebSocketWorker(5초 폴링) + 스냅샷 |
| 2026-03-25 | M25 | Dashboard: base.html + index.html + engagement_detail + dag_view(D3 위상정렬) + router.py |
| 2026-03-25 | M26 | SQLite 백업: WAL체크포인트+VACUUM INTO+integrity_check+SHA-256+artifacts.tar.gz+30일정리 |
| 2026-03-25 | M27 | 테스트: Hypothesis(상태전이 500예제) + state_machine/budget_enforcer/resource_resolver/dag/cascade |
| 2026-03-25 | M28 | launchd: com.ai-si-platform.engine.plist + install.sh(자동경로감지+launchd등록) |
