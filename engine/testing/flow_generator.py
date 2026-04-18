"""
flow_generator.py — Generate scenario-based Playwright test configs from DEFINE phase artifacts.

Reads User Flow (사용자 흐름도), Product Backlog (기능 백로그), and page definitions
to auto-generate cross-page user scenario tests executable by playwright_runner.js.

Universal: works for any project type. All operations wrapped in try/except
for graceful degradation — returns empty list on any failure.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Max scenarios to generate (prevent runaway)
MAX_SCENARIOS = 10
# Max steps per scenario
MAX_STEPS_PER_SCENARIO = 15

# ── Action pattern matchers (Korean + English) ──
_GOTO_PATTERNS = re.compile(
    r'(이동|접속|방문|화면으로|페이지로|navigate|goto|go\s+to|open)',
    re.IGNORECASE,
)
_CLICK_PATTERNS = re.compile(
    r'(클릭|누름|눌러|선택|탭|버튼을?\s*클릭|click|tap|press|select)',
    re.IGNORECASE,
)
_FILL_PATTERNS = re.compile(
    r'(입력|작성|기입|채움|fill|type|enter|input|write)',
    re.IGNORECASE,
)
_EXPECT_PATTERNS = re.compile(
    r'(확인|표시|노출|나타남|보임|검증|verify|expect|see|show|display|appear|visible)',
    re.IGNORECASE,
)
_SUBMIT_PATTERNS = re.compile(
    r'(저장|등록|제출|완료|승인|submit|save|register|confirm|approve)',
    re.IGNORECASE,
)

# ── URL slug extraction from step descriptions ──
_URL_HINT_RE = re.compile(
    r'[/]([\w-]+(?:/[\w-]+)*)',
)
_PAGE_NAME_RE = re.compile(
    r'[「「\'"]([^」」\'"]+)[」」\'"]|(?:(\S+)\s*(?:화면|페이지|목록|상세|등록|관리))',
)


def _extract_page_url_from_step(step_text: str, page_map: dict[str, str]) -> str | None:
    """Try to extract a page URL from a step description using known page map."""
    # Direct URL reference
    url_match = _URL_HINT_RE.search(step_text)
    if url_match:
        candidate = "/" + url_match.group(1)
        # Validate it looks like a real route
        if len(candidate) > 2 and not candidate.endswith(('.js', '.css', '.png')):
            return candidate

    # Match against known page names
    for page_name, page_url in page_map.items():
        if page_name in step_text:
            return page_url

    return None


def _extract_selector_from_step(step_text: str) -> str:
    """Extract a likely CSS selector from a step description."""
    # Button text extraction
    btn_match = re.search(
        r'[「\'"]([^」\'"]+)[」\'"].*(?:버튼|button)|(?:버튼|button).*[「\'"]([^」\'"]+)[」\'"]',
        step_text, re.IGNORECASE,
    )
    if btn_match:
        btn_text = btn_match.group(1) or btn_match.group(2)
        return f'button:has-text("{btn_text}")'

    # Generic button keyword
    btn_keyword = re.search(
        r'(저장|등록|추가|삭제|수정|확인|취소|승인|검색|조회|완료|신규|배정)',
        step_text,
    )
    if btn_keyword:
        return f'button:has-text("{btn_keyword.group(1)}")'

    # Input field
    field_match = re.search(
        r'(이름|성명|전화|연락처|주소|이메일|비밀번호|제목|내용|name|email|phone|title|address|password)',
        step_text, re.IGNORECASE,
    )
    if field_match:
        field = field_match.group(1).lower()
        field_map = {
            '이름': 'name', '성명': 'name', '전화': 'phone', '연락처': 'phone',
            '주소': 'address', '이메일': 'email', '비밀번호': 'password',
            '제목': 'title', '내용': 'content',
        }
        en_field = field_map.get(field, field)
        return f'input[name="{en_field}"], input[placeholder*="{field_match.group(1)}"]'

    return 'body'


def _extract_value_from_step(step_text: str) -> str:
    """Extract a likely input value from a step description."""
    # Quoted value
    val_match = re.search(r'[「\'"]([^」\'"]+)[」\'"]', step_text)
    if val_match:
        return val_match.group(1)
    return "테스트 데이터"


def _parse_flow_step(step_text: str, page_map: dict[str, str]) -> dict | None:
    """Convert a single flow step description into a Playwright action dict."""
    step_text = step_text.strip()
    if not step_text or len(step_text) < 3:
        return None

    # Determine action type by priority
    if _GOTO_PATTERNS.search(step_text):
        url = _extract_page_url_from_step(step_text, page_map)
        if url:
            return {"action": "goto", "url": url}

    if _SUBMIT_PATTERNS.search(step_text):
        selector = _extract_selector_from_step(step_text)
        return {"action": "click", "selector": selector}

    if _FILL_PATTERNS.search(step_text):
        selector = _extract_selector_from_step(step_text)
        value = _extract_value_from_step(step_text)
        return {"action": "fill", "selector": selector, "value": value}

    if _CLICK_PATTERNS.search(step_text):
        selector = _extract_selector_from_step(step_text)
        return {"action": "click", "selector": selector}

    if _EXPECT_PATTERNS.search(step_text):
        selector = _extract_selector_from_step(step_text)
        # Try to find expected text
        text_match = re.search(r'[「\'"]([^」\'"]+)[」\'"]', step_text)
        result = {"action": "expect", "selector": selector}
        if text_match:
            result["text"] = text_match.group(1)
        return result

    return None


def _parse_flow_document(content: str) -> list[dict]:
    """Parse a User Flow document (HTML/Markdown) into named flow sequences.

    Returns list of {"name": str, "steps": [str]}.
    """
    flows = []

    # Try to find numbered flow sections
    # Pattern: "흐름 1: ...", "Flow 1: ...", "## 1. ...", "### 흐름: ..."
    sections = re.split(
        r'(?:^|\n)(?:#{1,3}\s*)?(?:흐름\s*\d+|Flow\s*\d+|시나리오\s*\d+|Scenario\s*\d+)[.:：]\s*',
        content,
        flags=re.IGNORECASE,
    )

    if len(sections) <= 1:
        # Try splitting by h2/h3 headers
        sections = re.split(r'\n#{2,3}\s+', content)

    if len(sections) <= 1:
        # Try splitting by bold/strong markers
        sections = re.split(r'\n\*\*[^*]+\*\*\n', content)

    for section in sections:
        section = section.strip()
        if not section or len(section) < 20:
            continue

        # Extract flow name from first line
        first_line = section.split('\n')[0].strip()
        flow_name = re.sub(r'[#*`<>]', '', first_line).strip()[:80]
        if not flow_name:
            flow_name = "자동 생성 흐름"

        # Extract numbered steps
        step_texts = re.findall(
            r'(?:^|\n)\s*(?:\d+[.)]\s*|[-*]\s+|→\s*|step\s*\d+[.:]\s*)(.*?)(?=\n|$)',
            section,
            re.IGNORECASE,
        )

        if len(step_texts) >= 2:
            flows.append({
                "name": flow_name,
                "steps": [s.strip() for s in step_texts if s.strip()],
            })

    return flows[:MAX_SCENARIOS]


def _build_page_map_from_screen_list(screen_list_content: str) -> dict[str, str]:
    """Build {page_name: url} map from screen list artifact."""
    page_map = {}
    # SCR-XXX | name | url | type
    for m in re.finditer(
        r'SCR-\d{3}\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+?)(?:\s*[|｜]|$)',
        screen_list_content,
    ):
        name = m.group(1).strip()
        url = m.group(2).strip()
        if url and url.startswith('/'):
            page_map[name] = url
    return page_map


async def generate_user_flow_tests(
    db: Any,
    project_id: str,
) -> list[dict]:
    """Read DEFINE artifacts and generate scenario-based test configs.

    Returns list of test scenarios compatible with playwright_runner.js userScenarios:
    [
        {
            "name": "scenario name",
            "steps": [
                {"action": "goto", "url": "/path"},
                {"action": "click", "selector": "button:has-text('text')"},
                ...
            ]
        }
    ]

    Graceful degradation: returns empty list on any failure.
    """
    try:
        scenarios = []

        # ── 1. Load page map from screen list ──
        page_map: dict[str, str] = {}
        try:
            row = await db.fetchone(
                """SELECT av.storage_path FROM artifacts a
                   JOIN artifact_versions av ON a.id=av.artifact_id
                   WHERE a.project_id=? AND a.node_id IN
                     (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
                   ORDER BY av.version_num DESC LIMIT 1""",
                (project_id, project_id),
            )
            if row and row.get("storage_path"):
                page_map = _build_page_map_from_screen_list(row["storage_path"])
        except Exception as exc:
            logger.debug("flow_gen: screen_list load failed: %s", exc)

        # ── 2. Load User Flow artifact (사용자 흐름도) ──
        flow_content = ""
        try:
            _flow_names = ['사용자 흐름도', '사용자 흐름도 (User Flow)', 'User Flow']
            for fname in _flow_names:
                row = await db.fetchone(
                    """SELECT av.storage_path FROM artifacts a
                       JOIN artifact_versions av ON a.id=av.artifact_id
                       WHERE a.project_id=? AND a.node_id IN
                         (SELECT id FROM nodes WHERE name LIKE ? AND project_id=?)
                       ORDER BY av.version_num DESC LIMIT 1""",
                    (project_id, f'%{fname}%', project_id),
                )
                if row and row.get("storage_path"):
                    flow_content = row["storage_path"]
                    break
        except Exception as exc:
            logger.debug("flow_gen: user_flow load failed: %s", exc)

        # ── 3. Load Product Backlog (기능 백로그) as supplementary ──
        backlog_content = ""
        try:
            _bl_names = ['기능 백로그', '기능 백로그 (Product Backlog)', 'Product Backlog']
            for fname in _bl_names:
                row = await db.fetchone(
                    """SELECT av.storage_path FROM artifacts a
                       JOIN artifact_versions av ON a.id=av.artifact_id
                       WHERE a.project_id=? AND a.node_id IN
                         (SELECT id FROM nodes WHERE name LIKE ? AND project_id=?)
                       ORDER BY av.version_num DESC LIMIT 1""",
                    (project_id, f'%{fname}%', project_id),
                )
                if row and row.get("storage_path"):
                    backlog_content = row["storage_path"]
                    break
        except Exception as exc:
            logger.debug("flow_gen: backlog load failed: %s", exc)

        # ── 4. Parse flows from User Flow document ──
        if flow_content:
            raw_flows = _parse_flow_document(flow_content)

            for raw_flow in raw_flows:
                steps = []
                for step_text in raw_flow["steps"][:MAX_STEPS_PER_SCENARIO]:
                    action = _parse_flow_step(step_text, page_map)
                    if action:
                        steps.append(action)

                if len(steps) >= 2:
                    # Ensure scenario starts with a goto
                    if steps[0]["action"] != "goto":
                        # Try to infer from first page in page_map
                        if page_map:
                            first_url = list(page_map.values())[0]
                            steps.insert(0, {"action": "goto", "url": first_url})

                    scenarios.append({
                        "name": raw_flow["name"],
                        "steps": steps,
                    })

        # ── 5. Generate basic navigation scenarios from page map (if no flow doc) ──
        if not scenarios and page_map:
            # Generate a simple "visit all pages" scenario
            nav_steps = []
            for name, url in list(page_map.items())[:10]:
                nav_steps.append({"action": "goto", "url": url})
                nav_steps.append({
                    "action": "expect",
                    "selector": "body",
                })
            if nav_steps:
                scenarios.append({
                    "name": "전체 페이지 네비게이션 검증",
                    "steps": nav_steps,
                })

        logger.info(
            "flow_gen: project=%s scenarios=%d page_map=%d",
            project_id[:8] if project_id else "?",
            len(scenarios),
            len(page_map),
        )

        return scenarios[:MAX_SCENARIOS]

    except Exception as exc:
        logger.warning("flow_gen: generate_user_flow_tests failed: %s", exc)
        return []
