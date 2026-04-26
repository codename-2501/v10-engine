"""B4 회귀 — harness regex SC- prefix 강제, 단축형 XX-NNN 무시."""
from __future__ import annotations

from engine.skills.qa.harness import _harness_validate_screen_coverage


def test_단축형_XX_NNN_무시():
    """정의서 본문의 가독성 단축 표기 (HB-002 등) 가 total_defined 부풀림 안 함."""
    content = """
| ID | 이름 |
|---|---|
| SC-HB-001 | 습관 목록 |
| SC-HB-002 | 습관 등록 |

본문 — HB-001 화면에서 HB-002 로 이동하면 NT-001 알림이 발생.
"""
    r = _harness_validate_screen_coverage(content, [], [])
    # screen_list_parsed 의 total = 2 (단축형 HB-001/002, NT-001 무시)
    parsed = next(c for c in r["checks"] if c["name"] == "screen_list_parsed")
    assert parsed["total"] == 2


def test_SC_prefix_와_SCR_prefix_둘다_매칭():
    content = """
| SCR-001 | 메인 |
| SC-AU-001 | 로그인 |
"""
    r = _harness_validate_screen_coverage(content, [], [])
    parsed = next(c for c in r["checks"] if c["name"] == "screen_list_parsed")
    assert parsed["total"] == 2


def test_SC_prefix_45_정확():
    """이번 세션 회귀 — Habit Tracker 정의서 v3 의 본문 단축형 33개 + full 45 = 78 부풀림 차단."""
    content = "\n".join(
        f"| SC-{prefix}-{n:03d} | {prefix} 화면 {n} |"
        for prefix in ("AI", "AU", "HB", "HM")
        for n in range(1, 4)
    ) + "\n본문에서 AI-001 와 HB-002 같이 단축형 표기 사용."
    r = _harness_validate_screen_coverage(content, [], [])
    parsed = next(c for c in r["checks"] if c["name"] == "screen_list_parsed")
    # full SC-XX-NNN 12개만 카운트, 단축형 (AI-001/HB-002) 무시
    assert parsed["total"] == 12
