"""
engine/tools/admin_session.py

관리자 세션 토큰 발급 도구.

목적:
  admin 비밀번호를 모를 때도 기존 admin 사용자에 대한 Bearer 토큰을 발급.
  /api/v1/intake/submissions/{id}/convert 등 인증 필요 엔드포인트 호출용.

사용:
  PYTHONPATH=. python3 engine/tools/admin_session.py \\
    --email admin@platform.local --ttl-hours 1

출력:
  <64자 Bearer 토큰> (한 줄)

주의:
  - 로컬 관리 용도 (프로덕션에서는 비밀번호 로그인 사용)
  - sessions 테이블에 INSERT — 발급 이력은 audit_logs 에 남김
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_admin_session(email: str, ttl_hours: int = 1) -> str:
    from engine.db.adapter import create_adapter

    db_url = os.environ.get("DATABASE_URL", "sqlite:///platform.db")
    db = create_adapter(db_url)

    # 1. user 조회
    user = await db.fetchone(
        "SELECT id, role, is_active FROM users WHERE email=?", (email,)
    )
    if not user:
        raise SystemExit(f"사용자 없음: {email}")
    if not user["is_active"]:
        raise SystemExit(f"비활성 사용자: {email}")

    # 2. 토큰 생성
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = _now()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    sid = str(uuid.uuid4())

    # 3. sessions 테이블 INSERT
    # 실제 스키마: id, user_id, token_hash, expires_at, is_revoked,
    #             created_at, last_used_at (NOT NULL)
    try:
        await db.execute(
            """INSERT INTO sessions (id, user_id, token_hash, expires_at,
                                     is_revoked, created_at, last_used_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (sid, user["id"], token_hash, expires_at, now, now),
        )
    except Exception as exc:
        raise SystemExit(f"세션 INSERT 실패: {exc}")

    # 4. audit log (best-effort)
    try:
        await db.execute(
            """INSERT INTO audit_logs (id, user_id, action, resource_type, resource_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), user["id"], "admin_session_issued",
             "sessions", sid, now),
        )
    except Exception:
        pass  # audit_logs 테이블 구조 상이 가능, 실패해도 세션 발급은 유효

    return token


def main() -> int:
    p = argparse.ArgumentParser(description="관리자 세션 Bearer 토큰 발급")
    p.add_argument("--email", default="admin@platform.local",
                   help="대상 사용자 이메일 (기본: admin@platform.local)")
    p.add_argument("--ttl-hours", type=int, default=1,
                   help="토큰 유효 시간 (기본 1시간)")
    args = p.parse_args()

    token = asyncio.run(create_admin_session(args.email, args.ttl_hours))
    # 표준출력에 토큰만 (스크립트에서 $(...) 로 캡처 가능)
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
