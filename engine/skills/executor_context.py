"""
Executor Context helpers — screen list injection, QA feedback loading,
DEFINE artifact context injection for BUILD tasks.

Extracted from executor.py for maintainability.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from engine.core.dag_advancer import NodeSnapshot

logger = logging.getLogger(__name__)

# ── 화면 목록 강제 주입 키워드 ──
_SCREEN_INJECT_KEYWORDS_DESIGN = ("시안", "레시피", "화면", "디자인", "조립", "UI", "페이지")
_SCREEN_INJECT_KEYWORDS_BUILD = ("프론트엔드",)

# ── 금지어 회피 가이드 (G3) ──
# 문서형 산출물 시스템 프롬프트 말미에 append 되어, LLM이 첫 시도부터
# TODO/TBD/미정 등 placeholder 어휘 대신 의미있는 대체 표현을 쓰도록 유도.
FORBIDDEN_WORDS_GUIDE = (
    "\n\n## 금지어 회피 규칙 (엄수)\n"
    "아래 단어를 산출물 본문·테이블 셀·주석 어디에도 쓰지 마세요:\n"
    "- TODO / TBD / FIXME (영어 placeholder)\n"
    "- 미정 / 작성 예정 / 추후 작성 / 추후 결정 (한국어 placeholder)\n\n"
    "대신 사용하세요:\n"
    "- 미결정 사항: '향후 설계 필요' 또는 '추후 정책 확정 시 반영'\n"
    "- 미정 값: '미지정' 또는 '별도 협의'\n"
    "- 작업 대기: '다음 단계에서 진행'\n"
    "출력 직전 본인이 작성한 전체 내용에서 위 금지어가 단 하나도 없는지 재확인하세요.\n"
)

_SCR_TABLE_RE = re.compile(
    r'SCR-(\d{3})\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+?)\s*[|｜]\s*([^|｜\n]+)',
)


async def _inject_screen_list_requirement(db, project_id: str, node_name: str) -> str:
    """화면 목록 정의서에서 전체 화면 ID/이름/URL/유형을 추출하여 프롬프트 주입 텍스트 생성.

    대상 노드: "시안", "레시피", "화면", "디자인", "조립", "UI", "페이지" 포함하는 DESIGN TASK,
               "프론트엔드" 포함하는 BUILD TASK.
    """
    try:
        # ── 키워드 필터링 ──
        _is_design = any(kw in node_name for kw in _SCREEN_INJECT_KEYWORDS_DESIGN)
        _is_build = any(kw in node_name for kw in _SCREEN_INJECT_KEYWORDS_BUILD)
        if not _is_design and not _is_build:
            return ""

        # ── DB에서 화면 목록 정의서 로드 ──
        row = await db.fetchone(
            """SELECT av.storage_path FROM artifacts a
               JOIN artifact_versions av ON a.id=av.artifact_id
               WHERE a.project_id=? AND a.node_id IN
                 (SELECT id FROM nodes WHERE name='화면 목록 정의서' AND project_id=?)
               AND av.version_num = a.current_version""",
            (project_id, project_id),
        )
        if not row or not row["storage_path"]:
            return ""

        content = row["storage_path"]

        # ── SCR-XXX | 이름 | URL | 유형 추출 ──
        screens: list[tuple] = []
        for m in _SCR_TABLE_RE.finditer(content):
            scr_id = f"SCR-{m.group(1)}"
            name = m.group(2).strip()
            url = m.group(3).strip()
            stype = m.group(4).strip()
            screens.append((scr_id, name, url, stype))

        if not screens:
            # 테이블 파싱 실패 → 간략 SCR-XXX만 추출
            _simple = re.compile(r'SCR-(\d{3})\s*[|｜]\s*([^|｜\n]+)')
            for m2 in _simple.finditer(content):
                screens.append((f"SCR-{m2.group(1)}", m2.group(2).strip(), "", ""))

        if not screens:
            return ""

        # ── 중복 방지 (idempotent) ──
        # 호출자 측에서 이미 주입된 경우 대비 — 반환값에 마커 포함
        _MARKER = "<!-- SCREEN_LIST_INJECTED -->"

        # ── 테이블 생성 (compact, 3000자 이하 유지) ──
        lines = [
            f"\n\n{_MARKER}",
            "## ⚠️ 필수 화면 목록 (전체 구현 필수 — 누락 시 QA 반려)\n",
            "화면 목록 정의서에 정의된 모든 화면을 빠짐없이 구현해야 합니다.",
            "아래 목록에서 누락된 화면이 있으면 QA에서 자동 반려됩니다.\n",
            "| ID | 화면명 | URL | 유형 |",
            "|----|--------|-----|------|",
        ]
        for scr_id, name, url, stype in screens:
            lines.append(f"| {scr_id} | {name} | {url} | {stype} |")

        total = len(screens)
        lines.append(
            f"\n**총 {total}개 화면 전부 구현 필수. "
            "관리자 페이지만 구현하고 사용자/보호자/요양보호사 화면을 누락하지 마세요.**\n"
        )

        block = "\n".join(lines)

        # 3000자 제한
        if len(block) > 3000:
            block = block[:2950] + "\n... (일부 생략) ...\n"

        return block

    except Exception as exc:
        logger.warning("_inject_screen_list_requirement failed: %s", exc)
        return ""


async def _load_previous_qa_feedback(db: Any, node: "NodeSnapshot", model_adapter: Any = None) -> str:
    """Load detailed QA failure feedback + 근본 원인 분석.

    When a TASK goes INVALID after QA failure:
    1. QA verdict에서 실패 항목 추출 (기존)
    2. Haiku로 근본 원인 분석 + 구체적 수정 지시 생성 (신규)

    Returns empty string if no relevant feedback found.
    Always wrapped in try/except at call site — never breaks existing flow.
    """
    try:
        # Find QA nodes that reference this TASK node
        qa_rows = await db.fetchall(
            """SELECT description, name FROM nodes
               WHERE project_id=? AND node_type='QA'
               AND task_pair_node_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (node.project_id, node.id),
        )
        if not qa_rows:
            return ""

        qa_desc_raw = qa_rows[0]["description"]
        if not qa_desc_raw:
            return ""

        try:
            qa_verdict = json.loads(qa_desc_raw)
        except (ValueError, TypeError):
            return ""

        if qa_verdict.get("verdict") != "FAIL":
            return ""

        # ── Build detailed feedback block ──
        lines = [
            "\n\n## ⚠️ 이전 실행에서 발견된 문제 (반드시 수정)",
            f"QA 검증 방식: {qa_verdict.get('method', 'unknown')}",
            "",
        ]

        # Failures list (harness structural, interactivity, design match)
        failures = qa_verdict.get("failures", [])
        structural_failures = qa_verdict.get("structural_failures", failures)
        if structural_failures:
            lines.append("### 감지된 결함:")
            for i, f in enumerate(structural_failures[:10], 1):
                lines.append(f"  {i}. {f}")
            lines.append("")

        # Checks with FAIL status
        checks = qa_verdict.get("checks", [])
        failed_checks = [c for c in checks if not c.get("pass", True)]
        if failed_checks:
            lines.append("### 실패한 검증 항목:")
            for c in failed_checks[:8]:
                _name = c.get("name", c.get("check", "unknown"))
                _detail = c.get("detail", c.get("message", ""))
                lines.append(f"  - {_name}: {_detail}")
            lines.append("")

        # Design compliance issues (if any)
        design_issues = qa_verdict.get("design_issues", [])
        if design_issues:
            lines.append("### 디자인 불일치:")
            for d in design_issues[:5]:
                lines.append(f"  - {d}")
            lines.append("")

        lines.append("위 문제를 모두 수정한 코드를 생성하세요.")
        lines.append("특히 이전에 누락된 항목을 빠짐없이 포함해야 합니다.\n")

        result = "\n".join(lines)

        # ── 근본 원인 분석 (Haiku) ──
        # 실패 사유 + 이전 산출물 샘플을 분석해서 구체적 수정 방법 제시
        all_failures = structural_failures + [
            f"{c.get('name', '?')}: {c.get('detail', '')}"
            for c in failed_checks
        ]
        if all_failures and model_adapter and node.retry_count > 0:
            try:
                # 이전 산출물 샘플 로드
                prev_art = await db.fetchone(
                    """SELECT SUBSTR(av.storage_path, 1, 5000) as sample
                       FROM artifacts a
                       JOIN artifact_versions av ON a.id=av.artifact_id AND av.version_num=a.current_version
                       WHERE a.node_id=?""",
                    (node.id,),
                )
                sample = prev_art["sample"] if prev_art and prev_art["sample"] else ""

                analysis_resp = await model_adapter.call(
                    model="claude-sonnet-4-6",
                    system="당신은 산출물 오류 분석 전문가입니다. 간결하게 답변하세요.",
                    prompt=(
                        f"아래 산출물이 QA에서 실패했습니다.\n\n"
                        f"실패 사유:\n" + "\n".join(f"- {f}" for f in all_failures[:5]) + "\n\n"
                        f"산출물 샘플 (처음 5KB):\n{sample}\n\n"
                        f"1. 근본 원인 (왜 이 문제가 발생했는지)\n"
                        f"2. 구체적 수정 방법 (어떻게 고쳐야 하는지)\n"
                    ),
                    max_tokens=500,
                )
                result += f"\n\n### 🔍 근본 원인 분석 및 수정 방법\n{analysis_resp.content}\n"
                logger.info("qa_root_cause_analysis node=%s", node.id[:8])
            except Exception as _rca_err:
                logger.debug("qa_root_cause_analysis_failed node=%s error=%s", node.id[:8], _rca_err)

        # Limit to 4000 chars (근본 분석 포함으로 상향)
        if len(result) > 4000:
            result = result[:3900] + "\n... (일부 생략)\n"

        return result

    except Exception as exc:
        logger.debug("_load_previous_qa_feedback error: %s", exc)
        return ""


async def _inject_define_context_for_build(db: Any, project_id: str, node_name: str) -> str:
    """Load DEFINE phase artifacts relevant to BUILD and format as prompt context.

    Injected artifacts:
    1. 사용자 흐름도 (User Flow) — page-to-page navigation understanding
    2. 기능 백로그 (Product Backlog) — features each page needs
    3. 서비스 운영 정책서 — business rules (pricing, eligibility, etc.)

    Only triggers for BUILD TASK nodes with frontend-related keywords.
    Total injected content capped at 5000 chars to stay within token budget.
    Returns empty string on any failure (graceful degradation).
    """
    try:
        # ── Keyword filter: only frontend-related BUILD tasks ──
        _frontend_keywords = ("프론트엔드", "컴포넌트", "화면", "페이지", "UI")
        if not any(kw in node_name for kw in _frontend_keywords):
            return ""

        sections = []
        total_chars = 0
        CHAR_BUDGET = 5000

        # ── 1. User Flow (사용자 흐름도) — max 2000 chars ──
        try:
            _flow_names = ['사용자 흐름도', 'User Flow']
            for fname in _flow_names:
                row = await db.fetchone(
                    """SELECT av.storage_path FROM artifacts a
                       JOIN artifact_versions av ON a.id=av.artifact_id
                       WHERE a.project_id=? AND a.node_id IN
                         (SELECT id FROM nodes WHERE name LIKE ? AND project_id=?)
                       AND av.version_num = a.current_version""",
                    (project_id, f'%{fname}%', project_id),
                )
                if row and row.get("storage_path"):
                    content = row["storage_path"]
                    # Summarize: extract numbered steps and flow names
                    summary = _summarize_flow_document(content, max_chars=2000)
                    if summary:
                        sections.append(("업무 흐름 참조 (구현 시 반드시 반영)", summary))
                        total_chars += len(summary)
                    break
        except Exception as exc:
            logger.debug("define_ctx: flow load failed: %s", exc)

        # ── 2. Product Backlog (기능 백로그) — max 1500 chars ──
        if total_chars < CHAR_BUDGET:
            try:
                _bl_names = ['기능 백로그', 'Product Backlog']
                for fname in _bl_names:
                    row = await db.fetchone(
                        """SELECT av.storage_path FROM artifacts a
                           JOIN artifact_versions av ON a.id=av.artifact_id
                           WHERE a.project_id=? AND a.node_id IN
                             (SELECT id FROM nodes WHERE name LIKE ? AND project_id=?)
                           AND av.version_num = a.current_version""",
                        (project_id, f'%{fname}%', project_id),
                    )
                    if row and row.get("storage_path"):
                        content = row["storage_path"]
                        remaining = min(1500, CHAR_BUDGET - total_chars)
                        summary = _summarize_backlog(content, max_chars=remaining)
                        if summary:
                            sections.append(("주요 기능 요구사항", summary))
                            total_chars += len(summary)
                        break
            except Exception as exc:
                logger.debug("define_ctx: backlog load failed: %s", exc)

        # ── 3. Service Policy (서비스 운영 정책서) — max 1500 chars ──
        if total_chars < CHAR_BUDGET:
            try:
                _policy_names = ['서비스 운영 정책서', '운영 정책', '비즈니스 규칙']
                for fname in _policy_names:
                    row = await db.fetchone(
                        """SELECT av.storage_path FROM artifacts a
                           JOIN artifact_versions av ON a.id=av.artifact_id
                           WHERE a.project_id=? AND a.node_id IN
                             (SELECT id FROM nodes WHERE name LIKE ? AND project_id=?)
                           AND av.version_num = a.current_version""",
                        (project_id, f'%{fname}%', project_id),
                    )
                    if row and row.get("storage_path"):
                        content = row["storage_path"]
                        remaining = min(1500, CHAR_BUDGET - total_chars)
                        summary = _summarize_policy(content, max_chars=remaining)
                        if summary:
                            sections.append(("비즈니스 규칙", summary))
                            total_chars += len(summary)
                        break
            except Exception as exc:
                logger.debug("define_ctx: policy load failed: %s", exc)

        if not sections:
            return ""

        # ── Format output ──
        _MARKER = "<!-- DEFINE_CONTEXT_INJECTED -->"
        lines = [f"\n\n{_MARKER}"]
        for title, body in sections:
            lines.append(f"\n## {title}")
            lines.append(body)

        lines.append(
            "\n**위 업무 흐름과 기능 요구사항을 코드에 반드시 반영하세요. "
            "페이지 간 네비게이션, 상태 전이, 비즈니스 규칙이 실제로 동작해야 합니다.**\n"
        )

        lines.append(
            "\n## Next.js App Router 명명 규칙 (STRICT)\n"
            "- 동일 경로 내에서 `[param]` 세그먼트 이름은 유니크해야 합니다. "
            "중첩 상세 라우트는 리소스명 접미사를 사용하세요 (예: `[clientId]`, "
            "`[careLogId]`). `admin/clients/[id]/care-logs/[id]`처럼 `[id]`가 두 번 "
            "반복되면 Next.js가 기동 자체를 거부합니다.\n"
            "- 폴더명은 영숫자 + 하이픈만 허용합니다. 공백, `{`, `}`, `$`, "
            "백틱 등의 문자가 폴더명에 들어가면 안 됩니다 (예: `{data`는 금지).\n"
            "- 화면 이름을 추론하지 못해 `grp-01`, `screen-02`, `page-03` 같은 "
            "플레이스홀더 이름을 쓰지 마세요. 반드시 화면목록의 실제 화면명을 기반으로 "
            "의미 있는 slug를 만드세요.\n"
            "- 같은 리소스를 두 경로로 만들지 마세요 (예: `admin-caregivers`와 "
            "`admin/caregivers`를 동시에 만들면 중복). 한 리소스는 한 경로 트리로만.\n"
        )

        result = "\n".join(lines)
        if len(result) > CHAR_BUDGET + 200:
            result = result[:CHAR_BUDGET] + "\n... (생략)\n"

        return result

    except Exception as exc:
        logger.warning("_inject_define_context_for_build failed: %s", exc)
        return ""


def _summarize_flow_document(content: str, max_chars: int = 2000) -> str:
    """Extract key flow steps from a User Flow document, compact format."""
    import re as _re

    # Strip HTML tags
    text = _re.sub(r'<[^>]+>', ' ', content)
    text = _re.sub(r'\s+', ' ', text).strip()

    # Find numbered flow sequences
    flows = []
    # Pattern: sequences connected by → or numbered steps
    arrow_flows = _re.findall(r'([\w가-힣\s]+(?:\s*→\s*[\w가-힣\s]+){2,})', text)
    for af in arrow_flows:
        flows.append(af.strip())

    # Also extract numbered items
    numbered = _re.findall(r'(?:\d+[.)]\s*)(.+?)(?=\d+[.)]|\Z)', text)
    if numbered and not flows:
        flow = " → ".join(n.strip()[:40] for n in numbered[:8])
        if flow:
            flows.append(flow)

    if not flows:
        # Fallback: just truncate the raw text
        return text[:max_chars]

    result = ""
    for i, f in enumerate(flows, 1):
        line = f"{i}. {f}\n"
        if len(result) + len(line) > max_chars:
            break
        result += line

    return result.strip()


def _summarize_backlog(content: str, max_chars: int = 1500) -> str:
    """Extract feature list from a Product Backlog document."""
    import re as _re

    text = _re.sub(r'<[^>]+>', ' ', content)
    text = _re.sub(r'\s+', ' ', text).strip()

    # Find feature items (- Feature: description, or numbered features)
    features = _re.findall(
        r'[-*]\s*([\w가-힣][^-*\n]{10,80})',
        content,
    )
    if not features:
        features = _re.findall(
            r'\d+[.)]\s*([\w가-힣][^\n]{10,80})',
            content,
        )

    if features:
        result = ""
        for f in features:
            line = f"- {f.strip()}\n"
            if len(result) + len(line) > max_chars:
                break
            result += line
        return result.strip()

    # Fallback
    return text[:max_chars]


def _summarize_policy(content: str, max_chars: int = 1500) -> str:
    """Extract key business rules from a service policy document."""
    import re as _re

    text = _re.sub(r'<[^>]+>', ' ', content)
    text = _re.sub(r'\s+', ' ', text).strip()

    # Find rule-like patterns (numbered rules, bullet points with numbers/percentages)
    rules = _re.findall(
        r'[-*]\s*([\w가-힣].*?(?:\d+%|\d+시간|\d+원|\d+등급|필수|금지|제한|허용)[^\n]{0,60})',
        content,
    )
    if not rules:
        rules = _re.findall(
            r'([\w가-힣].*?(?:\d+%|\d+시간|\d+원|정산|수가|등급|기준|요건|조건)[^\n]{0,40})',
            text,
        )

    if rules:
        result = ""
        for r in rules:
            line = f"- {r.strip()}\n"
            if len(result) + len(line) > max_chars:
                break
            result += line
        return result.strip()

    # Fallback
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Harness 구조 요구사항 선제 주입
# ---------------------------------------------------------------------------
# 배경: engine/skills/qa/harness.py 의 _harness_validate_programmatic 가
# 프론트엔드 BUILD 산출물을 **자동 검증**한다. 검증 기준을 AI에게 미리 알려주지
# 않으면 같은 실수(interface 누락, export default 누락 등)를 반복한다.
# 이 함수는 harness 규칙 ↔ 생성 프롬프트를 동기화해 반복 실패를 구조적으로 차단.
#
# 범용성: 특정 프로젝트가 아니라 "프론트엔드 성격의 BUILD TASK" 전체에 적용.
# Harness 규칙이 바뀌면 본 함수의 문구도 같이 업데이트할 것.

_HARNESS_FRONTEND_KEYWORDS = ("프론트엔드", "컴포넌트", "페이지", "UI", "화면")
_HARNESS_REQ_MARKER = "<!-- HARNESS_REQUIREMENTS_INJECTED -->"


async def _inject_harness_structural_requirements(node: "NodeSnapshot") -> str:
    """BUILD 프론트엔드 TASK 산출물이 harness 자동 검증을 통과하도록,
    Harness가 보는 체크 항목을 프롬프트에 선제적으로 명시한다.

    반환: 주입 블록 문자열. 대상 아니면 빈 문자열.
    """
    try:
        if getattr(node, "phase", "") != "BUILD":
            return ""
        if getattr(node, "node_type", "") != "TASK":
            return ""
        _name = getattr(node, "name", "") or ""
        if not any(kw in _name for kw in _HARNESS_FRONTEND_KEYWORDS):
            return ""

        return (
            f"{_HARNESS_REQ_MARKER}\n"
            "# ⛔ 절대 규칙 — 아래 6개 조건을 모두 만족하지 않으면 응답을 종료하지 마세요\n"
            "\n"
            "이 조건은 프로그래매틱 자동 검증을 거치며, 하나라도 누락되면 코드를 읽지 않고 "
            "즉시 재생성 요청됩니다. 재생성은 **100% 같은 프롬프트**로 이뤄지므로 "
            "이번 응답에서 반드시 통과시키세요.\n"
            "\n"
            "1. **`// FILE: path/file.tsx` 태그** — 각 파일 시작 첫 줄. CSS 는 `/* FILE: ... */`.\n"
            "2. **`export default Component`** — 모든 .tsx/.jsx 컴포넌트 파일 필수.\n"
            "3. **JSX 태그/중괄호 밸런스** — 미완성 `<Foo`, 누락된 `}` 금지.\n"
            "4. **`import` 문 1개 이상** — 모든 .ts/.tsx 파일.\n"
            "5. **TypeScript `interface` 또는 `type` 정의 1개 이상** — "
            "컴포넌트 코드에 `interface Props {...}` 또는 `type X = ...` 중 최소 1개. "
            "코드 상단 import 바로 아래에 배치. 예:\n"
            "   ```tsx\n"
            "   interface Props { title: string; onSubmit: () => void; }\n"
            "   export default function MyComp({ title, onSubmit }: Props) { ... }\n"
            "   ```\n"
            "6. **placement 일치** — 화면 목록/레시피에 명시된 슬러그는 실제 코드에 그대로 존재.\n"
            "\n"
            "## 제출 전 셀프체크 (꼭 수행):\n"
            "- [ ] 모든 .tsx 파일에 // FILE: 태그가 있나?\n"
            "- [ ] 모든 .tsx 파일에 export default 가 있나?\n"
            "- [ ] 모든 .ts/.tsx 파일에 import 문이 있나?\n"
            "- [ ] 모든 .tsx 파일에 interface 또는 type 선언이 있나?\n"
            "- [ ] 위 4개를 하나라도 빠뜨렸다면 그 파일만 다시 수정 후 제출\n"
            "\n"
            "위 절대 규칙을 위반하면 이번 응답 전체가 버려지고 다시 요청됩니다. "
            "지금 작성한 코드를 제출 직전에 한 번 더 훑어 6개 조건을 직접 확인하세요.\n"
            "\n"
        )
    except Exception as exc:
        logger.debug("_inject_harness_structural_requirements error: %s", exc)
        return ""
