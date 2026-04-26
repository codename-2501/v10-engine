"""HTML 산출물 반응형 baseline patch — 컴포넌트 레벨 근본 보강.

LLM 이 전역 규칙 (responsive_rules.md) 을 읽어도 개별 컴포넌트 수준에서
반응형 디테일을 자주 누락한다. 관찰된 패턴:
  - 테이블에 overflow-x wrap 누락 (모바일에서 viewport 초과)
  - button 에 word-break/min-height 44px 누락 (터치 영역/텍스트 잘림)
  - flex 자식에 min-width:0 누락 (CLAUDE.md 규정, overflow 유발)
  - checkbox/radio 기본 브라우저 값 (16~20px, 터치 44px 미달)
  - <style> 블록 내 min-width:Xpx (X>320) 로 모바일 overflow
  - table td 에 data-label 누락 → 모바일 카드 reflow 시 header 정보 손실

본 모듈은 저장 시점에 **결정론적 CSS patch** 를 <head> 시작부에 삽입해
LLM 의 나중 선언이 cascade 로 override 가능하도록 baseline 만 제공.
즉 의도된 커스텀은 그대로 유지, 누락된 곳만 patch 가 fallback 으로 작동.

또한 다음 결정론적 HTML transform 을 수행:
  1. stylesheet 내 큰 고정 px 을 min(Xpx, 100%) 로 안전 치환 (모바일 overflow 차단)
  2. 모든 data table 의 <td> 에 data-label 자동 주입 (thead th 텍스트 기반)
     → @media (max-width: 600px) 에서 td::before content: attr(data-label) 로
       header 문자열을 카드 라벨처럼 표시 가능 → 풀 반응형 카드 reflow 자동화
"""
from __future__ import annotations

import re

# 컴포넌트 반응형 baseline — 누락 보강 목적. specificity 최소화해 LLM override 허용.
PATCH_CSS = """
/* === V10 responsive baseline patch ===
 * LLM 누락 보강용. 같은 specificity 에서 뒤 <style> 가 이기므로,
 * 프로젝트/섹션별 커스텀은 그대로 유지된다. 선언 없는 곳에만 fallback. */

/* 5. flex/grid 자식 overflow 방지 (CLAUDE.md 필수) */
* { min-width: 0; }

/* 미디어 반응형 */
img, video, iframe, svg, canvas { max-width: 100%; height: auto; }

/* 텍스트 wrap — 한글 어절 유지 + 영문 긴 단어 끊기 */
html, body { overflow-wrap: anywhere; word-break: keep-all; }

/* 1. 테이블 반응형 baseline */
table { max-width: 100%; border-collapse: collapse; }
.table-responsive, .table-wrap, [data-responsive-table] {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  max-width: 100%;
}
/* 모바일 카드 reflow — <td data-label="..."> 자동 주입 (saver 처리) 과 함께 동작.
 * data-no-reflow 가 있는 table 은 가로 스크롤만 (대시보드용 숫자 행렬 등).  */
@media (max-width: 600px) {
  table:not([data-no-reflow]) { display: block; width: 100%; }
  table:not([data-no-reflow]) thead { display: none; }
  table:not([data-no-reflow]) tbody,
  table:not([data-no-reflow]) tr { display: block; width: 100%; }
  table:not([data-no-reflow]) tr {
    margin-bottom: 12px;
    padding: 12px;
    border: 1px solid var(--border, rgba(255,255,255,0.08));
    border-radius: 10px;
    background: var(--surface, rgba(255,255,255,0.02));
  }
  table:not([data-no-reflow]) td {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    padding: 6px 0;
    border: 0;
    text-align: left;
    white-space: normal;
  }
  table:not([data-no-reflow]) td::before {
    content: attr(data-label);
    flex: 0 0 auto;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-3, rgba(255,255,255,0.5));
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  /* data-label 이 빈 문자열인 경우 ::before 공간 축소 (콜그룹 등) */
  table:not([data-no-reflow]) td[data-label=""]::before { content: none; }
}

/* 3, 4. 버튼 반응형 + 터치 영역 */
button, [role="button"], input[type="submit"], input[type="button"], .btn {
  min-height: 44px;
  word-break: break-word;
  overflow-wrap: anywhere;
  box-sizing: border-box;
}

/* 2. checkbox/radio 터치 사이즈 */
input[type="checkbox"], input[type="radio"] {
  min-width: 20px;
  min-height: 20px;
}
label:has(input[type="checkbox"]),
label:has(input[type="radio"]) {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

/* 6. 아이콘 래퍼 정렬 baseline */
.icon, .icon-wrap, .icon-box, [data-icon] {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

/* === 원형 요소 보존 (border-radius:50% + width 누락 케이스) ===
 * LLM 이 height 만 선언하고 width 생략하면 내재 크기로 타원화됨.
 * aspect-ratio:1 + flex-shrink:0 로 정방형 보존 (inline + class 기반 공통 커버).
 * HTML transform (_fix_circle_element_width) 이 안전망으로 실제 width 도 주입. */
[style*="border-radius:50%"],
[style*="border-radius: 50%"],
.avatar, .circle, .profile, .dot, .indicator, .badge-dot,
[class*="avatar"], [class*="circle"], [class*="profile"],
[class*="thumb"], [class*="-pic"], [class*="-dot"] {
  aspect-ratio: 1 / 1;
  flex-shrink: 0;
}

/* pill / 완전 둥근 요소 (999px) shrink 방지 */
[style*="border-radius:999px"],
[style*="border-radius: 999px"],
[style*="border-radius:9999px"],
[style*="border-radius: 9999px"] {
  flex-shrink: 0;
}

/* status dot / badge-dot 최소 크기 (8px 미만이면 안 보임) */
[class*="status-dot"], [class*="badge-dot"], [data-dot] {
  min-width: 8px;
  min-height: 8px;
}

/* progress bar 전용 shrink 방지 — 명시 class/data 기반만 매치해 dot 회귀 방지.
 * 초기 버전에서 height:Npx 기반 매칭 시도했으나 같은 높이 dot (8x8 등) 까지
 * 가로 타원으로 변형시키는 회귀 발견 (2026-04-24) → class/data 기반 스코프 축소. */
[data-progress-fill],
[class*="progress-fill"],
[class*="progress-bar"],
[class*="bar-fill"] {
  flex-shrink: 0;
  min-width: 40px;
}
""".strip()

# <head> 진입 직후 삽입. LLM 의 후속 <style> 가 뒤에 와서 override 가능.
_PATCH_BLOCK = f"<style data-v10-responsive-patch='1'>\n{PATCH_CSS}\n</style>"

_HEAD_OPEN_RE = re.compile(r"(<head[^>]*>)", re.IGNORECASE)
_ALREADY_PATCHED_RE = re.compile(r"data-v10-responsive-patch", re.IGNORECASE)
# 기존 patch 블록 — 새 버전으로 교체 시 완전히 제거 후 재삽입 (CSS 업데이트 반영).
_EXISTING_PATCH_BLOCK_RE = re.compile(
    r"<style[^>]*data-v10-responsive-patch[^>]*>[\s\S]*?</style>\s*",
    re.IGNORECASE,
)

# stylesheet 블록 내 `min-width: Xpx` 감지 — X>320 인 경우만 치환 대상.
# capture: (min-width) (:) (space*) (X) (px)
_STYLE_BLOCK_RE = re.compile(
    r"(<style[^>]*>)([\s\S]*?)(</style>)",
    re.IGNORECASE,
)
_MIN_WIDTH_PX_RE = re.compile(
    r"(min-width\s*:\s*)(\d+)(\s*px)",
    re.IGNORECASE,
)

# 원형 / 아이콘 박스 width 자동 주입 패턴 — border-radius 선언 + height:Npx + width 누락 감지.
_CIRCLE_STYLE_ATTR_RE = re.compile(
    r'(style\s*=\s*["\'])([^"\']*?)(["\'])',
    re.IGNORECASE,
)
# border-radius 선언 감지 — 50% / Npx / 99px / clamp() / var() 등 모든 값 포함.
# border 속성 (border-top/right 등) 과 혼동 방지 위해 정확 매칭.
_BORDER_RADIUS_ANY_RE = re.compile(
    r"(?<![a-z-])border-radius\s*:\s*[^;\"']+",
    re.IGNORECASE,
)
_HEIGHT_PX_RE = re.compile(r"(?<![a-z-])height\s*:\s*(\d+)\s*px", re.IGNORECASE)
# width 속성 감지 — max-width/min-width 는 width 선언 아님 (lookbehind 로 제외).
_WIDTH_DECL_RE = re.compile(r"(?<![a-z-])width\s*:", re.IGNORECASE)
# flex 컨테이너 안에서 shrink 되는 상황 — flex 자식 특성 감지 (보조 휴리스틱).
_FLEX_ITEM_HINT_RE = re.compile(
    r"flex-shrink\s*:\s*0|flex\s*:\s*0\s+0|display\s*:\s*flex",
    re.IGNORECASE,
)


def _fix_circle_element_width(content: str) -> str:
    """border-radius 선언 + height:Npx 있지만 width 없는 inline style 에 width:Npx 주입.

    원형 (border-radius:50%) 뿐 아니라 rounded-rect 아이콘 박스 (border-radius:12px 등)
    도 함께 커버. LLM 이 height 만 선언하면 텍스트 내재 크기로 width 결정 → 세로 알약.

    조건:
      1. style 에 border-radius 선언 (50% / Npx / 변수 등 모든 값)
      2. height:Npx 명시
      3. width 선언 없음
    → width:Npx 주입 (정사각형 강제)

    Why: 아이콘 박스/아바타/배지/인디케이터 등 borderradius 로 모양 잡는 요소는
    거의 항상 정사각형 의도. 직사각형이 의도라면 LLM 이 명시적 width 를 선언함.
    멱등: 이미 width 있으면 skip.
    """
    def _patch(m: re.Match) -> str:
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        if not _BORDER_RADIUS_ANY_RE.search(body):
            return m.group(0)
        if _WIDTH_DECL_RE.search(body):
            return m.group(0)
        hm = _HEIGHT_PX_RE.search(body)
        if not hm:
            return m.group(0)
        px = hm.group(1)
        new_body = body.rstrip(";").rstrip()
        if new_body and not new_body.endswith(";"):
            new_body += ";"
        new_body += f" width: {px}px"
        return f"{prefix}{new_body}{suffix}"

    return _CIRCLE_STYLE_ATTR_RE.sub(_patch, content)


# 테이블 td data-label 자동 주입 관련 패턴.
_TABLE_BLOCK_RE = re.compile(r"(<table\b[^>]*>)([\s\S]*?)(</table>)", re.IGNORECASE)
_THEAD_BLOCK_RE = re.compile(r"<thead\b[^>]*>([\s\S]*?)</thead>", re.IGNORECASE)
_TH_TEXT_RE = re.compile(r"<th\b[^>]*>([\s\S]*?)</th>", re.IGNORECASE)
_TBODY_BLOCK_RE = re.compile(r"(<tbody\b[^>]*>)([\s\S]*?)(</tbody>)", re.IGNORECASE)
_TR_BLOCK_RE = re.compile(r"(<tr\b[^>]*>)([\s\S]*?)(</tr>)", re.IGNORECASE)
_TD_TAG_RE = re.compile(r"<td\b([^>]*)>", re.IGNORECASE)
_HAS_DATA_LABEL_RE = re.compile(r"\bdata-label\s*=", re.IGNORECASE)
_HAS_DATA_NO_REFLOW_RE = re.compile(r"\bdata-no-reflow\b", re.IGNORECASE)
# data-label 값 안전화: HTML 태그 제거 + 엔티티/쌍따옴표 이스케이프.
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_th_text(html: str) -> str:
    """<th> inner HTML 에서 태그 제거 후 공백 정규화. data-label 값으로 안전."""
    text = _TAG_STRIP_RE.sub(" ", html)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # HTML 특수문자 (entity) 는 브라우저가 처리하므로 유지. 쌍따옴표만 이스케이프.
    return text.replace('"', "&quot;")


def _inject_td_data_labels(content: str) -> str:
    """모든 data table 의 `<td>` 에 `data-label="..."` 자동 주입.

    - thead 의 th 텍스트를 인덱스 순서대로 사용.
    - 이미 data-label 이 있는 td 는 skip (LLM 이 선언한 건 유지 — 멱등).
    - `data-no-reflow` 속성 있는 table 은 skip (opt-out — 숫자 행렬 등).
    - thead 없는 table 도 skip (data table 아님 — 레이아웃 table 추정).
    - colspan 은 간단화를 위해 index 만큼만 진행 (대부분 데이터 테이블에 드묾).
    """
    def _process_table(m: re.Match) -> str:
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        # opt-out
        if _HAS_DATA_NO_REFLOW_RE.search(open_tag):
            return m.group(0)

        thead_m = _THEAD_BLOCK_RE.search(inner)
        if not thead_m:
            return m.group(0)

        headers: list[str] = [
            _clean_th_text(t) for t in _TH_TEXT_RE.findall(thead_m.group(1))
        ]
        if not headers:
            return m.group(0)

        def _process_tr(tr_m: re.Match) -> str:
            tr_open, tr_inner, tr_close = tr_m.group(1), tr_m.group(2), tr_m.group(3)
            idx = [0]  # mutable counter for nested sub

            def _patch_td(td_m: re.Match) -> str:
                attrs = td_m.group(1)
                # 이미 data-label 있으면 skip (멱등)
                if _HAS_DATA_LABEL_RE.search(attrs):
                    idx[0] += 1
                    return td_m.group(0)
                label = headers[idx[0]] if idx[0] < len(headers) else ""
                idx[0] += 1
                # 속성 앞에 data-label 주입 (기존 속성 유지)
                new_attrs = f' data-label="{label}"{attrs}' if attrs else f' data-label="{label}"'
                return f"<td{new_attrs}>"

            new_inner = _TD_TAG_RE.sub(_patch_td, tr_inner)
            return tr_open + new_inner + tr_close

        # tbody 있으면 그 안만 처리, 없으면 thead 이후 전체
        tbody_m = _TBODY_BLOCK_RE.search(inner)
        if tbody_m:
            body_open, body_inner, body_close = (
                tbody_m.group(1), tbody_m.group(2), tbody_m.group(3),
            )
            new_body = _TR_BLOCK_RE.sub(_process_tr, body_inner)
            new_inner = (
                inner[: tbody_m.start()]
                + body_open + new_body + body_close
                + inner[tbody_m.end() :]
            )
        else:
            # thead 이후 부분만 (thead 자신은 변경 안 함)
            pre = inner[: thead_m.end()]
            post = inner[thead_m.end():]
            post = _TR_BLOCK_RE.sub(_process_tr, post)
            new_inner = pre + post

        return open_tag + new_inner + close_tag

    return _TABLE_BLOCK_RE.sub(_process_table, content)


def _sanitize_stylesheet_widths(content: str) -> str:
    """<style> 블록 내부의 min-width:Xpx (X>320) 을 min(Xpx, 100%) 로 치환.

    320px 를 기준으로 잡음 (모바일 최소 뷰포트 375px 대비 여유). 조건부 @media
    내부든 아니든 동일 적용 — min() 은 viewport 보다 작으면 그대로, 크면 viewport
    로 제한하므로 모든 맥락에서 안전.

    inline style 은 sanitize_inline_styles 가 이미 처리. 본 함수는 stylesheet 전용.
    """
    def _patch_block(m: re.Match) -> str:
        head, body, tail = m.group(1), m.group(2), m.group(3)

        def _patch_decl(dm: re.Match) -> str:
            prefix, value, suffix = dm.group(1), dm.group(2), dm.group(3)
            try:
                v = int(value)
            except ValueError:
                return dm.group(0)
            if v <= 320:
                return dm.group(0)
            return f"{prefix}min({value}px, 100%)"

        body = _MIN_WIDTH_PX_RE.sub(_patch_decl, body)
        return head + body + tail

    return _STYLE_BLOCK_RE.sub(_patch_block, content)


def apply_responsive_patch(content: str) -> str:
    """HTML 문서에 컴포넌트 반응형 baseline patch 삽입 + stylesheet 폭 sanitize +
    table td data-label 자동 주입.

    멱등: 이미 data-v10-responsive-patch 주입된 경우 CSS 추가는 skip. 단 sanitize
    와 td data-label 은 매번 실행 (이미 처리된 것은 내부 조건으로 skip 되므로 안전).
    <head> 없으면 patch CSS 는 skip (fragment 는 saver 에서 wrap 이 먼저 실행).
    """
    if not content or "<" not in content:
        return content

    # 1. <table> td 에 data-label 주입 (멱등 — 이미 있으면 skip)
    content = _inject_td_data_labels(content)
    # 1-B. 원형 요소 (border-radius:50%) 의 width 누락 자동 주입 — aspect-ratio 안전망
    content = _fix_circle_element_width(content)
    # 2. stylesheet 내 큰 고정 px 치환
    content = _sanitize_stylesheet_widths(content)
    # 3. PATCH CSS 블록 주입. 이미 있으면 제거 후 최신 버전으로 교체
    # (엔진 patch 업데이트 시 resave 만으로 최신 CSS 적용되도록).
    if _ALREADY_PATCHED_RE.search(content):
        content = _EXISTING_PATCH_BLOCK_RE.sub("", content, count=1)
    if not _HEAD_OPEN_RE.search(content):
        return content
    content = _HEAD_OPEN_RE.sub(
        lambda m: m.group(1) + "\n" + _PATCH_BLOCK + "\n",
        content, count=1,
    )
    return content
