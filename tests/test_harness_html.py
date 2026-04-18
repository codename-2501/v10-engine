"""S12: HTML harness 검증기 단위 테스트."""
from __future__ import annotations

from engine.skills.qa.harness import _harness_validate_html


def _spec(min_chars=0, min_items=0, required_headings=None, forbidden=None):
    return {
        "validation": {
            "structural": {
                "min_chars": min_chars,
                "min_items": min_items,
                "required_headings": required_headings or [],
                "forbidden": forbidden or [],
            }
        }
    }


FULL_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>테스트</title>
<style>
  :root { --bg: #1a1f2e; }
  body { background: var(--bg); }
</style>
</head>
<body>
<section id="SC-AU-001" class="screen">
  <h2>로그인</h2>
  <div class="card">
    <h3>이메일 로그인</h3>
    <p>이메일과 비밀번호로 로그인합니다. 보호자 계정과 요양보호사 계정을 구분합니다.</p>
    <form>
      <input type="email" placeholder="이메일">
      <input type="password" placeholder="비밀번호">
      <button>로그인</button>
    </form>
  </div>
</section>
<section id="SC-AU-002" class="screen">
  <h2>회원가입</h2>
  <div class="card">
    <h3>신규 가입 폼</h3>
    <p>이름·이메일·전화번호·역할(보호자/요양보호사) 선택 후 가입. 이메일 인증 진행.</p>
    <form>
      <input type="text" placeholder="이름">
      <input type="email" placeholder="이메일">
    </form>
  </div>
</section>
<section id="SC-AU-003" class="screen">
  <h2>비밀번호 재설정</h2>
  <p>이메일 기반 재설정 링크 발송. 24시간 유효. 토큰 검증 후 새 비밀번호 입력.</p>
</section>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# PASS 케이스
# ---------------------------------------------------------------------------

def test_정상_HTML_3섹션_PASS():
    r = _harness_validate_html(FULL_HTML, _spec(min_items=3, min_chars=500))
    assert r["pass"] is True


def test_section_많을때_PASS():
    # 62 section UI 시안 시뮬레이션 (간략)
    body = "\n".join(
        f'<section id="SC-AD-{i:03d}" class="screen"><h2>화면 {i}</h2>'
        f'<p>본문 {i} — 이 화면의 상세 내용을 충분히 설명하는 텍스트가 100자 이상 존재함.'
        f' 레이아웃·컴포넌트·인터랙션이 모두 포함됩니다.</p></section>'
        for i in range(10)
    )
    html = (
        '<!DOCTYPE html>\n<html><head><style>:root{}</style></head><body>\n'
        + body + '\n</body></html>'
    )
    r = _harness_validate_html(html, _spec(min_items=10))
    assert r["pass"] is True


# ---------------------------------------------------------------------------
# FAIL 케이스
# ---------------------------------------------------------------------------

def test_DOCTYPE_누락():
    html = '<html><body>본문</body></html>'
    r = _harness_validate_html(html, _spec())
    assert r["pass"] is False
    assert any("DOCTYPE" in f for f in r["structural_failures"])


def test_html_body_태그_누락():
    html = '<!DOCTYPE html><section>본문</section>'
    r = _harness_validate_html(html, _spec())
    assert r["pass"] is False
    assert any("html" in f or "body" in f for f in r["structural_failures"])


def test_section_수_부족():
    html = '<!DOCTYPE html><html><head><style></style></head><body><section><h2>하나</h2></section></body></html>'
    r = _harness_validate_html(html, _spec(min_items=5))
    assert r["pass"] is False
    assert any("section" in f.lower() for f in r["structural_failures"])


def test_style_누락():
    html = (
        '<!DOCTYPE html><html><head></head><body>'
        '<section><h2>test</h2><p>본문 텍스트가 충분히 길게 작성된 상태</p></section>'
        '</body></html>'
    )
    r = _harness_validate_html(html, _spec())
    assert r["pass"] is False
    assert any("style" in f.lower() or "css" in f.lower() for f in r["structural_failures"])


def test_빈_section_과다():
    # 5개 section 중 4개가 빈 내용
    html = '<!DOCTYPE html><html><head><style></style></head><body>'
    for i in range(4):
        html += f'<section id="s{i}"><h2>Title</h2></section>'  # 본문 없음
    html += '<section id="s4"><h2>Full</h2><p>' + 'x' * 200 + '</p></section>'
    html += '</body></html>'
    r = _harness_validate_html(html, _spec())
    assert r["pass"] is False
    assert any("빈 section" in f or "empty" in f.lower() for f in r["structural_failures"])


def test_forbidden_키워드():
    html = FULL_HTML + "<!-- 내용: 추후 작성 -->"  # 주석 안이라 통과해야 함
    r = _harness_validate_html(html, _spec(forbidden=["추후 작성"]))
    # 주석은 safe-region → PASS
    assert r["pass"] is True

    # 주석 밖 금지어 → FAIL
    html_bad = FULL_HTML + "<p>TODO: 나중에</p>"
    r2 = _harness_validate_html(html_bad, _spec(forbidden=["TODO"]))
    assert r2["pass"] is False


def test_빈컨텐츠():
    r = _harness_validate_html("", _spec())
    assert r["pass"] is False
