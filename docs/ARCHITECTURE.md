# V8 엔진 아키텍처

## 개요

V8 은 5단계 SI 자동화 엔진. 각 단계의 산출물을 LLM 으로 생성·검증·재시도까지
DAG 로 자동 관리.

```
DEFINE → DESIGN → BUILD → VERIFY → DELIVER
   ↓        ↓        ↓        ↓        ↓
요구사항  화면설계  코드     테스트   배포
백로그    DB설계   페이지   E2E      산출물
유스케이스 API설계  레시피
```

## 코어 모듈 (변경 금지)

| 파일 | 역할 |
|---|---|
| `engine/core/dag_advancer.py` | DAG 노드 상태 진행 엔진 |
| `engine/core/state_machine.py` | 상태 전이 테이블 (READY → IN_PROGRESS → COMPLETED 등) |
| `engine/ai/context_assembler.py` | 5-Layer 컨텍스트 조립 (system/spec/upstream/project/recipe) |
| `engine/core/budget_enforcer.py` | 노드 단위 토큰 예산 |
| `engine/core/cascade.py` | 변경 전파 (upstream 변경 → downstream invalidate) |

코어 외 모든 모듈은 자유 수정 가능. 코어 변경 필요한 경우 별도 레이어 추가로
우회.

## 주요 모듈 흐름

```
[1] DAGAdvancer
       │
       │ 노드 READY → 호출
       ▼
[2] executor.py (engine/skills/)
       │
       │ 5-Layer context 조립
       ▼
[3] ContextAssembler (코어)
       │
       │ system + spec + upstream + project + recipe
       ▼
[4] ModelAdapter.call (engine/ai/model_adapter.py)
       │
       │ Anthropic API 호출 (prompt caching 활성)
       ▼
[5] APIResponse → executor 가 산출물 저장
       │
       │ artifact_versions INSERT
       ▼
[6] QA 노드 트리거 → harness.py + AI QA
       │
       │ PASS → cascade phase2 → 다음 노드 READY
       │ FAIL → INVALID + 재시도 또는 SUSPENDED
```

## F1~F4: 산출물 생성 안정화

- **F1** Truncation 자동 확장 (`max_tokens` 도달 → 1.5× 재호출)
- **F2** Transient retry (네트워크 일시 오류 → backoff 30s/60s/120s)
- **F3** JSON 산출물 max_tokens override (32K)
- **F4** Section-split chunked document — `spec.sections` 정의 시 섹션별 분할
  생성 + outline-first ID 확정 + min_items 자동 재생성

## G1~G4: 금지어·placeholder 방어

- **G1** Korean-aware boundary 정규식
- **G2** Forbidden retry — 감지 시 1회 재시도
- **G3** Auto-fix framework (`harness_auto_fix.py`)
  - forbidden_words → 한국어 대체어 (조사 매칭)
  - lorem_ipsum → 프로젝트 맥락 placeholder
  - missing_headings → 빈 헤더 자동 삽입
  - short_table → outline_ids 기반 행 채움
- **G4** 모델 승격 (Sonnet → Opus on retry≥2)

## L1, L2: 일관성·variance 차단

- **L1** Outline-first — 섹션 분할 전 전체 ID 리스트 LLM 호출 1회 → 섹션마다
  재사용해 47 vs 50 같은 불일치 차단
- **L2** AI QA artifact_version 캐시 — 같은 artifact 버전에 대한 QA 결과 재사용

## S2-4: Self-consistency

핵심 spec(PRD/요구사항/화면 목록/보안 설계서) QA 호출은 N=3 병렬 → 점수
중앙값 + PASS 다수결. variance 근본 차단.

## S3-1: Phase Contract

각 단계 종료 시점에 `engine/core/phase_contract.py` 가
- 필수 artifact 존재
- COMPLETED 비율 임계
- FAILED/SUSPENDED 0건

검증. 위반 시 다음 단계 GATE 노드 AWAITING_APPROVAL 차단.

## Cascade (코어 + 확장)

- **코어 cascade**: upstream 변경 → downstream invalidate (단방향)
- **S2-5 역방향 rework**: downstream QA 사유에 upstream 키워드(DESIGN/API/DB/REQ)
  포함 시 upstream COMPLETED → INVALID. `upstream_rework_count` phase 당 2회 상한

## Observability

- `engine/observability/events.py` — event_counts 집계
- `engine/observability/drilldown.py` — 7종 verification badge 상세
- `engine/observability/metrics.py` — Prometheus 메트릭 (옵션)
- `engine/core/audit_log.py` — 노드 상태 전이·재시도·cascade 전부 기록

## Engagement-level 예산

`engine/core/engagement_budget.py` — 코어 budget_enforcer 와 별도 레이어로
engagement 단위 누적 토큰 모니터. 80% warn / 100% exceed 신호.

## i18n / Locale

`engine/i18n/__init__.py` — `engagements.locale` 컬럼 (ko/en/ko-en) 기반으로
spec prompt 말미에 언어 지시문 자동 첨부. auto_fix 도 locale 별 대체어 사용.

## Domain Profiles

`engine/intake/domain_profiles/` — 5종 업종 템플릿 (실버케어·이커머스·SaaS·
금융·제조). 키워드 매칭으로 자동 감지 후 spec_overrides 적용.

## Failure Learning

`engine/skills/gotchas_learning.py` — 과거 INVALID 노드 description 에서
카테고리 8종 자동 분류 → 임계 N회 이상이면 prompt hint 로 주입해 같은 실수
반복 차단.
