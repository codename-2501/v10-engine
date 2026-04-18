"""Harness-Supreme 통합 — S6.

AI QA 가 거짓 FAIL 주장할 때 harness 구조 검증이 이겨서 PASS 복원되는지
단위 검증. executor.py 의 S6 블록과 동일한 harness 호출 패턴 사용.
"""
from __future__ import annotations

from engine.skills.qa.harness import _harness_validate_document


# 실제 v4 리스크 케이스 재현 — 14K자 5 섹션 완비
RISK_CONTENT = """# 리스크 관리 계획서

## 리스크 식별

| 리스크ID | 분류 | 리스크명 | 상세 | 원인 | 영향영역 |
|---|---|---|---|---|---|
| RSK-001 | 기술 | 인프라 불안정 | 서버 장애 | AWS 이슈 | 전 서비스 |
| RSK-002 | 일정 | 요구사항 변경 | 범위 확대 | 클라이언트 | 일정 |
| RSK-003 | 인력 | 핵심 이탈 | 개발자 퇴사 | 번아웃 | 개발 |
| RSK-004 | 예산 | 토큰 초과 | LLM 비용 | 대형 프로젝트 | 예산 |
| RSK-005 | 외부 | 법규 변경 | 개정 | PIPA | 보안 |

## 리스크 평가

| 리스크ID | 영향도 | 확률 | 등급 | 근거 |
|---|---|---|---|---|
| RSK-001 | H | M | H | 가용성 핵심 |
| RSK-002 | M | H | M | 빈번 |
| RSK-003 | H | L | M | 단일 실패점 |
| RSK-004 | M | M | M | 예측 가능 |
| RSK-005 | H | L | M | 법적 제약 |

## 대응 전략

| 리스크ID | 대응전략 | 구체안 | 담당 | 기한 | 예상비용 |
|---|---|---|---|---|---|
| RSK-001 | 완화 | 다중 AZ | 인프라 | 상시 | 월 50만원 |
| RSK-002 | 수용 | 주간 리뷰 | PM | 상시 | 0 |
| RSK-003 | 완화 | 지식 공유 | PM | 분기 | 연 200만원 |
| RSK-004 | 회피 | 예산 cap | 운영 | 상시 | 0 |
| RSK-005 | 전가 | 법무 자문 | 법무 | 상시 | 연 300만원 |

## 모니터링 계획

| 리스크ID | 지표 | 임계값 | 주기 | 담당 | 보고대상 |
|---|---|---|---|---|---|
| RSK-001 | 다운타임 | >5분 | 실시간 | 인프라 | PM |
| RSK-002 | 범위변경 | 주 3건+ | 주간 | PM | 클라이언트 |
| RSK-003 | 이직의향 | 설문 | 분기 | 인사 | CEO |
| RSK-004 | 토큰소진 | 80% | 일간 | 운영 | PM |
| RSK-005 | 법규모니터 | 변경 공시 | 월간 | 법무 | 경영진 |

## 비상 계획

| 리스크ID | 트리거 | 비상절차 | 결정권자 | 복구목표 | 커뮤니케이션 |
|---|---|---|---|---|---|
| RSK-001 | 다운 | 백업 전환 | CTO | 1시간 | SMS |
| RSK-002 | 범위 급변 | 재견적 | PM | 주간 | 회의 |
| RSK-003 | 핵심 이탈 | 외부 채용 | CTO | 4주 | 공지 |
| RSK-004 | 예산 초과 | PAUSE | 운영 | 즉시 | 경영진 |
| RSK-005 | 법 침해 | 격리 | CISO | 1시간 | 법무 |

본문 추가 설명 문단 여럿... (생략, 실제 14K자 가정)
""" * 3  # 가정: 실제 v4 리스크 14K자 수준


RISK_SPEC = {
    "name": "리스크 관리 계획서",
    "phase": "DEFINE",
    "type": "document",
    "validation": {
        "structural": {
            "required_headings": [
                "리스크 식별", "리스크 평가", "대응 전략",
                "모니터링 계획", "비상 계획",
            ],
            "required_tables": 4,
            "min_chars": 3000,
            "forbidden": ["TODO", "TBD", "미정", "작성 예정", "추후 작성"],
        },
        "semantic": [],
    },
}


# ---------------------------------------------------------------------------
# 직접 harness 호출 — S6 블록이 실제 사용하는 함수
# ---------------------------------------------------------------------------

def test_실제_리스크_케이스_harness_PASS():
    """14K자·5 섹션·5 테이블 → harness 가 PASS 판정 (AI 가 뭐라 하든)."""
    r = _harness_validate_document(RISK_CONTENT, "리스크 관리 계획서", RISK_SPEC)
    assert r.get("pass") is True
    assert r.get("structural_failures", []) == []


def test_헤더_부족_harness_FAIL():
    """5 섹션 중 3 섹션만 있으면 harness FAIL."""
    short = """# 리스크 관리
## 리스크 식별
내용
## 리스크 평가
내용
## 대응 전략
내용
"""
    r = _harness_validate_document(short, "리스크 관리 계획서", RISK_SPEC)
    assert r.get("pass") is False


def test_spec_없으면_검증_X():
    """validation.structural 없는 spec 은 harness override 불가 대상."""
    no_struct_spec = {"name": "임의", "validation": {}}
    # harness_validate_document 는 structural 없으면 거의 자동 pass
    r = _harness_validate_document(RISK_CONTENT, "임의", no_struct_spec)
    # 구조 규칙 없으니 실패 조건도 없음 → pass
    assert r.get("pass") is True


def test_분량_부족_harness_FAIL():
    """min_chars 미만은 FAIL."""
    tiny = "## 리스크 식별\n짧음\n## 리스크 평가\n짧음\n## 대응 전략\n짧음\n## 모니터링 계획\n짧음\n## 비상 계획\n짧음\n"
    r = _harness_validate_document(tiny, "리스크 관리 계획서", RISK_SPEC)
    assert r.get("pass") is False  # min_chars 3000 미만


def test_금지어_다수_포함_harness_FAIL():
    """TODO 가 auto_fix 상한(5개) 초과로 등장하면 FAIL."""
    with_tbd = RISK_CONTENT + "\n\n" + "\n".join(
        f"추가 항목 TODO: 항목{i}" for i in range(10)
    )
    r = _harness_validate_document(with_tbd, "리스크 관리 계획서", RISK_SPEC)
    # 6건 이상 금지어 발생 → auto_fix 포기 → FAIL
    assert r.get("pass") is False
