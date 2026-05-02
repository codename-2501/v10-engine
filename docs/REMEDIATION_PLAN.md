# V10 Engine — 근본 원인 차단 + 범용 처리 계획

작성일: 2026-04-30
작성 컨텍스트: Habit Tracker (project_id=06d0b8c1) DESIGN→BUILD 라인 stuck 분석에서 발견된 사각지대를 v10 엔진 전체에 범용 적용.

---

## 0. Executive Summary

**현 stuck**: monitoring 컴포넌트 라이브러리 노드 retry 3회 모두 FAIL → DESIGN→BUILD GATE AWAITING_APPROVAL → BUILD 74 노드 전부 BLOCKED.

**진짜 근본 원인 (3-tier)**:
- L1 (직접): `macro_diagnose` 의 upstream 카테고리 화이트리스트가 7종 (DESIGN/API/DB/REQ/INFRA/MOBILE/DATA) 으로 닫힘 → root cause 가 화이트리스트 밖이면 자동 추적 실패.
- L2 (구조): intake 단계의 `_domain_profile` 자동 분류가 사용자 검토 없이 영구 적용 → 오분류가 component_categories 슬롯 cascade.
- L3 (메타): source-of-truth (화면 목록 정의서) 100% 커버리지를 하위 단계가 강제하지 않음 → 단계별 LLM 출력의 부분 집합으로 산출되어도 GATE 통과.

**범용 처리 5축**:
1. macro_diagnose 카테고리 개방 (화이트리스트 폐기 + AI free-form 분류 + node-level 라우팅)
2. intake 도메인 분류 confirm 게이트 (자동 결정 → 1회 사용자 검토)
3. 컴포넌트 카테고리 슬롯 적합성 자동 검증
4. Source-of-Truth 커버리지 강제 게이트
5. HTML 산출물 safeguard CSS 단일 진입점 통일

5축 모두 *코어 5파일 변경 없이 가능* (dag_advancer, state_machine, context_assembler, budget_enforcer, cascade — 변경 0).

---

## 1. 발견된 4가지 문제와 RC 사슬

| # | 표면 증상 | RC L1 | RC L2 | RC L3 |
|---|-----------|-------|-------|-------|
| 1 | monitoring 컴포넌트 FAILED → BUILD 74 BLOCKED | macro_diagnose 카탈로그 사각지대 | intake 도메인 오분류 (manufacturing) | 자동 분류 결과 영구 적용 (사용자 confirm 부재) |
| 2 | UI 시안 28 SC vs 화면 설계서 32 SC vs 페이지 조립 47 SC 미스매치 | 단계간 SC 커버리지 검증 부재 | LLM 토큰 cap 시 부분 집합 산출 가능 | source-of-truth 100% 강제 룰 부재 |
| 3 | UI 시안 v28 body min-width 미설정 | safeguard CSS 적용 시점/위치 비일관 | _save_artifact 후처리 분기 다중 경로 | 단일 진입점 통일 부재 |
| 4 | 페이지 조립 47 페이지 (slug prefix scr/sc 혼재) | 페이지 ID 정규화 룰 부재 | LLM 이 단계마다 자체 prefix 도입 가능 | ID 표기법 schema 강제 부재 |

4문제 모두 *cascade 격리 부재* 라는 공통 메타로 환원. 한 단계의 부정확/누락이 하위 단계를 막거나 하위가 모순된 결과를 산출하는 것을 엔진이 자동 catch 하지 못하는 패턴.

---

## 2. 설계 원칙 (모든 액션이 따를 공리)

### P1. 격리 우선, 정확도 후순위
LLM/키워드 분류는 본질적으로 부정확. 분류 정확도 100% 추구는 무한 패치 루프 → 잘못된 분류라도 cascade 안 되도록 *격리* 가 우선.

### P2. 자동 결정의 영구 적용 금지
intake 도메인 분류, 컴포넌트 카테고리 슬롯, 화면 목록 등 *상위 단계 자동 산출* 은 *제안* 으로 다루고, 영구 적용 전 (a) 사용자 confirm 또는 (b) 하위 단계에서 의미 검증 게이트 통과.

### P3. Source-of-Truth 무결성 강제
모든 단계가 자기보다 상위의 source-of-truth (화면 목록, 컴포넌트 정의서, 도메인 프로파일) 100% 커버리지를 자동 검증. 미달 시 GATE 차단.

### P4. 후처리 단일 진입점
산출물 종류 (HTML/JSON/Markdown) 별로 후처리는 *단일 함수* 만 통과. 다중 분기 금지.

### P5. 코어 5파일 보존
`dag_advancer.py`, `state_machine.py`, `context_assembler.py`, `budget_enforcer.py`, `cascade.py` 변경 금지. 변경은 skills/ 또는 intake/ 또는 신규 모듈에 한정.

### P6. 측정 가능한 추가만
모든 패치는 (a) 변경 전 metric 측정 (b) 변경 후 측정 (c) 회귀 테스트로 검증. 측정 불가능한 패치 금지.

---

## 3. 5축 구체 액션

### A1. macro_diagnose 카테고리 개방 + Node-level 라우팅

**문제 (RC L1)**: `engine/skills/executor_cascade.py:415` 의 `_UPSTREAM_KEYWORDS` 가 7카테고리로 닫힘. AI fallback (`_classify_upstream_categories_ai:501`) 도 prompt 에 "가능한 카테고리: DESIGN/API/DB/REQ/INFRA/MOBILE/DATA" 못박고 `_VALID_CATEGORIES` 화이트리스트로 필터. INTAKE/DOMAIN_PROFILE/COMPONENT_CONFIG 같은 영역 root cause 는 catch 불가.

**근본성**: 카테고리 카탈로그 자체를 폐기 → 카테고리가 늘어날 때마다 패치하는 게 아니라 *임의 root cause 노드를 직접 지목* 가능하게 만듦.

**범용성**: intake / 화면목록 / 컴포넌트 정의서 / 도메인 프로파일 / 어떤 상위 노드든 root cause 후보가 됨. 본 케이스 (intake 도메인 오분류) 외에도 모든 미지의 사각지대 자동 catch.

**변경 위치**:
- `engine/skills/executor_cascade.py:415-447` `_UPSTREAM_KEYWORDS` — 그대로 유지 (정확 매치 우선) 하되 fallback 강화
- `engine/skills/executor_cascade.py:455` `_VALID_CATEGORIES` — 폐기 또는 동적 (engagement 의 모든 TASK 노드 이름 union)
- `engine/skills/executor_cascade.py:501` `_classify_upstream_categories_ai` → `_classify_upstream_root_node_ai` 로 재설계

**의사 코드**:
```python
async def _classify_upstream_root_node_ai(
    verdict_text: str,
    model_adapter,
    *,
    project_id: str,
    cur_node_id: str,
    db,
) -> dict | None:
    """카테고리 대신 구체 노드 ID 를 직접 지목.

    1. 같은 project 의 모든 COMPLETED TASK 노드 list 조회 (id, name, phase)
    2. AI 에 verdict text + 노드 list 제공
    3. 출력: {"root_cause_node_ids": ["<id1>","<id2>"], "confidence": 0.0~1.0,
              "reasoning": "한 줄"}
    4. confidence < 0.7 → None
    5. 반환된 노드 ID 가 실제 같은 project 인지 검증 (hallucination 방지)
    """
    # 노드 후보 수집
    candidates = await db.fetchall(
        "SELECT id, name, phase FROM nodes "
        "WHERE project_id=? AND node_type='TASK' AND state='COMPLETED' "
        "ORDER BY phase, name",
        (project_id,),
    )
    if not candidates:
        return None

    cand_lines = "\n".join(
        f"  [{c['phase']}] {c['id'][:8]} — {c['name']}"
        for c in candidates if c['id'] != cur_node_id
    )
    prompt = (
        "QA 실패 사유를 보고 어느 상위 TASK 노드가 root cause 인지 지목.\n"
        "직접 증거 없으면 빈 배열. 추측 금지.\n\n"
        "## 후보 노드 (project 내 COMPLETED TASK)\n"
        f"{cand_lines}\n\n"
        "## QA 실패 사유\n"
        f"{verdict_text[:1500]}\n\n"
        '## 출력 (JSON)\n{"root_cause_node_ids": ["<8자리 prefix>"], '
        '"confidence": 0.0~1.0, "reasoning": "한 줄"}'
    )
    # ... call model, parse, validate ids in candidates ...
```

`trigger_upstream_rework_if_needed` 에서:
1. 키워드 매치 시도 (기존 유지) — 빠른 정확 매치
2. 0건이면 AI free-form node-level 라우팅 시도
3. node ID 지목되면 해당 노드만 INVALID

**영향**:
- 변경 파일: `engine/skills/executor_cascade.py` (1개)
- 코어 5파일 변경 없음
- DB 스키마 변경 없음 (upstream_rework_audit 그대로 활용)

**위험 / 롤백**:
- AI hallucination 으로 잘못된 노드 INVALID — 완화: confidence ≥ 0.7 + 후보 list 안의 ID 만 허용 + phase 당 2회 cap 유지
- 환경변수 `V10_UPSTREAM_NODE_ROUTING_ENABLED=false` 로 즉시 비활성 가능 (기존 카탈로그만 사용)
- `upstream_rework_audit` 에 method='ai_node_level' 기록 → 사후 조사 가능

**측정 / 검증**:
- 기준 metric: macro_diagnose hit rate (현재 키워드 매치율). 측정값은 `upstream_rework_audit` 에서 SELECT count.
- 변경 후 hit rate 증가 측정. 거짓 양성 (잘못된 노드 INVALID) 추적: `upstream_rework_audit` 의 outcome 필드 추가.
- 회귀 테스트: 기존 7카테고리 키워드 매치 테스트 케이스 100% 통과.

---

### A2. Intake 도메인 분류 Confirm 게이트

**문제 (RC L2)**: `engine/intake/processor.py:535` `detect_profile_hybrid` 가 LLM confidence ≥ 0.85 시 키워드 결과 무시하고 LLM 답 채택. 사용자 검토 없음 → 영구 `_domain_profile` 결정. 본 케이스: 키워드 점수 personal=5 > manufacturing=2 인데 LLM 이 manufacturing 채택 (확신도 ≥ 0.85 가정).

**근본성**: 분류 정확도 추구가 아니라 *영구 적용 조건 강화*. 키워드 vs LLM 충돌 시 사용자 1회 검토.

**범용성**: domain_profile 외에 intake 단계의 다른 자동 결정 (size_estimator, screen_estimate, _project_plan) 에도 같은 패턴 적용 가능.

**변경 위치**:
- `engine/intake/domain_profiles/__init__.py:106-136` `detect_profile_hybrid` — 충돌 감지 + 결과 메타데이터 반환
- `engine/intake/processor.py:535` 호출부 — 충돌 시 `_domain_profile_pending=True` 플래그 추가, 영구 적용 보류
- 신규: `engine/intake/intake_review_gate.py` — DEFINE phase 시작 전 사용자 confirm 게이트

**의사 코드**:
```python
async def detect_profile_hybrid(...) -> dict:
    """반환값을 str 에서 dict 로 변경.

    {
      "decision": "personal",  # 최종 채택
      "keyword_top": "personal", "keyword_score": 5,
      "llm_top": "manufacturing", "llm_confidence": 0.87,
      "conflict": True,  # 키워드 != LLM
      "needs_review": True,  # conflict + score gap >= 2 → True
    }
    """
    # ... existing logic ...
    keyword_top = detect_profile(text)
    keyword_score = _detect_score(text, keyword_top)
    llm_result = await classify_domain_llm(...)
    # ...
    decision = _resolve_decision(keyword_top, llm_result)
    needs_review = (
        keyword_top != (llm_result or {}).get("top")
        and keyword_score >= 3
        # 키워드가 명확한데 LLM 이 다르면 검토 필요
    )
    return {
        "decision": decision,
        "keyword_top": keyword_top, "keyword_score": keyword_score,
        "llm_top": llm_result and llm_result["top"],
        "llm_confidence": llm_result and llm_result["confidence"],
        "conflict": ...,
        "needs_review": needs_review,
    }


# processor.py
result = await detect_profile_hybrid(...)
raw["_domain_profile"] = result["decision"]
raw["_domain_profile_data"] = load_profile(result["decision"])
raw["_domain_profile_review"] = {
    "needs_review": result["needs_review"],
    "alternatives": [result["keyword_top"], result["llm_top"]],
    "evidence": result,
}

# DEFINE phase 첫 노드 실행 전: needs_review=True 면 GATE 노드 생성
# (기존 GATE 노드 메커니즘 재사용 — 신규 노드 타입 만들 필요 없음)
```

**영향**:
- 변경 파일: `engine/intake/domain_profiles/__init__.py`, `engine/intake/processor.py`, 신규 `engine/intake/intake_review_gate.py`
- 코어 5파일 변경 없음
- 신규 GATE 노드 = 기존 GATE 노드 type 재사용 (state_machine.py 변경 0)

**위험 / 롤백**:
- 사용자 검토 게이트로 인한 intake 후 1회 정지 — 환경변수 `V10_INTAKE_REVIEW_GATE=false` 로 즉시 비활성
- 기존 자동 진행 프로젝트 영향 없음 (`needs_review=False` 케이스는 그대로 자동)

**측정 / 검증**:
- needs_review=True 발생률 측정 (전체 intake 중 충돌 비율)
- 사용자가 GATE 에서 키워드 vs LLM 어느 쪽 선택했는지 통계 → AI confidence 신뢰도 calibration 데이터로 활용

---

### A3. 컴포넌트 카테고리 슬롯 적합성 자동 검증

**문제 (RC L2 파생)**: `_domain_profile_data.component_categories` 의 슬롯 (예: manufacturing 의 oee_gauge, downtime_tracker) 이 프로젝트 features (습관 트래커: 체크인, 스트릭, AI 인사이트) 와 의미 매핑 안 됨. splitting.py 에서 카테고리별 LLM 호출 시 슬롯 강제 → LLM 은 도메인 컴포넌트 (habit-specific) 작성 → QA 가 "카테고리 불일치" 로 FAIL.

**근본성**: 카테고리 슬롯이 *프로젝트 features 에 적합한지* 를 splitting 단계에서 *사전 검증*. 부적합 슬롯은 SKIP 또는 사용자 확인.

**범용성**: domain_profile 오분류 외에도 정상 분류된 케이스에서도 일부 슬롯이 features 에 안 맞을 수 있음 (예: saas 분류는 맞는데 admin_backoffice 슬롯이 본 프로젝트엔 불필요). 모든 카테고리에 적용.

**변경 위치**:
- `engine/skills/splitting.py:646-673` 컴포넌트 카테고리 분할 직전에 슬롯 적합성 검증 단계 삽입

**의사 코드**:
```python
async def _validate_category_slot_fit(
    category_name: str,
    chunk_items: list[str],
    project_features: list[str],
    project_summary: str,
    model_adapter,
) -> dict:
    """슬롯이 프로젝트 features 와 의미 매핑되는지 LLM 검증.

    출력: {
      "fit_ratio": 0.0~1.0,  # 슬롯 중 매핑 가능한 비율
      "unmapped_slots": ["oee_gauge", "downtime_tracker"],
      "suggested_skip": True if fit_ratio < 0.3 else False,
      "alternative_slots": [...]  # features 에서 추출한 대안
    }
    """
    prompt = (
        f"카테고리: {category_name}\n"
        f"기본 슬롯: {chunk_items}\n"
        f"프로젝트 features: {project_features}\n"
        f"프로젝트 설명: {project_summary[:500]}\n\n"
        "각 슬롯이 이 프로젝트에 적합한지 평가. "
        "부적합 슬롯은 unmapped_slots 에. "
        "fit_ratio < 0.3 이면 suggested_skip=true. "
        "대안 슬롯 (features 기반) 제안."
    )
    # ... AI 호출, parse ...


# splitting.py 호출부
for category in profile_data["component_categories"]:
    fit = await _validate_category_slot_fit(
        category["name"], category["chunk_items"],
        project_features, project_summary, model_adapter,
    )
    if fit["suggested_skip"]:
        # 카테고리 노드를 SKIPPED 로 생성 (TASK + QA 모두)
        # 또는 needs_review 플래그로 사용자 확인
        category_action = "skip"
    elif fit["fit_ratio"] < 0.7 and fit["alternative_slots"]:
        # 슬롯 부분 교체 — 부적합 빼고 대안 추가
        category["chunk_items"] = (
            [s for s in category["chunk_items"] if s not in fit["unmapped_slots"]]
            + fit["alternative_slots"]
        )
    # else: 그대로 진행
```

**영향**:
- 변경 파일: `engine/skills/splitting.py` (1개)
- 코어 5파일 변경 없음
- DB 스키마 변경 없음

**위험 / 롤백**:
- LLM 검증 비용 (매 카테고리당 1 호출) — 캐시 적용 (project_id + category_hash)
- AI 가 잘못 SKIP 판정 → fit_ratio 임계값 낮게 (0.3) 설정 + needs_review 옵션
- 환경변수 `V10_CATEGORY_SLOT_FIT_CHECK=false` 로 비활성 가능

**측정 / 검증**:
- skip 비율 측정 (정상 프로젝트는 ~0%, 오분류는 ~30%+ 예상 — 추정)
- 슬롯 교체 후 QA score 변화 측정

---

### A4. Source-of-Truth 커버리지 강제 게이트

**문제 (RC L3)**: 화면 목록 정의서가 source-of-truth 인데 시안 (28) / 와이어프레임 (32) / 페이지 레시피 (?) / 페이지 조립 (47) 이 각각 부분 집합. 단계간 SC 커버리지 자동 검증 부재.

**근본성**: 산출물 자체의 무결성 (LLM 출력 잘림 등) 검증을 강화하는 게 아니라, *상위 source-of-truth 의 SC 100% 커버* 를 단계 GATE 조건으로 강제. 누락 자체를 못 통과시킴.

**범용성**: 화면 목록 외에도 (a) 컴포넌트 정의서 → 컴포넌트 라이브러리 (b) PRD features → 기능 백로그 (c) DB ERD → API 설계서 등 모든 source-of-truth → 하위 산출물 관계에 적용.

**변경 위치**:
- 신규: `engine/skills/qa/coverage_gate.py` — source-of-truth 커버리지 검증 plugin
- `engine/skills/qa/harness.py` 또는 `engine/skills/qa/prompt.py` — QA 시 coverage_gate 호출
- 또는 phase GATE 노드의 자동 승인 조건에 추가

**의사 코드**:
```python
# coverage_gate.py
SOURCE_OF_TRUTH_MAP = {
    "UI 디자인 시안": ("화면 목록 정의서", "SC-[A-Z0-9_-]+", "id=\"({ID})\""),
    "화면 설계서 (와이어프레임+스토리보드)": ("화면 목록 정의서", "SC-[A-Z0-9_-]+", "id=\"({ID})\""),
    "페이지 레시피": ("화면 목록 정의서", "SC-[A-Z0-9_-]+", '"screen_id":\s*"({ID})"'),
    "페이지 조립": ("화면 목록 정의서", "SC-[A-Z0-9_-]+", "id=\"({ID})\""),
    "컴포넌트 라이브러리": ("컴포넌트 정의서", "[a-z_]+", '"name":\s*"({ID})"'),
    # ... 확장 가능
}

async def check_source_of_truth_coverage(
    db, downstream_node_id: str, downstream_content: str,
) -> dict:
    """downstream 산출물이 source-of-truth 의 ID 100% 커버하는지 검증.

    반환: {
      "coverage": 0.0~1.0,
      "missing_ids": ["SC-AI-004", "SC-AU-001", ...],
      "extra_ids": [...],  # source 에 없는데 downstream 에만 있음
      "verdict": "PASS" if coverage >= 1.0 else "FAIL",
    }
    """
    # 1. downstream node name 으로 source mapping 조회
    # 2. source artifact 조회 → ID set 추출
    # 3. downstream content 에서 ID set 추출
    # 4. diff 계산
    # 5. verdict 반환

# QA 호출부
sot_check = await check_source_of_truth_coverage(db, node.id, content)
if sot_check["verdict"] == "FAIL":
    # QA score 자동 차감 (예: 50 → 25)
    # missing_ids 를 verdict text 에 포함 → macro_diagnose 가 source 노드 INVALID 트리거
    verdict["score"] = min(verdict["score"], QA_SCORE_PARTIAL - 1)
    verdict["coverage_failures"] = sot_check["missing_ids"]
```

**영향**:
- 신규 파일: `engine/skills/qa/coverage_gate.py`
- 변경 파일: `engine/skills/qa/prompt.py` (QA 후처리에 coverage_gate 호출)
- 코어 5파일 변경 없음

**위험 / 롤백**:
- 기존 통과 프로젝트가 갑자기 FAIL 될 수 있음 — 단계적 활성: 신규 프로젝트 (created_at >= 패치일) 만 적용, 기존 프로젝트는 환경변수 opt-in
- SOURCE_OF_TRUTH_MAP 의 ID 패턴 정규식 부정확 시 false fail — pytest 로 cover 보장
- 환경변수 `V10_COVERAGE_GATE_ENABLED=false` 로 즉시 비활성

**측정 / 검증**:
- 기존 모든 프로젝트에 dry-run 측정 → coverage gap 분포
- gap 이 큰 프로젝트 수동 확인 후 일괄 reproduce 필요 여부 판단

---

### A5. HTML 산출물 safeguard CSS 단일 진입점 통일

**문제 (RC L3 파생)**: `_save_artifact` 가 `executor.py:5029`, `executor_partial.py:195`, `batch.py:726`, `assembly.py:336` 등 다중 경로에서 호출. 후처리 (normalize_html_structure + safeguard CSS) 적용이 일부 경로에만 들어가 있어 산출물별 CSS 일관성 부재 (UI 시안 v28 에 body min-width 미적용).

**근본성**: 후처리를 *호출 측 책임* 에서 *saver 책임* 으로 이전. 모든 HTML 산출물이 동일 후처리 통과 보장.

**범용성**: HTML 외에 JSON/Markdown 산출물도 동일 패턴 적용 가능 (스키마 검증, 이모지 제거, 줄 끝 공백 등).

**변경 위치**:
- `engine/skills/artifact/saver.py` — `_save_artifact` 안에 art_type='html' 케이스에서 후처리 강제 호출

**의사 코드**:
```python
# saver.py
async def _save_artifact(db, node, content, art_type):
    if art_type == "html":
        content = await _postprocess_html_unified(content, node)
    elif art_type == "json":
        content = await _postprocess_json_unified(content, node)
    elif art_type == "markdown":
        content = await _postprocess_markdown_unified(content, node)
    # ... 기존 저장 로직 ...


async def _postprocess_html_unified(content: str, node) -> str:
    """모든 HTML 산출물 단일 후처리 파이프라인.

    순서:
      1. normalize_html_structure (DOCTYPE, charset, viewport, html lang)
      2. inject_safeguard_css (body min-width:0, overflow-wrap, table reflow 등)
      3. data-v10-safeguard 마커 부착
      4. SC-ID 중첩 검증 (executor.py 의 후처리와 동일 로직 끌어옴)
      5. canonical_css 삽입 (필요 시)
    """
    content = normalize_html_structure(content)
    content = inject_safeguard_css(content)  # 멱등 — 이미 있으면 skip
    content = mark_safeguard_applied(content)
    content = strip_nested_sections(content)
    content = ensure_canonical_css(content)
    return content
```

**영향**:
- 변경 파일: `engine/skills/artifact/saver.py` 1개
- 신규 헬퍼: `_postprocess_html_unified`, `inject_safeguard_css` 등 (기존 로직을 saver 로 이전)
- 호출 측 (`executor.py`, `executor_partial.py`, `batch.py`, `assembly.py`) 의 중복 후처리 코드 제거
- 코어 5파일 변경 없음

**위험 / 롤백**:
- 멱등성 검증 필수 (기존 산출물 재저장 시 후처리 두 번 적용되면 손상) — `data-v10-safeguard` 마커 확인 후 skip
- 환경변수 `V10_UNIFIED_POSTPROCESS=false` 로 기존 분기 경로 fallback

**측정 / 검증**:
- 기존 산출물 random sample 100개에 dry-run → 변경 diff 측정
- 모든 산출물에 `data-v10-safeguard` 마커 100% 부착 검증

---

## 4. 우선순위 + 의존 관계

| 순서 | 액션 | 이유 | 의존 |
|------|------|------|------|
| 1 | A5. saveguard 단일 진입점 | 가장 작은 변경, 즉시 효과, 다른 액션의 토대 | 없음 |
| 2 | A1. macro_diagnose 카테고리 개방 | 진짜 stuck 케이스 (monitoring) 자동 회복 가능성 | 없음 |
| 3 | A4. Source-of-Truth 커버리지 게이트 | 단계간 일관성 자동 보장 | A5 와 독립 |
| 4 | A3. 카테고리 슬롯 적합성 검증 | 도메인 오분류 cascade 격리 | A1 권장 (slot fit fail 시 root cause 추적) |
| 5 | A2. Intake confirm 게이트 | 분류 정확도 개선 (모든 위 액션의 *상류* 처리) | 모든 위 액션 적용 후 마지막 (intake 가 변경되면 나머지가 안정 검증된 후가 안전) |

A1+A3+A4 만 적용해도 본 케이스 자동 회복 가능성 높음 (추정). A2 는 신규 프로젝트의 사전 차단.

---

## 5. 검증 시나리오

### 5.1 본 케이스 회복 검증
1. A5 적용 → UI 시안 등 기존 산출물 재저장 시 body min-width:0 등 일괄 부착 확인
2. A1 적용 → monitoring QA fail verdict 텍스트로 macro_diagnose AI 호출 → root cause 노드로 intake submission 또는 컴포넌트 정의서 노드 지목되는지 확인
3. A3 적용 → splitting 단계에서 monitoring 카테고리의 oee_gauge 등 슬롯이 unmapped 로 분류되는지 확인 → suggested_skip=True
4. monitoring TASK 노드 retry → A1 의 root cause 추적이 정확하다면 상위 노드 INVALID 후 재생성 → monitoring TASK 가 features 기반 슬롯으로 다시 생성 → QA PASS 가능성

### 5.2 회귀 테스트
- 기존 7카테고리 키워드 매치 케이스 100% 통과 (A1)
- saveguard CSS 가 기존에 적용된 산출물에 두 번째 적용되지 않음 (A5 멱등)
- coverage gate 가 100% 커버하는 정상 프로젝트는 PASS (A4)

### 5.3 신규 프로젝트 흐름
- intake 시 키워드 vs LLM 충돌이면 needs_review=True (A2)
- DEFINE 첫 노드 진행 전 사용자 confirm GATE 노출
- splitting 시 카테고리 슬롯 적합성 검증 (A3)
- 단계 진행 시 source-of-truth coverage 자동 검증 (A4)

---

## 6. 한계 / 미해결

1. **AI 분류 정확도 의존**: A1, A3 모두 haiku 호출 결과에 의존. confidence threshold (0.7) 가 적절한지 *측정 안 됨* — 운영 데이터 누적 후 calibration 필요.
2. **Source-of-Truth 매핑 카탈로그 (A4)**: `SOURCE_OF_TRUTH_MAP` 자체가 정적 카탈로그. 새 산출물 종류 추가 시 매핑 등록 필요. A1 처럼 노드 list 기반 동적 매핑은 별도 검토.
3. **카테고리 슬롯 동적 추출 (A3 확장)**: 현재는 기본 슬롯 + 부적합 교체. 더 근본은 *features 에서 카테고리 자체를 동적 추출*. 본 plan 에서 미포함 (별도 검토).
4. **사용자 confirm 게이트 UX (A2)**: GATE 노드 메커니즘 재사용은 가능하지만 사용자 대시보드/CLI 에서 어떻게 노출할지는 별도 설계 필요.
5. **measurement infra 미구비**: P6 (측정 가능한 추가만) 를 따르려면 metrics 수집 인프라 필요. 현재 `upstream_rework_audit` 정도만 있음 — 본 plan 범위 밖.

---

## 7. 첫 실행 단계 (명령 받으면 시작)

순서대로:
1. 브랜치 생성 (`git checkout -b feat/remediation-axis-1`)
2. A5 (saver 단일 진입점) 패치 — 가장 작고 위험 낮음
3. 회귀 테스트 (`pytest tests/skills/artifact/`)
4. 본 Habit Tracker 산출물에 dry-run → diff 확인
5. PR 생성 → main 머지 후 다음 axis 진행

각 axis 별 단일 PR. 한 번에 5개 합치지 않음 (rollback 단위 명확화).

---

## 부록 A — 변경 파일 매트릭스

| 파일 | A1 | A2 | A3 | A4 | A5 |
|------|----|----|----|----|----|
| engine/skills/executor_cascade.py | ✓ | | | | |
| engine/intake/domain_profiles/__init__.py | | ✓ | | | |
| engine/intake/processor.py | | ✓ | | | |
| engine/intake/intake_review_gate.py (신규) | | ✓ | | | |
| engine/skills/splitting.py | | | ✓ | | |
| engine/skills/qa/coverage_gate.py (신규) | | | | ✓ | |
| engine/skills/qa/prompt.py | | | | ✓ | |
| engine/skills/artifact/saver.py | | | | | ✓ |
| **코어 5파일** (dag_advancer, state_machine, context_assembler, budget_enforcer, cascade) | **0** | **0** | **0** | **0** | **0** |

코어 5파일 변경 0 — 「Core Files Rule」 메모리 룰 준수.

---

## 부록 B — 환경변수 (모든 axis 즉시 비활성 가능)

| 변수 | 기본 | 효과 |
|------|------|------|
| `V10_UPSTREAM_NODE_ROUTING_ENABLED` | true | A1 비활성 시 false |
| `V10_INTAKE_REVIEW_GATE` | true | A2 비활성 시 false |
| `V10_CATEGORY_SLOT_FIT_CHECK` | true | A3 비활성 시 false |
| `V10_COVERAGE_GATE_ENABLED` | true | A4 비활성 시 false |
| `V10_UNIFIED_POSTPROCESS` | true | A5 비활성 시 false |

전부 false 로 설정하면 현재 엔진 동작과 동일 — 0 회귀 보장.

---

## 8. 측정 인프라 (Calibration Foundation)

본 plan 의 모든 axis 가 "근본/범용" 이라 주장하려면 *측정 후 보정* 이 필수. 측정 없이 가설로 남으면 협소 패치 누적과 다를 바 없음.

### 8.1 신규 metrics 테이블

```sql
CREATE TABLE remediation_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    axis TEXT NOT NULL,           -- 'A1' ~ 'A5'
    event_type TEXT NOT NULL,     -- axis별 정의
    project_id TEXT,
    node_id TEXT,
    payload TEXT NOT NULL,        -- JSON, axis별 다름
    actual_outcome TEXT,          -- 'correct' | 'false_positive' | 'false_negative' | 'unknown'
    feedback_at TEXT,             -- 사용자가 outcome 평가한 시각
    created_at TEXT NOT NULL
);
CREATE INDEX idx_metrics_axis_event ON remediation_metrics(axis, event_type);
CREATE INDEX idx_metrics_outcome ON remediation_metrics(actual_outcome);
```

신규 단일 테이블 + 마이그레이션 1개. 코어 변경 없음.

### 8.2 axis 별 측정 정의

| Axis | event_type | payload 핵심 필드 | actual_outcome 결정 방식 |
|------|------------|-------------------|-------------------------|
| **A1** | `node_routing_decision` | `{verdict_text, candidate_count, chosen_node_id, confidence, method: keyword|ai}` | INVALID 된 노드가 retry 후 정상 PASS 하면 `correct`, 또 같은 fail 반복하면 `false_positive` |
| **A1** | `keyword_match_hit` | `{categories, kw_matched}` | 동일 (downstream rework 후 결과로 판정) |
| **A2** | `domain_classification` | `{keyword_top, kw_score, llm_top, llm_conf, decision, needs_review}` | needs_review=True 일 때 사용자가 키워드/LLM 어느 쪽 confirm 했는지 + DESIGN 까지 도달 시 도메인 적합성 평가 |
| **A3** | `slot_fit_check` | `{category, fit_ratio, unmapped_slots, suggested_skip}` | suggested_skip=True 인 카테고리의 후속 QA 결과 (skip 안 했을 때 fail 했으면 `correct`) |
| **A4** | `coverage_check` | `{node_name, source_node_name, missing_ids, extra_ids, coverage}` | 사용자가 missing_ids retry 했을 때 정상 통과하면 `correct`, 누락 IDs 가 실제로 불필요했으면 `false_positive` |
| **A5** | `postprocess_apply` | `{art_type, marker_present, applied: ['normalize','safeguard','strip_nested',...]}` | 멱등성 검증 — marker_present=True 이면 skip, 아니면 apply. unknown 외 outcome 없음 (deterministic) |

### 8.3 수집 방식 (신규 인프라 최소화)

- **자동**: 각 axis 의 의사 결정 지점에서 `remediation_metrics` INSERT (1줄 추가).
- **outcome 결정**: cron 또는 watchdog 가 주기적 (예: 5분) 으로 `actual_outcome IS NULL` 레코드 후처리 — downstream 노드 상태 확인해서 결정.
- **사용자 피드백**: A2 confirm GATE 의 사용자 선택 즉시 기록. A1/A3/A4 의 false positive 는 사용자 manual override (retry/skip API) 호출 시 audit 로그에서 역추적.

### 8.4 dry-run 모드

각 axis 환경변수에 `V10_*_DRYRUN=true` 추가 — 결정은 적용하지 않고 metrics 만 기록. 운영 활성화 전 충분한 데이터 수집 가능.

```python
# 예: A1
if os.environ.get("V10_UPSTREAM_NODE_ROUTING_DRYRUN") == "true":
    # 결정만 metrics 에 기록, 실제 INVALID 호출 안 함
    await _record_metric("A1", "node_routing_decision", payload)
    return  # no-op
# 정상 경로
await _trigger_invalidation(chosen_node_id)
await _record_metric("A1", "node_routing_decision", payload)
```

### 8.5 첫 측정 윈도우 (sample size 결정)

- **A1, A4**: 운영 모든 프로젝트에서 발생, 빠르게 누적 가능. **2주 또는 100건** 중 빠른 쪽 도달 시 1차 분석.
- **A2, A3**: intake / splitting 단계라 신규 프로젝트만 발생. **4주 또는 30건** 중 빠른 쪽 도달 시 1차 분석.
- **A5**: deterministic 이라 별도 sample size 불필요. 첫 100건 멱등성 100% 확인 후 종료.

sample size 는 *추정*. 실제 분포 보고 조정 가능.

---

## 9. 보정 계획 (Calibration Plan)

### 9.1 보정 결정 트리거 (운영 1차 분석 후)

| Axis | False Positive Rate | 보정 액션 |
|------|---------------------|-----------|
| **A1** | > 15% | confidence threshold 0.7 → 0.85 상향. 또는 후보 노드 list 를 phase 별로 제한 (현재는 모든 COMPLETED) |
| **A1** | > 30% | AI 라우팅 자체 비활성 (`V10_UPSTREAM_NODE_ROUTING_ENABLED=false`) + 키워드 카탈로그 수동 확장으로 회귀 |
| **A2** | needs_review 발생률 > 50% | 충돌 감지 임계 (kw_score gap >= 3) 상향. 또는 LLM 분류 자체 retire 검토 |
| **A2** | 사용자 confirm 에서 LLM 선택 비율 > 80% | 자동 적용으로 복귀 (LLM 신뢰도 검증됨) |
| **A3** | suggested_skip 정확도 < 70% | fit_ratio 임계 0.3 → 0.2 하향 (덜 공격적). 또는 비활성 후 features 동적 추출 mode 검토 |
| **A4** | false fail > 10% | SOURCE_OF_TRUTH_MAP 정규식 패턴 보정. 또는 coverage 임계 1.0 → 0.95 완화 |
| **A5** | 멱등성 위반 > 0건 | 즉시 비활성 + 마커 검증 로직 강화 |

threshold 변경은 환경변수만 (코드 0 변경 + 즉시 적용 가능). 카탈로그/패턴 변경은 별도 PR.

### 9.2 보정 사이클

- **주간**: metrics 분포 자동 리포트 (cron). false_positive_rate, hit_rate, sample_count 표.
- **격주**: 1차 분석 — 9.1 트리거 도달 axis 식별.
- **월간**: 보정 PR 또는 환경변수 변경. axis 별 보정 히스토리 (`docs/CALIBRATION_HISTORY.md` — 신규) 에 누적 기록.
- **분기**: 전체 plan 재검토 — 본 5축 중 deprecated 또는 신규 axis 추가 검토.

### 9.3 보정 의사결정 권한

- threshold (환경변수): 자동 — 트리거 도달 시 자동 변경 + 사용자 알림 (또는 사용자 명시 승인).
- 카탈로그/패턴: 수동 PR — 사용자 리뷰 필수.
- axis 비활성/재활성: 사용자 결정 (`V10_*_ENABLED` 토글).

### 9.4 보정 종결 기준

각 axis 가 다음 모두 만족하면 *측정 인프라에서 졸업* (metrics 수집 빈도 1/10 로 down-sample):
- false_positive_rate < 5% 가 4주 연속
- sample_count > 500
- 마지막 보정 변경 이후 8주 이상 안정

졸업 axis 는 docs/CALIBRATION_HISTORY.md 에 "stable" 마크. 신규 axis 또는 아직 졸업 못 한 axis 만 active 측정.

### 9.5 보정 측정의 한계 (재귀적 honesty)

- `actual_outcome` 결정 자체가 휴리스틱: "재시도 후 PASS 했으면 correct" 는 추정. 진짜 root cause 가 다른데도 우연히 통과할 수 있음.
- 사용자 피드백 (manual override) 은 sparse signal — 사용자가 매번 outcome 평가하지 않음.
- LLM confidence calibration 자체가 LLM 에 의존 → 자기 참조 위험. 보정 시 **사용자 manual override 통계** 에 더 가중치.
- Sample size 30~100 은 통계적으로 작음. 95% CI 가 ±10%p 수준. *trend 만 보고 absolute 결정 금지*.

---

## 10. Outcome 결정 알고리즘 (axis 별 deterministic 명세)

§8.2 의 "actual_outcome 결정 방식" 이 모호했음. axis 별로 *정확한 절차* 정의. cron worker (`engine/lifecycle/metrics_outcome_worker.py` 신규) 가 5분 주기로 NULL outcome 레코드 후처리.

### A1 — node_routing_decision outcome
```sql
-- cur_node_id = chosen_node_id 의 다운스트림이 chosen 노드 INVALID 후 cascade 로 영향
-- 1차 판정 (24시간 윈도우):
SELECT n.state, n.retry_count, n.failure_reasons
FROM nodes n WHERE n.id = ?  -- 원래 fail 한 cur_node_id

-- COMPLETED + 새로운 failure_reason 없음    → 'correct'
-- 같은 failure_reason 재발                → 'false_positive'
-- 24시간 내 미결정 (still IN_PROGRESS 등)  → 재대기 (TTL 72시간)
-- 72시간 미결정                          → 'unknown' (escalate)
```
거짓 양성 강하게 검증: chosen_node 가 INVALID 됐는데 retry 후에도 *원래 노드* 가 같은 fail 반복 = 라우팅 잘못된 것 확정.

### A2 — domain_classification outcome
needs_review=False (자동 적용) 케이스의 사후 검증:
- DESIGN phase 종료 시점에 *도메인 적합성 LLM 평가* 노드 1개 자동 추가 (cost: ~$0.002/project)
- 평가: "본 프로젝트 산출물이 _domain_profile=X 와 의미적으로 매칭하나?" (yes/no/partial + reason)
- yes → `correct`, no/partial → `false_positive` (Habit Tracker 같은 케이스 catch)

needs_review=True 케이스: 사용자 GATE 에서 어떤 옵션 선택했는지 직접 기록 (deterministic).

### A3 — slot_fit_check outcome
*카운터팩추얼 평가 어려움*. 근사 방식:
- suggested_skip=True 카테고리를 실제 SKIP 했을 때 → BUILD 단계에서 해당 카테고리 컴포넌트 *부재* 가 문제 되었나? (downstream QA 가 missing 항목 지적했으면 `false_positive`)
- suggested_skip=False 카테고리 → 후속 QA 정상 PASS 시 `correct`, FAIL 시 `false_negative` (skip 했어야 했음)

### A4 — coverage_check outcome
- missing_ids retry 트리거 후 retry 산출물에 missing_ids 가 *전부 등장* 하면 `correct`
- retry 후에도 일부 missing_ids 등장 안 함 → `false_positive` 가능 (실제로 그 ID 가 source 에서도 정의 부실했을 수)
- 사용자 manual override (skip / accept anyway) → `false_positive` 카운트

### A5 — postprocess_apply outcome
deterministic 검증:
- 동일 산출물 재저장 시 marker 가 한 번만 적용되었는지 (DB hash 비교)
- safeguard CSS 가 후처리 후 산출물에 100% 존재하는지 (regex)
- 0건 위반 = `correct` 일괄 처리

### 10.6 Outcome 결정의 메타 한계
- A2 의 "DESIGN phase 종료 시점 도메인 적합성 LLM 평가 노드" 는 *outcome 결정 자체가 LLM 의존*. LLM 이 잘못 평가하면 false outcome → §9 보정도 잘못된 방향.
- 완화: §15 manual spot-check 100건이 outcome 알고리즘 자체 정확도 검증.
- A1 의 "같은 failure_reason 재발" 판정도 fuzzy. failure_reason 텍스트 비교 임계 (Jaccard ≥ 0.7 등) 명시 필요 — 추후 sample 보고 결정.
- A3 카운터팩추얼은 근본적 한계: skip 한 케이스의 "skip 안 했으면 어땠을까" 측정 불가능. 근사만 가능.

---

## 11. Metrics 인프라 신뢰성

§8 의 `remediation_metrics` 테이블만으로는 부족. 추가 보장:

### 11.1 누락 detection
- 각 axis 결정 코드에 `_record_metric()` 호출이 빠지면 즉시 alarm.
- 검증: 매시간 cron 이 axis 결정 *기대 발생량* (예: A4 는 매 QA pass 마다) vs 실제 metrics 행 수 비교. 비율이 90% 미만이면 `audit_logs` 에 `metric_drop_detected` 로그.
- A1 의 키워드 매치 hit 시 `upstream_rework_audit` 와 cross-check.

### 11.2 NULL outcome TTL
```sql
CREATE INDEX idx_metrics_null_outcome ON remediation_metrics(actual_outcome, created_at)
WHERE actual_outcome IS NULL;
-- TTL 72시간 초과 NULL → 'unknown' 로 강제 + audit_logs 에 'outcome_decision_timeout'
```
unknown 비율 > 10% 면 outcome 결정 알고리즘 자체에 문제 있다는 신호.

### 11.3 INSERT 성능
- async INSERT (existing `_db.execute` 패턴 재사용 — overhead < 1ms 추정)
- 배치 INSERT 미적용 (단건 빈도 낮음 — A1 매 QA fail 시 1회, A4 매 QA 시 1회)
- 측정: §8 sample 윈도우 동안 axis 결정 응답 시간 p50/p99 비교 (변경 전/후)

### 11.4 false_positive vs false_negative 구분
- `false_positive`: 시스템이 "X 가 원인" 이라 결정했는데 실제 X 가 원인 아님
- `false_negative`: 시스템이 "원인 없음/skip" 이라 했는데 실제 원인 있었음 (또는 skip 안 해야 했음)
- A1: false_positive 만 의미 (positive 결정만 함)
- A3: 양방향 다 측정 (skip vs no-skip 둘 다 결정)
- A4: false_positive (잘못 missing 표시), false_negative (놓친 missing) 둘 다 가능

### 11.5 메타 누락 detection (누락 catch 자체의 누락 catch)
§11.1 의 "기대 발생량 vs 실제 행 수 비교" cron 자체가 멈추거나 잘못 작동하면 누락 detection 도 누락. 보호:
- cron 가 매 실행마다 `audit_logs` 에 `metric_drop_check_run` heartbeat 기록.
- watchdog (engine/lifecycle/watchdog.py) 가 cron heartbeat 부재 시 alarm — 60분 cron 인데 90분 heartbeat 없으면 `metric_drop_cron_silent`.
- watchdog 자체 부재는 별개 문제 (이미 dag_advancer 와 같은 lifecycle 영역).

### 11.6 기대 발생량 모델
"기대 발생량" 추정 방법:
- A1: 각 phase 내 QA fail 노드 수 × macro_diagnose 호출 비율 (현재 ~100%, 코드에 강제됨).
- A2: 신규 intake 생성 수 (intake_submissions 테이블 INSERT 수).
- A3: splitting 단계의 component_categories 수 합.
- A4: QA pass 노드 수 (pass 한 노드도 coverage check 받는다고 가정).
- A5: HTML 산출물 saver 호출 수 (기존 `artifact_versions` row 수에서 art_type='html' 카운트).

기대량 ±20% 윈도우 안이면 정상. 밖이면 누락 의심. 임계 ±20% 도 추정 — 운영 데이터 보고 보정.

---

## 12. 통계 검정 절차

§9.1 의 트리거 임계값 (15%, 30%, 50%) 은 추정. 통계적 검정 부재. 다음 절차로 보완:

### 12.1 Sample size 결정
- 95% CI 폭 ±5%p 기준 (Wilson 구간):
  - 추정 비율 p = 0.10 (false positive 10% 가정) → n ≈ 138
  - p = 0.20 → n ≈ 246
  - p = 0.30 → n ≈ 323
- 결론: **각 axis 1차 분석 최소 n=200** (§8.5 의 100건 → 200건으로 상향).
- 더 정확한 정밀도 (CI 폭 ±2.5%p) 원하면 n=600.

### 12.2 임계 도달 검정
- §9.1 의 "false_positive_rate > 15%" 판정은 단순 비율 비교가 아닌 **Wilson 신뢰구간 하한** 으로 결정.
  - 예: 관측 n=200, fp=35 → 비율 17.5%. Wilson 95% CI = [12.7%, 23.5%]. 하한 12.7% > 15% 트리거 미도달 → 보정 안 함.
  - 관측 n=200, fp=42 → 21%, Wilson CI = [15.7%, 27.4%]. 하한 15.7% > 15% → 트리거 도달.
- 성급한 보정 방지.

### 12.3 다중 비교 보정
- 5축 × 다수 metric (axis 당 1~3개) → α=0.05 단순 적용은 false discovery 발생.
- Bonferroni: α / k 적용 (예: k=10 이면 α=0.005). 보수적이지만 안전.
- 또는 BH (Benjamini-Hochberg) FDR control — 더 power 높음.

### 12.4 보정 효과 검증 (A/B)
- 임계 변경 (예: confidence 0.7 → 0.85) 후 다음 sample 윈도우에서 *변경 전 baseline 과 비교*.
- 비교: McNemar test (matched pairs) 또는 chi-square (independent samples).
- p < 0.05 + effect size 의미 있을 때만 변경 유지.

### 12.5 Prior 결정 절차 (sample size 계산용)
§12.1 의 "추정 비율 p = 0.10" 같은 prior 는 첫 운영 전엔 데이터 없음. 절차:
- **첫 sample window (n=200)**: prior 무관. n=200 일률 적용.
- **이후**: 직전 window 의 관측 비율을 prior 로 사용 (Beta-Binomial conjugate update).
  - 예: 첫 window 에서 fp_rate=12% 관측 → prior=0.12 → 다음 window n 계산.
- prior 가 매우 작거나 큰 경우 (p < 0.01 or > 0.5) 일정 floor (n=200) 유지 — 통계적 안정성.
- Bayesian update 적용 시 conjugate prior Beta(α, β) — α=관측 fp 수+1, β=관측 정상 수+1.

### 12.6 다중 비교 trade-off
Bonferroni (보수) vs BH (FDR) 선택:
- **초기 (운영 6개월 미만)**: Bonferroni — false discovery 최소화 우선. 보정 변경이 잦으면 시스템 불안정.
- **운영 안정화 후**: BH (FDR<0.05) — 변경 power 회복.
- 본 문서에서 Bonferroni 시작, §13.3 분기 재검토 시 BH 전환 검토.

### 12.7 A/B 후속 sample size
§12.4 변경 효과 검증의 sample 도 별도 계산:
- McNemar test 의 power 0.8, effect size d=0.2 (small) 가정 → n ≈ 200 (matched pairs 기준)
- 즉 변경 후 다음 window 도 n=200. 그 이전엔 변경 effect 판정 보류.
- 매개 변수가 많은 변경 (예: confidence + threshold 동시 변경) 은 separate window 분리 권장 — 효과 분리 가능해야 함.

---

## 13. Plan Governance

본 문서 자체의 변경 거버넌스 부재했음. 보완:

### 13.1 Versioning
- 본 문서 v1.0. 변경 시 semver:
  - axis 추가/제거 → major (v2.0)
  - 트리거 임계/측정 방식 보정 → minor (v1.1)
  - 오타/표현 → patch (v1.0.1)
- 변경 이력은 `docs/REMEDIATION_PLAN_CHANGELOG.md` (신규).

### 13.2 변경 절차
- 모든 변경은 PR. 본 plan 의 §10-12 같은 명세 변경은 사용자 review 필수.
- §9.1 임계 자동 보정 (env 만 변경) 은 PR 없이 가능하지만 `CALIBRATION_HISTORY.md` 에 기록.

### 13.3 검토 주기
- **격주**: §9.2 metrics 분석 회의 (사용자 + 자율 자동 리포트)
- **분기**: 본 plan 전체 재검토
  - axis 졸업 검토 (§9.4)
  - 신규 axis 도입 검토 (운영 데이터에서 새로운 사각지대 발견 시)
  - 제거 검토 (사용 안 되거나 다른 axis 로 흡수 가능 시)
- **연간**: plan 자체의 효과 retrospective. 5축 도입 전 vs 후의 stuck 발생률, 평균 노드 retry 횟수, 사용자 manual override 빈도 등 비교.

### 13.4 졸업 axis 처리
§9.4 졸업 기준 도달 axis 는:
- `docs/CALIBRATION_HISTORY.md` 에 stable 마크
- metrics 수집 빈도 down-sample (1/10)
- 본 문서 §3 에서 별도 "Stable Axes" 섹션으로 이동
- env 토글은 유지 (회귀 시 즉시 비활성)

### 13.5 폐기 axis 처리
- 운영 데이터로 axis 가 *효과 없음* 확정되면 (예: false_positive 만 발생, hit rate 0):
  - PR 로 코드 제거
  - 본 문서 §3 에서 제거, 부록 D (신규) "Retired Axes" 에 사후 분석 기록
  - 환경변수 제거

### 13.6 Semver 경계 명확화
- **major (v2.0)**: §3 의 axis 5종 변경 (axis 추가/제거/대체), 코어 5파일 변경 룰 변경, §6 설계 원칙 변경
- **minor (v1.1)**: §9 임계 변경, §10 outcome 알고리즘 변경, §12 통계 방법 변경, axis 졸업/폐기
- **patch (v1.0.1)**: 표현 정정, 오타, 명세 명확화 (의미 변화 없음)
- 경계 모호 시 (예: §11 무결성 룰 추가) → minor 로 분류 (보수적). 사후 PR 리뷰에서 major 로 재분류 가능.

### 13.7 충돌 해결 절차
사용자 의견 vs metrics 충돌 시:
- metrics 가 명확 (n ≥ 200, p < 0.05, effect size 의미) + 사용자 우려 → **사용자 결정 우선**. 단 metrics 결과는 `CALIBRATION_HISTORY.md` 에 *반대 신호* 로 기록 → 추후 누적 패턴 검토.
- 사용자 의견 vs 사용자 의견 (이전 결정 vs 신규 의견) → 본 문서가 우선 (versioned). 변경 원하면 PR.
- metrics 만 있고 사용자 의견 없음 → §9.1 자동 트리거 적용.

### 13.8 회의 의제 템플릿 (격주 / 분기 / 연간)
**격주 (15분)**:
1. axis 별 sample 누적 현황 (n)
2. 트리거 도달 axis 목록 (Wilson 하한 검사)
3. 보정 액션 결정

**분기 (60분)**:
1. 5축 hit rate / fp rate / 졸업 후보
2. 운영 데이터에서 새로운 사각지대 신호 (신규 axis 후보)
3. 다중 비교 방법 전환 (Bonferroni → BH) 검토
4. §14 항목 중 plan 흡수 후보

**연간 (120분)**:
1. 5축 도입 전 vs 후의 stuck 발생률, 평균 retry, manual override 빈도 비교
2. Plan 자체 효과 평가 (어떤 axis 가 의미 있었나)
3. v2 plan 작성 여부 결정

### 13.9 Archive 데이터 보존
- 졸업/폐기 axis 의 metrics 데이터는 *최소 1년 보존* (해석 누적 학습용).
- 1년 후 down-sampling: monthly aggregate 만 유지, raw row 삭제.
- 폐기 axis 의 코드는 PR 머지 후 git history 만으로 충분 (코드 보존 안 함).

---

## 14. Plan 밖 영역 (의도적 미포함)

본 plan 이 *완전* 하지 않은 부분 — 본 문서에서 의도적으로 다루지 않음. 별도 작업 또는 별도 문서:

| 영역 | 이유 | 별도 작업 후보 |
|------|------|---------------|
| 사용자 알림 인프라 (Slack/email/대시보드) | infra 영역, 본 plan 스코프 밖 | `docs/NOTIFICATION_INFRA.md` |
| A2 confirm GATE UX (대시보드/CLI 노출) | 프론트 작업 별도 | UI 디자인 시안 + workspace 변경 |
| LLM cost 분석 (A1/A3 의 추가 호출 비용) | 별도 cost 모델링 필요 | `docs/LLM_COST_ANALYSIS.md` |
| 다른 도메인 케이스 데이터 수집 strategy | 운영 1 cycle 후 retrospective 영역 | §13.3 분기 재검토 |
| outcome 결정 휴리스틱의 정확도 검증 | ~~meta-측정 (측정의 측정) — 무한 재귀 위험~~ → §15 흡수 | §15 manual spot-check |
| 사용자 outcome 평가 UX (manual feedback 수집) | UI/UX 영역 — frontend 작업 별도 | UI 디자인 시안 + workspace |
| Plan 자체의 outcome (5축이 정말 stuck 줄였나) | ~~연간 retrospective~~ → §16 흡수 | §16 plan retrospective |
| 다른 도메인 케이스 데이터 수집 strategy | ~~분기 재검토~~ → §16.4 v2 plan 조건 흡수 | §16.4 |

본 영역들은 *본 plan 의 한계 인정* 이며 §13.3 분기 재검토에서 추가 plan 작성 여부 결정.

---

## 15. Outcome 휴리스틱 정확도 검증 (Manual Spot-check)

§14 에서 plan 안으로 흡수.

§10 의 outcome 결정 알고리즘이 LLM/휴리스틱 의존 → 자체 정확도 검증 필요. 무한 재귀 방지 위해 수동 spot-check.

### 15.1 절차
- 주기: 월간
- 표본: 직전 1개월 metrics 에서 axis 별 무작위 20건 (총 100건/월)
- 평가자: 사용자 1명 (또는 다른 자율 분석 세션)
- 평가 기준: 자동 결정된 `actual_outcome` (correct/false_positive/false_negative/unknown) 이 실제 결과와 일치하는가
- 일치율 계산 → axis 별 outcome accuracy

### 15.2 보정 트리거
- outcome accuracy < 80% → §10 알고리즘 보정 PR (해당 axis)
- accuracy < 60% → 해당 axis metrics 의 trustworthiness 경고. §9.1 자동 보정 일시 중단 + 사용자 검토 필수
- accuracy ≥ 95% → 다음 분기 spot-check 표본 수 50% 감축

### 15.3 평가 도구
- CLI 명령: `python tools/spot_check_outcomes.py --axis A1 --month 2026-05`
- 출력: 무작위 20건 샘플 + 각 metrics 의 raw payload + 사용자가 평가할 양식
- 결과 저장: `docs/SPOT_CHECK_HISTORY.md` 또는 별도 테이블 `spot_check_results`

### 15.4 자동화 한계
spot-check 자체는 자동화 불가 (자동화하면 다시 휴리스틱 검증의 휴리스틱 → 무한 재귀). 수동 100건/월 가 비용 vs 효과 적정점 — *추정*. 운영 후 부담 크면 50건/월 축소 검토.

---

## 16. Plan 의 Plan — 본 문서 진화 자체의 retrospective

§14 에서 plan 안으로 흡수.

본 문서가 *적응적 plan* 이라면 plan 자체의 진화도 측정/회고되어야 함. 메타 plan.

### 16.1 Plan 변경 이력 metric
- 매 PR 머지 시 `docs/REMEDIATION_PLAN_CHANGELOG.md` 에 자동 append (pre-commit hook)
- 항목: version, axis 영향, 변경 type (major/minor/patch), 트리거 (운영 데이터 / 사용자 요청 / 자율 분석), 변경 후 effect 측정 여부

### 16.2 분기 retrospective 항목
§13.3 분기 회의 의제 추가:
- 직전 3개월 plan 변경 횟수 (너무 잦으면 plan 불안정 신호)
- major 변경 발생 여부 (잦으면 plan 의 본 framework 자체에 결함)
- 사용자 요청 변경 vs metrics 트리거 변경 비율
- 변경 후 effect 측정 누락률

### 16.3 Plan 폐기 기준
본 plan 자체가 효과 없다고 판정되면:
- 5축 모두 false_positive_rate > 30% 이고 보정 4회 후에도 개선 없음 → plan 자체 효과 의심
- 사용자 manual override 빈도가 plan 도입 전 baseline 보다 *증가* → plan 이 개입 추가만 하고 도움 안 됨
- 위 두 신호 + 1년 운영 데이터 → v2 plan 작성 또는 polynomial rollback (axis 전부 비활성, 측정만 유지)

### 16.4 v2 Plan 작성 조건
- 5축 framework 자체의 한계 명확 (예: cascade 격리만으로 부족, 다른 차원 필요)
- 신규 도메인/케이스 데이터로 본 framework 가 catch 못 하는 패턴 발견
- v2 는 v1 의 졸업 axis 통합 + 신규 axis 로 재구성

---

## 17. 운영 시작 마일스톤 (Timeline)

### 17.1 Day 0 (PR 머지 시점)
- A5 PR 머지 — `_save_artifact` 단일 진입점 적용
- 환경변수 모두 false 로 시작 (dry-run 만 활성)
- `remediation_metrics` 테이블 생성 (마이그레이션 1개)
- `metrics_outcome_worker.py` 등록 (5분 cron)
- `metric_drop_detector.py` 등록 (60분 cron)

### 17.2 Day 1~14 (첫 sample 수집)
- A5 결과 확인 (deterministic — 1주 안에 멱등성 위반 0건 기대)
- A1, A4 dry-run metrics 누적 시작 (목표: n=100 이상)
- 누락 detection cron 정상 작동 검증 (heartbeat 확인)

### 17.3 Day 15~30 (1차 분석)
- A1, A4 dry-run 데이터로 §12.1 sample size 도달 여부 확인
- 첫 spot-check (§15) 실행
- 환경변수 활성 결정 (dry-run → 정상 운영). 단 outcome accuracy ≥ 80% 일 때만.

### 17.4 Day 31~90 (2차 분석)
- A2, A3 sample 누적 시작 (활성 후 신규 intake 만)
- §9.1 트리거 도달 여부 첫 검토
- 보정 액션 (있으면) PR

### 17.5 Day 91+ (정상 운영)
- 격주 회의 시작 (§13.3)
- 분기 retrospective 첫 회 (§13.3 + §16.2)
- §15 월간 spot-check 정착

타임라인 자체도 *추정*. 운영 환경/데이터 분포에 따라 ±50% 변동 가능.

---

## 부록 C — 솔직한 자기평가 (honesty_first 적용)

- "근본/범용" 표현 사용했지만 *측정 후 검증 필요*. 본 plan 은 가설. §8-9 측정/보정 인프라가 그 검증 절차.
- A1 의 hit rate 개선, A3 의 skip 정확도, A4 의 false fail 비율 모두 *추정*. 실측은 PR 적용 후 §8 metrics 으로 수집.
- §9 의 threshold 트리거 (15%, 30%, 50% 등) 자체도 *추정*. 첫 sample 분포 확인 후 보정.
- "최선" 이라 단정하지 않음. 다른 설계 (예: features 기반 카테고리 동적 추출, source-of-truth 자동 inference) 도 검토 가치 있음 — 본 plan 보다 더 근본일 가능성. 본 plan 은 *현재 구조 유지하면서 격리 강화* 방향.
- Habit Tracker 1건 데이터로 도출한 plan — §8 의 metrics 누적이 본 plan 자체의 검증 데이터.
- §8.2 outcome 결정 휴리스틱이 부정확하면 §9 보정도 부정확 → 재귀 위험. §9.5 에 명시.
- §10-13 추가 후에도 *완전* 아님. §14 가 plan 밖 영역 인정 (UI/notification/cost 등 7항목).
- "치밀하게 plan 했다" 라고 단정 X. 5축 + 측정 + 보정 + 거버넌스 outline 확보, 그러나 운영 1 cycle 후 metrics 보고 §10-12 다시 보정 필요. 본 문서는 *적응적 plan 의 시작점* 이며 종착점 아님.
- §15-17 보강 후에도 plan 밖 영역 4종 (UI 알림, A2 GATE UX, LLM cost, 사용자 outcome 평가 UX) 잔존. 모두 frontend/UX/cost 영역 — 본 문서 (engine 격리 강화) 와는 직교.
- 보강의 한계: 추가할수록 plan 자체가 비대해져 *plan 의 plan* 의 plan 으로 무한 회귀 가능. §16 이 그 끝. §16 도 추가 보강 가능하지만 효용 체감.
- 진짜 솔직: 본 문서 v0.3 (작성 단계 3회 보강) 이후 보강은 marginal. 이 시점부터는 *운영 데이터로 검증* 만이 다음 단계.
