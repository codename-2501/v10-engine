# V10 Engine — 근본 원인 차단 + 범용 처리 계획 v2.0

작성일: 2026-05-01
이전 버전: `REMEDIATION_PLAN.md` (v1.x, 2026-04-30)
재작성 사유: 사용자 워크플로우 정의 (`/Users/codename/Downloads/웨이브 워크플로.rtf`, 2026-05-01) 수령 후 plan framework 자체 재구성. v1 의 5축은 *내가 인지한 사각지대 (Habit Tracker 1건)* 만 다룸. 사용자 정의는 더 본질적이고 범용적 — plan 의 §1-§7 재작성 + axis 5종 신규 추가.

---

## 0. Executive Summary (v2)

**Plan framework**: 사용자 워크플로우 정의의 12요소 중 v10 엔진에 ✓ 7개 / △ 4개 / ✗ 1개 매핑. 핵심 갭 5종 (G1-G5):

| 갭 | 사용자 정의 | v10 현황 |
|----|------------|----------|
| G1 | 검증 결과 3분류 중 "구조적 문제 (전역 영향)" | macro_diagnose 가 자기/상위 2분류만 |
| G2 | 상위 노드로 부분/맥락 정보 전달 | 무조건 통째 INVALID, change_type 미지정 |
| G3 | 재귀적 root cause 추적 (5+ hop 가능) | UPSTREAM_REWORK_LIMIT_PER_PHASE=2, 깊이 명시 X |
| G4 | 영향 기반 재작업 결정 (의미 우선) | change_classifier syntactic 편향 (15줄/20%) |
| G5 | "수정도 검증 트리거" + 무한 루프 방지 | cascade 자동 처리, cycle detection 부재 |

**v2 axis 10종**:
- A1-A5 (v1 계승): 도메인 분류 / intake confirm / 슬롯 적합성 / 커버리지 / saver 단일 진입점
- A6-A10 (신규): 상위 부분 라우팅 / 재귀 체인 / 의미 분류 / 구조적 분류 / 무한 루프 detection

**불변 원칙 (v1→v2 유지)**: 코어 5파일 변경 0. 환경변수로 모든 axis 즉시 비활성. 측정 후 보정.

---

## 1. v1 → v2 변경 사유

v1 작성 시 root cause 분석은 Habit Tracker monitoring FAILED 1건에서 도출. 사용자가 RTF 파일로 *워크플로우 정의 자체* 를 제시 → plan framework 의 *지향점* 이 명확해짐:

- v1: "본 케이스 + 같은 류 사각지대 격리"
- v2: "사용자 정의 워크플로우와 엔진 동작의 정합 + 본 케이스는 그 부분집합으로 자동 해결"

framework shift. v1 의 5축은 *intake/도메인 격리* 라는 협소 영역에 집중. v2 는 *재귀적 검증 + 양방향 전파 + 영향 기반 분기* 라는 일반 메커니즘 강화.

---

## 2. 사용자 워크플로우 정의 12요소 vs v10 엔진 매핑

### 2.1 사용자 정의 (RTF 8-section + 추가)

1. **DAG 기반 실행 구조** — 5 phase + 하위 (A-1~A-5). 순차/병렬/AND.
2. **단계별 검증 시스템** — 모든 단계 종료 시 검증.
3. **검증 결과 3분류**:
   - 자체 단계 문제 → 단계만 수정
   - 상위 단계 문제 → 상위로 전달
   - **구조적 문제 (전역 영향)** → 광범위 재작업
4. **재귀적 원인 추적** — root cause 도달까지 반복 (사용자 케이스: B-3 → A-5 → A-4 → A-3 → A-2 → A-1, 5 hop)
5. **영향 기반 재작업** — Local Fix vs Global Rework 분기
6. **재작업 범위 룰** — "근원 단계부터 하위 모두 재검토 대상" + "무조건이 아니라 영향도 분석 후 결정"
7. **수정도 검증 트리거** — "수정 = 강제 검증 이벤트", "모든 수정은 문제처럼 취급"
8. **양방향 전파** — 역추적 (하→상) + 정방향 (상→하) 동시
9. **변경 영향 제한 장치** — 인터페이스 고정, 단계별 계약 정의
10. **재작업 기준 명확화** — 어느 수준부터 전체 재작업인지 규칙화
11. **수정은 잠재적 오류 생성기** — 무한 루프 방지 명시
12. **DAG 노드별 검증 layer** — `[A-3 실행] → [검증] → OK/FAIL`

### 2.2 v10 엔진 매핑 표

| # | 요소 | 구현 위치 | 상태 |
|---|------|----------|------|
| 1 | DAG 구조 | `state_machine.py:Phase`, `dag_advancer.py:topological_sort` | ✓ |
| 2 | 단계별 검증 | TASK ↔ QA pair (`qa_pair_node_id`) | ✓ |
| 3a | 자체 단계 분류 | macro_diagnose 0건 시 self-handle | ✓ |
| 3b | 상위 단계 분류 | macro_diagnose hit 시 상위 INVALID | ✓ (카테고리 7종 닫힘) |
| **3c** | **구조적 분류** | — | **✗ 부재** |
| 4 | 재귀 추적 | macro_diagnose 재트리거 가능 | △ cap=2, 깊이 명시 X |
| 5 | Local vs Global 분기 | change_classifier (PARTIAL/CONTEXTUAL) | △ 하위 cascade 시점만 |
| 6 | 재작업 범위 룰 | CascadeInvalidator 2-Phase | ✓ |
| 7 | 수정도 검증 | 상위 INVALID → 자동 재실행 + QA | ✓ |
| 8a | 역추적 | macro_diagnose | ✓ |
| 8b | 정방향 전파 | CascadeInvalidator | ✓ |
| 9 | 인터페이스/계약 | `phase_contract.py` (일부) | △ 부분 |
| **10** | **재작업 기준 명확화** | change_classifier syntactic 임계 | **△ 의미적 변화 catch X** |
| **11** | **무한 루프 방지** | rework_count cap, cycle detection 부재 | **△ 부분** |
| 12 | 노드별 검증 layer | TASK ↔ QA pair | ✓ |

집계: ✓ 7개 / △ 4개 / ✗ 1개. 갭 5종 = G1 (3c) / G2 (5 상위 시점) / G3 (4 깊이) / G4 (10) / G5 (11).

---

## 3. 설계 원칙 (v1 6원칙 + v2 추가 4원칙)

### v1 계승 원칙
- **P1. 격리 우선, 정확도 후순위**
- **P2. 자동 결정의 영구 적용 금지**
- **P3. Source-of-Truth 무결성 강제**
- **P4. 후처리 단일 진입점**
- **P5. 코어 5파일 보존**
- **P6. 측정 가능한 추가만**

### v2 신규 원칙

#### P7. 양방향 정보 전파 (사용자 정의 #8)
역추적 시 *상위로 전달되는 정보* 가 카테고리/노드 ID 만 아니라 *부분 수정 분기 정보* 도 포함. 정방향 전파는 기존대로.

#### P8. 깊이 무제한 + Cycle Detection (사용자 정의 #4 + #11)
재귀 깊이 cap 대신 *cycle detection* 으로 무한 루프 방지. 깊은 layer (5+ hop) 자연스럽게 추적 가능.

#### P9. 의미 우선 분류 (사용자 정의 #10)
PARTIAL/CONTEXTUAL 판정에서 syntactic 임계 (15줄/20%) 는 *후보 진입 조건* 으로만 사용. *확정* 은 LLM 의미 분류 통과 시만.

#### P10. 수정 = 검증 이벤트 (사용자 정의 #7)
모든 수정 (TASK 재실행, 상위 INVALID 후 회복) 후 자동 QA 트리거 + cascade 영향 마킹. 수정 자체가 새 fail 발생 시 즉시 G5 메커니즘 (cycle detection) 검사.

---

## 4. 10축 구체 액션

### 4.1 v1 계승 (A1-A5) — 협소 격리 영역

상세 명세는 v1 `REMEDIATION_PLAN.md` §3 참조. v2 에서 변경 없음 (다만 §11-13 outcome/metric 정의는 v2 통합 표 사용).

| Axis | 핵심 | 변경 파일 | Cover 갭 |
|------|------|----------|----------|
| A1 | macro_diagnose 카테고리 폐기 → AI 노드 라우팅 | `executor_cascade.py` | G1 부분, G2 부분 |
| A2 | intake 도메인 분류 confirm GATE | `domain_profiles/__init__.py`, `processor.py` | (사용자 워크플로우 외 영역) |
| A3 | 컴포넌트 카테고리 슬롯 적합성 | `splitting.py` | (위 동일) |
| A4 | source-of-truth 커버리지 GATE | `qa/coverage_gate.py` (신규) | G1 부분 |
| A5 | HTML saver 단일 진입점 | `artifact/saver.py` | (사용자 워크플로우 외) |

### 4.2 v2 신규 (A6-A10) — 사용자 정의 갭 cover

#### A6 — 상위 노드 부분 수정 라우팅 (G2 cover)

**문제**: `trigger_upstream_rework_if_needed` 가 상위 노드 `state='INVALID'` 만 set, change_type/affected_sections 미지정 → 무조건 통째 재실행. 사용자 핵심 요구 미충족.

**근본성**: 상위 노드 *어느 부분* 이 결함인지 정보 보존. 정보 손실 차단.

**범용성**: 모든 상위/하위 관계에 적용. 본 plan A1 와 결합되면 *임의 상위 노드* + *부분 수정 분기* 로 사용자 정의 #5 완전 충족.

**변경 위치**:
- `executor_cascade.py:782-792` (`trigger_upstream_rework_if_needed`) — 상위 INVALID UPDATE 에 change_type/affected_sections 추가
- macro_diagnose 호출 시 verdict text 에서 *섹션 정보* 도 추출 — LLM 분류 신규
- `executor_partial.py` — 상위 노드 partial patch 진입 가능하게 task_snapshot 처리 분기 추가

**의사 코드**:
```python
# executor_cascade.py
async def _classify_upstream_root_with_sections(
    verdict_text: str, model_adapter, *, candidates: list[dict],
) -> dict | None:
    """A1 의 노드 라우팅 + 섹션 정보 동시 추출.

    출력: {
      "root_cause_node_ids": ["abc12345"],
      "affected_sections": {
        "abc12345": ["## 도메인 정의", "## 사용자 페르소나"]
      },
      "change_type": "PARTIAL" | "CONTEXTUAL" | "STRUCTURAL",
      "confidence": 0.0~1.0,
    }
    """
    # AI 호출 — 노드 list + 노드별 섹션 헤딩 list 제공
    # 의미 분류로 부분/전체 결정 (P9 적용)


# trigger_upstream_rework_if_needed 의 UPDATE
await db.execute(
    """UPDATE nodes
       SET state='INVALID',
           invalidation_change_type=?,    -- 신규: A6
           invalidation_affected_sections=?,  -- 신규: A6
           upstream_rework_count = COALESCE(upstream_rework_count,0)+1,
           updated_at=?
       WHERE id=? AND state='COMPLETED'
         AND COALESCE(upstream_rework_count,0) < ?""",
    (change_type, json.dumps(affected_sections),
     now, c["id"], UPSTREAM_REWORK_LIMIT_PER_PHASE),
)
```

상위 노드 재실행 경로:
```python
# executor.py 의 노드 dispatch 에서
if node.state == NodeState.INVALID and node.invalidation_change_type == "PARTIAL":
    # _execute_partial_patch 경로로 분기
    sections = json.loads(node.invalidation_affected_sections)
    await _execute_partial_patch(db, node, sections, ...)
else:
    # 기존 통째 재실행
    await _execute_full(...)
```

**영향**:
- 변경 파일: `executor_cascade.py`, `executor.py` 의 dispatch, `executor_partial.py` (상위 노드 partial 지원)
- 코어 5파일 변경 없음
- DB 스키마: 기존 `invalidation_change_type` / `invalidation_affected_sections` 컬럼 활용 — 변경 0

**위험 / 롤백**:
- 상위 노드 partial patch 가 의도치 않은 상태 (예: 헤딩 외 본문 일부만 잘못된 경우) 미대응 — 환경변수 `V10_UPSTREAM_PARTIAL=false` 로 통째 재실행 fallback
- LLM 의미 분류 실패 시 기본값 `change_type='CONTEXTUAL'` (안전 측)

**측정**:
- A6 partial patch 적용률 (전체 upstream rework 중 PARTIAL 비율)
- partial patch 후 상위 QA PASS 율 vs CONTEXTUAL 비교 — 효과 검증

---

#### A7 — 재귀 체인 + Cycle Detection (G3 cover)

**문제**: `UPSTREAM_REWORK_LIMIT_PER_PHASE=2` 만 — 같은 노드 2회 cap. 깊이 추적 X. 사용자 케이스 (B-3 → A-1, 5 hop) 미보장.

**근본성**: cap 기반 깊이 제한 폐기 → cycle detection 으로 무한 루프 방지. 사용자 정의 #4 (root cause 도달까지 반복) + #11 (무한 루프 방지) 동시 충족.

**범용성**: 모든 재귀 추적 케이스에 적용. 5 hop / 10 hop / 임의 깊이 가능.

**변경 위치**:
- 신규 컬럼: `nodes.rework_chain TEXT` (JSON 배열, root cause → cur node 경로 누적)
- `executor_cascade.py:trigger_upstream_rework_if_needed` — INVALID 시 chain append
- 신규 함수: `_detect_rework_cycle(db, node_id, candidate_id) -> bool`

**의사 코드**:
```python
# 마이그레이션 040: ALTER TABLE nodes ADD COLUMN rework_chain TEXT DEFAULT '[]'

async def _detect_rework_cycle(db, cur_node_id: str, candidate_upstream_id: str) -> bool:
    """candidate 가 cur_node 의 chain 에 이미 있으면 cycle.

    cycle 발견 시 해당 candidate INVALID 안 하고 NEEDS_HUMAN escalate.
    """
    row = await db.fetchone("SELECT rework_chain FROM nodes WHERE id=?", (cur_node_id,))
    chain = json.loads(row["rework_chain"] or "[]")
    return candidate_upstream_id in chain


# trigger_upstream_rework_if_needed 안
for c in candidates:
    if await _detect_rework_cycle(db, failed_task_node_id, c["id"]):
        # cycle — NEEDS_HUMAN escalate
        await db.execute(
            "UPDATE nodes SET state='NEEDS_HUMAN', "
            "description=? WHERE id=?",
            (json.dumps({"reason": "rework_cycle_detected", "chain": chain}), failed_task_node_id),
        )
        continue

    # chain 누적 후 INVALID
    new_chain = chain + [c["id"]]
    await db.execute(
        "UPDATE nodes SET state='INVALID', rework_chain=?, ... WHERE id=?",
        (json.dumps(new_chain), c["id"]),
    )
```

cap 조건도 변경:
```python
# UPSTREAM_REWORK_LIMIT_PER_PHASE 폐기 (또는 8 같은 큰 값으로 완화)
# 대신 chain 길이 cap (예: 10) 도달 시 NEEDS_HUMAN
```

**영향**:
- 변경 파일: `executor_cascade.py`
- 신규 마이그레이션: 040 (`rework_chain` 컬럼)
- 코어 5파일 변경 없음

**위험**:
- chain 데이터 누적 → 노드 row 크기 증가 (10 hop = JSON 배열 ~250 bytes — 무시 가능)
- cycle detection false positive (실제 같은 노드가 별도 사유로 다시 root cause 인 경우) — 별도 사유면 chain 에 timestamp 도 포함 필요. 첫 구현은 단순 cycle, 운영 후 보정.

**측정**:
- 평균 chain 깊이 분포 (1 hop ~ 10 hop)
- cycle detection 발동 빈도
- NEEDS_HUMAN escalate 율 (높으면 chain cap 너무 작음 또는 진짜 구조적 문제)

---

#### A8 — 의미적 변화 분류 강화 (G4 cover)

**문제**: change_classifier 의 PARTIAL 판정이 syntactic 임계 (15줄/20%) 우선. 의미적 변화 (1줄만 바뀌어도 정의 뒤집힘) syntactic 안에 들어오면 PARTIAL 잘못 분류.

**근본성**: syntactic 후보 진입 → LLM 의미 검증 → 통과 시만 PARTIAL 확정. 의미 fail 시 CONTEXTUAL 강제 escalate.

**범용성**: 모든 cascade 시점 (시점 2) 의 분기에 적용. A6 의 시점 1 (상위 INVALID) 분기와 일관 적용.

**변경 위치**:
- `engine/ai/change_classifier.py:220-240` (PARTIAL 확정 규칙 P1-P5) 보강

**의사 코드**:
```python
# change_classifier.py 의 _classify_heuristic 안
# 기존: P1-P5 모두 충족 → PARTIAL 즉시 반환
# 변경: P1-P5 통과는 *후보* 만 확정. 의미 검증 추가.

if p1_ok and p2_ok and p3_ok and p4_ok and sections:
    # syntactic 후보 PARTIAL — 의미 검증 호출
    semantic_check = await _classify_semantic_preservation(
        prev_content, new_content, sections, model_adapter,
    )
    if semantic_check.get("preserves_semantics", False):
        return {"type": "PARTIAL", "affected_sections": sections, ...}
    else:
        # 의미 변화 — CONTEXTUAL escalate
        return {
            "type": "CONTEXTUAL",
            "reason": f"의미 변화 감지: {semantic_check.get('reason', '?')}",
            ...
        }


async def _classify_semantic_preservation(
    prev: str, new: str, sections: list[str], model_adapter,
) -> dict:
    """diff 가 의미 보존하는지 LLM 검증.

    출력: {"preserves_semantics": bool, "reason": str, "confidence": 0.0~1.0}
    confidence < 0.6 시 보수적으로 False.
    """
    prompt = (
        "이전 산출물과 변경 산출물의 차이가 *의미를 보존* 하는지 판단.\n"
        "다음 케이스는 의미 변화 (preserves_semantics=False):\n"
        "- 정의/요구사항의 단어 1개 변경 (예: '필수' ↔ '선택')\n"
        "- API contract 의 응답 스키마 변경\n"
        "- 호환성 깨지는 구조 변경\n"
        "- 비즈니스 로직 의미 반전\n\n"
        f"## 변경된 섹션\n{', '.join(sections)}\n\n"
        f"## 이전\n{prev[:1500]}\n\n"
        f"## 변경 후\n{new[:1500]}\n\n"
        '## 출력 (JSON)\n{"preserves_semantics": true|false, "reason": "...", "confidence": 0.0~1.0}'
    )
    # ... haiku 호출, parse, validate ...
```

**영향**:
- 변경 파일: `change_classifier.py` 1개
- 코어 5파일 변경 없음
- LLM 호출 증가 — PARTIAL 후보 진입한 케이스만 (전체 cascade 의 일부)

**위험**:
- LLM cost 증가 (전체 PARTIAL 후보 케이스 × haiku 1회) — `V10_SEMANTIC_PRESERVE_CHECK=false` 로 비활성 가능
- LLM 의미 분류 false negative (의미 변화인데 보존이라 답) → CONTEXTUAL 처리되지 못하고 PARTIAL 진입 — A6/A10 메커니즘으로 후속 catch 가능

**측정**:
- semantic check 발동률 (PARTIAL 후보 중)
- preserves=False 비율
- preserves=True 후 PARTIAL 적용 → 후속 QA 결과 (의미 보존이 정확했나)

---

#### A9 — 구조적 문제 분류 (G1 cover)

**문제**: 사용자 정의 검증 결과 3분류 중 "구조적 문제 (전역 영향)" 케이스가 v10 엔진에 명시적으로 없음. macro_diagnose 는 단일 노드 라우팅만.

**근본성**: 다중 phase 영향 / phase_contract 위반 / 다중 카테고리 동시 hit 시 *구조적* 으로 분류 → phase 전체 또는 engagement 전체 INVALID.

**범용성**: PRD 전제 오류, 도메인 프로파일 근본 오분류, phase_contract 위반 등 cross-cut 결함 모두 catch.

**변경 위치**:
- `executor_cascade.py:_classify_upstream_root_with_sections` (A6 의 의사 코드) 의 출력에 `change_type='STRUCTURAL'` 추가
- 신규 함수: `_handle_structural_failure(db, project_id, root_node_ids) -> None`

**의사 코드**:
```python
# 검출 트리거
def _is_structural(routing_result: dict) -> bool:
    """다음 케이스는 STRUCTURAL:
    1. routing 결과 root_cause_node_ids >= 3 (다중 노드 동시 결함)
    2. root_cause_node_ids 가 다른 phase 에 분산 (cross-phase)
    3. phase_contract 위반 카테고리 매치 (계약 깨짐)
    4. AI 분류 결과 self-classify 'STRUCTURAL'
    """
    nodes = routing_result.get("root_cause_node_ids", [])
    phases = routing_result.get("affected_phases", set())
    return (
        len(nodes) >= 3
        or len(phases) >= 2
        or routing_result.get("phase_contract_violation")
        or routing_result.get("change_type") == "STRUCTURAL"
    )


async def _handle_structural_failure(db, project_id, root_node_ids):
    """STRUCTURAL 케이스: phase 전체 또는 engagement 전체 검토.

    - 영향 phase 의 모든 COMPLETED TASK 노드 INVALID (cascade Phase 1)
    - GATE 노드 reset (재진입 가능)
    - engagement 단위 NEEDS_HUMAN 또는 사용자 review GATE 추가
    """
    # ... 다중 노드 cascade INVALID ...
    # ... NEEDS_HUMAN 또는 사용자 GATE 생성 ...
```

**영향**:
- 변경 파일: `executor_cascade.py`
- 코어 5파일 변경 없음
- 사용자 GATE 추가 (UX 마찰)

**위험**:
- false STRUCTURAL 분류 → 정상 phase 까지 INVALID → 재작업 cost 폭발 — `V10_STRUCTURAL_DETECTION=false` 로 비활성 가능
- 임계 (3개 노드, 2개 phase) 추정 — 운영 데이터 보고 보정

**측정**:
- STRUCTURAL 분류 발동률 (전체 cascade 의 ~%)
- STRUCTURAL 후 사용자 review GATE 결과 (실제 구조적 vs false positive)

---

#### A10 — 수정 무한 루프 Detection (G5 cover)

**문제**: 사용자 정의 #7 "수정도 검증 트리거" + #11 "수정은 잠재적 오류 생성기" — 수정 자체가 새 fail 생성 시 무한 루프 방지 명시 부재. rework_count cap 만으로는 불충분.

**근본성**: 한 노드의 *재실행 시퀀스* 패턴 추적. 같은 노드가 N회 재실행 후에도 같은 fail 사유 반복 시 NEEDS_HUMAN.

**범용성**: A7 의 cycle detection 은 *cross-node* 관점. A10 은 *single-node 시퀀스* 관점. 둘 동시 적용 → 양방향 무한 루프 차단.

**변경 위치**:
- 신규 컬럼: `nodes.failure_signature_history TEXT` (JSON 배열, 직전 N회 fail 사유 hash)
- `executor.py` 의 QA fail 핸들러 — 매 fail 시 signature 기록 + history 비교

**의사 코드**:
```python
# 마이그레이션 041: ALTER TABLE nodes ADD COLUMN failure_signature_history TEXT DEFAULT '[]'

import hashlib

def _failure_signature(verdict_text: str) -> str:
    """fail 사유의 의미적 hash. 동일 의미면 같은 hash.

    단순: SHA256(normalized_verdict_text[:300])
    개선: LLM embedding 기반 (별도)
    """
    normalized = re.sub(r'\d+', 'N', verdict_text[:300].lower())  # 숫자 정규화
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# QA fail 핸들러
async def on_qa_fail(db, node_id, verdict_text):
    sig = _failure_signature(verdict_text)
    row = await db.fetchone(
        "SELECT failure_signature_history FROM nodes WHERE id=?", (node_id,),
    )
    history = json.loads(row["failure_signature_history"] or "[]")

    # 직전 3회 중 같은 sig 가 2회 이상 → 무한 루프 의심
    recent = history[-3:]
    if recent.count(sig) >= 2:
        await db.execute(
            "UPDATE nodes SET state='NEEDS_HUMAN', "
            "description=? WHERE id=?",
            (json.dumps({"reason": "modification_loop_detected",
                         "signature": sig, "history": history[-5:]}),
             node_id),
        )
        return

    history.append(sig)
    await db.execute(
        "UPDATE nodes SET failure_signature_history=? WHERE id=?",
        (json.dumps(history[-10:]), node_id),  # 최근 10회만 유지
    )
```

**영향**:
- 변경 파일: `executor.py` 또는 `qa/prompt.py` 의 fail 핸들러
- 신규 마이그레이션: 041
- 코어 5파일 변경 없음

**위험**:
- signature hash 가 너무 fuzzy (동일 의미를 다른 hash 로) → loop 미검출. 첫 구현은 단순 hash, 운영 후 LLM embedding 으로 업그레이드 (별도 axis 또는 A10 v2)
- false positive (정상 케이스인데 우연히 같은 sig 반복) → cap 3회로 충분히 보수적

**측정**:
- modification_loop_detected 발동률
- detect 후 사용자 manual override 결과 (실제 loop vs false positive)

---

## 5. 우선순위 + 의존 관계

| 순서 | Axis | 이유 | 의존 |
|------|------|------|------|
| 1 | **A5** | 가장 작은 변경, 즉시 효과 (saver 단일 진입점) | — |
| 2 | **A1** | macro_diagnose 라우팅 확장 — A6/A9 토대 | — |
| 3 | **A6** | 상위 부분 수정 라우팅 — 사용자 핵심 요구 | A1 권장 (라우팅 정확도 의존) |
| 4 | **A8** | 의미 분류 강화 — A6 와 결합 | A6 권장 |
| 5 | **A7** | 재귀 체인 + cycle detection | A1+A6 권장 |
| 6 | **A10** | 수정 무한 루프 detection | A7 와 보완 관계 |
| 7 | **A4** | source-of-truth 커버리지 GATE | — |
| 8 | **A9** | 구조적 분류 (다중 노드/phase) | A1+A6+A7 권장 |
| 9 | **A3** | 카테고리 슬롯 적합성 | A2 권장 |
| 10 | **A2** | intake 도메인 confirm — 신규 프로젝트만 | 모든 위 axis 적용 후 마지막 |

A6+A7+A8 이 사용자 정의 핵심 갭 (G2/G3/G4) 동시 cover. 본 plan v2 의 *진짜* 핵심은 이 3축.

---

## 6. 검증 시나리오

### 6.1 사용자 정의 케이스 재현 (B-3 → A-1)

가상 시나리오 (사용자 RTF 의 정확한 케이스):
- BUILD phase 의 B-3 (예: "프론트엔드 컴포넌트 구현") 검증 fail
- root cause 가 DEFINE phase 의 A-1 (예: "PRD 의 사용자 페르소나 정의")
- 5-hop 거슬러 자동 추적 가능?

A1 + A6 + A7 적용 후:
1. B-3 QA fail → macro_diagnose AI 라우팅 → A-5 (상위 노드) 지목 + 섹션 정보
2. A-5 INVALID + change_type='PARTIAL' + sections=['## X 항목']
3. A-5 partial patch 재실행 → 또 fail
4. macro_diagnose 재호출 → A-4 지목, chain=[B-3, A-5, A-4]
5. ... 반복 ...
6. A-1 INVALID, chain depth=5, cycle detection 통과
7. A-1 사용자 confirm GATE (A2 발동) 또는 자동 재생성 → 정방향 cascade

검증 포인트:
- chain 누적 정확성
- cycle false positive 없음
- 5-hop 추적 시간 (총 LLM 호출 수)
- 사용자 manual override 빈도

### 6.2 Habit Tracker 본 케이스 회복

A1 + A6 + A2 적용 후:
1. monitoring FAIL → AI 라우팅 → intake 의 `_domain_profile` 노드 지목
2. intake INVALID + change_type='PARTIAL' + sections=['_domain_profile']
3. intake partial patch — A2 confirm GATE 발동 → 사용자 personal 선택
4. domain_profile 갱신 → cascade 정방향 → splitting 재실행
5. monitoring 카테고리가 personal 슬롯으로 재구성 → A3 슬롯 적합성 통과
6. monitoring TASK 재생성 → QA PASS

### 6.3 회귀 테스트

- v1 의 회귀 테스트 100% 통과
- A6 추가 후 기존 통째 INVALID 케이스 동작 동일 (PARTIAL 명시 없으면 CONTEXTUAL 기본)
- A7 의 cycle detection 이 정상 다중 추적을 차단하지 않음 (서로 다른 노드 chain 은 통과)
- A8 의 의미 분류가 syntactic PARTIAL 케이스 95% 이상 통과 (의미 보존이 정상이라 추정)

---

## 7. 한계 / 미해결

### v1 계승 한계
- AI 분류 정확도 의존 (A1, A3, A6, A8, A9 모두 LLM 호출)
- 측정 인프라 필요 (P6, §9 보강)

### v2 신규 한계
- **A6 의 상위 partial patch**: 코드/JSON 산출물에 적용 시 섹션 특정 어려움. markdown 산출물 우선 적용, 코드는 별도 (axis A11 후보)
- **A7 cycle detection**: 단순 노드 ID 비교. 같은 노드가 *다른* 사유로 다시 root cause 인 정상 케이스를 false cycle 로 차단 가능. timestamp 추가 보정 필요
- **A8 의미 분류**: LLM 판정 자체가 fuzzy. confidence threshold 0.6 임의. 운영 보정
- **A9 STRUCTURAL 임계**: "3개 노드 / 2개 phase" 추정. 운영 데이터로 validation
- **A10 signature hash**: 단순 SHA256, 의미 동일 미세 변형 미인식. embedding 기반 업그레이드 별도

---

## 8. 첫 실행 단계

순서:
1. 마이그레이션 040, 041 적용 (rework_chain, failure_signature_history)
2. 브랜치 `feat/remediation-v2-axis-A5` 생성 — A5 부터 (가장 안전)
3. PR 머지 → 회귀 테스트 → 다음 axis (A1)
4. A1 → A6 → A8 → A7 → A10 → A4 → A9 → A3 → A2 순서
5. 각 axis 별 단일 PR (rollback 단위 명확화)
6. 모든 axis 환경변수 false 시작 (dry-run) → §17 timeline 따라 활성

---

## 9. 측정 인프라 (v1 §8 계승 + v2 axis 확장)

### 9.1 신규 metrics 테이블 (v1 동일, axis 만 확장)

```sql
CREATE TABLE remediation_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    axis TEXT NOT NULL,           -- 'A1' ~ 'A10'
    event_type TEXT NOT NULL,
    project_id TEXT,
    node_id TEXT,
    payload TEXT NOT NULL,
    actual_outcome TEXT,
    feedback_at TEXT,
    created_at TEXT NOT NULL
);
```

### 9.2 axis 별 측정 정의 (10축)

| Axis | event_type | payload 핵심 | actual_outcome |
|------|------------|-------------|----------------|
| A1 | `node_routing_decision` | `chosen_node_id, confidence, method` | retry 후 PASS=correct, 재발=fp |
| A2 | `domain_classification` | `kw_top, kw_score, llm_top, llm_conf, decision` | DESIGN 끝의 적합성 평가 |
| A3 | `slot_fit_check` | `category, fit_ratio, suggested_skip` | 후속 QA 결과 |
| A4 | `coverage_check` | `node, source, missing_ids, coverage` | retry 후 missing 채워짐=correct |
| A5 | `postprocess_apply` | `art_type, marker_present, applied` | 멱등성 hash 검증 |
| **A6** | `upstream_partial_routing` | `change_type, sections, confidence` | 상위 partial QA 결과 |
| **A7** | `rework_chain_step` | `chain_depth, cur_node, candidate, cycle_detected` | NEEDS_HUMAN 발동 시점 |
| **A8** | `semantic_preservation_check` | `preserves, confidence, reason` | partial QA 결과 |
| **A9** | `structural_classification` | `nodes_count, phases_affected, trigger` | 사용자 review 결과 |
| **A10** | `modification_loop_detection` | `signature, history_recent, loop_detected` | NEEDS_HUMAN 후 사용자 결정 |

### 9.3 dry-run 모드 (v1 동일, 환경변수 axis 확장)

각 axis 환경변수: `V10_<AXIS>_DRYRUN=true` — metrics 만 기록, 실제 적용 X. §15 timeline 따라 단계적 활성.

### 9.4 첫 측정 윈도우

v1 §8.5 기준 + v2 axis 추가:
- A1, A4, A6, A7, A8: 운영 모든 프로젝트 — **2주 또는 200건** (v1 §12.1 보정)
- A2, A3, A9: 신규/특수 케이스 — **4주 또는 30건**
- A5, A10: deterministic — 100건 검증

---

## 10. 보정 계획 (v1 §9 계승 + v2 axis 추가)

### 10.1 보정 결정 트리거

v1 §9.1 표 + v2 axis 추가:

| Axis | False Positive Rate | 보정 액션 |
|------|---------------------|-----------|
| **A6** | partial QA fail > 25% | partial 임계 보정 또는 CONTEXTUAL 강제 fallback |
| **A7** | NEEDS_HUMAN 율 > 20% | chain depth cap 상향 (10→15) 또는 cycle 정의 보정 |
| **A8** | preserves=True 후 후속 fail > 15% | confidence threshold 0.6→0.75 상향 |
| **A9** | false STRUCTURAL > 10% | 임계 (3/2) 상향 (5/3) |
| **A10** | false loop > 10% | history window (3회→5회) 또는 hash 알고리즘 보정 |

### 10.2 보정 사이클

v1 §9.2 와 동일 (격주/분기/연간).

### 10.3 졸업 / 폐기 기준

v1 §9.4 와 동일 (false_positive < 5% × 4주 + sample > 500 + 8주 안정).

---

## 11. Outcome 결정 알고리즘 (v1 §10 계승 + v2 axis 추가)

### 11.1 v1 axis (A1-A5) — v1 §10 명세 그대로

### 11.2 v2 신규 axis outcome

#### A6 — upstream_partial_routing
- partial 적용 후 24h 내 상위 노드 QA 결과:
  - PASS → `correct`
  - FAIL + 같은 affected_sections 다시 가리킴 → `false_positive` (부분이 아니라 전체였어야)
  - FAIL + 다른 sections → `unknown` (부분 자체는 맞을 수 있음)
- 72h 미결정 → `unknown` escalate

#### A7 — rework_chain_step
- chain depth 도달 후 root cause 노드 PASS → 모든 chain step `correct`
- NEEDS_HUMAN 발동 후 사용자가 chain 의 다른 노드 지목 → 자동 결정 노드 `false_positive`
- cycle detection 후 사용자가 cycle 인정 → `correct`, 사용자가 정상 케이스 → `false_positive`

#### A8 — semantic_preservation_check
- preserves=True 후 partial 적용 → 후속 QA PASS → `correct`
- preserves=True 후 후속 QA FAIL (의미 변화였음) → `false_negative`
- preserves=False → CONTEXTUAL 강제 → 후속 QA PASS → `correct`
- preserves=False → 후속 QA 도 FAIL → `unknown` (의미 분류 정확성 미확정)

#### A9 — structural_classification
- STRUCTURAL 발동 후 사용자 review → 다중 phase 재작업 결정 → `correct`
- STRUCTURAL 발동 후 사용자가 단일 노드만 수정 → `false_positive`
- false STRUCTURAL 비율 > 임계 → §10.1 트리거

#### A10 — modification_loop_detection
- NEEDS_HUMAN 발동 후 사용자가 노드 manual edit + 정상 진행 → `correct`
- 사용자가 retry 트리거 → 정상 PASS → `false_positive` (loop 아니었음)
- retry 도 FAIL → `correct` (진짜 loop)

### 11.3 Outcome 메타 한계

v1 §10.6 + v2 추가:
- A6 partial QA 결과 평가 자체가 정확하지 않을 수 있음 (다음 fail 사유가 *원래 fail 의 잔재* 인지 별개 신규 fail 인지 모호)
- A8 의미 분류 outcome 은 LLM 호출 → §15 spot-check 으로 보완
- A9 STRUCTURAL outcome 은 사용자 결정 의존 — sparse signal

---

## 12-17. v1 계승 (변경 없음)

- §12 metrics 인프라 신뢰성 (v1 §11)
- §13 통계 검정 절차 (v1 §12)
- §14 plan governance (v1 §13)
- §15 plan 밖 영역 (v1 §14)
- §16 outcome spot-check (v1 §15)
- §17 plan retrospective (v1 §16)

v1 §11-§16 의 명세 그대로. §13.1 semver 에 따라 v2.0 = major (axis 5종 추가).

---

## 18. 운영 시작 마일스톤 (v2 timeline)

### 18.1 Day 0 — 기반
- 마이그레이션 040 (`rework_chain`), 041 (`failure_signature_history`)
- A5 PR 머지 — 즉시 활성
- A1, A6, A4, A7, A8, A10 dry-run 활성 (metrics only)
- A2, A3, A9 비활성 (신규 프로젝트 발생까지 대기)

### 18.2 Day 1~14 — A5 검증 + dry-run 데이터 수집
- A5 멱등성 100% 검증
- A1, A4 dry-run sample 누적 (목표 n=100)
- A6, A7, A8, A10 dry-run sample 시작

### 18.3 Day 15~30 — 1차 활성
- §13 통계 검정으로 dry-run 데이터 분석
- accuracy ≥ 80% axis 활성 (A1, A4 우선)
- A6 활성 (A1 정확도 ≥ 80% 시 권장)

### 18.4 Day 31~60 — A7/A8 활성
- A6 1차 데이터 분석 → 활성 결정
- A7 활성 (A6 활성 후, cycle detection 검증 가능)
- A8 활성 (A6 와 결합, 의미 분류 strict 적용)

### 18.5 Day 61~90 — A10/A9 활성
- A7 chain 데이터 누적 후 A10 (loop detection) 활성
- 신규 프로젝트 누적 시 A2/A3 dry-run 시작
- A9 STRUCTURAL 검출 임계 1차 보정

### 18.6 Day 91+ — 정상 운영
- 격주 회의 (§14.3 v1)
- 분기 retrospective (§14.3 v1 + §17 plan retrospective)
- 월간 spot-check (§16)

타임라인 추정. 운영 데이터/사용자 결정에 따라 ±50% 변동.

---

## 19. v1 → v2 마이그레이션 절차

### 19.1 문서 마이그레이션
- v1 (`REMEDIATION_PLAN.md`) 그대로 유지 (history 보존)
- v2 (`REMEDIATION_PLAN_V2.md`) 가 active plan
- v2 PR 머지 시 v1 README 헤더에 "Superseded by v2.0" 마크
- v1 의 §1-§7 deprecated, v2 의 §1-§7 가 대체. v1 의 §8-§17 (측정/거버넌스) 은 v2 §9-§17 로 계승.

### 19.2 코드 마이그레이션
- 마이그레이션 040, 041 첫 PR
- A5 PR (가장 안전, v1 의 A5 그대로)
- A1 PR (v1 의 A1 그대로)
- A6 PR (v2 신규)
- ... 순차

### 19.3 환경변수 마이그레이션
- v1 환경변수 (`V10_UPSTREAM_NODE_ROUTING_ENABLED` 등) 그대로 유지
- v2 신규 추가:
  - `V10_UPSTREAM_PARTIAL` (A6)
  - `V10_REWORK_CHAIN_TRACKING` (A7)
  - `V10_SEMANTIC_PRESERVE_CHECK` (A8)
  - `V10_STRUCTURAL_DETECTION` (A9)
  - `V10_MODIFICATION_LOOP_DETECTION` (A10)

---

## 부록 A — 변경 파일 매트릭스 (v2 10축)

| 파일 | A1 | A2 | A3 | A4 | A5 | A6 | A7 | A8 | A9 | A10 |
|------|----|----|----|----|----|----|----|----|----|-----|
| executor_cascade.py | ✓ | | | | | ✓ | ✓ | | ✓ | |
| domain_profiles/__init__.py | | ✓ | | | | | | | | |
| processor.py | | ✓ | | | | | | | | |
| intake_review_gate.py (신규) | | ✓ | | | | | | | | |
| splitting.py | | | ✓ | | | | | | | |
| qa/coverage_gate.py (신규) | | | | ✓ | | | | | | |
| qa/prompt.py | | | | ✓ | | | | | | ✓ |
| artifact/saver.py | | | | | ✓ | | | | | |
| executor.py | | | | | | ✓ | | | | ✓ |
| executor_partial.py | | | | | | ✓ | | | | |
| ai/change_classifier.py | | | | | | | | ✓ | | |
| 마이그레이션 040 (신규) | | | | | | | ✓ | | | |
| 마이그레이션 041 (신규) | | | | | | | | | | ✓ |
| **코어 5파일** | **0** | **0** | **0** | **0** | **0** | **0** | **0** | **0** | **0** | **0** |

코어 5파일 변경 0 — 「Core Files Rule」 메모리 룰 준수 (v1 → v2 일관).

---

## 부록 B — 환경변수 (10축 즉시 비활성)

| 변수 | 기본 | Axis | 역할 |
|------|------|------|------|
| `V10_UPSTREAM_NODE_ROUTING_ENABLED` | true | A1 | macro_diagnose AI 라우팅 |
| `V10_INTAKE_REVIEW_GATE` | true | A2 | intake confirm GATE |
| `V10_CATEGORY_SLOT_FIT_CHECK` | true | A3 | 슬롯 적합성 검증 |
| `V10_COVERAGE_GATE_ENABLED` | true | A4 | source-of-truth coverage |
| `V10_UNIFIED_POSTPROCESS` | true | A5 | saver 단일 진입점 |
| `V10_UPSTREAM_PARTIAL` | true | A6 | 상위 부분 라우팅 |
| `V10_REWORK_CHAIN_TRACKING` | true | A7 | 재귀 chain + cycle detection |
| `V10_SEMANTIC_PRESERVE_CHECK` | true | A8 | 의미 분류 강화 |
| `V10_STRUCTURAL_DETECTION` | true | A9 | 구조적 분류 |
| `V10_MODIFICATION_LOOP_DETECTION` | true | A10 | 무한 루프 detection |
| `V10_*_DRYRUN` | false | 모두 | dry-run (metrics only) |

전부 false 로 설정 시 v1 동작 + v2 변경 없음. 즉 v1 → v2 머지해도 환경변수 false 유지하면 0 회귀.

---

## 부록 C — 솔직한 자기평가 (v2)

### 강화된 부분
- v1 의 5축 협소 영역 → v2 10축 사용자 정의 핵심 갭 (G1-G5) 모두 cover
- 사용자 케이스 (B-3 → A-1, 5-hop) 시뮬레이션 가능 — 실제 효과는 PR 적용 후 측정
- 양방향 전파 (역추적 + 정방향) 메커니즘 정합

### 여전한 한계
- v2 도 *가설*. 운영 측정 없이 "근본/범용" 단정 X (v1 부록 C 와 동일 stance)
- A6 partial patch 가 코드/JSON 산출물에 적용 어려움 (markdown 우선)
- A7 cycle detection 의 timestamp 미보정 → 정상 다중 추적 false 차단 가능
- A8 LLM 의미 분류 자체 fuzzy
- A9 STRUCTURAL 임계 추정
- A10 hash 단순 (embedding 별도 axis 또는 v3)
- 마이그레이션 040, 041 추가 — DB 스키마 변경 (v1 보다 위험 증가)

### v1 → v2 사용자 정의 충족도
- 사용자 정의 12요소: ✓ 7개 / △ 4개 → v2 적용 후 ✓ 11개 / △ 1개 (남은 △ = A11 후보 영역, 코드/JSON 산출물 partial)
- v2 적용 후에도 1개 △ 잔존 — *완전* 미달성, *대부분 충족*

### Plan 진화 자체
- v1 작성 (969 줄) → v2 작성 (이 문서, ~1300+ 줄) — plan 자체의 비대화
- §17 (plan retrospective) 가 plan 비대 자체를 catch — 분기 재검토 시 axis 졸업/통합 검토
- v2 → v3 가 발생할 조건: 사용자 새 정의 / 운영 데이터로 framework 결함 발견 / A6-A10 효과 없음

### 진짜 마무리
v2 도 plan 의 *시작점*. v2 머지 후 운영 1 cycle (90일+) 데이터 → §17 retrospective → v2.x patch 또는 v3 결정. 본 문서 자체가 *self-modifying* 이라 종착점 없음. 사용자 워크플로우 정의가 다시 진화하면 v3 트리거.

명령 받으면 §8 첫 실행 단계 (마이그레이션 040 + A5 PR) 부터 시작.
