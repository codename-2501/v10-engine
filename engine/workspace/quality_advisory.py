"""Quality advisories — 퀄리티 회귀 감지기 (SHOULD 단계).

CLAUDE.md의 '금지 패턴'을 정규식 + CSS 분석으로 자동 감지하여 산출물의
"AI스러움" 점수를 내고 workspace_deployments.warnings로 보고한다.

핵심 원칙: 여기서 FAIL은 배포 차단 아님 (advisory only). 기존 MUST 계약에
회귀 없음. 사용자에게만 보고하여 재생성 결정 참고.

금지 패턴(CLAUDE.md 명시):
- 보라색 그라디언트 (#8b5cf6 계열, linear-gradient(..., purple))
- 시스템 기본 폰트 (-apple-system, BlinkMacSystemFont, Segoe UI)
- 쿠키커터 레이아웃 (동일 카드 그리드·hero+3cards 반복)
- AI스러운 placeholder 카피 (Lorem ipsum, "예시 텍스트", "준비 중")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)


# CLAUDE.md 명시 금지 색상 (보라 계열)
_FORBIDDEN_COLORS = [
    "#8b5cf6", "#a855f7", "#6366f1", "#7c3aed", "#9333ea",  # purple/violet
    "rebeccapurple", "blueviolet", "purple",
]

# 금지 폰트 (시스템 기본)
_FORBIDDEN_FONTS = [
    "-apple-system", "blinkmacsystemfont", "segoe ui", "helvetica neue",
    "arial, sans-serif",  # plain arial as sole fallback
]

# placeholder / AI 잔재 카피
_PLACEHOLDER_PHRASES = [
    "lorem ipsum", "lorem, ipsum", "dolor sit amet",
    "예시 텍스트", "샘플 텍스트", "준비 중입니다",
    "추후 업데이트", "tbd", "todo", "아직 준비",
]

# 쿠키커터 힌트 (순수 3-column hero-card grid의 전형)
_COOKIE_CUTTER_RE = re.compile(
    r"grid-template-columns:\s*repeat\(3\s*,\s*1fr\)", re.IGNORECASE,
)


@dataclass
class QualityFinding:
    category: str         # 'forbidden_color' | 'forbidden_font' | 'placeholder_copy' | 'cookie_cutter'
    file: str
    snippet: str          # 매치된 텍스트 (최대 120자)
    line: int | None = None


@dataclass
class QualityReport:
    score: float           # 0.0(최악) ~ 1.0(최선)
    findings: list[QualityFinding] = field(default_factory=list)
    summary: str = ""

    def to_warning_entries(self) -> list[str]:
        return [
            f"quality_advisory[{f.category}]: {f.file}:{f.line or '?'} {f.snippet[:100]}"
            for f in self.findings
        ]


def _scan_text(file: Path, text: str) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    lower = text.lower()

    # 1) forbidden colors
    for col in _FORBIDDEN_COLORS:
        if col.lower() in lower:
            # 줄 번호 추출
            idx = lower.find(col.lower())
            line = lower[:idx].count("\n") + 1
            snippet = text[max(0, idx - 20): idx + 60].replace("\n", " ")
            findings.append(
                QualityFinding("forbidden_color", str(file), snippet, line)
            )

    # 2) forbidden fonts — **primary 위치에 쓰인 경우만** (fallback은 OK)
    # Pretendard 먼저 오고 -apple-system이 fallback이면 정당 → primary(첫 요소) 검사.
    for m in re.finditer(
        r"font-family\s*:\s*([^;}\n]+)", text, re.IGNORECASE,
    ):
        value = m.group(1).strip().lower()
        # 첫 번째 폰트 토큰만 추출 (comma 이전)
        first = value.split(",", 1)[0].strip().strip("\"'")
        for bad in _FORBIDDEN_FONTS:
            if bad in first and "pretendard" not in first:
                line = text[: m.start()].count("\n") + 1
                findings.append(
                    QualityFinding(
                        "forbidden_font", str(file),
                        f"font-family primary: {first[:60]}", line,
                    )
                )
                break

    # 3) placeholder copy
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in lower:
            idx = lower.find(phrase)
            line = lower[:idx].count("\n") + 1
            snippet = text[max(0, idx - 10): idx + 60].replace("\n", " ")
            findings.append(
                QualityFinding("placeholder_copy", str(file), snippet, line)
            )

    # 4) cookie-cutter 3-column grid
    for m in _COOKIE_CUTTER_RE.finditer(text):
        line = text[: m.start()].count("\n") + 1
        findings.append(
            QualityFinding(
                "cookie_cutter", str(file),
                "grid-template-columns: repeat(3, 1fr)", line,
            )
        )

    return findings


def scan_workspace_quality(workspace_path: Path) -> QualityReport:
    """워크스페이스 전체를 스캔하여 품질 리포트 생성.

    스캔 대상:
    - preview/*.html (디자인 시안)
    - frontend/src/**/*.tsx (생성된 컴포넌트)
    - frontend/src/**/*.css (스타일시트)

    Returns: QualityReport (score = 1.0 - min(findings/20, 1.0))
    """
    all_findings: list[QualityFinding] = []

    scan_paths: list[Path] = []
    preview_dir = workspace_path / "preview"
    if preview_dir.is_dir():
        scan_paths.extend(preview_dir.glob("*.html"))

    fe_src = workspace_path / "frontend" / "src"
    if fe_src.is_dir():
        for ext in ("*.tsx", "*.ts", "*.css", "*.scss"):
            scan_paths.extend(fe_src.rglob(ext))

    for f in scan_paths:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # 너무 큰 파일은 건너뜀 (node_modules 등 방어)
        if len(text) > 500_000:
            continue
        all_findings.extend(_scan_text(f, text))

    # 점수: findings 20개 이상이면 최악(0), 0개면 최선(1).
    score = max(0.0, 1.0 - (len(all_findings) / 20.0))

    # 카테고리별 요약
    counts: dict[str, int] = {}
    for f in all_findings:
        counts[f.category] = counts.get(f.category, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no issues"

    return QualityReport(score=round(score, 2), findings=all_findings, summary=summary)
