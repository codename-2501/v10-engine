# V8 기여 가이드

## 절대 규칙

### 코어 5개 파일 변경 금지

- `engine/core/dag_advancer.py`
- `engine/core/state_machine.py`
- `engine/ai/context_assembler.py`
- `engine/core/budget_enforcer.py`
- `engine/core/cascade.py`

코어 변경이 필요하면 **별도 레이어 추가로 우회**. 예시:
- 노드 단위 → engagement 단위 budget → `engine/core/engagement_budget.py` 신설
- 단방향 cascade → 역방향 rework → `executor_cascade.py` 에 헬퍼 추가
- 단계 contract → `engine/core/phase_contract.py` 신설

### Magic number 금지

모든 임계값·상수는 `engine/config/thresholds.py` 에 정의 후 import. 개별 모듈
안에 hard-coded 숫자가 있으면 코드 리뷰 거부.

### 한국어 boundary 정규식

ASCII `\b` 는 한글 접촉 시 동작 다름. 필수 패턴:
```python
(?<![A-Za-z0-9_])PATTERN(?![A-Za-z0-9_])
```

## 개발 흐름

### 새 기능 추가
1. `engine/config/thresholds.py` 에 임계값 정의 (있으면)
2. 신규 모듈은 `engine/<적합한 sub>/` 에 생성
3. 코어 5개 파일은 절대 만지지 않음
4. 단위 테스트 `tests/test_<module>.py` 작성
5. `pytest tests/` 전체 PASS 확인
6. 커밋 (Co-Authored-By 포함)
7. `git push backup2 main`

### Spec 추가 (산출물 종류 확장)
1. `engine/skills/specs/<phase>/<name>.yaml` 생성
2. 필수 필드: `name`, `phase`, `type`, `prompt`, `validation`
3. 대형 문서면 `sections` 추가 (F4 outline-first 활성)
4. `min_items` 명시 (자동 재생성 트리거)

### Auto-fix 추가
1. `engine/skills/qa/harness_auto_fix.py` 에 `try_auto_fix_<name>` 함수 추가
2. `AutoFixResult` 인터페이스 준수
3. Safe regions (코드블록·heading·URL) 보호
4. 남용 임계 (MAX_AUTO_FIX_*) 정의
5. `run_auto_fix_pipeline` 에 추가
6. 단위 테스트 작성

## 테스트

```bash
# 전체
python3 -m pytest tests/ -q

# 특정 파일
python3 -m pytest tests/test_<name>.py -v

# Hypothesis 포함
python3 -m pytest tests/ --hypothesis-seed=0
```

## 커밋 메시지

```
feat(S2-X): 한 문장 요약

상세 설명 (선택)
- 변경 1
- 변경 2

회귀 위험: 평가 (낮음/중간/높음 + 사유)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

## 푸시

`backup2` 리모트로 푸시:
```bash
git push backup2 main
```

## 주요 디렉토리

```
engine/
├── ai/                  # LLM adapter, context, model_router
├── config/              # thresholds (모든 magic number)
├── core/                # 코어 (변경 금지) + audit_log/phase_contract/engagement_budget
├── i18n/                # locale 지원
├── intake/              # 프로젝트 접수 + domain_profiles
├── lifecycle/           # startup, watchdog
├── observability/       # events, drilldown, metrics, logger
├── security/            # crypto
└── skills/              # executor, executor_cascade, gotchas_learning,
    ├── specs/           #   spec YAML들 (DEFINE/DESIGN/BUILD/VERIFY/DELIVER)
    ├── qa/              #   harness, harness_auto_fix, self_consistency
    └── artifact/        #   loader

frontend/                # router + templates (코어 무수정 원칙 그대로)
tests/                   # pytest
docs/                    # 이 가이드
```
