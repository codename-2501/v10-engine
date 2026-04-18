"""
engine/intake/reference_analyzer.py
인테이크 레퍼런스 URL 크롤링 + AI 분석.

인테이크 폼의 references / competitors 필드에서 URL을 추출하고,
각 URL의 페이지를 가져와서 AI로 요약 분석한 결과를
raw_json에 `_reference_analysis` 키로 추가.

context_assembler가 global_context에 이 데이터를 포함시키므로
PRD/기능명세 등 하위 산출물에서 경쟁사·벤치마크 정보를 활용할 수 있음.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# URL 추출 정규식
_URL_RE = re.compile(
    r'https?://[^\s<>"\')\]，,]+',
    re.IGNORECASE,
)

# 크롤링 제한
_MAX_URLS = 5           # 최대 분석 URL 수
_FETCH_TIMEOUT = 10     # 초
_MAX_CONTENT_LEN = 50_000  # 페이지 본문 최대 바이트


async def analyze_references(raw: dict, adapter: Any = None) -> list[dict]:
    """인테이크 데이터에서 URL을 추출하고 크롤링 + AI 분석.

    Args:
        raw:     인테이크 raw_json dict.
        adapter: AI 호출용 CLIProxyAdapter (None이면 AI 요약 건너뜀).

    Returns:
        분석 결과 리스트. 각 항목:
        {
            "url": "https://...",
            "title": "페이지 제목",
            "description": "메타 설명",
            "content_summary": "AI 요약 (있으면)",
            "features": ["기능1", "기능2", ...],
            "ux_notes": "UX 특징",
            "fetch_error": null | "에러 메시지"
        }
    """
    # 1. URL 수집 — 구조화 데이터(reference_urls) 우선 + 텍스트 폴백
    url_entries: list[dict] = []  # [{url, purpose, note}]
    seen_urls = set()

    # 1a. 구조화된 reference_urls (관리자 폼) + references 배열 (공개 폼)
    for ref in raw.get("reference_urls", []) + (
        raw.get("references", []) if isinstance(raw.get("references"), list) else []
    ):
        url = ref.get("url", "").strip()
        if url and url.startswith("http") and url not in seen_urls:
            seen_urls.add(url)
            url_entries.append({
                "url": url,
                "purpose": ref.get("purpose", "both"),
                "note": ref.get("note") or ref.get("reason", ""),
            })

    # 1b. 텍스트 필드에서 URL 추출 (구 폼 호환 — 문자열인 경우만)
    for field in ("references", "competitors", "additional_desc",
                  "description", "tech_notes"):
        text = raw.get(field, "")
        if isinstance(text, str) and text:
            for url in _URL_RE.findall(text):
                if url not in seen_urls:
                    seen_urls.add(url)
                    # 텍스트 출처에서 purpose 추론
                    purpose = "competitor" if field == "competitors" else "both"
                    url_entries.append({"url": url, "purpose": purpose, "note": ""})

    if not url_entries:
        return []

    url_entries = url_entries[:_MAX_URLS]
    logger.info("reference_analyzer: %d URLs found", len(url_entries))

    # 2. 크롤링
    results = []
    for entry in url_entries:
        page = await _fetch_page(entry["url"])
        page["purpose"] = entry["purpose"]
        page["note"] = entry["note"]
        results.append(page)

    # 3. AI 요약 — purpose별 프롬프트 분기
    if adapter:
        for page in results:
            if page.get("fetch_error") or not page.get("text_content"):
                continue
            try:
                summary = await _ai_summarize(adapter, page, page["purpose"])
                page.update(summary)
            except Exception as e:
                logger.warning("reference_ai_summary_failed url=%s error=%s",
                               page["url"], str(e))

    # text_content는 너무 크니 최종 결과에서 제거
    for page in results:
        page.pop("text_content", None)
        page.pop("raw_html", None)

    return results


async def _fetch_page(url: str) -> dict:
    """URL을 크롤링하여 제목, 메타 설명, 본문 텍스트 추출."""
    import aiohttp
    from lxml import html as lxml_html

    result = {
        "url": url,
        "title": "",
        "description": "",
        "text_content": "",
        "fetch_error": None,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; AXFactory/1.0; intake-analyzer)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            }
            async with session.get(url, headers=headers, allow_redirects=True,
                                   max_redirects=5) as resp:
                if resp.status != 200:
                    result["fetch_error"] = f"HTTP {resp.status}"
                    return result

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    result["fetch_error"] = f"비 HTML 응답: {content_type}"
                    return result

                body = await resp.read()
                if len(body) > _MAX_CONTENT_LEN:
                    body = body[:_MAX_CONTENT_LEN]

        # HTML 파싱
        tree = lxml_html.fromstring(body)

        # 제목
        title_el = tree.find(".//title")
        if title_el is not None and title_el.text:
            result["title"] = title_el.text.strip()[:200]

        # 메타 설명
        for meta in tree.findall(".//meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            if name in ("description", "og:description"):
                content = meta.get("content", "").strip()
                if content:
                    result["description"] = content[:500]
                    break

        # 본문 텍스트 추출 (nav, header, footer, script, style 제거)
        for tag in tree.iter("script", "style", "nav", "footer", "header", "noscript"):
            tag.getparent().remove(tag)
        text = tree.text_content()
        # 빈 줄 정리
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        result["text_content"] = "\n".join(lines)[:8000]

    except Exception as e:
        result["fetch_error"] = str(e)[:200]

    return result


async def _ai_summarize(adapter: Any, page: dict, purpose: str = "both") -> dict:
    """AI로 페이지 내용을 purpose별로 분기 분석.

    purpose:
        "design"     → UI/UX, 색상, 레이아웃, 타이포그래피, 톤앤매너
        "feature"    → 기능 목록, UX 플로우, API, 사용자 시나리오
        "competitor" → 경쟁사 분석 (기능 + 강점/약점)
        "both"       → 디자인 + 기능 통합 분석
    """
    header = f"""URL: {page['url']}
제목: {page['title']}
설명: {page['description']}

본문 (일부):
{page['text_content'][:5000]}
"""

    if purpose == "design":
        prompt = f"""아래 웹사이트를 **디자인 관점**으로 분석하세요.

{header}
JSON으로만 응답:
{{"content_summary":"이 서비스 2~3문장 요약","color_scheme":"주요 색상 팔레트 (hex 포함)","typography":"폰트/글씨 크기 특징","layout_pattern":"레이아웃 패턴 (카드형/리스트형/대시보드 등)","design_tone":"디자인 톤 (따뜻한/모던/미니멀/포멀 등)","nav_structure":"네비게이션 구조 (탭/사이드바/햄버거 등)","accessibility":"접근성 특징 (고대비/큰글씨/음성 등)","design_strengths":["강점 2~3개"],"design_weaknesses":["약점 2~3개 — 우리가 개선할 포인트"]}}"""

    elif purpose == "feature":
        prompt = f"""아래 웹사이트를 **기능 관점**으로 분석하세요.

{header}
JSON으로만 응답:
{{"content_summary":"이 서비스 2~3문장 요약","features":["주요 기능 최대 10개"],"user_flows":["핵심 사용자 흐름 3~5개 (예: 회원가입→서비스 신청→결제)"],"api_integrations":["외부 연동 서비스 (있으면)"],"pricing":"요금 정보 (있으면)","strengths":["기능적 강점 2~3개"],"weaknesses":["기능적 약점 2~3개 — 우리가 보완할 포인트"]}}"""

    elif purpose == "competitor":
        prompt = f"""아래는 경쟁사 웹사이트입니다. **경쟁 분석** 관점으로 분석하세요.

{header}
JSON으로만 응답:
{{"content_summary":"이 서비스 2~3문장 요약","target_users":"타겟 사용자","features":["주요 기능 최대 10개"],"pricing":"요금 정보 (있으면)","market_position":"시장 포지셔닝 (프리미엄/가성비/공공 등)","strengths":["강점 2~3개"],"weaknesses":["약점 2~3개 — PRD에서 차별화 포인트로 활용"],"differentiation_opportunities":["우리가 이 경쟁사 대비 차별화할 수 있는 기회 2~3개"]}}"""

    else:  # "both"
        prompt = f"""아래 웹사이트를 **디자인 + 기능** 통합 관점으로 분석하세요.

{header}
JSON으로만 응답:
{{"content_summary":"이 서비스 2~3문장 요약","features":["주요 기능 최대 10개"],"design_tone":"디자인 톤","color_scheme":"주요 색상","layout_pattern":"레이아웃 패턴","nav_structure":"네비게이션 구조","ux_notes":"UI/UX 특징","pricing":"요금 정보 (있으면)","strengths":["강점 2~3개"],"weaknesses":["약점 2~3개"]}}"""

    resp = await adapter.call(
        model="claude-haiku-4-5-20251001",
        prompt=prompt,
        max_tokens=1200,
        temperature=0,
    )

    import json
    text = resp.content
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            result = json.loads(json_match.group())
            result["analysis_type"] = purpose
            return result
        except (json.JSONDecodeError, TypeError):
            pass

    return {"content_summary": text[:500], "analysis_type": purpose}
