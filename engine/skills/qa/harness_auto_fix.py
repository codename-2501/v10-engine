"""Harness 자동수정 프레임워크 (범용).

Harness QA가 감지한 구조 결함을 programmatic하게 즉시 수정해 재시도 루프를
우회한다. 이미 route_slug·invalid_folder·programmatic_verify에서 검증된 패턴.

현재 지원 영역:
- forbidden_words (TODO/TBD/미정 등) → 대체 표현 치환
- 향후 확장 포인트:
  * lorem_ipsum → 프로젝트 맥락 카피
  * missing_heading → outline 기반 자동 삽입
  * short_tables → 최소 행 수 자동 채움

설계 원칙:
- safe_regions 제외 (코드블록·heading·링크 URL·HTML 속성)
- 치환 횟수 > MAX_AUTO_FIX 이면 no-op (남용 감지 → 기존 FAIL 경로)
- 조사 매칭 (한국어 문법 고려: TBD로 → 미지정으로)
- 감사 마커 주석 삽입 (<!-- auto-fix: ... -->)
- 실패 시 원본 그대로 반환 (side-effect free)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# 남용 판정 임계값 — 이 이상 치환 필요하면 LLM 출력 자체가 문제.
# 자동수정 포기하고 기존 FAIL 경로로 넘김 (G4 모델 승격 발동).
MAX_AUTO_FIX_FORBIDDEN = 5


# 한국어 조사 매칭 치환 맵 — 순서 중요 (긴 것 먼저).
# TBD 기본 치환은 마지막. 조사 포함 형태가 우선 매치.
_FORBIDDEN_REPLACEMENTS: list[tuple[str, str]] = [
    # TBD
    # Korean-aware boundary — (?<![a-zA-Z0-9_]) 및 (?![a-zA-Z0-9_])
    (r"(?<![a-zA-Z0-9_])TBD\s*으로(?![a-zA-Z0-9_])", "미지정으로"),
    (r"(?<![a-zA-Z0-9_])TBD\s*로(?![a-zA-Z0-9_])", "미지정으로"),
    (r"(?<![a-zA-Z0-9_])TBD\s*은(?![a-zA-Z0-9_])", "미지정은"),
    (r"(?<![a-zA-Z0-9_])TBD\s*는(?![a-zA-Z0-9_])", "미지정은"),
    (r"(?<![a-zA-Z0-9_])TBD\s*이(?![a-zA-Z0-9_])", "미지정이"),
    (r"(?<![a-zA-Z0-9_])TBD\s*가(?![a-zA-Z0-9_])", "미지정이"),
    (r"(?<![a-zA-Z0-9_])TBD\s*을(?![a-zA-Z0-9_])", "미지정을"),
    (r"(?<![a-zA-Z0-9_])TBD\s*를(?![a-zA-Z0-9_])", "미지정을"),
    (r"(?<![a-zA-Z0-9_])TBD\s*의(?![a-zA-Z0-9_])", "미지정의"),
    (r"(?<![a-zA-Z0-9_])TBD(?![a-zA-Z0-9_])", "미지정"),
    # TODO
    (r"(?<![a-zA-Z0-9_])TODO\s*:\s*", "다음 단계에서 진행: "),
    (r"(?<![a-zA-Z0-9_])TODO(?![a-zA-Z0-9_])", "다음 단계 진행 예정"),
    # FIXME
    (r"(?<![a-zA-Z0-9_])FIXME\s*:\s*", "수정 필요: "),
    (r"(?<![a-zA-Z0-9_])FIXME(?![a-zA-Z0-9_])", "수정 필요"),
    # 한국어 placeholder (Korean-only → \b OK but use same pattern for consistency)
    (r"(?<![a-zA-Z0-9_])작성\s*예정(?![a-zA-Z0-9_])", "다음 단계에서 진행"),
    (r"(?<![a-zA-Z0-9_])추후\s*작성(?![a-zA-Z0-9_])", "다음 단계에서 진행"),
    (r"(?<![a-zA-Z0-9_])추후\s*결정(?![a-zA-Z0-9_])", "향후 정책 확정 시 반영"),
    (r"(?<![a-zA-Z0-9_])추후\s*확정(?![a-zA-Z0-9_])", "향후 정책 확정 시 반영"),
]

# 치환 금지 영역 감지 정규식
# - 코드블록: ``` ... ``` 또는 ~~~ ... ~~~
# - 인라인 코드: `...`
# - Markdown heading: ^#+ 로 시작하는 줄 (heading 텍스트 파괴 방지)
# - HTML 주석: <!-- ... -->
# - URL / 링크: http(s)://...  [text](url)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_HEADING_RE = re.compile(r"^#+\s.*$", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_URL_RE = re.compile(r"https?://\S+")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")


@dataclass
class AutoFixResult:
    applied: bool
    count: int
    new_content: str
    skipped_reason: str | None = None
    details: list[str] = field(default_factory=list)


def _find_safe_regions(content: str) -> list[tuple[int, int]]:
    """치환 금지 영역(시작, 끝) 튜플 목록. overlap 허용."""
    regions: list[tuple[int, int]] = []
    for pat in (
        _CODE_BLOCK_RE,
        _INLINE_CODE_RE,
        _HEADING_RE,
        _HTML_COMMENT_RE,
        _URL_RE,
        _MD_LINK_RE,
    ):
        for m in pat.finditer(content):
            regions.append((m.start(), m.end()))
    # 정렬 + 병합
    if not regions:
        return []
    regions.sort()
    merged: list[tuple[int, int]] = [regions[0]]
    for s, e in regions[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _position_in_safe(pos: int, regions: list[tuple[int, int]]) -> bool:
    """pos가 safe_region 내부인가? (binary search 단순화 — 영역 소수)."""
    for s, e in regions:
        if s <= pos < e:
            return True
        if pos < s:
            return False
    return False


def _replace_outside_safe(
    content: str, pattern: re.Pattern, repl: str, safe: list[tuple[int, int]],
) -> tuple[str, int]:
    """safe 영역 밖에서만 패턴 치환. (new_content, 치환 개수) 반환."""
    out: list[str] = []
    last = 0
    count = 0
    for m in pattern.finditer(content):
        if _position_in_safe(m.start(), safe):
            continue
        out.append(content[last:m.start()])
        out.append(repl)
        last = m.end()
        count += 1
    out.append(content[last:])
    return "".join(out), count


def try_auto_fix_forbidden_words(content: str) -> AutoFixResult:
    """금지어 자동수정 시도.

    동작:
      1. safe_regions 계산 (코드블록·heading·링크 등 제외)
      2. 각 치환 규칙 순차 적용 (조사 매칭 우선)
      3. 총 치환 횟수 > MAX_AUTO_FIX_FORBIDDEN 이면 no-op (원본 반환)
      4. 성공 시 감사 마커 주석 추가

    Returns: AutoFixResult
    """
    if not content:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="empty_content")

    safe = _find_safe_regions(content)
    new_content = content
    total = 0
    details: list[str] = []

    for pat_str, repl in _FORBIDDEN_REPLACEMENTS:
        pat = re.compile(pat_str, re.IGNORECASE)
        new_content, n = _replace_outside_safe(new_content, pat, repl, safe)
        if n > 0:
            details.append(f"{pat_str} → {repl} ({n}건)")
            total += n
            # safe_regions는 원본 content 기준이지만 치환 후 offset이 밀림.
            # 치환 후 재계산.
            safe = _find_safe_regions(new_content)

    if total == 0:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="no_matches")

    if total > MAX_AUTO_FIX_FORBIDDEN:
        logger.warning(
            "auto_fix_forbidden_abandoned count=%d > max=%d — 남용 패턴, 기존 FAIL 경로",
            total, MAX_AUTO_FIX_FORBIDDEN,
        )
        return AutoFixResult(applied=False, count=total, new_content=content,
                             skipped_reason="exceeds_max", details=details)

    # 감사 마커 추가 (문서 말미)
    marker = (
        f"\n\n<!-- harness_auto_fix: forbidden_words {total}건 치환"
        f" ({', '.join(d.split(' → ')[1].split(' ')[0] for d in details[:3])}...)"
        f" -->\n"
    )
    new_content = new_content.rstrip() + marker

    logger.info(
        "auto_fix_forbidden_applied count=%d details=%s",
        total, "; ".join(details),
    )
    return AutoFixResult(applied=True, count=total, new_content=new_content,
                         details=details)


# ────────────────────────────────────────────────────────────────────────
# Auto-fix framework 확장 (S2-6)
# 동일 AutoFixResult 인터페이스 재사용. caller 가 try_* 함수들을 순차 호출.
# ────────────────────────────────────────────────────────────────────────


# Lorem ipsum / placeholder 카피 패턴 → 의미있는 한국어 placeholder 로 치환.
_LOREM_RE = re.compile(
    r"\b(?:lorem\s+ipsum[^.\n]*\.|dolor\s+sit\s+amet[^.\n]*\.|"
    r"consectetur\s+adipiscing[^.\n]*\.)",
    re.IGNORECASE,
)
_PLACEHOLDER_COPY_RE = re.compile(
    r"(여기에\s*\w*\s*텍스트|샘플\s*텍스트|예시\s*문구|placeholder\s*text)",
    re.IGNORECASE,
)

MAX_AUTO_FIX_LOREM = 8


def try_auto_fix_lorem_ipsum(
    content: str, project_name: str | None = None,
) -> AutoFixResult:
    """Lorem ipsum / 한국어 placeholder 카피 자동 치환.

    프로젝트 맥락이 있으면 project 이름 활용한 짧은 설명으로 대체.
    없으면 일반적 '실제 서비스 카피로 교체 필요' 마커 (다음 retry 에서 LLM 보강).
    """
    if not content:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="empty_content")
    safe = _find_safe_regions(content)
    new_content = content
    total = 0
    details: list[str] = []

    repl = (f"{project_name} 관련 본문 (자동 보강 필요)"
            if project_name else "본문 (자동 보강 필요)")

    new_content, n1 = _replace_outside_safe(new_content, _LOREM_RE, repl, safe)
    if n1 > 0:
        details.append(f"lorem_ipsum × {n1}")
        total += n1
        safe = _find_safe_regions(new_content)
    new_content, n2 = _replace_outside_safe(
        new_content, _PLACEHOLDER_COPY_RE, repl, safe,
    )
    if n2 > 0:
        details.append(f"placeholder_copy × {n2}")
        total += n2

    if total == 0:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="no_matches")
    if total > MAX_AUTO_FIX_LOREM:
        logger.warning(
            "auto_fix_lorem_abandoned count=%d > max=%d", total, MAX_AUTO_FIX_LOREM,
        )
        return AutoFixResult(applied=False, count=total, new_content=content,
                             skipped_reason="exceeds_max", details=details)
    new_content = (
        new_content.rstrip()
        + f"\n\n<!-- harness_auto_fix: lorem_ipsum {total}건 치환 -->\n"
    )
    logger.info("auto_fix_lorem_applied count=%d", total)
    return AutoFixResult(applied=True, count=total, new_content=new_content,
                         details=details)


def try_auto_fix_missing_headings(
    content: str, required_headings: list[str],
) -> AutoFixResult:
    """spec.required_headings 중 누락된 항목을 빈 헤더로 자동 삽입.

    위치: 본문 말미 (LLM 이 다음 turn 에서 채우도록). 헤더만 있고 본문 0줄이라
    여전히 FAIL 가능 — 자동수정은 "최소 구조 확보" 단계.
    """
    if not content or not required_headings:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="no_input")

    # heading 존재 여부는 'name' 부분 매칭 (정확한 prefix 까진 안 봄).
    missing: list[str] = []
    content_lower = content.lower()
    for h in required_headings:
        if (h or "").strip().lower() not in content_lower:
            missing.append(h)

    if not missing:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="all_present")

    # 5개 초과면 전체 재생성이 더 합리적 → 자동수정 포기
    if len(missing) > 5:
        return AutoFixResult(applied=False, count=len(missing),
                             new_content=content, skipped_reason="too_many_missing")

    appended = "\n\n"
    for h in missing:
        appended += f"## {h}\n\n(자동 삽입 — 다음 단계에서 보강)\n\n"
    appended += (
        f"<!-- harness_auto_fix: missing_headings {len(missing)}건 추가 "
        f"({', '.join(missing[:3])}{'...' if len(missing) > 3 else ''}) -->\n"
    )
    new_content = content.rstrip() + appended
    logger.info(
        "auto_fix_missing_headings count=%d list=%s",
        len(missing), missing,
    )
    return AutoFixResult(
        applied=True, count=len(missing),
        new_content=new_content, details=missing,
    )


def try_auto_fix_short_table(
    content: str, min_rows: int, outline_ids: list[str] | None = None,
) -> AutoFixResult:
    """본문에 나오는 첫 표가 min_rows 미만이면 outline_ids 기반 행 자동 추가.

    outline_ids 없으면 no-op (LLM 보강 필요). 있으면 ID 만 행으로 채워
    최소 골격 확보 — 다음 turn 에서 LLM 이 이름·세부 채움.
    """
    if not content or not outline_ids or min_rows <= 0:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="no_input")

    # 첫 표 위치 찾기 — '|---' 구분선이 있는 첫 블록
    lines = content.split("\n")
    table_start = -1
    table_end = -1
    in_table = False
    sep_seen = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("|") and not in_table:
            in_table = True
            table_start = i
        if s.startswith("|---") or s.startswith("|--"):
            sep_seen = True
        if in_table and not s.startswith("|"):
            table_end = i
            break

    if table_start < 0 or not sep_seen:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="no_table")
    if table_end < 0:
        table_end = len(lines)

    # 데이터 행 카운트
    data_rows = sum(
        1 for ln in lines[table_start:table_end]
        if ln.strip().startswith("|") and not ln.strip().startswith("|--")
    ) - 1  # 헤더 제외
    data_rows = max(0, data_rows)

    if data_rows >= min_rows:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="table_sufficient")

    # 헤더 컬럼 수 추정 (구분선에서 셀 갯수)
    sep_line = next(
        (lines[i] for i in range(table_start, table_end)
         if lines[i].strip().startswith("|--")),
        None,
    )
    if sep_line:
        col_count = sep_line.count("|") - 1
    else:
        col_count = 3

    # 부족분만 ID 사용 (남는 ID 는 무시)
    needed = min_rows - data_rows
    available = [oid for oid in outline_ids][:needed]
    if not available:
        return AutoFixResult(applied=False, count=0, new_content=content,
                             skipped_reason="no_ids_available")

    new_rows: list[str] = []
    for oid in available:
        cells = [oid] + ["(자동 삽입)"] * (col_count - 1)
        new_rows.append("| " + " | ".join(cells) + " |")

    # 표 끝에 행 삽입
    insertion = "\n".join(new_rows)
    out_lines = lines[:table_end] + [insertion] + lines[table_end:]
    new_content = "\n".join(out_lines)
    new_content = (
        new_content.rstrip()
        + f"\n\n<!-- harness_auto_fix: short_table {len(available)}행 추가 -->\n"
    )
    logger.info(
        "auto_fix_short_table added=%d new_total=%d min=%d",
        len(available), data_rows + len(available), min_rows,
    )
    return AutoFixResult(
        applied=True, count=len(available),
        new_content=new_content,
        details=[f"+{len(available)} rows from outline"],
    )


def run_auto_fix_pipeline(
    content: str,
    *,
    required_headings: list[str] | None = None,
    min_table_rows: int = 0,
    outline_ids: list[str] | None = None,
    project_name: str | None = None,
) -> AutoFixResult:
    """모든 auto-fix 를 순차 적용 + 합산 결과 반환.

    실패한 fix 는 skip, 성공한 것만 누적. 최종 content 는 마지막 단계 결과.
    """
    cur = content
    total = 0
    all_details: list[str] = []
    applied_any = False

    for fn, kwargs in (
        (try_auto_fix_forbidden_words, {}),
        (try_auto_fix_lorem_ipsum, {"project_name": project_name}),
        (try_auto_fix_missing_headings,
         {"required_headings": required_headings or []}),
        (try_auto_fix_short_table,
         {"min_rows": min_table_rows, "outline_ids": outline_ids or []}),
    ):
        try:
            r = fn(cur, **kwargs) if kwargs else fn(cur)
            if r.applied:
                cur = r.new_content
                total += r.count
                applied_any = True
                all_details.extend(r.details or [])
        except Exception as e:
            logger.warning("auto_fix step %s failed: %s", fn.__name__, e)

    return AutoFixResult(
        applied=applied_any, count=total, new_content=cur,
        details=all_details,
    )
