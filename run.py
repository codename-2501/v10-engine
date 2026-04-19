"""
run.py
AI SI 매뉴팩처링 플랫폼 v10 — 실행 진입점.

사용법:
  python run.py --port 8007       # V10 기본 실행 (0.0.0.0:8007)
  python run.py --host 127.0.0.1 --port 8007  # 로컬만 허용
  python run.py --port 8000 --dev             # 개발 모드 (hot-reload + 콘솔 로그)

  또는 uvicorn 직접:
  uvicorn api.server:app --host 0.0.0.0 --port 8007 --workers 1
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="AI SI 매뉴팩처링 플랫폼 v10")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "1")),
                        help="워커 수. SQLite 사용 시 반드시 1.")
    parser.add_argument("--dev", action="store_true", help="개발 모드 (reload=True)")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn 미설치. pip install uvicorn 실행 후 재시도.")
        sys.exit(1)

    if args.dev:
        os.environ.setdefault("ENV", "development")
        uvicorn.run(
            "api.server:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level=args.log_level,
        )
    else:
        os.environ.setdefault("ENV", "production")
        uvicorn.run(
            "api.server:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_level=args.log_level,
        )


if __name__ == "__main__":
    main()
