"""
Stage 23: Shared Context Ledger.

chunk 간 네비/푸터/라우트/공통 버튼 등 "재사용 결정사항" 을 DB 에 기록.
첫 chunk 결과에서 자동 추출 → 이후 chunk prompt 에 snippet prepend → 일관성 유지.

사용 (executor.py chunk 루프 안):

    ledger = SharedContextLedger(db)
    snippet = await ledger.as_prompt_snippet(engagement_id, node_id)
    # snippet 을 item_prompt 에 prepend

    # 생성 성공 후
    await ledger.extract_and_record(engagement_id, node_id, section_html, item_key)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("V8_SHARED_LEDGER", "1") != "0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SharedContextLedger:
    """chunk 간 공통 결정사항 기록·조회."""

    # 추출 대상 key 목록 (기본). spec 별 커스터마이즈 가능.
    DEFAULT_KEYS = ["nav_menu", "footer", "brand_header", "route_map",
                    "common_actions"]

    # HTML 패턴 — 각 key 에 대해 정규식 or Selector 기반 추출
    EXTRACTORS: dict[str, tuple[str, int]] = {
        # (패턴, 포함할 flags)
        "nav_menu": (r"<nav[^>]*>[\s\S]*?</nav>", re.I),
        "footer": (r"<footer[^>]*>[\s\S]*?</footer>", re.I),
        "brand_header": (r"<header[^>]*class=['\"][^'\"]*brand[^'\"]*['\"][^>]*>[\s\S]*?</header>", re.I),
    }

    def __init__(self, db: Any, enabled: bool | None = None) -> None:
        self._db = db
        self._enabled = _ENABLED if enabled is None else enabled

    async def extract_and_record(
        self,
        engagement_id: str,
        node_id: str,
        html: str,
        origin_item: str,
    ) -> int:
        """HTML 에서 공통 요소 자동 추출 후 기록. 이미 기록된 key 는 skip.

        반환: 이번 호출로 신규 기록된 key 수.
        """
        if not self._enabled or not html:
            return 0
        extracted = self._extract(html)
        if not extracted:
            return 0

        # 이미 기록된 key 조회 (중복 방지)
        existing = await self._existing_keys(engagement_id, node_id)
        new_keys = [k for k in extracted if k not in existing]
        if not new_keys:
            return 0

        for k in new_keys:
            try:
                await self._db.execute(
                    """INSERT INTO shared_context
                         (engagement_id, node_id, context_key, value,
                          origin_item, version, created_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?)
                       ON CONFLICT(engagement_id, node_id, context_key) DO NOTHING""",
                    (engagement_id, node_id, k, extracted[k][:10000],
                     origin_item, _now()),
                )
            except Exception as e:
                logger.debug("shared_context_insert_fail key=%s err=%s", k, e)

        if new_keys:
            logger.info(
                "shared_context_recorded node=%s keys=%s origin=%s",
                node_id[:8], new_keys, origin_item,
            )
        return len(new_keys)

    async def as_prompt_snippet(
        self, engagement_id: str, node_id: str,
        max_chars: int = 3000,
    ) -> str:
        """다음 chunk prompt 에 prepend 할 ledger 요약.

        반환: "## 공통 결정사항 (이전 chunk 결과):\n - nav_menu: ...\n ..." 형태.
        비어있으면 빈 문자열.
        """
        if not self._enabled:
            return ""
        rows = await self._db.fetchall(
            """SELECT context_key, value, origin_item FROM shared_context
               WHERE engagement_id=? AND node_id=?
               ORDER BY created_at ASC""",
            (engagement_id, node_id),
        )
        if not rows:
            return ""

        parts = ["## 기존 결정사항 — 이후 섹션은 아래와 일관되게 사용할 것"]
        used = 0
        for r in rows:
            key = r["context_key"]
            val = str(r["value"])[:max_chars // max(1, len(rows))]
            origin = r["origin_item"]
            parts.append(
                f"- **{key}** (from {origin}):\n```\n{val}\n```",
            )
            used += len(val)
            if used >= max_chars:
                break
        return "\n".join(parts) + "\n\n---\n"

    async def invalidate(self, engagement_id: str, node_id: str) -> None:
        """노드 전체 ledger 초기화 (수동 재시작용)."""
        await self._db.execute(
            "DELETE FROM shared_context WHERE engagement_id=? AND node_id=?",
            (engagement_id, node_id),
        )

    async def _existing_keys(
        self, engagement_id: str, node_id: str,
    ) -> set[str]:
        rows = await self._db.fetchall(
            "SELECT context_key FROM shared_context WHERE engagement_id=? AND node_id=?",
            (engagement_id, node_id),
        )
        return {r["context_key"] for r in (rows or [])}

    def _extract(self, html: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, (pattern, flags) in self.EXTRACTORS.items():
            try:
                m = re.search(pattern, html, flags)
                if m:
                    out[key] = m.group(0).strip()
            except Exception:
                continue

        # route_map: a href 집합 (HTML 내 고유 경로)
        hrefs = set(re.findall(r'href=["\']([^"\']+)["\']', html))
        internal = [h for h in hrefs if h.startswith("/") or h.startswith("#SC-")]
        if internal:
            out["route_map"] = json.dumps(sorted(internal)[:30], ensure_ascii=False)

        # common_actions: button 텍스트 상위 10개
        btns = re.findall(r"<button[^>]*>([\s\S]*?)</button>", html, re.I)
        btn_texts = []
        for b in btns:
            clean = re.sub(r"<[^>]+>", "", b).strip()
            if 2 <= len(clean) <= 40:
                btn_texts.append(clean)
        if btn_texts:
            # 중복 제거 후 10개
            seen: set[str] = set()
            dedup = []
            for t in btn_texts:
                if t not in seen:
                    seen.add(t)
                    dedup.append(t)
                if len(dedup) >= 10:
                    break
            out["common_actions"] = json.dumps(dedup, ensure_ascii=False)

        return out
