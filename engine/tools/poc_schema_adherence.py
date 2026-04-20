"""
engine/tools/poc_schema_adherence.py

Phase 0 PoC: LLM schema 준수율 측정.

목표: 화면 목록 정의서 skill 의 새 JSON 스키마 출력이 sonnet 에서
얼마나 잘 지켜지는지 실측. 10회 호출 후 준수율 계산.

준수 조건:
  1. JSON 배열 (또는 {screens: [...]}) 파싱 가능
  2. 각 원소에 필수 필드 (id, category, name, description) 존재
  3. id 가 unique

통과 기준: ≥ 95%

사용:
  PYTHONPATH=. python3 engine/tools/poc_schema_adherence.py
  PYTHONPATH=. python3 engine/tools/poc_schema_adherence.py --runs 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass

from engine.db.adapter import create_adapter
from engine.ai.model_adapter import ModelAdapter, OAuthProvider


SYSTEM_PROMPT = (
    "You are a software architect. Output STRICT JSON matching the given schema. "
    "No markdown fences, no prose, no explanation. Pure JSON only."
)

USER_PROMPT_TEMPLATE = """\
다음 프로젝트의 화면 목록을 JSON 으로 출력하세요.

프로젝트: 마이 루틴 — 개인 습관 추적 모바일 앱
주요 기능: 습관 추가/수정/삭제, 달력 뷰, 통계, AI 코칭, 알림, 로그인/회원가입

## 출력 스키마 (엄수)
{
  "screens": [
    {
      "id": "<고유 ID, 예: auth-login, home-dashboard>",
      "category": "<auth | home | habit | stats | ai | settings | error>",
      "name": "<화면 한글 이름>",
      "description": "<화면 목적 1-2 문장>"
    }
  ]
}

규칙:
1. screens 배열에 최소 20개 화면
2. id 는 unique (중복 금지), 영문 소문자 + 하이픈만
3. 각 화면에 4개 필드 (id, category, name, description) 모두 존재
4. JSON 외 텍스트 절대 금지 (마크다운 코드블록도 금지)

출력:"""


@dataclass
class RunResult:
    run_id: int
    ok: bool
    reason: str
    screens_count: int
    duplicates: int
    missing_fields: int


async def _load_oauth_provider(db) -> OAuthProvider:
    row = await db.fetchone(
        "SELECT oauth_config_encrypted, token_expires_at "
        "FROM provider_credentials WHERE provider='anthropic' LIMIT 1"
    )
    if not row:
        raise RuntimeError("provider_credentials row 없음")
    return OAuthProvider(
        oauth_config_encrypted=row["oauth_config_encrypted"],
        token_expires_at=row.get("token_expires_at"),
    )


def _validate(raw: str) -> RunResult:
    """JSON 파싱 + 스키마 검증."""
    # 코드블록 껍데기 제거 시도 (엄격히는 실격, 관찰용)
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        return RunResult(run_id=0, ok=False, reason=f"json_parse_error: {e}",
                         screens_count=0, duplicates=0, missing_fields=0)

    # 배열 또는 {screens: [...]} 허용
    if isinstance(data, dict) and "screens" in data:
        screens = data["screens"]
    elif isinstance(data, list):
        screens = data
    else:
        return RunResult(run_id=0, ok=False, reason="invalid_top_level",
                         screens_count=0, duplicates=0, missing_fields=0)

    if not isinstance(screens, list):
        return RunResult(run_id=0, ok=False, reason="screens_not_array",
                         screens_count=0, duplicates=0, missing_fields=0)

    ids = []
    missing = 0
    required = {"id", "category", "name", "description"}
    for s in screens:
        if not isinstance(s, dict):
            missing += 1
            continue
        if not required.issubset(s.keys()):
            missing += 1
            continue
        ids.append(s.get("id"))

    duplicates = len(ids) - len(set(ids))
    ok = (
        len(screens) >= 20
        and missing == 0
        and duplicates == 0
    )
    reason = "ok" if ok else (
        f"count={len(screens)} missing_fields={missing} dups={duplicates}"
    )
    return RunResult(
        run_id=0, ok=ok, reason=reason,
        screens_count=len(screens), duplicates=duplicates,
        missing_fields=missing,
    )


async def run_one(adapter: ModelAdapter, run_id: int) -> RunResult:
    try:
        resp = await adapter.call(
            model="claude-sonnet-4-6",
            system=SYSTEM_PROMPT,
            prompt=USER_PROMPT_TEMPLATE,
            max_tokens=8000,
            temperature=0.3,
        )
    except Exception as e:
        return RunResult(run_id=run_id, ok=False, reason=f"api_error: {e}",
                         screens_count=0, duplicates=0, missing_fields=0)

    r = _validate(resp.content or "")
    r.run_id = run_id
    return r


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    args = ap.parse_args()

    db = create_adapter(os.environ.get("DATABASE_URL", "sqlite:///platform.db"))
    provider = await _load_oauth_provider(db)
    adapter = ModelAdapter(provider)

    print(f"\n=== Phase 0 PoC — Schema Adherence ({args.runs} runs, sonnet) ===\n")

    results: list[RunResult] = []
    for i in range(args.runs):
        r = await run_one(adapter, i + 1)
        results.append(r)
        mark = "✓" if r.ok else "✗"
        print(f"  run {i+1:2d} {mark} screens={r.screens_count:3d} "
              f"dups={r.duplicates} missing={r.missing_fields} — {r.reason}")

    passed = sum(1 for r in results if r.ok)
    rate = passed / args.runs * 100
    print(f"\n  결과: {passed}/{args.runs} 통과 ({rate:.0f}%)")
    print(f"  임계 (≥ 95%): {'✅ PASS' if rate >= 95 else '❌ FAIL'}")

    if rate < 95:
        print("\n  권고: 프롬프트 튜닝 필요 — few-shot / JSON mode / 스키마 재설계")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
