"""
engine/ai/change_classifier.py
변경 분류기 — Partial/Contextual Cascade 라우팅.

upstream artifact diff를 받아 downstream 노드에 미치는 영향을 분류:
  PARTIAL    — 특정 섹션만 영향, 해당 섹션만 패치
  CONTEXTUAL — 전체 맥락 변경, 전체 재실행 필요

안전 규칙:
  - confidence < CONFIDENCE_THRESHOLD → 강제 CONTEXTUAL
  - diff 없음 / 파싱 실패 → 강제 CONTEXTUAL
  - QA 노드 타입 → 항상 CONTEXTUAL (QA는 항상 전체 재실행)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.ai.model_adapter import ModelAdapter

logger = logging.getLogger(__name__)

# confidence 임계값: 미만이면 CONTEXTUAL fallback
CONFIDENCE_THRESHOLD = 0.75

# 분류에 사용할 모델 (sonnet — 정확도 90%+ 유지)
CLASSIFIER_MODEL = "claude-sonnet-4-6"

# 분류기 프롬프트 시스템 메시지
_SYSTEM = """\
당신은 소프트웨어 산출물 변경 영향 분류 전문가입니다.

두 종류의 변경을 구분합니다:
- PARTIAL: 기존 내용은 유지되고, 새 항목 추가 또는 특정 섹션만 수정.
  downstream 노드에서 해당 부분만 패치하면 충분.
  예) 컴포넌트 5개 추가 (기존 유지), API 엔드포인트 1개 추가, 특정 설명 수정
- CONTEXTUAL: 기존 내용의 구조·핵심 맥락이 바뀌어서 downstream 전체를 다시 써야 함.
  예) 시스템 아키텍처 변경, 핵심 비즈니스 규칙 변경, 기술 스택 변경, 레이아웃 구조 변경

★ 핵심 판단 기준:
- diff에서 삭제(-) 줄이 거의 없고 추가(+) 줄만 있으면 → 높은 확률로 PARTIAL (순수 추가)
- 하지만 추가된 내용이 기존 구조의 전제를 바꾸면 (예: "single-column" → "micro-frontend") → CONTEXTUAL
- 줄 수가 아니라 "변경의 의미"로 판단하세요. 100줄 추가라도 기존을 건드리지 않으면 PARTIAL.
- 1줄 수정이라도 핵심 구조가 바뀌면 CONTEXTUAL.

반드시 JSON만 반환하세요. 다른 텍스트 금지.
"""

_USER_TEMPLATE = """\
## 변경된 upstream 산출물 diff
```diff
{diff}
```

## downstream 노드 정보
- 노드명: {downstream_name}
- 산출물 유형: {downstream_type}

위 diff가 downstream 노드에 미치는 영향을 분류하세요.

반환 형식 (JSON only):
{{
  "type": "PARTIAL" | "CONTEXTUAL",
  "affected_sections": ["섹션명1", "섹션명2"],
  "confidence": 0.0~1.0,
  "reason": "한 줄 설명"
}}

PARTIAL인 경우 affected_sections에 수정이 필요한 섹션 헤딩(## 으로 시작)을 나열하세요.
CONTEXTUAL인 경우 affected_sections는 빈 배열 []로 반환하세요.
"""

_SAFE_FALLBACK = {
    "type": "CONTEXTUAL",
    "affected_sections": [],
    "confidence": 1.0,
    "reason": "안전 fallback (분류 불가)",
}

# ---------------------------------------------------------------------------
# 하네스: 규칙 기반 1차 분류 (AI 호출 없음)
# ---------------------------------------------------------------------------

# CONTEXTUAL 확정 고위험 키워드 (changed 줄에서 탐지)
_CONTEXTUAL_KEYWORDS: frozenset[str] = frozenset({
    "아키텍처", "기술 스택", "전면", "교체", "재설계", "전환",
    "architecture", "rewrite", "redesign", "migration", "overhaul",
    "기반 변경", "플랫폼 변경", "프레임워크 변경",
})

# PARTIAL 확정 임계값
_PARTIAL_MAX_CHANGED_LINES = 15    # 추가+삭제 합계 상한
_PARTIAL_MAX_CHANGE_RATIO  = 0.20  # 전체 문서 대비 20% 미만
# 주: C1이 이미 40% 초과를 CONTEXTUAL로 확정하므로 P3는 20~40% 구간에서만 None(AI fallback) 처리

# CONTEXTUAL 확정 임계값
_CONTEXTUAL_MIN_CHANGE_RATIO   = 0.40  # 전체 문서 대비 40% 초과
_CONTEXTUAL_MAX_HEADING_CHANGE = 2     # 헤딩 추가+삭제 합계 초과 시


def _extract_sections_from_diff(diff: str) -> list[str]:
    """
    unified diff에서 변경된 줄이 속한 섹션 헤딩 추출.
    old_content 없이 diff 자체만 파싱.

    두 가지 경로로 섹션 헤딩 추적:
      1. @@ hunk 헤더 끝에 포함된 섹션 컨텍스트 (difflib 자동 포함)
         예: "@@ -10,3 +10,4 @@ ## 인증 API"
      2. context(' ') / 삭제('-') 줄에서 ## / ### 헤딩 직접 탐지
    반환: 중복 제거된 섹션 헤딩 리스트 (원문 그대로)
    """
    sections: list[str] = []
    seen: set[str] = set()
    current_section: str | None = None

    for raw_line in diff.split("\n"):
        if not raw_line:
            continue

        # ── 경로 1: @@ hunk 헤더에서 섹션 추출 ──
        # 형식: "@@ -start,count +start,count @@ <section context>"
        # generate_diff가 lineterm="\n" 이면 줄 시작에 @@, 아닌 경우도 방어 처리
        if "@@" in raw_line:
            parts = raw_line.split("@@")
            # 두 번째 @@ 이후 = 섹션 컨텍스트
            if len(parts) >= 3:
                hunk_ctx = parts[2].strip()
                if hunk_ctx.startswith("## ") or hunk_ctx.startswith("### "):
                    current_section = hunk_ctx
            if raw_line.startswith("@@"):
                continue  # 순수 헤더 줄은 더 이상 처리 불필요

        prefix = raw_line[0]
        content = raw_line[1:]

        # ── 경로 2: context / 삭제 줄에서 헤딩 직접 추적 ──
        if prefix in (" ", "-"):
            stripped = content.strip()
            if stripped.startswith("## ") or stripped.startswith("### "):
                current_section = stripped

        # 실제 변경 줄 — current_section 기록 (diff 헤더 제외)
        if prefix in ("+", "-") and not raw_line.startswith("+++") and not raw_line.startswith("---"):
            if current_section and current_section not in seen:
                seen.add(current_section)
                sections.append(current_section)

    return sections


def _heuristic_classify(
    diff: str,
    old_content: str,
    downstream_node_name: str,
    downstream_artifact_type: str,
) -> dict | None:
    """
    규칙 기반 1차 분류. AI 호출 없음.

    Returns:
        dict  — 확정된 분류 결과 (CONTEXTUAL 또는 PARTIAL)
        None  — 규칙으로 확정 불가 → AI fallback 필요
    """
    # 변경 줄 분리 (diff 헤더 제외, 공백 전용 줄 제외)
    changed_lines = [
        ln for ln in diff.split("\n")
        if ln and ln[0] in ("+", "-")
        and not ln.startswith("+++")
        and not ln.startswith("---")
    ]
    changed_count = len(changed_lines)

    # 헤딩 변경 줄
    heading_changes = [
        ln for ln in changed_lines
        if ln[1:].lstrip().startswith("## ") or ln[1:].lstrip().startswith("### ")
    ]
    heading_change_count = len(heading_changes)

    # 고위험 키워드 탐지 (삭제 줄에서만 — 추가 줄에 키워드 있는 건 새 내용 추가지 구조 변경 아님)
    removed_text = " ".join(ln[1:] for ln in changed_lines if ln.startswith("-")).lower()
    has_risky_keyword = any(kw in removed_text for kw in _CONTEXTUAL_KEYWORDS)

    # 변경 비율 (old_content 기준)
    total_old_lines = len(old_content.splitlines()) if old_content else 0
    change_ratio = changed_count / max(total_old_lines, 1) if total_old_lines > 0 else None

    # ── CONTEXTUAL 확정 규칙 (하나라도 충족 시 즉시 반환) ──
    if has_risky_keyword:
        reason = f"고위험 키워드 발견 → CONTEXTUAL"
        logger.info("change_classifier_heuristic rule=C3 result=CONTEXTUAL downstream=%s",
                    downstream_node_name[:40])
        return {"type": "CONTEXTUAL", "affected_sections": [], "confidence": 0.95, "reason": reason}

    if heading_change_count > _CONTEXTUAL_MAX_HEADING_CHANGE:
        reason = f"헤딩 변경 {heading_change_count}개 → CONTEXTUAL"
        logger.info("change_classifier_heuristic rule=C2 result=CONTEXTUAL downstream=%s",
                    downstream_node_name[:40])
        return {"type": "CONTEXTUAL", "affected_sections": [], "confidence": 0.92, "reason": reason}

    if change_ratio is not None and change_ratio > _CONTEXTUAL_MIN_CHANGE_RATIO:
        # 삭제 줄이 적으면 순수 추가일 수 있음 → AI fallback으로 맥락 판단
        _removed = [ln for ln in changed_lines if ln.startswith("-")]
        if len(_removed) <= max(changed_count * 0.1, 2):
            logger.info(
                "change_classifier_heuristic rule=C1_defer downstream=%s ratio=%.2f "
                "removed=%d → AI fallback (순수 추가 가능성)",
                downstream_node_name[:40], change_ratio, len(_removed),
            )
            # None 반환 대신 아래 P 규칙/AI fallback으로 계속
        else:
            reason = f"변경 비율 {change_ratio:.1%} > 40% + 삭제 {len(_removed)}줄 → CONTEXTUAL"
            logger.info("change_classifier_heuristic rule=C1 result=CONTEXTUAL downstream=%s ratio=%.2f",
                        downstream_node_name[:40], change_ratio)
            return {"type": "CONTEXTUAL", "affected_sections": [], "confidence": 0.95, "reason": reason}

    # ── PARTIAL 확정 규칙 (모두 충족 시만 반환) ──
    p1_ok = changed_count <= _PARTIAL_MAX_CHANGED_LINES
    p2_ok = heading_change_count == 0
    p3_ok = (change_ratio is not None and change_ratio < _PARTIAL_MAX_CHANGE_RATIO) or (change_ratio is None and changed_count <= 5)
    # p4 (고위험 키워드 부재) — C3에서 이미 CONTEXTUAL 반환했으므로 이 지점에서 항상 충족

    if p1_ok and p2_ok and p3_ok:
        sections = _extract_sections_from_diff(diff)
        if sections:  # P5: 섹션 특정 가능해야 PARTIAL 확정
            logger.info(
                "change_classifier_heuristic rule=P result=PARTIAL downstream=%s "
                "changed=%d ratio=%s sections=%d",
                downstream_node_name[:40], changed_count,
                f"{change_ratio:.1%}" if change_ratio is not None else "N/A",
                len(sections),
            )
            return {
                "type": "PARTIAL",
                "affected_sections": sections,
                "confidence": 0.88,
                "reason": f"소규모 변경 ({changed_count}줄, 섹션 {len(sections)}개 특정) → PARTIAL",
            }

    # 모호한 케이스 → AI fallback
    logger.debug(
        "change_classifier_heuristic inconclusive downstream=%s changed=%d ratio=%s heading_chg=%d",
        downstream_node_name[:40], changed_count,
        f"{change_ratio:.1%}" if change_ratio is not None else "N/A",
        heading_change_count,
    )
    return None


async def classify_change(
    diff: str,
    downstream_node_name: str,
    downstream_artifact_type: str,
    model_adapter: "ModelAdapter",
    old_content: str = "",    # 하네스 분류 정확도 향상용 (기본값 = 기존 동작 유지)
) -> dict:
    """
    upstream diff를 분석해 downstream에 미치는 영향을 분류.

    Returns:
        {
          "type": "PARTIAL" | "CONTEXTUAL",
          "affected_sections": list[str],
          "confidence": float,
          "reason": str,
        }
    confidence < CONFIDENCE_THRESHOLD → 강제 CONTEXTUAL.
    """
    if not diff or not diff.strip():
        return _SAFE_FALLBACK

    # QA 노드는 항상 CONTEXTUAL (전체 재검증)
    if "QA" in downstream_artifact_type.upper() or downstream_node_name.startswith("[QA]"):
        return {
            "type": "CONTEXTUAL",
            "affected_sections": [],
            "confidence": 1.0,
            "reason": "QA 노드는 항상 전체 재실행",
        }

    # ── 하네스 1차 분류 (AI 호출 없음) ──
    heuristic = _heuristic_classify(diff, old_content, downstream_node_name, downstream_artifact_type)
    if heuristic is not None:
        return heuristic
    # None → AI fallback (아래 Haiku 호출로 진행)

    # diff 길이 제한 (분류기용 — 토큰 절약)
    diff_truncated = diff[:3000]
    if len(diff) > 3000:
        diff_truncated += "\n... (truncated)"

    user_prompt = _USER_TEMPLATE.format(
        diff=diff_truncated,
        downstream_name=downstream_node_name,
        downstream_type=downstream_artifact_type,
    )

    try:
        response = await model_adapter.call(
            model=CLASSIFIER_MODEL,
            system=_SYSTEM,
            prompt=user_prompt,
            max_tokens=512,
        )
        raw = response.content.strip()

        # JSON 파싱
        # 코드 블록 감싸져 있으면 제거
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        parsed = json.loads(raw)

        change_type = parsed.get("type", "CONTEXTUAL")
        confidence = float(parsed.get("confidence", 0.0))
        affected_sections = parsed.get("affected_sections", [])
        reason = parsed.get("reason", "")

        # 타입 검증
        if change_type not in ("PARTIAL", "CONTEXTUAL"):
            change_type = "CONTEXTUAL"

        # confidence 임계값 미만 → CONTEXTUAL fallback
        if confidence < CONFIDENCE_THRESHOLD:
            logger.info(
                "change_classifier_low_confidence downstream=%s confidence=%.2f → CONTEXTUAL",
                downstream_node_name[:40], confidence,
            )
            return {
                "type": "CONTEXTUAL",
                "affected_sections": [],
                "confidence": confidence,
                "reason": f"confidence 부족 ({confidence:.2f} < {CONFIDENCE_THRESHOLD}) → fallback",
            }

        # PARTIAL인데 섹션 없으면 CONTEXTUAL fallback
        if change_type == "PARTIAL" and not affected_sections:
            logger.info(
                "change_classifier_no_sections downstream=%s → CONTEXTUAL",
                downstream_node_name[:40],
            )
            return {
                "type": "CONTEXTUAL",
                "affected_sections": [],
                "confidence": confidence,
                "reason": "PARTIAL이지만 affected_sections 비어있음 → fallback",
            }

        logger.info(
            "change_classifier result=%s sections=%d confidence=%.2f downstream=%s",
            change_type, len(affected_sections), confidence,
            downstream_node_name[:40],
        )
        return {
            "type": change_type,
            "affected_sections": affected_sections,
            "confidence": confidence,
            "reason": reason,
        }

    except json.JSONDecodeError as e:
        logger.warning("change_classifier_json_parse_failed error=%s → CONTEXTUAL", str(e))
        return _SAFE_FALLBACK
    except Exception as e:
        logger.warning("change_classifier_failed error=%s → CONTEXTUAL", str(e))
        return _SAFE_FALLBACK


def generate_diff(old_content: str, new_content: str) -> str:
    """
    두 텍스트 사이의 unified diff 생성.
    같으면 빈 문자열 반환.

    splitlines() + lineterm="" 후 "\n".join으로 각 줄 분리.
    ("".join + lineterm="" 조합은 헤더가 이어붙어 파싱 불가.)
    """
    import difflib
    if old_content == new_content:
        return ""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="이전 버전",
        tofile="새 버전",
        lineterm="",
    ))
    return "\n".join(diff_lines) + "\n" if diff_lines else ""
