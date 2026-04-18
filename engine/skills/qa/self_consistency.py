"""Self-consistency sampling for AI QA (S2-4).

핵심 문서(PRD·요구사항·화면목록·보안설계서) QA 에서 LLM 호출 N=3 후 점수
중앙값/판정 다수결로 variance 제거. "62 → 48 → 28 뒤집힘" 근본 차단.

설계:
- 적용 대상: spec.self_consistency_n 또는 spec.name 화이트리스트
- 토큰 비용: 대상 spec QA 만 N배. 전체 QA 비용 영향은 미미.
- L2 cache 와 결합: 첫 N=3 결과를 합산 후 캐싱 (다음 호출은 캐시 hit)
- 코어 무수정 — executor QA 경로에서 import 후 spec 화이트리스트 검사 후 호출
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


# 기본 화이트리스트 (spec.self_consistency_n 미지정 시 적용 대상).
# S6 이후: Harness-Supreme 이 variance 구제 주 메커니즘. self_consistency 는
# rate-limit 환경에서 토큰 폭탄 유발 (실측: PRD·리스크 QA 각 138K/137K 토큰).
# 실제 결정은 S6 harness_supreme 이 함 → self_consistency 기여도 0 + 낭비.
# 전부 제거. spec.self_consistency_n 명시한 경우만 N=3 적용 (opt-in).
DEFAULT_WHITELIST: set = set()

DEFAULT_N = 3


@dataclass
class ConsistencyVerdict:
    median_score: int
    pass_rate: float          # PASS 비율 (0.0 ~ 1.0)
    final_pass: bool          # 다수결
    samples: list[dict]       # [{score, pass, raw}]
    n: int


def should_apply(spec: dict | None, task_name: str | None = None) -> int:
    """이 호출에 self-consistency 를 적용할지 → N 값 (0 = 미적용)."""
    if spec is None:
        return 0
    explicit = spec.get("self_consistency_n")
    if isinstance(explicit, int) and explicit > 1:
        return min(explicit, 5)  # 5 cap (비용 보호)
    name = (task_name or spec.get("name") or "")
    for kw in DEFAULT_WHITELIST:
        if kw in name:
            return DEFAULT_N
    return 0


_SCORE_RE = re.compile(r"(?:점수|score)\s*[:=]?\s*(\d{1,3})", re.IGNORECASE)
_PASS_RE = re.compile(r"\bPASS\b", re.IGNORECASE)
_FAIL_RE = re.compile(r"\bFAIL\b", re.IGNORECASE)


def parse_verdict(content: str) -> tuple[int, bool]:
    """LLM QA 응답에서 (점수, PASS 여부) 추출.

    실패 시 (0, False) — 보수적 처리.
    """
    if not content:
        return 0, False
    score = 0
    m = _SCORE_RE.search(content)
    if m:
        try:
            score = max(0, min(100, int(m.group(1))))
        except ValueError:
            pass
    has_pass = bool(_PASS_RE.search(content))
    has_fail = bool(_FAIL_RE.search(content))
    # FAIL 우선 (안전): 둘 다 있으면 FAIL
    if has_fail:
        return score, False
    if has_pass:
        return score, True
    # 점수 50 이상이면 PASS 로 가정 (스레드 일치)
    return score, score >= 50


async def run_consistency_qa(
    n: int,
    qa_call: Callable[[], Awaitable[Any]],
) -> ConsistencyVerdict:
    """qa_call 을 N회 병렬 실행 → 점수 중앙값 + PASS 다수결.

    qa_call: () → APIResponse-like (속성 .content). caller 가 매 호출마다
    동일 prompt/system 으로 호출하도록 closure 구성.
    """
    if n <= 1:
        # 단일 호출
        try:
            resp = await qa_call()
            content = getattr(resp, "content", "") or ""
            score, passed = parse_verdict(content)
            return ConsistencyVerdict(
                median_score=score, pass_rate=1.0 if passed else 0.0,
                final_pass=passed,
                samples=[{"score": score, "pass": passed,
                          "raw": content, "response": resp}],
                n=1,
            )
        except Exception as e:
            logger.warning("self_consistency single call failed: %s", e)
            return ConsistencyVerdict(0, 0.0, False, [], 1)

    # N 회 병렬
    try:
        results = await asyncio.gather(
            *(qa_call() for _ in range(n)),
            return_exceptions=True,
        )
    except Exception as e:
        logger.warning("self_consistency gather failed: %s", e)
        return ConsistencyVerdict(0, 0.0, False, [], n)

    samples: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            samples.append({"score": 0, "pass": False,
                            "raw": f"error: {str(r)[:100]}", "response": None})
            continue
        text = getattr(r, "content", "") or ""
        s, p = parse_verdict(text)
        # raw 는 full 저장 (executor 가 _parse_qa_verdict 재실행해야 하므로).
        # response 객체 자체도 보관해 caller 가 선택해 APIResponse 호환으로 사용.
        samples.append({"score": s, "pass": p, "raw": text, "response": r})

    scores = sorted(x["score"] for x in samples)
    median = scores[len(scores) // 2] if scores else 0
    passes = sum(1 for x in samples if x["pass"])
    pass_rate = passes / len(samples) if samples else 0.0
    # 다수결 (n=3 → 2/3 이상)
    final_pass = passes > len(samples) / 2

    logger.info(
        "self_consistency_qa n=%d median=%d pass_rate=%.0f%% → %s",
        n, median, pass_rate * 100, "PASS" if final_pass else "FAIL",
    )
    return ConsistencyVerdict(
        median_score=median,
        pass_rate=pass_rate,
        final_pass=final_pass,
        samples=samples,
        n=n,
    )
