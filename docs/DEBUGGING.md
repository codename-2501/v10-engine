# V8 디버깅 가이드

## logger 이벤트 명명 규약

logger 호출은 모두 `<영역>_<액션>_<결과>` 패턴 — grep 으로 빠르게 추적 가능.

### Harness 검증

| 이벤트 | 의미 | 조치 |
|---|---|---|
| `harness_document_pass` | 문서 구조 검증 통과 | — |
| `harness_document_fail` | 구조 검증 실패 | failures 배열 첫 3건 확인 |
| `harness_ai_code_pass` / `_fail` | AI 코드 산출물 | structural_failures 확인 |
| `harness_programmatic_fail` | 프로그래매틱 코드 | recipe_count, page_slugs 확인 |
| `harness_design_match_fail` | 디자인 ↔ TSX 불일치 | 디자인 시안 vs 실제 컴포넌트 비교 |
| `harness_screen_coverage_fail` | 화면 ID 커버리지 미달 | screen_coverage_base 임계 확인 |
| `harness_define_xref_fail` | DEFINE 교차참조 깨짐 | 화면목록↔유저플로우 매칭 확인 |
| `harness_interactivity_fail` | 인터랙션 패턴 누락 | useState / use client 확인 |

### Auto-fix

| 이벤트 | 의미 |
|---|---|
| `auto_fix_forbidden_applied` | 금지어 치환 성공 |
| `auto_fix_forbidden_abandoned` | 남용 임계 초과 → 포기 |
| `auto_fix_lorem_applied` | Lorem 카피 치환 |
| `auto_fix_missing_headings` | 헤더 자동 삽입 |
| `auto_fix_short_table` | 표 행 자동 추가 |

### F4 Chunked Document

| 이벤트 | 의미 | 조치 |
|---|---|---|
| `chunked_doc_outline_ids` | outline 호출 성공 | count 확인 |
| `chunked_doc_outline_insufficient` | outline 파싱 < 3 | LLM 응답 형식 문제 |
| `chunked_doc_section_truncated` | 섹션 max_tokens 도달 | F1 확장 적용됐는지 확인 |
| `chunked_doc_section_insufficient` | min_items 미달 → 재생성 | retry 결과 확인 |
| `chunked_doc_shared_ids_initialized` | 첫 ID 풀 등록 | 이후 섹션 일관성 보장 |
| `chunked_doc_shared_ids_expanded` | 추가 ID 발견 | 정상 — 신규 항목 |
| `chunked_doc_complete` | 전체 생성 완료 | size, truncated 확인 |

### Cascade

| 이벤트 | 의미 |
|---|---|
| `upstream_cascade_node` | upstream 노드 cascade 적용 |
| `upstream_cascade_skip_source` | 원인 노드 → skip (루프 방지) |
| `upstream_rework_invalidated` | 역방향 rework — upstream INVALID 전환 |
| `upstream_rework_skipped` | rework_count 상한 도달 → skip |
| `early_completed_invalidated` | downstream COMPLETED → INVALID |

### Phase Contract

| 이벤트 | 의미 | 조치 |
|---|---|---|
| `phase_contract_blocked` | 다음 단계 GATE 차단됨 | violations 확인 → 해당 노드 재실행 |
| `engagement_budget_warn` | 토큰 80% 도달 | operator 결정 (계속/PAUSE) |
| `engagement_budget_exceed` | 토큰 100% 초과 | engagement PAUSE 권장 |

### Prompt Cache (S3-2)

| 이벤트 | 의미 |
|---|---|
| `prompt_cache model=... read=N write=N` | 캐시 hit/miss + 절감률 |

## DB 조회로 추적

```sql
-- 최근 INVALID 노드의 verdict 사유
SELECT id, task_name, description
FROM nodes
WHERE state='INVALID' AND engagement_id=?
ORDER BY updated_at DESC LIMIT 10;

-- engagement 누적 토큰
SELECT SUM(input_tokens), SUM(output_tokens)
FROM agent_token_usage atu
JOIN nodes n ON atu.node_id = n.id
WHERE n.engagement_id=?;

-- 이벤트 집계 (S2-1)
SELECT event_name, count, last_at
FROM event_counts
WHERE project_id=? OR project_id IS NULL
ORDER BY count DESC LIMIT 20;

-- Phase contract 위반 이력 (S3-1)
SELECT ts, phase, summary FROM contract_violations
WHERE engagement_id=? ORDER BY ts DESC;

-- Audit log — 노드 상태 전이 추적 (S3-5)
SELECT ts, action, before, after, meta
FROM audit_log
WHERE node_id=? ORDER BY ts DESC;
```

## 자주 발생하는 증상

### "노드가 SUSPENDED 인데 자동 복구 안 됨"
1. `nodes.description` JSON parse → `cooldown_until` 확인
2. `SUSPENDED_MIN_COOLDOWN_MINUTES` (기본 3분) 경과했는가?
3. `SUSPENDED_MAX_RESUMES` (기본 3회) 도달했는가?
4. `failure_reasons` 누적 bytes > `RESUME_RUNAWAY_FR_BYTES` (6000)?
5. watchdog 가 동작 중인가? `WATCHDOG_INTERVAL_SECONDS` 확인

### "QA 점수가 매번 다르게 나옴"
- `engine/skills/qa/self_consistency.py` 적용 대상인가?
- `spec.self_consistency_n` 명시 또는 spec.name 이 화이트리스트 (`PRD`,
  `요구사항 정의서` 등)에 있어야 N=3 활성

### "외곽 호출이 발동 안 함"
- `len(sections) >= OUTLINE_MIN_SECTIONS (3)` ?
- `cached_sections` 가 있으면 outline 스킵 — task_snapshot 확인

### "TBD가 산출물에 계속 들어옴"
1. `forbidden_words` regex 확인 (`harness.py:502`)
2. `auto_fix_forbidden_words` 동작 로그 확인
3. `MAX_AUTO_FIX_FORBIDDEN` (기본 5) 초과로 abandoned 인가?
4. retry 시 모델 승격 (Sonnet→Opus) 됐는가?

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `V8_ENGAGEMENT_TOKEN_BUDGET` | 2000000 | engagement 단위 상한 |
| `PLATFORM_ENCRYPT_KEY` | (필수) | OAuth credential 암복호화 |
