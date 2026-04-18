# AI SI 매뉴팩처링 플랫폼 — 완전 설계 명세서 v8 (최종 확정본)

> 작성일: 2026-03-24
> v2 갱신: 인테이크 연동 브릿지 + Phase 재정의 + API 명세
> v3 갱신: Engagement Layer + 크로스 프로젝트 DAG + Dashboard 10-stage 통합
> v4 갱신: Model Adapter + claude_init.md 9대 규칙 + 에러 A~H + Circuit Breaker + 리소스 3계층 + QA 뱃지
> v5 갱신: 10-state 상태 머신 / DAGAdvancer 직렬화 / ImpactAnalyzer 배치화 / OAuth 3사 / CB 영속화 / Cascade 2-Phase / PathGuard / Graceful Shutdown / SQLite 백업 / 구조화 로깅 / 토큰 예산
> **v8 갱신 (최종 보완)**:
>   - TOKEN_BUDGET MappingProxyType 불변 선언 + node_budget_overrides 테이블
>   - Circuit Breaker BEGIN IMMEDIATE (SELECT FOR UPDATE SQLite 미지원 수정)
>   - CodeVerificationSandbox 3단계 (AST → Static → Docker) — subprocess+ulimit 대체
>   - ShutdownManager: SIGTERM → 25초 에이전트 대기 → SUSPENDED → 5초 DB drain
>   - GateNotificationService: Outbox 재사용 + 지수 백오프 재시도
>   - DatabaseAdapter 추상화: 각 Adapter가 자기 방언 직접 구현 (자동 번역 없음)
>   - Observability 3기둥: structlog(JSON) + prometheus_client(메트릭) + AlertRules
>   - BudgetEnforcer: L1(Pre-call 추정) + L2(max_tokens 제한) + L3(Phase 누적 추적)
>   - 테스트 전략: Hypothesis property-based testing + 독립 벤치마크 스크립트
>   - pip 의존성 7개 확정: fastapi uvicorn aiohttp PyJWT cryptography structlog prometheus_client
> 상태: **설계 완료 (v8 최종) — 코딩 즉시 착수 가능**

---

## 완성도 현황

| 단계 | 항목 | 완성도 | 상태 |
|------|------|--------|------|
| 1 | 시스템 비전/목표 | 99% | ✅ 확정 |
| 2 | 기술 스택 | 99% | ✅ **v8 확정 — pip 7개 (cryptography+structlog+prometheus_client 추가)** |
| 3 | 핵심 알고리즘 | 99% | ✅ v5 확정 — 10-state + DAGAdvancer 직렬화 |
| 3-E | Engagement Layer | 99% | ✅ v3 확정 |
| 4 | 도메인 이벤트 목록 | 99% | ✅ 확정 (455개 + QA뱃지 이벤트) |
| 5 | DB 스키마 (DDL) | 99% | ✅ **schema_v8.sql (38개 테이블, 10개 뷰) — migration 001~013** |
| 6 | RBAC/보안 모델 | 99% | ✅ PathGuard + CodeVerificationSandbox 3단계 |
| 7 | 에이전트 프롬프트 구조 | 99% | ✅ claude_init.md 9대 규칙 + Model Adapter |
| 8 | 에러 복구 플로우 | 99% | ✅ **v8 확정 — CB BEGIN IMMEDIATE + ShutdownManager** |
| 9 | API 명세 | 99% | ✅ Artifact/Revision/Escalation/Constitution/Badge/Resource/Credentials |
| 9-F | 리소스 3계층 관리 | 99% | ✅ GLOBAL/ENGAGEMENT/PROJECT + Fallback |
| 10 | 인테이크 연동 브릿지 | 99% | ✅ 1→N 프로젝트 생성 |
| 11 | Dashboard 10-stage 매핑 | 99% | ✅ v3 확정 |
| 12 | QA 뱃지 시스템 | 99% | ✅ artifact_qa_stamps + 대시보드 표시 |
| 13 | DAGAdvancer 직렬화 | 99% | ✅ Queue 기반 직렬 advance |
| 14 | SUSPENDED 상태 세분화 | 99% | ✅ suspension_reason 컬럼 |
| 15 | PathGuard 보안 | 99% | ✅ 경로 순회 공격 방어 |
| 16 | **Observability 스택** | 99% | ✅ **v8 신규 — structlog + prometheus_client + AlertRules** |
| 17 | **BudgetEnforcer** | 99% | ✅ **v8 신규 — L1/L2/L3 3단계 예산 집행** |
| 18 | **CodeVerificationSandbox** | 99% | ✅ **v8 신규 — 3단계 코드 검증** |
| 19 | **DatabaseAdapter** | 99% | ✅ **v8 신규 — SQLite/PostgreSQL 추상화** |
| 20 | **GateNotificationService** | 99% | ✅ **v8 신규 — Outbox 재사용 + 지수 백오프** |
| 21 | 실제 코드 | 0% | 🔲 **설계 완료 → 즉시 착수 가능** |

---

## 1. 시스템 비전 (99% ✅)

### 한 줄 정의
> 설계자 10명 이하가 Claude Code(Opus/Sonnet)를 통해 고객 요구사항 입력 → 기획/디자인/개발 산출물을 자동 생성하는 **독재적 Python DAG 엔진**

### 확정 사항
- **동시 프로젝트**: 최대 5개
- **운용 인원**: 설계자 10명 이하
- **배포 환경**: 로컬(Mac) 우선 → 추후 클라우드 이전 가능하게 설계
- **운용 시간**: 24/7 무중단
- **운용 하드웨어**: Mac mini m4pro, RAM 64GB+
- **성공 기준**: 생산 시간 단축 + 투입 인력 절감 + 산출물 품질 일관성 (3가지 모두)

### 설계 철학
- **Rules > AI**: Python 엔진만이 상태를 결정. AI는 샌드박스 내 실행 함수로만 취급
- **AI는 집행자**: claude_init.md를 읽은 Claude Code는 규율 집행자로 동작. 창의적 제안 금지
- **UI는 껍데기**: 프론트엔드는 상태 표시만. 모든 판단 로직은 Python 엔진 독점
- **Delta 우선**: 전체 문서 재전송 금지. 변경된 최소 단위(Delta)만 AI에게 전달
- **외부 프레임워크 배제**: LangChain, CrewAI, LangGraph 등 일체 사용 금지
- **폴리글랏 산출물**: 공장(Python)은 하나. 결과물은 고객 요구에 따라 어떤 기술 스택이든 생성 가능
- **인테이크 독립**: 인테이크 폼은 시스템 외부 인터페이스. 엔진과 분리되어 독립 진화 가능

---

## 2. 기술 스택 (99% ✅)

### 핵심 원칙
```
1순위: Python 표준 라이브러리 (pip 없이)
2순위: 불가피할 때만 최소 외부 패키지
3순위: 확장 시 교체 가능하도록 추상화
```

### 확정 스택

| 계층 | 기술 | pip 필요 | 비고 |
|------|------|---------|------|
| 오케스트레이션 엔진 | asyncio | ❌ | Python 표준 |
| DB | sqlite3 | ❌ | Python 표준 |
| 변경 감지 | difflib, ast | ❌ | Python 표준 |
| API 서버 | FastAPI + uvicorn | ✅ 2개 | 최소 불가피 |
| AI SDK | anthropic | ✅ 1개 | Anthropic API 공식 SDK |
| OAuth 토큰 교환 | aiohttp | ✅ 1개 | 3사 OAuth 지원 |
| Google SA JWT 서명 | PyJWT | ✅ 1개 | Service Account 인증 |
| AES-256-GCM 암호화 | cryptography | ✅ 1개 | **v8 신규** — 환경변수/API 키 암호화 |
| 구조화 로깅 | structlog | ✅ 1개 | **v8 신규** — JSON 로그 출력 |
| 메트릭 수집 | prometheus_client | ✅ 1개 | **v8 신규** — /metrics 엔드포인트 노출 |
| 프론트엔드 | Jinja2 (FastAPI 내장) | ❌ | 빌드 없음 |
| 동적 UI | HTMX (self-hosted JS) | ❌ | 14KB 파일 1개 |
| 실시간 통신 | WebSocket (FastAPI 내장) | ❌ | |
| DAG 시각화 | D3.js (self-hosted) | ❌ | |
| 절전 방지 | **SleepGuard** (cross-platform) | ❌ | **v8 수정** — caffeinate(macOS) + 타 플랫폼 대응 |
| 자동 재시작 | launchd (macOS 내장) | ❌ | |

**전체 pip 의존성 (v8 확정): `fastapi uvicorn aiohttp PyJWT cryptography structlog prometheus_client` — 7개**

> ⚠️ v5에서 제거된 의존성:
> - `apscheduler`: 불필요 (asyncio.sleep 루프로 대체 가능한 cron 작업 없음)

### DB 전환 전략 (v8 수정 — DatabaseAdapter 추상화)

```python
# engine/db/adapter.py
# 핵심 원칙: 각 Adapter가 자기 방언을 직접 구현. 자동 번역 없음.
# SQLite → PostgreSQL 전환 도구: pgloader

from abc import ABC, abstractmethod

class DatabaseAdapter(ABC):
    @abstractmethod
    async def fetchone(self, query: str, params: tuple) -> dict | None: ...
    @abstractmethod
    async def fetchall(self, query: str, params: tuple) -> list[dict]: ...
    @abstractmethod
    async def execute(self, query: str, params: tuple) -> None: ...
    @abstractmethod
    async def begin_immediate(self): ...  # SQLite 전용: 원자적 잠금
    @abstractmethod
    async def begin_serializable(self): ...  # PostgreSQL 전용: SERIALIZABLE

class SQLiteAdapter(DatabaseAdapter):
    """SQLite WAL 모드. workers=1 전용 (멀티 프로세스 = PostgreSQL 사용)."""
    async def begin_immediate(self):
        # BEGIN IMMEDIATE → DB 레벨 잠금, row-level 아님 (SELECT FOR UPDATE 미지원)
        await self.execute("BEGIN IMMEDIATE", ())
    ...

class PostgreSQLAdapter(DatabaseAdapter):
    """멀티 프로세스 환경. asyncpg 기반."""
    async def begin_immediate(self):
        await self.execute("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE", ())
    ...
```

> **pgloader 마이그레이션 명령 (SQLite → PostgreSQL)**:
> ```bash
> pgloader sqlite:///platform.db postgresql://user:pass@host/platform_db
> ```
> 각 Adapter가 자기 방언(json_extract/jsonb, datetime/CURRENT_TIMESTAMP, randomblob/gen_random_uuid 등)으로 직접 구현. 자동 방언 번역 모듈 없음.

### Mac 운용 설정

```bash
# 절전 방지 (v8 수정: SleepGuard — 크로스 플랫폼)
# engine/platform/sleep_guard.py 참조

# 부팅 시 자동 시작 (launchd)
# ~/Library/LaunchAgents/com.ai-si-platform.engine.plist
```

---

## 3. 핵심 알고리즘 (99% ✅)

### 3-1. 9대 MANDATORY Rules (엔진 하드코딩, 예외 없음)

```
Rule 1: DAG 구조 강제 — 순환 참조(Cycle) 절대 불가
Rule 2: 위상 정렬 실행 순서만 허용 — 임의 순서 금지
Rule 3: 모든 의존성이 COMPLETED일 때만 노드 실행 가능
Rule 4: COMPLETED 노드도 상위 변경 시 INVALID 가능
Rule 5: 영향받지 않은 노드는 절대 중단하지 않음
Rule 6: 동시 변경 시 최신 스냅샷 기준으로 충돌 해결
Rule 7: 신규 노드 추가 시 자동으로 의존성 등록 + 상태 부여
Rule 8: 모든 규칙 MANDATORY, AI 임의 해석 및 예외 금지
Rule 9: QA-Pairing 필수 — Task 노드 생성 시 QA 노드 자동 쌍 생성
         QA 노드가 COMPLETED 승인해야만 Task도 COMPLETED 처리
```

### 3-2. 노드 상태 (10가지)

```
NOT_STARTED        초기 상태
READY              의존성 충족, 실행 대기
IN_PROGRESS        에이전트 실행 중
COMPLETED          TASK + QA 통과
BLOCKED            선행 미완료
INVALID            상위 변경으로 무효
AWAITING_APPROVAL  Gate/외부 승인 대기
NEEDS_HUMAN        3회 실패, 설계자 개입
SUSPENDED          예산 초과 또는 Circuit Breaker 차단 (v5 신규)
FAILED             복구 불가
```

### 3-3. 상태 전이 규칙 (VALID_TRANSITIONS — 엔진 하드코딩)

```python
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    'NOT_STARTED':       frozenset({'READY', 'BLOCKED', 'INVALID'}),
    'READY':             frozenset({'IN_PROGRESS', 'BLOCKED', 'INVALID'}),
    'IN_PROGRESS':       frozenset({'COMPLETED', 'FAILED', 'INVALID',
                                    'NEEDS_HUMAN', 'SUSPENDED', 'AWAITING_APPROVAL'}),
    'COMPLETED':         frozenset({'INVALID'}),
    'BLOCKED':           frozenset({'READY', 'NOT_STARTED', 'INVALID'}),
    'INVALID':           frozenset({'READY', 'NOT_STARTED'}),
    'AWAITING_APPROVAL': frozenset({'COMPLETED', 'INVALID'}),
    'NEEDS_HUMAN':       frozenset({'IN_PROGRESS', 'INVALID', 'FAILED'}),
    'SUSPENDED':         frozenset({'READY', 'INVALID', 'FAILED'}),
    'FAILED':            frozenset({'READY', 'INVALID'}),
}
```

> **테스트 전략 (v8 신규 — Hypothesis property-based testing)**:
> 단순 커버리지 80% 목표 대신 상태 전이 완전성 검증.
> ```python
> # tests/test_state_machine.py
> from hypothesis import given, strategies as st
> from engine.state_machine import VALID_TRANSITIONS, StateMachine
>
> @given(
>     from_state=st.sampled_from(list(VALID_TRANSITIONS.keys())),
>     to_state=st.sampled_from(list(VALID_TRANSITIONS.keys()))
> )
> def test_all_valid_transitions_succeed(from_state, to_state):
>     if to_state in VALID_TRANSITIONS[from_state]:
>         # 허용된 전이 → 반드시 성공
>         StateMachine.transition(mock_node(from_state), to_state)
>     else:
>         # 금지된 전이 → 반드시 InvalidTransitionError
>         with pytest.raises(InvalidTransitionError):
>             StateMachine.transition(mock_node(from_state), to_state)
> ```

### 3-4. DAG 실행 방식: 하이브리드

```
Phase 간: 순차 실행
  API_SERVER 완료 → Gate → PLANNING 시작
  PLANNING 완료  → Gate → DESIGN 시작
  DESIGN 완료    → Gate → DEVELOPMENT 시작
  DEVELOPMENT 완료 → Gate → INFRASTRUCTURE 시작
  INFRASTRUCTURE 완료 → Gate → DELIVERY

Phase 내: 병렬 실행 (의존성 없는 노드끼리 동시 실행)
```

### 3-5. DAGAdvancer 직렬화 (v5 신규)

```python
# engine/dag_advancer.py
# 핵심: asyncio.Queue 기반 단일 소비자 → 경쟁 조건 없는 직렬 advance

class DAGAdvancer:
    def __init__(self):
        self._queue: asyncio.Queue[str] = asyncio.Queue()  # dag_id 큐
        self._running: bool = False

    async def enqueue(self, dag_id: str) -> None:
        await self._queue.put(dag_id)

    async def run(self) -> None:
        """단일 소비자 루프 — 절대 병렬 advance 없음"""
        self._running = True
        while self._running:
            try:
                dag_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._advance_dag(dag_id)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue

    async def _advance_dag(self, dag_id: str) -> None:
        """COMPLETED 노드 탐색 → READY 전환 가능한 노드 찾아 실행"""
        ready_nodes = db.query(
            "SELECT * FROM nodes WHERE dag_id=? AND state='READY' ORDER BY priority",
            (dag_id,)
        )
        for node in ready_nodes:
            await self._start_node(node)
```

### 3-6. Gate 노드

```
위치: 모든 Phase 전환점
  API_SERVER 완료 → GATE → PLANNING 시작
  PLANNING 완료   → GATE → DESIGN 시작
  DESIGN 완료     → GATE → DEVELOPMENT 시작
  DEVELOPMENT 완료 → GATE → INFRASTRUCTURE 시작
  INFRASTRUCTURE 완료 → GATE → DELIVERY

모드:
  수동(기본): 설계자 Approve 클릭 전까지 AWAITING_APPROVAL 대기
  자동(토글 ON): QA 통과 즉시 AUTO_APPROVED 처리
```

### 3-7. Phase 열거값 (확정)

```
API_SERVER      기술 아키텍처 설계, API 명세, DB 스키마 초안
PLANNING        요구사항 분석, 기능 명세, 플로우차트, IA, 와이어프레임
DESIGN          UI/UX 디자인, 화면설계서, 디자인 시스템, 프로토타입
DEVELOPMENT     프론트엔드 개발, 백엔드 개발, 통합
INFRASTRUCTURE  서버 구축, CI/CD, 모니터링, 도메인, 배포 스크립트
DELIVERY        최종 산출물 전달, 클라이언트 인수인계
```

### 3-8. TOKEN_BUDGET 불변 선언 (v8 수정)

```python
# engine/config.py
# 핵심: MappingProxyType → 런타임 변이 방지
# 노드별 예산 조정은 node_budget_overrides 테이블에 별도 기록

from types import MappingProxyType

TOKEN_BUDGET: MappingProxyType = MappingProxyType({
    'max_input':   150_000,  # 150K 입력 토큰 한도
    'max_output':  16_000,   # 16K 출력 토큰 한도
    'phase_limit': {
        'API_SERVER':      500_000,
        'PLANNING':        600_000,
        'DESIGN':          400_000,
        'DEVELOPMENT':   1_200_000,
        'INFRASTRUCTURE':  300_000,
        'DELIVERY':        200_000,
    }
})

# 금지: TOKEN_BUDGET['max_input'] = 200_000  → TypeError (불변)
# 노드별 오버라이드: node_budget_overrides 테이블에 INSERT
```

### 3-9. Cascade Invalidation 2-Phase 프로토콜 (v5 신규)

```python
# Phase 1: 빠른 마킹 (DB 잠금 최소화)
async def phase1_mark_pending(changed_node_id: str) -> list[str]:
    impacted = get_impacted_nodes(changed_node_id)
    for node_id in impacted:
        db.execute("""
            UPDATE nodes
            SET invalidation_pending=1,
                invalidation_source_id=?,
                invalidation_queued_at=datetime('now')
            WHERE id=?
        """, (changed_node_id, node_id))
    return impacted

# Phase 2: 실제 상태 전이 (낙관적 잠금 + 버전 확인)
async def phase2_apply_invalid(node_id: str) -> None:
    node = db.get(node_id)
    if node.invalidation_pending == 0:
        return  # 이미 처리됨 (중복 방지)
    if node.state == 'IN_PROGRESS':
        await terminate_agent(node_id)
    db.execute("""
        UPDATE nodes
        SET state='INVALID',
            invalidation_pending=0,
            version=version+1
        WHERE id=? AND version=?
    """, (node_id, node.version))
    emit('NodeInvalidatedByParentChange', node_id)
```

### 3-10. 재시도 전략

```
1차 실패 → 실패 이유를 프롬프트에 추가 후 재시도
2차 실패 → 1차 + 2차 실패 이유 누적 후 재시도
3차 실패 → 즉시 NEEDS_HUMAN (에스컬레이션)
```

### 3-11. Rate Limit 동적 조절

```
초기값: max_concurrent_agents = 5
429 응답: backoff_time = 2^retry_count × 1초 (최대 64초), max = max(1, current-1)
60초 연속 성공: max = min(15, current+1)
```

---

## 3-E. Engagement Layer 아키텍처 (99% ✅)

### 계층 구조

```
Engagement (클라이언트 계약 단위)
├── Master Project         — API_SERVER, PLANNING, DELIVERY (공유 페이즈)
├── WEB Project            — DESIGN, DEVELOPMENT, INFRASTRUCTURE
├── MOBILE Project         — DESIGN, DEVELOPMENT, INFRASTRUCTURE
├── API Project            — DESIGN, DEVELOPMENT, INFRASTRUCTURE
├── ADMIN Project          — DESIGN, DEVELOPMENT, INFRASTRUCTURE
└── INFRA Project          — DEVELOPMENT, INFRASTRUCTURE
```

### IntakeProcessor v3 (1→N 프로젝트 생성)

```python
class IntakeProcessorV3:
    def process(self, submission: IntakeSubmission) -> Engagement:
        engagement = Engagement.create(
            name=submission.project_name,
            client_name=submission.client_name,
            global_context=self._build_global_context(submission)
        )
        master = Project.create(
            engagement_id=engagement.id, component_type='MASTER'
        )
        component_types = self._extract_components(submission)
        subprojects = [
            Project.create(engagement_id=engagement.id, component_type=ct)
            for ct in component_types
        ]
        eng_dag = EngagementDag.create(engagement_id=engagement.id)
        self._create_cross_project_gates(eng_dag, master, subprojects)
        return engagement
```

### 크로스 프로젝트 Gate 매핑

| from_project | from_phase | to_project | to_phase | gate_type |
|-------------|-----------|-----------|---------|-----------|
| MASTER | PLANNING | ALL | DESIGN | AUTO |
| WEB | DESIGN | MOBILE | DESIGN | MANUAL_DESIGNER |
| API | DEVELOPMENT | WEB | DEVELOPMENT | AUTO |
| API | DEVELOPMENT | MOBILE | DEVELOPMENT | AUTO |
| ALL | DEVELOPMENT | MASTER | DELIVERY | CLIENT_APPROVAL |

### Dashboard 10-stage ↔ 6-Phase 매핑

```
Dashboard Stage       → Platform Phase / Rule              비고
─────────────────────────────────────────────────────────────────
pm                    → PLANNING (TASK 노드들)              기획 작성
pm-review             → Rule 9: QA_PAIRING                  기획 QA 자동 쌍
approval              → GATE(CLIENT_APPROVAL)               클라이언트 승인
qa-design             → GATE(MANUAL_DESIGNER)               디자인 게이트
design                → DESIGN (TASK 노드들)                디자인 작성
dev                   → DEVELOPMENT (TASK 노드들)           개발 작성
qa-code               → Rule 9: QA_PAIRING                  코드 QA 자동 쌍
qa-fix                → Node.retry_loop + revision_requests  수정 반복
devops                → INFRASTRUCTURE (TASK 노드들)        배포 설정
docs                  → DELIVERY (TASK 노드들)              최종 납품
```

---

## 4. 도메인 이벤트 (455개 + v8 추가)

### 카테고리별 이벤트 수

| 카테고리 | 수 | 주요 이벤트 |
|----------|-----|------------|
| System | 6 | SystemStarted, SystemShutdownInitiated, SystemShutdownCompleted, SystemBackupCompleted, SystemBackupFailed, SystemRecoveryCompleted |
| User | 12 | UserRegistered, UserLoggedIn, UserLoggedOut, UserLocked, UserRoleChanged ... |
| Intake | 15 | IntakeSubmitted, IntakeValidated, IntakeConversionCompleted ... |
| Engagement | 22 | EngagementCreated, EngagementActivated, EngagementPaused, CrossProjectGateApproved ... |
| Project | 28 | ProjectCreated, ProjectPhaseAdvanced, ProjectCompleted ... |
| Requirements | 18 | RequirementsBaselined, RequirementsConflictDetected ... |
| DAG | 14 | DagInitialized, DagValidated, DagExecutionStarted ... |
| Node | 52 | NodeStateChanged, NodeInvalidatedByParentChange, NodeKilledBySystem, NodeRetrying, NodeStallDetected, NodeSuspendedByBudget, NodeSuspendedByCircuitBreaker, NodeSuspendedByShutdown ... |
| Edge | 6 | EdgeCreated, EdgeDeactivated ... |
| Gate | 16 | GateTriggered, GateApproved, GateRejected, GateAutoApproved ... |
| AgentRun | 24 | AgentRunStarted, AgentRunCompleted, AgentRunFailed, AgentRunRateLimited ... |
| Budget | 10 | BudgetWarning70, BudgetWarning90, BudgetExceeded, NodeBudgetOverrideCreated ... |
| Artifact | 20 | ArtifactCreated, ArtifactVersionCreated, ArtifactInvalidated, QAStampGranted ... |
| Delta | 8 | DeltaCreated, DeltaApplied ... |
| Impact | 12 | ImpactAnalysisStarted, ImpactNodesFound ... |
| Snapshot | 6 | SnapshotCreated, SnapshotRestored ... |
| Constitution | 8 | ConstitutionActivated, ConstitutionConflictDetected ... |
| Template | 12 | TemplateCreated, TemplatePublished ... |
| Pattern | 10 | PatternRecorded, PatternValidated ... |
| Escalation | 20 | EscalationCreated, EscalationResolved, EscalationAutoResolved ... |
| Outbox | 8 | OutboxMessageCreated, OutboxMessageDelivered, OutboxDeadLettered ... |
| Concurrency | 6 | ConcurrencyThrottleReduced, ConcurrencyThrottleIncreased ... |
| RevisionRequest | 12 | RevisionRequestCreated, RevisionRequestResolved ... |
| **Notification (v8)** | **8** | **NotificationSubscribed, NotificationDelivered, NotificationFailed, NotificationDeadLettered ...** |
| **CircuitBreaker (v8)** | **6** | **CircuitBreakerOpened, CircuitBreakerHalfOpen, CircuitBreakerClosed ...** |
| **BudgetEnforcer (v8)** | **6** | **BudgetEnforcerL1Exceeded, BudgetEnforcerL2Applied, BudgetEnforcerL3Warning ...** |
| **Verification (v8)** | **6** | **VerificationStage1Passed, VerificationStage2Blocked, VerificationStage3DockerRun ...** |

---

## 5. DB 스키마 (99% ✅)

**→ schema_v8.sql 참조 (38개 테이블, 10개 뷰, migration 001~013)**

### 핵심 테이블 목록

| # | 테이블 | 용도 |
|---|--------|------|
| 1 | schema_migrations | 마이그레이션 버전 추적 |
| 2 | users | RBAC 사용자 |
| 3 | constitution_versions | claude_init.md 버전 관리 |
| 4 | templates | DAG 템플릿 |
| 5 | intake_submissions | 인테이크 폼 원본 |
| 6 | engagements | 클라이언트 계약 단위 |
| 7 | projects | 서브프로젝트 |
| 8 | project_members | 프로젝트 멤버 |
| 9 | engagement_members | 인게이지먼트 멤버 |
| 10 | requirements | 요구사항 |
| 11 | requirement_versions | 요구사항 버전 |
| 12 | dags | DAG 상태 |
| 13 | engagement_dags | 크로스 프로젝트 DAG |
| 14 | engagement_edges | 크로스 프로젝트 Gate 엣지 |
| 15 | **nodes** | **핵심 — 10-state, invalidation_pending (v8)** |
| 16 | edges | DAG 의존성 엣지 |
| 17 | agent_runs | 에이전트 실행 기록 |
| 18 | agent_processes | PID + 생존 감지 |
| 19 | artifacts | 산출물 |
| 20 | artifact_versions | 산출물 버전 히스토리 |
| 21 | deltas | 변경분 (Delta-First) |
| 22 | revision_requests | 수정 요청 |
| 23 | project_env_vars | 환경변수 암호화 저장 |
| 24 | cost_tracking | 일별 비용 집계 |
| 25 | budget_limits | 프로젝트 예산 상한 |
| 26 | rate_limit_state | Rate Limit 동적 조절 상태 |
| 27 | cross_project_patterns | 크로스 프로젝트 학습 패턴 |
| 28 | escalations | 에스컬레이션 |
| 29 | event_store | 불변 이벤트 로그 (단일 진실 원천) |
| 30 | outbox | Outbox 패턴 (원자성 보장) |
| 31 | aggregate_snapshots | 집합체 스냅샷 |
| 32 | sessions | JWT 서버사이드 무효화 |
| 33 | audit_logs | 감사 로그 (불변) |
| 34 | provider_credentials | AI 제공자 자격증명 (OAuth 포함) |
| 35 | artifact_qa_stamps | QA 뱃지 이력 + **검증 결과 (v8)** |
| 36 | **node_budget_overrides** | **v8 신규 — TOKEN_BUDGET 노드별 오버라이드** |
| 37 | **provider_circuit_breakers** | **v8 신규 — DB 기반 CB 상태 공유** |
| 38 | **notification_subscriptions** | **v8 신규 — Outbox 재사용 알림 구독** |
| 39 | **agent_token_usage** | **v8 신규 — Phase별 토큰 누적 추적** |

---

## 6. RBAC/보안 모델 (99% ✅)

### 역할 3계층

```
ADMIN           — 시스템 전체 관리, provider_credentials 접근, 헌법 활성화
SENIOR_DESIGNER — 프로젝트 CRUD, Gate 승인, 에스컬레이션 처리
DESIGNER        — 프로젝트 조회, 노드 재실행 요청
```

### PathGuard (v5 신규)

```python
# engine/storage/path_guard.py
class PathGuard:
    ALLOWED_ROOTS = [
        Path('./artifacts').resolve(),
        Path('./sandboxes').resolve(),
        Path('./backups').resolve(),
    ]

    @classmethod
    def validate(cls, path: str) -> Path:
        resolved = Path(path).resolve()
        for root in cls.ALLOWED_ROOTS:
            if str(resolved).startswith(str(root)):
                return resolved
        raise SecurityError(f"경로 순회 공격 차단: {path!r}")
```

### CodeVerificationSandbox (v8 신규 — 3단계)

> subprocess+ulimit 대체. Windows 호환성 + 크로스 플랫폼 코드 검증.

```python
# engine/verification/sandbox.py

import ast
import re
import asyncio
from pathlib import Path

class CodeVerificationSandbox:
    """
    3단계 코드 검증 샌드박스.
    Stage 1: AST 파싱 (구문 오류 탐지)
    Stage 2: 정적 위험 분석 화이트리스트 (금지 패턴 검사)
    Stage 3: Docker 실행 (선택적 — Docker 사용 가능 환경에서만)
    """

    ALLOWED_MODULES: frozenset[str] = frozenset({
        "os", "sys", "json", "re", "math", "datetime",
        "pathlib", "typing", "collections", "itertools",
        "fastapi", "pydantic", "sqlalchemy", "aiohttp",
        "structlog",
    })

    BLOCKED_PATTERNS: list[str] = [
        r"subprocess",
        r"eval\s*\(",
        r"exec\s*\(",
        r"__import__\s*\(",
        r"open\s*\(.*['\"]w['\"]",  # 쓰기 모드 파일 오픈
        r"os\.system\s*\(",
        r"shutil\.rmtree",
        r"socket\s*\.",
    ]

    async def verify(self, artifact) -> VerificationResult:
        # Stage 1: AST 파싱
        try:
            tree = ast.parse(artifact.content)
        except SyntaxError as e:
            return VerificationResult(
                stage='AST', passed=False,
                output={'error': str(e)}
            )

        # Stage 2: 정적 위험 분석
        risk = self._analyze_ast(tree, artifact.content)
        if risk.is_blocked:
            return VerificationResult(
                stage='STATIC', passed=False,
                output={'blocked_patterns': risk.matched, 'risk_score': risk.score}
            )

        # Stage 3: Docker 실행 (선택적)
        if artifact.test_content and self._docker_available():
            return await self._run_in_docker(artifact)

        return VerificationResult(
            stage='STATIC', passed=True,
            output={'risk_score': risk.score, 'modules_used': risk.imports}
        )

    def _analyze_ast(self, tree: ast.AST, source: str) -> RiskAnalysis:
        imports = [
            node.names[0].name.split('.')[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        blocked_imports = [m for m in imports if m not in self.ALLOWED_MODULES]
        matched_patterns = [
            p for p in self.BLOCKED_PATTERNS
            if re.search(p, source)
        ]
        score = len(blocked_imports) * 10 + len(matched_patterns) * 20
        return RiskAnalysis(
            imports=imports,
            matched=matched_patterns + blocked_imports,
            score=score,
            is_blocked=(score >= 20)
        )

    def _docker_available(self) -> bool:
        try:
            result = asyncio.run(
                asyncio.create_subprocess_exec("docker", "info",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
            )
            return result.returncode == 0
        except (FileNotFoundError, OSError):
            return False

    async def _run_in_docker(self, artifact) -> VerificationResult:
        # 격리된 컨테이너에서 테스트 실행 (네트워크 없음, 읽기 전용 마운트)
        cmd = [
            "docker", "run", "--rm", "--network=none",
            "--read-only", "--memory=256m", "--cpus=0.5",
            "-v", f"{artifact.sandbox_path}:/code:ro",
            "python:3.12-slim", "python", "/code/test_runner.py"
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        passed = proc.returncode == 0
        return VerificationResult(
            stage='DOCKER', passed=passed,
            output={'exit_code': proc.returncode, 'stdout': stdout.decode()[:1000]}
        )
```

### AES-256-GCM 암호화

```python
# engine/crypto.py
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os, base64

class AES256GCM:
    @staticmethod
    def encrypt(plaintext: str, key_hex: str) -> str:
        key = bytes.fromhex(key_hex)
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
        return base64.b64encode(nonce + ciphertext).decode()

    @staticmethod
    def decrypt(encrypted_b64: str, key_hex: str) -> str:
        key = bytes.fromhex(key_hex)
        raw = base64.b64decode(encrypted_b64)
        nonce, ciphertext = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode()
```

---

## 7. 에이전트 프롬프트 구조 (99% ✅)

### Orchestrator-Controlled Single Call

```
에이전트 루프 완전 금지:
  ✗ while True: agent.run()
  ✗ 에이전트 자율 반복 호출
  ✓ Python 엔진이 단일 API 호출 → 결과 파싱 → 다음 액션 결정
```

### ContextAssembler 5-Layer

```python
class ContextAssembler:
    """
    Layer 0: 헌법 (claude_init.md — 최우선, 항상 포함)
    Layer 1: 프로젝트 글로벌 컨텍스트 (requirements, global_context)
    Layer 2: Phase 컨텍스트 (현재 Phase 목표)
    Layer 3: UpstreamDelta (상위 노드 변경분 — Delta-First)
    Layer 4: 노드 명세 (이 노드가 생성해야 할 산출물)
    Layer 5: 실패 이력 (retry_count > 0 일 때만 포함)
    """

    MAX_TOKENS: int = 100_000  # 전체 컨텍스트 상한

    def assemble(self, node: Node, budget: dict) -> str:
        layers = [
            self._constitution_layer(node),     # 0: 항상 포함
            self._project_layer(node),           # 1: 항상 포함
            self._phase_layer(node),             # 2: 항상 포함
            self._upstream_delta_layer(node),    # 3: Delta-First
            self._node_spec_layer(node),         # 4: 항상 포함
            self._failure_history_layer(node),   # 5: retry > 0 시만
        ]
        return self._trim_to_budget(layers, budget['max_input'])

    def _trim_to_budget(self, layers: list[str], limit: int) -> str:
        """
        토큰 상한 초과 시 Layer 3(UpstreamDelta)부터 선택적 트림.
        Layer 0, 4는 절대 트림 안 함.
        """
        ...
```

### Model Provider Adapter (v5 신규)

```python
# engine/auth/credential_provider.py

class CredentialProvider(ABC):
    @abstractmethod
    async def get_headers(self) -> dict[str, str]: ...

class AnthropicAPIKeyProvider(CredentialProvider):
    async def get_headers(self) -> dict[str, str]:
        key = AES256GCM.decrypt(self.encrypted_key, os.environ['AES_ENCRYPTION_KEY'])
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}

class OAuthProvider(CredentialProvider):
    """Anthropic/OpenAI/Google OAuth 토큰 자동 갱신"""
    async def get_headers(self) -> dict[str, str]:
        if self._is_token_expired():
            await self._refresh_token()
        return {"Authorization": f"Bearer {self._access_token}"}
```

### claude_init.md 헌법 구조 (9대 규칙)

```markdown
# AI 에이전트 헌법 v1.0

## 핵심 정체성
당신은 규율 집행자입니다. 규칙을 준수하고 산출물을 생성합니다.
창의적 제안, 대안 제시, 스코프 확장 — 모두 금지입니다.

## 9대 MANDATORY Rules
[Rule 1~9 동일]

## 산출물 생성 규칙
1. 지정된 파일 형식만 출력
2. 코드: 완전하고 즉시 실행 가능한 형태
3. 마크다운: 구조화된 문서 형식
4. 미완성 TODO 절대 금지

## 실패 처리
- 불가능한 작업 → BLOCKED 신호 명시
- 정보 부족 → 필요한 정보 목록 명시
- 규칙 위반 요청 → 거부 후 거부 사유 명시
```

---

## 8. 에러 복구 플로우 (99% ✅)

### 에러 유형 A~H

| 유형 | 트리거 | 처리 |
|------|--------|------|
| A: API 오류 | HTTP 4xx/5xx | 지수 백오프 재시도 |
| B: Rate Limit | HTTP 429 | backoff + 동시성 감소 |
| C: 좀비 노드 | heartbeat 30분 이상 없음 | 강제 종료 + FAILED |
| D: Stall | exit=0, 진척 없음 3회 | NEEDS_HUMAN 에스컬레이션 |
| E: 예산 초과 | BudgetEnforcer L1/L2/L3 | SUSPENDED |
| F: Circuit Breaker OPEN | 5회 연속 실패 | SUSPENDED + 에스컬레이션 |
| G: Cascade Invalidation | 상위 노드 변경 | 2-Phase 무효화 |
| H: Shutdown | SIGTERM 수신 | ShutdownManager |

### Circuit Breaker (v8 수정 — BEGIN IMMEDIATE)

```python
# engine/circuit_breaker.py
# ⚠️ SQLite는 SELECT FOR UPDATE 미지원 → BEGIN IMMEDIATE 사용

class CircuitBreaker:
    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT = 60.0  # 초
    HALF_OPEN_MAX_CALLS = 3

    async def record_failure(self, provider: str) -> CBState:
        async with db.begin_immediate():  # DB 레벨 잠금
            row = db.fetchone(
                "SELECT failure_count, state, version, opened_at "
                "FROM provider_circuit_breakers WHERE provider_name=?",
                (provider,)
            )
            new_count = row['failure_count'] + 1
            new_state = row['state']

            if new_count >= self.FAILURE_THRESHOLD and row['state'] == 'CLOSED':
                new_state = 'OPEN'
                opened_at = datetime.utcnow().isoformat()
                emit('CircuitBreakerOpened', provider)
            else:
                opened_at = row['opened_at']

            # version 낙관적 잠금으로 동시 업데이트 충돌 감지
            affected = db.execute("""
                UPDATE provider_circuit_breakers
                SET failure_count=?, state=?, opened_at=?,
                    last_attempt_at=datetime('now'),
                    version=version+1
                WHERE provider_name=? AND version=?
            """, (new_count, new_state, opened_at, provider, row['version']))

            if affected == 0:
                raise ConcurrentUpdateError("CB state was modified concurrently")

        return CBState(state=new_state, failure_count=new_count)

    async def record_success(self, provider: str) -> None:
        async with db.begin_immediate():
            row = db.fetchone(
                "SELECT state, success_count, version "
                "FROM provider_circuit_breakers WHERE provider_name=?",
                (provider,)
            )
            if row['state'] == 'HALF_OPEN':
                new_count = row['success_count'] + 1
                if new_count >= self.HALF_OPEN_MAX_CALLS:
                    # 완전 복구
                    db.execute("""
                        UPDATE provider_circuit_breakers
                        SET state='CLOSED', failure_count=0, success_count=0,
                            opened_at=NULL, version=version+1
                        WHERE provider_name=? AND version=?
                    """, (provider, row['version']))
                    emit('CircuitBreakerClosed', provider)
                else:
                    db.execute("""
                        UPDATE provider_circuit_breakers
                        SET success_count=?, version=version+1
                        WHERE provider_name=? AND version=?
                    """, (new_count, provider, row['version']))

    async def check_state(self, provider: str) -> str:
        """OPEN 상태에서 recovery_timeout 경과 시 HALF_OPEN 전환"""
        row = db.fetchone(
            "SELECT state, opened_at FROM provider_circuit_breakers WHERE provider_name=?",
            (provider,)
        )
        if row['state'] == 'OPEN' and row['opened_at']:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(row['opened_at'])).total_seconds()
            if elapsed >= self.RECOVERY_TIMEOUT:
                async with db.begin_immediate():
                    db.execute("""
                        UPDATE provider_circuit_breakers SET state='HALF_OPEN', version=version+1
                        WHERE provider_name=? AND state='OPEN'
                    """, (provider,))
                emit('CircuitBreakerHalfOpen', provider)
                return 'HALF_OPEN'
        return row['state']
```

### ValidationGateway C1~C9

| 케이스 | 유형 | 트리거 | 처리 |
|--------|------|--------|------|
| C1 | STRUCTURAL | DAG 순환 감지 | CycleError → 즉시 거부 |
| C2 | STRUCTURAL | 위상 정렬 위반 | TopoViolationError → 거부 |
| C3 | SEMANTIC | 의존성 INVALID 상태 | 노드 BLOCKED |
| C4 | STRUCTURAL | 요구사항 대규모 변경 | ImpactAnalyzer 배치 실행 |
| C5 | STRUCTURAL | 헌법 버전 업그레이드 | ConflictDetector 실행 |
| C6 | SEMANTIC | 동시 수정 충돌 | GateConflictError(409) |
| C7 | SEMANTIC | 예산 초과 예상 | BudgetEnforcer L1 사전 차단 |
| C8 | STRUCTURAL | 헌법 활성화 | 단계적 전환 + 충돌 보고 |
| C9 | SEMANTIC | 수동 재실행 요청 | INVALID → READY 전환 (설계자 승인) |

---

## 9. API 명세 (99% ✅)

### 기본 규칙

```
Base URL: /api/v1/
인증: Bearer JWT (Authorization: Bearer {token})
에러 형식: {"error": {"code": "...", "message": "...", "details": {}}}
낙관적 잠금: 409 Conflict + {"current_version": N}
Rate Limit: 429 + Retry-After 헤더
```

### 주요 엔드포인트 그룹

**인게이지먼트 (22개)**
```
POST   /api/v1/engagements                     생성
GET    /api/v1/engagements                     목록
GET    /api/v1/engagements/{id}               상세
PATCH  /api/v1/engagements/{id}               수정
DELETE /api/v1/engagements/{id}               소프트 삭제
POST   /api/v1/engagements/{id}/pause         중지
POST   /api/v1/engagements/{id}/resume        재개
POST   /api/v1/engagements/{id}/force-close   강제 종료
GET    /api/v1/engagements/{id}/summary       현황 (v_engagement_summary)
GET    /api/v1/engagements/{id}/dag           Engagement DAG
GET    /api/v1/engagements/{id}/dag/gates     크로스 게이트 목록
POST   /api/v1/engagements/{id}/dag/gates/{edge_id}/approve
POST   /api/v1/engagements/{id}/dag/gates/{edge_id}/reject
GET    /api/v1/engagements/{id}/projects
POST   /api/v1/engagements/{id}/projects
DELETE /api/v1/engagements/{id}/projects/{id}
GET    /api/v1/engagements/{id}/members
POST   /api/v1/engagements/{id}/members
PATCH  /api/v1/engagements/{id}/members/{user_id}
DELETE /api/v1/engagements/{id}/members/{user_id}
GET    /api/v1/engagements/{id}/env-vars
POST   /api/v1/engagements/{id}/env-vars
DELETE /api/v1/engagements/{id}/env-vars/{key}
```

**노드**
```
GET    /api/v1/projects/{id}/nodes            목록 (state/phase 필터)
GET    /api/v1/projects/{id}/nodes/{node_id}  상세
POST   /api/v1/projects/{id}/nodes/{node_id}/start
POST   /api/v1/projects/{id}/nodes/{node_id}/retry
POST   /api/v1/projects/{id}/nodes/{node_id}/approve
POST   /api/v1/projects/{id}/nodes/{node_id}/suspend
POST   /api/v1/projects/{id}/nodes/{node_id}/resume
```

**산출물**
```
GET    /api/v1/projects/{id}/artifacts
GET    /api/v1/projects/{id}/artifacts/{artifact_id}
GET    /api/v1/projects/{id}/artifacts/{artifact_id}/download
GET    /api/v1/projects/{id}/artifacts/{artifact_id}/versions
GET    /api/v1/projects/{id}/artifacts/{artifact_id}/diff?from_version=2&to_version=3
```

**Provider Credentials**
```
GET    /api/v1/credentials          목록 (값 마스킹)
POST   /api/v1/credentials          등록 (API Key 또는 OAuth)
PATCH  /api/v1/credentials/{id}     수정 (is_active, is_default)
DELETE /api/v1/credentials/{id}     소프트 삭제
POST   /api/v1/credentials/{id}/test    연결 테스트
POST   /api/v1/credentials/{id}/refresh  OAuth 토큰 갱신
```

**리소스 관리**
```
GET    /api/v1/resources/global                전역 리소스 목록
POST   /api/v1/resources/global                전역 리소스 등록
PATCH  /api/v1/resources/global/{key}
DELETE /api/v1/resources/global/{key}
GET    /api/v1/projects/{id}/resources/resolved   해석 현황
GET    /api/v1/projects/{id}/resources/missing    미등록 필수 리소스
```

**Observability (v8 신규)**
```
GET    /metrics                     Prometheus 메트릭 엔드포인트
GET    /api/v1/health               시스템 상태 (DB 연결, CB 상태, 에이전트 수)
GET    /api/v1/alerts               현재 활성 알림 목록
```

### WebSocket

```
WS /ws/engagements/{id}/stream
→ Outbox의 destination='WEBSOCKET' 이벤트 구독
→ NodeStateChanged, GateTriggered, AgentProcessUpdated, BudgetWarning, CBStateChanged 등
```

---

## 9-F. 리소스 3계층 관리 (99% ✅)

### ResourceResolver

```python
# engine/resource_resolver.py
def resolve(key: str, project_id: str, engagement_id: str) -> str | None:
    """PROJECT → ENGAGEMENT → GLOBAL 순서로 조회"""
    val = _fetch('PROJECT', project_id, key)
    if val: return val
    val = _fetch('ENGAGEMENT', engagement_id, key)
    if val: return val
    row = db.queryrow("""
        SELECT value_encrypted FROM project_env_vars
        WHERE scope='GLOBAL' AND scope_id='GLOBAL' AND key=?
    """, [key])
    return AES256GCM.decrypt(row['value_encrypted'], ...) if row else None
```

### 노드 타입별 필수 리소스

```python
NODE_REQUIRED_KEYS: dict[str, list[str]] = {
    'INFRASTRUCTURE': ['GITHUB_TOKEN', 'DEPLOYMENT_TARGET'],
    'DEVELOPMENT':    ['GITHUB_TOKEN'],
    'API_SERVER':     [],
    'PLANNING':       [],
    'DESIGN':         [],
}
```

---

## 10. 인테이크 연동 브릿지 (99% ✅)

### Phase 레지스트리

```python
SCOPE_TO_PHASE: dict[str, str] = {
    '기획':           'PLANNING',
    'UI·UX 디자인':   'DESIGN',
    '프론트엔드 개발': 'DEVELOPMENT',
    '백엔드 개발':     'DEVELOPMENT',
    '인프라·배포':     'INFRASTRUCTURE',
    'QA·테스트':      None,
    '운영·유지보수':   None,
    '컨설팅·자문':     None,
}
ALWAYS_ACTIVE_PHASES = ['DELIVERY']
API_SERVER_TRIGGERS = ['API/플랫폼', '백오피스/어드민']
```

### NODE_TEMPLATES 레지스트리

features × scope → 노드 목록. 각 TASK 노드는 Rule 9에 의해 QA 노드와 자동 쌍 생성.

```python
NODE_TEMPLATES: list[dict] = [
    # ── API_SERVER ──────────────────────────────────
    {"trigger": {"projectTypes": ["API/플랫폼", "백오피스/어드민"]}, "phase": "API_SERVER",
     "nodes": [
        {"name": "기술 아키텍처 설계", "model": "opus"},
        {"name": "REST API 명세서 초안", "model": "sonnet"},
        {"name": "DB 스키마 설계", "model": "opus"},
        {"name": "인증 구조 설계", "model": "sonnet"},
     ]},
    # ── PLANNING ────────────────────────────────────
    {"trigger": {"scope": ["기획"]}, "phase": "PLANNING",
     "nodes": [
        {"name": "요구사항 분석서 (PRD)", "model": "opus"},
        {"name": "정보 구조도 (IA)", "model": "sonnet"},
        {"name": "사용자 플로우차트", "model": "sonnet"},
        {"name": "기능 명세서", "model": "sonnet"},
        {"name": "와이어프레임 명세", "model": "sonnet"},
     ]},
    # ── DESIGN ──────────────────────────────────────
    {"trigger": {"scope": ["UI·UX 디자인"]}, "phase": "DESIGN",
     "nodes": [
        {"name": "디자인 시스템 정의", "model": "sonnet"},
        {"name": "핵심 화면 설계", "model": "sonnet"},
        {"name": "프로토타입 연결", "model": "sonnet"},
     ]},
    # ── DEVELOPMENT ─────────────────────────────────
    {"trigger": {"scope": ["프론트엔드 개발"]}, "phase": "DEVELOPMENT",
     "nodes": [
        {"name": "프로젝트 초기 세팅 (보일러플레이트)", "model": "sonnet"},
        {"name": "공통 컴포넌트 개발", "model": "sonnet"},
        {"name": "라우팅/네비게이션 구현", "model": "sonnet"},
     ]},
    {"trigger": {"scope": ["백엔드 개발"]}, "phase": "DEVELOPMENT",
     "nodes": [
        {"name": "프로젝트 초기 세팅 (서버)", "model": "sonnet"},
        {"name": "DB 마이그레이션 스크립트", "model": "sonnet"},
        {"name": "공통 미들웨어/유틸 개발", "model": "sonnet"},
     ]},
    # ── INFRASTRUCTURE ──────────────────────────────
    {"trigger": {"scope": ["인프라·배포"]}, "phase": "INFRASTRUCTURE",
     "nodes": [
        {"name": "서버 환경 구성", "model": "sonnet"},
        {"name": "CI/CD 파이프라인 구축", "model": "sonnet"},
        {"name": "도메인/SSL 설정", "model": "sonnet"},
        {"name": "모니터링/알림 설정", "model": "sonnet"},
        {"name": "배포 스크립트 작성", "model": "sonnet"},
     ]},
    # ── DELIVERY (항상 생성) ─────────────────────────
    {"trigger": {"always": True}, "phase": "DELIVERY",
     "nodes": [
        {"name": "최종 산출물 패키징", "model": "sonnet"},
        {"name": "README 및 운영 가이드 작성", "model": "sonnet"},
        {"name": "클라이언트 인수인계 문서", "model": "sonnet"},
     ]},
    # ── Features ────────────────────────────────────
    {"trigger": {"features": ["회원가입·로그인"]}, "phase": "DEVELOPMENT",
     "nodes": [
        {"name": "회원가입/로그인 API 개발", "model": "sonnet"},
        {"name": "마이페이지 기능 개발", "model": "sonnet"},
     ]},
    {"trigger": {"features": ["결제"]}, "phase": "DEVELOPMENT",
     "nodes": [
        {"name": "결제 모듈 개발", "model": "sonnet"},
        {"name": "PG사 연동 (토스/카카오페이/기타)", "model": "sonnet"},
        {"name": "정산/환불 로직 개발", "model": "sonnet"},
     ]},
    {"trigger": {"features": ["채팅"]}, "phase": "DEVELOPMENT",
     "nodes": [{"name": "WebSocket 실시간 채팅 구현", "model": "sonnet"}]},
    {"trigger": {"features": ["채팅"]}, "phase": "INFRASTRUCTURE",
     "nodes": [{"name": "Redis 세션 서버 구성", "model": "sonnet"}]},
    {"trigger": {"features": ["푸시알림"]}, "phase": "DEVELOPMENT",
     "nodes": [{"name": "FCM/APNs 푸시알림 연동", "model": "sonnet"}]},
    {"trigger": {"features": ["관리자 대시보드"]}, "phase": "DEVELOPMENT",
     "nodes": [{"name": "어드민 대시보드 개발", "model": "sonnet"}]},
    {"trigger": {"features": ["통계·리포트"]}, "phase": "DEVELOPMENT",
     "nodes": [
        {"name": "통계/리포트 API 개발", "model": "sonnet"},
        {"name": "차트/시각화 컴포넌트 개발", "model": "sonnet"},
     ]},
    {"trigger": {"features": ["본인인증"]}, "phase": "DEVELOPMENT",
     "nodes": [{"name": "본인인증 모듈 개발 (PASS/통신사)", "model": "sonnet"}]},
    {"trigger": {"features": ["외부API"]}, "phase": "DEVELOPMENT",
     "nodes": [{"name": "외부 API 연동 개발", "model": "sonnet"}]},
]
```

### Phase 간 의존성

```python
PHASE_ORDER = ['API_SERVER', 'PLANNING', 'DESIGN', 'DEVELOPMENT', 'INFRASTRUCTURE', 'DELIVERY']

INTRA_PHASE_DEPS = [
    ("요구사항 분석서 (PRD)", "기능 명세서"),
    ("기능 명세서", "와이어프레임 명세"),
    ("기술 아키텍처 설계", "REST API 명세서 초안"),
    ("기술 아키텍처 설계", "DB 스키마 설계"),
    ("프로젝트 초기 세팅 (서버)", "DB 마이그레이션 스크립트"),
    ("프로젝트 초기 세팅 (보일러플레이트)", "공통 컴포넌트 개발"),
    ("최종 산출물 패키징", "README 및 운영 가이드 작성"),
    ("README 및 운영 가이드 작성", "클라이언트 인수인계 문서"),
]
```

---

## 12. 운영 인프라 (99% ✅)

### 12-1. Graceful Shutdown (v8 개선 — ShutdownManager)

```python
# engine/lifecycle/shutdown.py

import asyncio
import signal
from contextlib import asynccontextmanager

class ShutdownManager:
    """
    SIGTERM → 5단계 종료 절차
    Stage 1: 신규 작업 차단
    Stage 2: 실행 중 에이전트 대기 (최대 25초)
    Stage 3: 타임아웃 초과 에이전트 → SUSPENDED
    Stage 4: DatabaseWriterActor drain (최대 5초)
    Stage 5: DB 정리 + SystemShutdownCompleted 이벤트
    """
    AGENT_TIMEOUT = 25.0  # 초 (v8 확정: 에이전트 대기)
    DRAIN_TIMEOUT = 5.0   # 초 (v8 확정: DB drain)

    def __init__(self, dag_advancer, writer_actor):
        self._dag_advancer = dag_advancer
        self._writer_actor = writer_actor
        self._is_shutting_down = False
        self._active_agents: dict[str, asyncio.Task] = {}

    def setup_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

    async def shutdown(self) -> None:
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        logger = structlog.get_logger()
        logger.info("shutdown.initiated")

        # Stage 1: 신규 작업 차단
        self._dag_advancer.stop_accepting()
        logger.info("shutdown.new_tasks_blocked")

        # Stage 2~3: 에이전트 대기 + 타임아웃 처리
        completed, suspended = await self._wait_for_agents(self.AGENT_TIMEOUT)
        if suspended:
            await self._suspend_agents(suspended, reason='SHUTDOWN_DRAIN')
        logger.info("shutdown.agents_handled",
                    completed=len(completed), suspended=len(suspended))

        # Stage 4: DB drain
        await self._drain_writer_actor(self.DRAIN_TIMEOUT)
        logger.info("shutdown.db_drained")

        # Stage 5: DB 정리
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        emit('SystemShutdownCompleted', {'suspended_count': len(suspended)})
        logger.info("shutdown.complete")

    async def _wait_for_agents(self, timeout: float) -> tuple[list, list]:
        if not self._active_agents:
            return [], []
        try:
            done, pending = await asyncio.wait(
                self._active_agents.values(),
                timeout=timeout
            )
            return list(done), list(pending)
        except Exception:
            return [], list(self._active_agents.values())

    async def _suspend_agents(self, tasks: list, reason: str) -> None:
        for task in tasks:
            task.cancel()
        node_ids = [self._get_node_id(t) for t in tasks]
        for node_id in node_ids:
            db.execute(
                "UPDATE nodes SET state='SUSPENDED', suspension_reason=? WHERE id=?",
                (reason, node_id)
            )
            emit('NodeSuspendedByShutdown', {'node_id': node_id, 'reason': reason})

    async def _drain_writer_actor(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._writer_actor.drain(), timeout=timeout)
        except asyncio.TimeoutError:
            structlog.get_logger().warning("drain_timeout", timeout=timeout)
```

### 12-2. Startup Recovery (v5 신규)

```python
# engine/lifecycle/startup.py

async def startup_recovery() -> None:
    """
    재시작 후 고아 노드 정리.
    IN_PROGRESS 고아 노드 → SUSPENDED (재시작 후 재개 가능)
    suspension_reason='SHUTDOWN_DRAIN' → READY (정상 재개)
    """
    # 좀비 IN_PROGRESS 노드 → SUSPENDED
    orphan_nodes = db.fetchall("""
        SELECT id FROM nodes
        WHERE state = 'IN_PROGRESS'
          AND (last_heartbeat IS NULL
               OR julianday('now') - julianday(last_heartbeat) > 5.0 / 1440.0)
    """)
    for row in orphan_nodes:
        db.execute("""
            UPDATE nodes SET state='SUSPENDED', suspension_reason='SHUTDOWN_DRAIN'
            WHERE id=?
        """, (row['id'],))
        emit('NodeSuspendedByShutdown', {'node_id': row['id']})

    # SHUTDOWN_DRAIN 노드 → READY (정상 재개)
    shutdown_nodes = db.fetchall("""
        SELECT id FROM nodes WHERE suspension_reason='SHUTDOWN_DRAIN'
    """)
    for row in shutdown_nodes:
        db.execute("""
            UPDATE nodes SET state='READY', suspension_reason=NULL WHERE id=?
        """, (row['id'],))
        emit('NodeStateChanged', {'node_id': row['id'], 'from': 'SUSPENDED', 'to': 'READY'})
```

### 12-3. SQLite 백업 전략

```
일일 자동 백업 (매일 03:00, asyncio.sleep 루프):
1. WAL 체크포인트: PRAGMA wal_checkpoint(TRUNCATE)
2. VACUUM INTO 'backups/platform_YYYYMMDD_HHMMSS.db'
3. PRAGMA integrity_check → 'ok' 아니면 SystemBackupFailed + 알림
4. SHA-256(백업 파일) 체크섬 기록
5. tar -czf backups/artifacts_YYYYMMDD.tar.gz ./artifacts/
6. 30일 초과 백업 자동 삭제
```

### 12-4. 마이그레이션 프레임워크

```
파일 구조: engine/migrations/versions/
  001_initial.sql ~ 013_agent_token_usage.sql

파일 형식:
  -- UP:
  CREATE TABLE ...
  -- DOWN:
  DROP TABLE ...

실행: MigrationRunner.run_pending()
  → schema_migrations 테이블에서 적용 완료 목록 조회
  → 미적용 마이그레이션 순차 실행
  → SHA-256 체크섬 기록
  → 실패 시 즉시 롤백
```

---

## 16. Observability 스택 (v8 신규 — 99% ✅)

### 3기둥: 로그 + 메트릭 + 알림

#### structlog (JSON 구조화 로깅)

```python
# engine/observability/logger.py

import structlog

def configure_logging(env: str = "production") -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

# 사용 예시:
# logger = structlog.get_logger()
# logger.info("node.execution.started",
#             node_id=node.id, project_id=project.id,
#             correlation_id=ctx_var.get())
```

#### prometheus_client (메트릭)

```python
# engine/observability/metrics.py

from prometheus_client import Gauge, Counter, Histogram, start_http_server

# 활성 에이전트 수 (Phase별)
ACTIVE_AGENTS = Gauge(
    "platform_active_agents_total",
    "현재 실행 중인 에이전트 수",
    labelnames=["phase"]
)

# Circuit Breaker 상태 (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
CB_STATE = Gauge(
    "platform_circuit_breaker_state",
    "Provider Circuit Breaker 상태",
    labelnames=["provider"]
)

# DB 쓰기 지연
DB_WRITE_LATENCY = Histogram(
    "platform_db_write_latency_seconds",
    "DatabaseWriterActor 쓰기 지연",
    buckets=[0.001, 0.01, 0.05, 0.1, 0.5, 1.0]
)

# API 호출 카운터
API_CALLS = Counter(
    "platform_model_api_calls_total",
    "AI Provider API 호출 횟수",
    labelnames=["provider", "model", "status"]  # status: success|failure|rate_limited
)

# 토큰 사용량 히스토그램
TOKEN_USAGE = Histogram(
    "platform_token_usage_per_call",
    "API 호출당 토큰 사용량",
    labelnames=["provider", "type"],  # type: input|output
    buckets=[100, 500, 1000, 5000, 10000, 50000, 100000]
)

# Outbox 큐 깊이
OUTBOX_QUEUE_DEPTH = Gauge("platform_outbox_queue_depth", "미처리 Outbox 메시지 수")

def start_metrics_server(port: int = 8001) -> None:
    """FastAPI /metrics 엔드포인트 대신 독립 포트 사용 (선택적)"""
    start_http_server(port)
```

#### AlertRules (임계값 알림)

```python
# engine/observability/alert_rules.py

ALERT_RULES = [
    AlertRule(
        name="HighActiveAgents",
        condition=lambda: ACTIVE_AGENTS._value.sum() > 12,
        severity="WARNING",
        message="활성 에이전트 12개 초과 — Rate Limit 위험",
        cooldown=300  # 5분
    ),
    AlertRule(
        name="CircuitBreakerOpen",
        condition=lambda provider: CB_STATE.labels(provider=provider)._value.get() == 2,
        severity="CRITICAL",
        message="{provider} Circuit Breaker OPEN — 에이전트 실행 불가",
        cooldown=60
    ),
    AlertRule(
        name="DBWriteLatencyHigh",
        condition=lambda: DB_WRITE_LATENCY.observe(0) and False,  # P95 > 100ms
        severity="HIGH",
        message="DB 쓰기 P95 지연 100ms 초과",
        cooldown=120
    ),
    AlertRule(
        name="OutboxBacklog",
        condition=lambda: OUTBOX_QUEUE_DEPTH._value.get() > 100,
        severity="WARNING",
        message="Outbox 미처리 메시지 100개 초과",
        cooldown=120
    ),
]

class AlertManager:
    async def check_and_fire(self) -> None:
        """asyncio.sleep(30) 루프로 주기적 체크"""
        for rule in ALERT_RULES:
            if rule.should_fire():
                await self._send_alert(rule)
                rule.record_fired()
```

---

## 17. BudgetEnforcer (v8 신규 — 99% ✅)

### 3단계 예산 집행

```python
# engine/budget/enforcer.py

class BudgetEnforcer:
    """
    L1: Pre-call 추정 (CJK 비율 기반 토큰 추정)
    L2: API max_tokens 하드 제한 (Claude API 파라미터)
    L3: Phase 누적 추적 (agent_token_usage 테이블)
    """
    ESTIMATION_TOLERANCE = 1.15  # 추정치의 15% 여유

    async def call_with_budget(
        self, node: Node, prompt: str, model_client
    ) -> APIResponse:
        budget = self._get_budget(node)

        # L1: Pre-call 추정
        estimated = self._estimate_tokens(prompt)
        if estimated > budget.max_input * self.ESTIMATION_TOLERANCE:
            await self._suspend_node_for_budget(node, estimated, budget)
            raise InputBudgetExceededError(
                f"추정 입력 토큰 {estimated} > 허용 {budget.max_input}"
            )

        # L2: max_tokens API 하드 제한 주입
        response = await model_client.call(
            prompt=prompt,
            max_tokens=budget.max_output  # API 레벨에서 강제 차단
        )

        # L3: Phase 누적 추적
        await self._record_usage(node, response)
        phase_total = await self._get_phase_total(node.engagement_id, node.phase)
        phase_limit = TOKEN_BUDGET['phase_limit'][node.phase]
        if phase_total > phase_limit * 0.9:
            await self._emit_budget_warning(node, phase_total, phase_limit)
        if phase_total > phase_limit:
            await self._suspend_node_for_budget(node, phase_total, budget)
            raise PhaseBudgetExceededError(
                f"{node.phase} Phase 예산 초과: {phase_total}/{phase_limit}"
            )

        return response

    def _estimate_tokens(self, text: str) -> int:
        """
        CJK 문자는 1자 = 약 1.5~2 토큰으로 추정.
        ASCII는 1자 = 약 0.25 토큰으로 추정.
        """
        cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        ascii_count = len(text) - cjk_count
        return int(cjk_count * 1.7 + ascii_count * 0.25)

    def _get_budget(self, node: Node) -> BudgetSpec:
        """
        우선순위: node_budget_overrides > TOKEN_BUDGET (불변 전역 설정)
        """
        override = db.fetchone(
            "SELECT * FROM node_budget_overrides WHERE node_id=?", (node.id,)
        )
        if override:
            return BudgetSpec(
                max_input=override['max_input_override'] or TOKEN_BUDGET['max_input'],
                max_output=override['max_output_override'] or TOKEN_BUDGET['max_output'],
            )
        return BudgetSpec(
            max_input=TOKEN_BUDGET['max_input'],
            max_output=TOKEN_BUDGET['max_output'],
        )

    async def _record_usage(self, node: Node, response: APIResponse) -> None:
        db.execute("""
            INSERT INTO agent_token_usage
            (node_id, agent_run_id, engagement_id, phase, model_name,
             input_tokens, output_tokens, estimated_input)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (node.id, response.run_id, node.engagement_id, node.phase,
              response.model, response.input_tokens, response.output_tokens,
              self._last_estimated))

    async def _get_phase_total(self, engagement_id: str, phase: str) -> int:
        row = db.fetchone("""
            SELECT total_tokens FROM v_phase_token_summary
            WHERE engagement_id=? AND phase=?
        """, (engagement_id, phase))
        return row['total_tokens'] if row else 0
```

---

## 18. GateNotificationService (v8 신규 — 99% ✅)

### Outbox 재사용 + 지수 백오프

```python
# engine/notifications.py
# 핵심: 별도 notification_deliveries 테이블 없음.
# 기존 outbox 테이블 재사용 (destination='NOTIFICATION')

class GateNotificationService:
    async def notify_gate_triggered(self, gate_node_id: str) -> None:
        node = db.get_node(gate_node_id)
        subs = db.fetchall("""
            SELECT * FROM notification_subscriptions
            WHERE engagement_id=? AND event_type='GateTriggered' AND is_active=1
        """, (node.engagement_id,))

        for sub in subs:
            # Outbox에 INSERT → DB 트랜잭션과 원자적
            event_id = emit('GateTriggered', {'gate_node_id': gate_node_id})
            db.execute("""
                INSERT INTO outbox (id, event_id, event_type, payload, destination,
                                    status, max_retries, next_retry_at)
                VALUES (?, ?, 'GateTriggered', ?, 'NOTIFICATION', 'PENDING', 5, datetime('now'))
            """, (new_uuid(), event_id,
                  json.dumps({'subscription_id': sub['id'], 'node_id': gate_node_id})))

class NotificationWorker:
    """asyncio.sleep 루프로 Outbox 폴링 + 지수 백오프 재시도"""
    RETRY_DELAYS = [30, 60, 120, 300, 600]  # 초 (5단계)

    async def run(self) -> None:
        while True:
            pending = db.fetchall("""
                SELECT o.*, ns.channel_type, ns.encrypted_endpoint, ns.encrypted_recipient
                FROM outbox o
                LEFT JOIN notification_subscriptions ns
                  ON JSON_EXTRACT(o.payload, '$.subscription_id') = ns.id
                WHERE o.destination='NOTIFICATION'
                  AND o.status IN ('PENDING', 'FAILED')
                  AND (o.next_retry_at IS NULL OR o.next_retry_at <= datetime('now'))
                LIMIT 10
            """)
            for msg in pending:
                await self._deliver(msg)
            await asyncio.sleep(5)

    async def _deliver(self, msg: dict) -> None:
        try:
            if msg['channel_type'] == 'WEBHOOK':
                await self._send_webhook(msg)
            elif msg['channel_type'] == 'EMAIL':
                await self._send_email(msg)
            # LOG_ONLY는 이미 structlog로 기록됨

            db.execute("""
                UPDATE outbox SET status='DELIVERED', processed_at=datetime('now')
                WHERE id=?
            """, (msg['id'],))
        except Exception as e:
            retry_count = msg['retry_count'] + 1
            if retry_count >= msg['max_retries']:
                db.execute(
                    "UPDATE outbox SET status='DEAD_LETTERED', error_message=? WHERE id=?",
                    (str(e), msg['id'])
                )
                emit('OutboxDeadLettered', {'outbox_id': msg['id']})
            else:
                delay = self.RETRY_DELAYS[min(retry_count - 1, len(self.RETRY_DELAYS) - 1)]
                db.execute("""
                    UPDATE outbox SET status='FAILED', retry_count=?, next_retry_at=?,
                                      error_message=?
                    WHERE id=?
                """, (retry_count,
                      (datetime.utcnow() + timedelta(seconds=delay)).isoformat(),
                      str(e), msg['id']))
```

---

## 19. 테스트 전략 (v8 신규 — 99% ✅)

### 테스트 계층

```
단위 테스트 (pytest):
  - 상태 머신 전이 (Hypothesis property-based)
  - ValidationGateway C1~C9
  - ContextAssembler 레이어 조립
  - BudgetEnforcer L1/L2/L3
  - CodeVerificationSandbox Stage 1/2
  - ResourceResolver fallback 체인

통합 테스트 (pytest + SQLite in-memory):
  - DAGAdvancer 직렬화 검증
  - Circuit Breaker 상태 전이 (DB 기반)
  - Outbox → NotificationWorker 전달
  - Cascade Invalidation 2-Phase

부하 테스트 (독립 스크립트):
  tools/benchmark_db.py  — pytest 아님 (오버헤드 없는 순수 DB 성능 측정)
```

### Hypothesis 상태 전이 테스트

```python
# tests/test_state_machine.py

from hypothesis import given, settings, strategies as st
from engine.state_machine import VALID_TRANSITIONS, StateMachine, InvalidTransitionError
import pytest

ALL_STATES = list(VALID_TRANSITIONS.keys())

@given(
    from_state=st.sampled_from(ALL_STATES),
    to_state=st.sampled_from(ALL_STATES)
)
@settings(max_examples=500)
def test_state_transitions_are_exhaustive(from_state, to_state):
    """모든 VALID_TRANSITIONS 경로 커버 검증 (경로 커버리지 우선)"""
    if to_state in VALID_TRANSITIONS[from_state]:
        # 허용 전이 → 반드시 성공
        node = make_mock_node(state=from_state)
        StateMachine.transition(node, to_state)
        assert node.state == to_state
    else:
        # 금지 전이 → 반드시 InvalidTransitionError
        node = make_mock_node(state=from_state)
        with pytest.raises(InvalidTransitionError):
            StateMachine.transition(node, to_state)

@given(st.lists(st.sampled_from(ALL_STATES), min_size=2, max_size=10))
def test_state_transition_chain_consistency(state_chain):
    """임의의 상태 전이 시퀀스에서 일관성 검증"""
    node = make_mock_node(state=state_chain[0])
    for target in state_chain[1:]:
        if target in VALID_TRANSITIONS.get(node.state, frozenset()):
            StateMachine.transition(node, target)
        # 금지 전이는 조용히 스킵 (체인 중단 없음)
```

### DB 벤치마크 스크립트

```python
# tools/benchmark_db.py
# pytest 없이 독립 실행 — 순수 SQLite 성능 측정

import time, sqlite3

def benchmark_concurrent_reads(db_path: str, iterations: int = 1000):
    conn = sqlite3.connect(db_path)
    start = time.perf_counter()
    for _ in range(iterations):
        conn.execute("SELECT COUNT(*) FROM nodes WHERE state='READY'").fetchone()
    elapsed = time.perf_counter() - start
    print(f"읽기 {iterations}회: {elapsed:.3f}초, {iterations/elapsed:.0f} QPS")

def benchmark_write_transaction(db_path: str, iterations: int = 100):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    start = time.perf_counter()
    for i in range(iterations):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE nodes SET version=version+1 WHERE id=?", (f"node_{i}",))
        conn.execute("COMMIT")
    elapsed = time.perf_counter() - start
    print(f"쓰기 {iterations}회: {elapsed:.3f}초, {iterations/elapsed:.0f} TPS")

if __name__ == '__main__':
    benchmark_concurrent_reads("platform.db")
    benchmark_write_transaction("platform.db")
```

---

## 20. SleepGuard (v8 신규 — 크로스 플랫폼)

```python
# engine/platform/sleep_guard.py
# v8 수정: subprocess + caffeinate → 크로스 플랫폼 대응

import platform
import subprocess
import asyncio

class SleepGuard:
    """
    macOS:   caffeinate -i (내장)
    Linux:   systemd-inhibit (선택적)
    Windows: SetThreadExecutionState Win32 API
    """

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._os = platform.system()

    async def __aenter__(self):
        if self._os == 'Darwin':
            self._process = subprocess.Popen(['caffeinate', '-i'])
        elif self._os == 'Linux':
            try:
                self._process = subprocess.Popen([
                    'systemd-inhibit', '--what=idle', '--who=AI-SI-Platform',
                    '--why=engine-running', '--mode=block', 'sleep', 'infinity'
                ])
            except FileNotFoundError:
                pass  # systemd-inhibit 없으면 스킵
        elif self._os == 'Windows':
            self._win_prevent_sleep()
        return self

    async def __aexit__(self, *args):
        if self._process:
            self._process.terminate()
        if self._os == 'Windows':
            self._win_allow_sleep()

    def _win_prevent_sleep(self) -> None:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    def _win_allow_sleep(self) -> None:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
```

---

## 비용 구조 (확정)

| 항목 | 비용 여부 | 비고 |
|------|-----------|------|
| Claude API | **유료** | Opus $75/MTok output, Sonnet $15/MTok output |
| 서버/인프라 | 무료 | 로컬 Mac 구동 |
| DB (SQLite) | 무료 | Python 내장 |
| 웹서버 | 무료 | fastapi + uvicorn |
| 프론트엔드 | 무료 | HTMX 14KB |
| cryptography | 무료 | 오픈소스 |
| structlog | 무료 | 오픈소스 |
| prometheus_client | 무료 | 오픈소스 |
| 모든 기타 | 무료 | |

**비용 절감 설계:**
- Delta 우선: 변경분만 AI 전달 → 입력 토큰 대폭 절감
- Sonnet/Opus 역할 분리: 단순 작업 Sonnet, 판단 필요 시만 Opus
- BudgetEnforcer L1/L2/L3 3단계 예산 집행
- cost_tracking + budget_limits + agent_token_usage 실시간 모니터링
- 프로젝트 토큰 예산 상한: 70%/90%/100% 3단계 경고

---

## 코딩 시작 권장 순서 (v8 최종)

```
설계 완료. DESIGN_SPEC_v8.md + schema_v8.sql 기반으로 코딩을 시작하라.

현재 완성도 (v8 최종):
 1  시스템 비전              99% ✅
 2  기술 스택                99% ✅ pip 7개 확정
 3  핵심 알고리즘             99% ✅ 10-state, DAGAdvancer, BudgetEnforcer
 3-E Engagement Layer         99% ✅
 4  도메인 이벤트             99% ✅ 455+ 개
 5  DB 스키마                99% ✅ schema_v8.sql (38개 테이블, migration 001~013)
 6  RBAC/보안                99% ✅ PathGuard + CodeVerificationSandbox
 7  에이전트 프롬프트 구조     99% ✅ 9대 규칙 + Model Adapter
 8  에러 복구 플로우           99% ✅ A~H + CB BEGIN IMMEDIATE + ShutdownManager
 9  API 명세                 99% ✅
 9-F 리소스 3계층 관리        99% ✅
10  인테이크 연동 브릿지       99% ✅
11  Dashboard 통합 매핑       99% ✅
12  운영 인프라               99% ✅ Shutdown/백업/마이그레이션/로깅/예산
16  Observability 스택        99% ✅ structlog + prometheus_client + AlertRules
17  BudgetEnforcer            99% ✅ L1/L2/L3
18  CodeVerificationSandbox   99% ✅ 3단계 (AST→Static→Docker)
19  DatabaseAdapter           99% ✅ SQLite/PostgreSQL 추상화
20  GateNotificationService   99% ✅ Outbox 재사용 + 지수 백오프

코딩 시작 권장 순서:
 1. engine/db/adapter.py          — DatabaseAdapter + SQLiteAdapter
 2. engine/crypto.py              — AES256GCM (cryptography 패키지)
 3. engine/state_machine.py       — 10-state VALID_TRANSITIONS + InvalidTransitionError
 4. engine/dag_advancer.py        — asyncio.Queue 직렬 advance
 5. engine/dag.py                 — DAG 상태 머신
 6. engine/auth/                  — CredentialProvider + OAuth 구현체
 7. engine/api_client.py          — Model Adapter + CredentialProvider 통합
 8. engine/budget/enforcer.py     — BudgetEnforcer L1/L2/L3
 9. engine/context.py             — ContextAssembler 5-Layer + 선택적 트림
10. engine/executor.py            — execute_node() + BudgetEnforcer 주입
11. engine/storage/path_guard.py  — PathGuard 경로 보안
12. engine/verification/sandbox.py — CodeVerificationSandbox 3단계
13. engine/circuit_breaker.py     — BEGIN IMMEDIATE + DB 기반 CB 상태
14. engine/validators/            — ValidationGateway C1~C9 + ImpactAnalyzer 배치
15. engine/recovery.py            — 에러 A~H + Cascade 2-Phase
16. engine/notifications.py       — GateNotificationService + NotificationWorker
17. engine/observability/         — logger.py + metrics.py + alert_rules.py
18. engine/lifecycle/shutdown.py  — ShutdownManager (SIGTERM→25s→5s)
19. engine/lifecycle/startup.py   — startup_recovery
20. engine/platform/sleep_guard.py — SleepGuard (크로스 플랫폼)
21. engine/backup.py              — SQLite 백업 + asyncio 스케줄러
22. engine/migrations/            — MigrationRunner
23. api/main.py                   — FastAPI 라우터 (/api/v1/ 통일) + /metrics
24. frontend/                     — HTMX 대시보드

핵심 확정 사항 (변경 불가):
- 유일한 유료 서비스: Claude API
- pip 의존성: fastapi uvicorn aiohttp PyJWT cryptography structlog prometheus_client (7개)
- 인증: API Key + OAuth 3사 (Anthropic, OpenAI, Google)
- 운용: Mac mini m4pro, RAM 64GB+, 24/7
- 역할: ADMIN / SENIOR_DESIGNER / DESIGNER
- DB: SQLite (workers=1) → DatabaseAdapter → PostgreSQL 전환 가능 (pgloader 사용)
- 노드 상태: 10개 (SUSPENDED, AWAITING_APPROVAL 포함)
- 동시 프로젝트: 최대 5개 (인게이지먼트 단위)
- Phase: API_SERVER > PLANNING > DESIGN > DEVELOPMENT > INFRASTRUCTURE > DELIVERY
- 인게이지먼트 계층: Engagement > Master Project + N Subprojects
- TOKEN_BUDGET: MappingProxyType (불변)
- Circuit Breaker: BEGIN IMMEDIATE (SELECT FOR UPDATE SQLite 미지원)
- 코드 검증: AST→Static→Docker (subprocess+ulimit 아님)
- 알림: Outbox 재사용 + 지수 백오프 (30/60/120/300/600초)
- 테스트: Hypothesis property-based + tools/benchmark_db.py
- SQLite workers: 반드시 1 (멀티 프로세스 = PostgreSQL 사용)
```

---

*v1: DESIGN_SPEC.md (원본 보존)*
*v2: 인테이크 연동 브릿지 + Phase 재정의 + API 명세*
*v3: Engagement Layer + 크로스 프로젝트 DAG + Dashboard 10-stage 통합*
*v4: Model Adapter + claude_init.md 9대 규칙 + 에러 A~H + Circuit Breaker + API 완성 + 리소스 3계층 + QA 뱃지*
*v5: 10-state / DAGAdvancer 직렬화 / ImpactAnalyzer 배치화 / OAuth 3사 / CB 영속화 / Cascade 2-Phase / PathGuard / Graceful Shutdown / SQLite 백업 / 구조화 로깅 / 토큰 예산*
*v8: TOKEN_BUDGET 불변 / CB BEGIN IMMEDIATE (SELECT FOR UPDATE 버그 수정) / CodeVerificationSandbox 3단계 / ShutdownManager 25s/5s / Outbox 재사용 알림 / DatabaseAdapter 추상화 / Observability 3기둥 / BudgetEnforcer L1/L2/L3 / Hypothesis 테스트 / pip 7개 확정*
*설계 완료. 코딩 즉시 착수 가능.*
