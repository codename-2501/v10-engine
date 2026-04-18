"""Heartbeat 모듈 — 실행 중인 에이전트를 감시하고 타임아웃 처리.

executor.py에서 분리된 프로세스 관리 모듈.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


async def executor_with_heartbeat(
    executor_fn: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    heartbeat_interval: int = 60,
    heartbeat_timeout: int = 10,
    **kwargs: Any,
) -> Any:
    """에이전트 실행 함수를 heartbeat로 감싸기.

    args:
        executor_fn: 실행할 async 함수
        heartbeat_interval: 하트비트 간격 (초)
        heartbeat_timeout: 각 하트비트 타임아웃 (초)

    returns:
        executor_fn의 반환값
    """

    async def hb_loop() -> None:
        """주기적 하트비트 루프."""
        while True:
            try:
                # 예: DB 헬스체크, 로그 기록 등
                await asyncio.sleep(heartbeat_interval)
                logger.debug("heartbeat: execution still running")
            except asyncio.CancelledError:
                logger.debug("heartbeat loop cancelled")
                break
            except Exception as e:
                logger.warning("heartbeat error: %s", e)

    # 메인 실행 태스크
    main_task = asyncio.create_task(executor_fn(*args, **kwargs))

    # 하트비트 태스크
    hb_task = asyncio.create_task(hb_loop())

    try:
        result = await asyncio.wait_for(main_task, timeout=None)
        return result
    except asyncio.TimeoutError:
        logger.error("executor timeout")
        raise
    except Exception as e:
        logger.error("executor error: %s", e)
        raise
    finally:
        # 정리: 하트비트 태스크 취소
        if not hb_task.done():
            hb_task.cancel()
            try:
                await asyncio.wait_for(hb_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
