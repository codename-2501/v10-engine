"""Accessibility audit — WCAG 검증 advisory.

Puppeteer 사용 가능 시 axe-core로 실측, 아니면 정적 HTML 검사로 대체.
기존 visual_check.py의 브라우저 인프라를 공유하여 토큰 0·런타임만 추가.

검사 항목:
- <img> alt 속성 누락
- <button> 텍스트 내용 없음 (aria-label 없음)
- <label> for 속성과 input id 불일치
- heading 순서 (h1→h2→h3, h1 없음 또는 중복)
- form <label> 없는 input
- color-contrast (정적으로는 불가, axe-core 있을 때만)

실패는 warnings에 누적, 배포 차단 아님.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class A11yFinding:
    rule: str
    file: str
    message: str
    line: int | None = None


@dataclass
class A11yReport:
    findings: list[A11yFinding] = field(default_factory=list)
    files_scanned: int = 0

    def to_warning_entries(self) -> list[str]:
        return [
            f"a11y[{f.rule}]: {Path(f.file).name}:{f.line or '?'} {f.message[:100]}"
            for f in self.findings
        ]


def _check_img_alt(text: str, file: str) -> list[A11yFinding]:
    findings = []
    # <img ...> 중 alt 속성이 없거나 비어있는 것
    for m in re.finditer(r"<img\b([^>]*)>", text, re.IGNORECASE):
        attrs = m.group(1)
        alt_match = re.search(r"""alt\s*=\s*['"]([^'"]*)['"]""", attrs, re.IGNORECASE)
        if alt_match is None:
            # decorative image는 alt="" 이면 OK, 아예 없으면 문제
            line = text[: m.start()].count("\n") + 1
            findings.append(
                A11yFinding("img-alt-missing", file, "<img> lacks alt attribute", line)
            )
    return findings


def _check_button_label(text: str, file: str) -> list[A11yFinding]:
    findings = []
    # 빈 <button></button> 또는 아이콘만 있는 button without aria-label
    for m in re.finditer(
        r"<button\b([^>]*)>([\s\S]*?)</button>", text, re.IGNORECASE,
    ):
        attrs = m.group(1)
        content = m.group(2).strip()
        has_aria = bool(re.search(r"""aria-label\s*=\s*['"][^'"]+['"]""", attrs, re.IGNORECASE))
        has_text = bool(re.search(r"[가-힣A-Za-z0-9]", re.sub(r"<[^>]+>", "", content)))
        if not has_aria and not has_text:
            line = text[: m.start()].count("\n") + 1
            findings.append(
                A11yFinding(
                    "button-label", file,
                    "button without aria-label or text content", line,
                )
            )
    return findings


def _check_heading_order(text: str, file: str) -> list[A11yFinding]:
    findings = []
    headings = [
        (int(m.group(1)), m.start())
        for m in re.finditer(r"<h([1-6])\b", text, re.IGNORECASE)
    ]
    if not headings:
        return findings
    # h1 있는지
    if not any(h[0] == 1 for h in headings):
        findings.append(
            A11yFinding("heading-h1-missing", file, "no <h1> on page", 1)
        )
    # h1 2개 이상
    h1_count = sum(1 for h in headings if h[0] == 1)
    if h1_count > 1:
        findings.append(
            A11yFinding(
                "heading-h1-duplicate", file,
                f"{h1_count} <h1> tags (should be 1 per page)", None,
            )
        )
    # 순서 건너뜀 (h1 -> h3 without h2)
    prev_level = 0
    for level, start in headings:
        if prev_level and level > prev_level + 1:
            line = text[:start].count("\n") + 1
            findings.append(
                A11yFinding(
                    "heading-order-skip", file,
                    f"h{level} appears after h{prev_level} (skipped h{prev_level+1})",
                    line,
                )
            )
        prev_level = max(prev_level, level)
    return findings


def _check_label_input(text: str, file: str) -> list[A11yFinding]:
    findings = []
    # <input ...> 중 id 있는데 해당 id를 for로 가리키는 label이 없는 경우
    input_ids = {
        m.group(1): m.start()
        for m in re.finditer(
            r"""<input\b[^>]*\bid\s*=\s*['"]([^'"]+)['"]""", text, re.IGNORECASE,
        )
    }
    label_fors = set(
        re.findall(r"""<label\b[^>]*\bfor\s*=\s*['"]([^'"]+)['"]""", text, re.IGNORECASE)
    )
    for inp_id, start in input_ids.items():
        if inp_id not in label_fors:
            # aria-label 있는지도 확인 (대안)
            input_match = re.search(
                rf"""<input\b[^>]*\bid\s*=\s*['"]{re.escape(inp_id)}['"][^>]*>""",
                text, re.IGNORECASE,
            )
            if input_match and re.search(
                r"""aria-label\s*=\s*['"][^'"]+['"]""",
                input_match.group(0), re.IGNORECASE,
            ):
                continue
            line = text[:start].count("\n") + 1
            findings.append(
                A11yFinding(
                    "input-label", file,
                    f"input#{inp_id} has no matching <label for> or aria-label",
                    line,
                )
            )
    return findings


def scan_workspace_a11y(workspace_path: Path) -> A11yReport:
    """워크스페이스의 HTML/TSX 파일을 정적 분석하여 a11y 이슈 감지."""
    findings: list[A11yFinding] = []
    count = 0

    scan_paths: list[Path] = []
    preview_dir = workspace_path / "preview"
    if preview_dir.is_dir():
        scan_paths.extend(preview_dir.glob("*.html"))

    fe_src = workspace_path / "frontend" / "src"
    if fe_src.is_dir():
        scan_paths.extend(fe_src.rglob("*.tsx"))
        scan_paths.extend(fe_src.rglob("*.jsx"))

    for f in scan_paths:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if len(text) > 500_000:
            continue
        count += 1
        f_str = str(f)
        findings.extend(_check_img_alt(text, f_str))
        findings.extend(_check_button_label(text, f_str))
        findings.extend(_check_heading_order(text, f_str))
        findings.extend(_check_label_input(text, f_str))

    return A11yReport(findings=findings, files_scanned=count)
