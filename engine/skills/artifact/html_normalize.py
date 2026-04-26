"""HTML 구조 정규화 — 저장/렌더 공통 helper.

LLM 이 `</section>`/`</main>` 등 블록 태그 닫기를 빼먹으면 브라우저 파싱 시
다음 섹션이 이전 섹션의 자식으로 nesting 되어 width 가 부모 grid/flex 규칙에
따라 계속 축소되는 layout 왜곡이 발생한다 (카탈로그 후반부 68px 까지 쪼그라듦).

본 모듈은 open/close 불일치를 감지하고 다음 open 태그 직전에 누락 close 를
삽입하여 flat 구조를 강제한다. saver (DB 저장 시점) 와 view endpoint
(렌더 시점) 양쪽에서 동일 로직을 사용해 일관된 정상 상태를 보장.
"""
from __future__ import annotations

import re

_BLOCK_TAGS = ("section", "main", "article", "aside", "nav")


def _flatten_tag(html: str, tag: str) -> str:
    """특정 블록 태그의 nesting 을 해제해 flat 구조로 강제.

    알고리즘:
      - open/close 태그를 순회하며 depth 추적
      - depth > 0 인 상태에서 또 open 을 만나면 이전 태그 close 를 먼저 삽입
      - 잉여 close (depth == 0 에서 close) 는 drop (과잉 닫힘 방지)
      - 문서 끝에 남은 depth 만큼 close 태그 보충
    """
    pat = re.compile(rf"(<{tag}(?:\s[^>]*)?>|</{tag}\s*>)", re.IGNORECASE)
    open_re = re.compile(rf"<{tag}(?:\s[^>]*)?>", re.IGNORECASE)
    parts = pat.split(html)
    depth = 0
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if open_re.match(part):
            if depth > 0:
                out.append(f"</{tag}>")
                depth -= 1
            out.append(part)
            depth += 1
        elif part.lower().startswith(f"</{tag}"):
            if depth > 0:
                out.append(part)
                depth -= 1
            # depth==0 잉여 close 는 drop
        else:
            out.append(part)
    if depth > 0:
        out.append(f"</{tag}>" * depth)
    return "".join(out)


_DOCTYPE_RE = re.compile(r"^\s*<!DOCTYPE\s+html", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html\b", re.IGNORECASE)


def is_html_fragment(content: str) -> bool:
    """HTML fragment 감지 — DOCTYPE 없음 AND <html> 태그 없음.

    둘 다 없어야 fragment 로 판정. 하나라도 있으면 완전 document 로 간주.
    LLM 이 본문 섹션만 반환해 browser 가 quirks mode 로 렌더 → viewport/charset
    규칙이 작동 안 하는 회귀 방지용.
    """
    if not content or "<" not in content:
        return False
    stripped = content.lstrip()
    if _DOCTYPE_RE.match(stripped):
        return False
    if _HTML_OPEN_RE.search(content):
        return False
    return True


def wrap_html_fragment(content: str, title: str | None = None) -> str:
    """fragment 를 표준 HTML5 document 로 래핑.

    감지 조건 (is_html_fragment) 미충족 시 no-op. 이미 완전 HTML 이면 그대로 반환.
    wrap 시 meta charset UTF-8 + viewport (모바일 반응형) 필수 포함.
    `:root` 디자인 토큰은 wrap 이후 enforce_design_tokens 가 head 에 주입하므로
    wrap 단계에서는 빈 head 만 생성 (순서 의존).
    """
    if not is_html_fragment(content):
        return content
    safe_title = (title or "문서").strip() or "문서"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{safe_title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{content}\n"
        "</body>\n"
        "</html>\n"
    )


def normalize_html_structure(content: str, title: str | None = None) -> str:
    """HTML 구조 정규화 — fragment 래핑 + 블록 태그 nesting 해제.

    순서:
      1. fragment (DOCTYPE/html 누락) 감지되면 표준 HTML5 document 로 wrap
      2. section/main/article/aside/nav 의 open/close 개수 불일치 시 flatten

    `:root` 디자인 토큰 주입 (enforce_design_tokens) 은 본 함수 이후 별도 호출.
    wrap 이 head 를 만들어 두므로 토큰 주입 경로가 정상 작동.
    """
    if not content or "<" not in content:
        return content
    content = wrap_html_fragment(content, title=title)
    for tag in _BLOCK_TAGS:
        opens = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", content, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}\s*>", content, re.IGNORECASE))
        if opens != closes:
            content = _flatten_tag(content, tag)
    return content


_ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]+\}", re.IGNORECASE | re.DOTALL)
_BODY_BG_RE = re.compile(
    r"(body\s*\{[^}]*?background\s*:\s*)([^;}]+)",
    re.IGNORECASE | re.DOTALL,
)

# LLM 이 inline style 에 박아 모바일 overflow 를 유발하는 속성 패턴.
# inline style 은 외부 CSS specificity 를 무조건 이기므로 saver 에서 제거해야
# safeguard/재정의가 의도대로 작동. !important 의존을 피하기 위한 근본 처리.
_INLINE_STYLE_RE = re.compile(
    r'(style\s*=\s*)(["\'])([^"\']*)(["\'])',
    re.IGNORECASE,
)
# 제거 대상 속성 — px 단위 고정 min-width/width 가 대표 원인.
# width:100% / 0 / auto 등은 허용. min-width:Xpx / width:Xpx 만 제거.
_BAD_WIDTH_DECL_RE = re.compile(
    r"(?:min-)?width\s*:\s*\d+(?:\.\d+)?\s*px\s*;?",
    re.IGNORECASE,
)


def sanitize_inline_styles(content: str) -> str:
    """inline style 속성에서 모바일 overflow 유발 고정 px 폭 선언 제거.

    대상 (inline 한정):
      - `min-width: Xpx` — 부모 컨테이너보다 크면 모바일 overflow 직접 원인.
      - `width: Xpx` — 고정 px 폭. 같은 이유로 제거.

    외부 <style> 블록의 규칙은 건드리지 않음 (specificity 로 safeguard 가 교정 가능).
    inline style 만이 CSS 로 재정의 불가하므로 여기서 제거. `!important` 의존 회피.
    """
    if not content or 'style=' not in content.lower():
        return content

    def _clean(m: re.Match) -> str:
        prefix, q_open, body, q_close = m.group(1), m.group(2), m.group(3), m.group(4)
        cleaned = _BAD_WIDTH_DECL_RE.sub("", body)
        # 남은 빈 선언/연속 세미콜론 정리
        cleaned = re.sub(r";\s*;", ";", cleaned)
        cleaned = cleaned.strip().rstrip(";").strip()
        if not cleaned:
            return ""  # 빈 style 속성 전체 제거
        return f"{prefix}{q_open}{cleaned}{q_close}"

    return _INLINE_STYLE_RE.sub(_clean, content)


def enforce_design_tokens(content: str, reference_root_css: str) -> str:
    """프로젝트 기준 :root 블록으로 색/폰트 토큰을 결정론적으로 강제.

    동작:
      1. 기존 :root { ... } 블록 있으면 reference 로 **그대로 치환** (값 교체)
      2. :root 블록 없으면 첫 <style> 내부 시작 지점에 **삽입**
      3. body 의 background 가 var() 미사용이면 var(--bg) 로 치환
         (LLM 이 inline hex 로 흰 배경 만드는 회귀 방지)

    LLM 프롬프트 준수 확률 게임을 탈피 — 산출물이 기준과 100% 일치.
    """
    if not content or not reference_root_css:
        return content

    # 1. :root 교체 또는 삽입
    if _ROOT_BLOCK_RE.search(content):
        content = _ROOT_BLOCK_RE.sub(
            lambda _m: reference_root_css, content, count=1,
        )
    else:
        style_open_re = re.compile(r"(<style[^>]*>)", re.IGNORECASE)
        if style_open_re.search(content):
            content = style_open_re.sub(
                r"\1\n" + reference_root_css + "\n", content, count=1,
            )
        elif "</head>" in content.lower():
            content = re.sub(
                r"</head>",
                f"<style>\n{reference_root_css}\n</style>\n</head>",
                content, count=1, flags=re.IGNORECASE,
            )

    # 2. body background 가 var() 미사용이면 var(--bg) 로 치환 (흰 배경 회귀 방지)
    def _fix_body_bg(m: re.Match) -> str:
        val = m.group(2).strip()
        if "var(--" in val:
            return m.group(0)
        return m.group(1) + "var(--bg)"

    content = _BODY_BG_RE.sub(_fix_body_bg, content, count=1)
    return content
