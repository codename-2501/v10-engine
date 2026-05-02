"""Hook registry — third-party plugin protocol.

v10 의 주요 함수들이 *post-event hook* 을 발화. 외부 plugin (wave-engine 등)
이 register_hook() 으로 callback 등록 → monkey-patch 없이 확장.

설계:
- 각 hook point 는 *고유 이름* (string)
- 여러 plugin 이 같은 이름에 register 가능 (chain)
- 한 callback 의 예외가 다른 callback 영향 X (graceful)
- async/sync 함수 모두 지원

사용:
    # plugin 측
    from engine.core.hook_registry import register_hook
    register_hook("post_save_artifact", my_callback)

    # v10 측 (hook 발화)
    from engine.core.hook_registry import call_hooks
    await call_hooks("post_save_artifact", db, node, content, art_type)

핵심 코어 (dag_advancer, state_machine, context_assembler, budget_enforcer,
cascade) 은 이 모듈 import X — hook registry 는 *부수* 시스템.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)


_HOOKS: dict[str, list[Callable[..., Any]]] = defaultdict(list)


def register_hook(name: str, fn: Callable[..., Any]) -> None:
    """Hook 등록. idempotent — 같은 fn 중복 등록 시 무시."""
    if fn in _HOOKS[name]:
        return
    _HOOKS[name].append(fn)
    logger.debug("hook_registered name=%s fn=%s", name, getattr(fn, "__qualname__", str(fn)))


def unregister_hook(name: str, fn: Callable[..., Any]) -> bool:
    """Hook 제거. 반환: 제거됐는지."""
    if fn in _HOOKS.get(name, []):
        _HOOKS[name].remove(fn)
        return True
    return False


def list_hooks(name: str) -> list[Callable[..., Any]]:
    """등록된 hook list (debug 용)."""
    return list(_HOOKS.get(name, []))


def clear_hooks(name: str | None = None) -> None:
    """Hook 초기화 — test 용. name=None 시 전체."""
    if name is None:
        _HOOKS.clear()
    else:
        _HOOKS.pop(name, None)


async def call_hooks(name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """이름의 모든 hook 호출. 예외는 swallow + 로그 (graceful chain).

    반환: 각 hook 의 반환값 list. 실패한 hook 은 None.
    """
    results: list[Any] = []
    for fn in _HOOKS.get(name, []):
        try:
            if asyncio.iscoroutinefunction(fn):
                r = await fn(*args, **kwargs)
            else:
                r = fn(*args, **kwargs)
            results.append(r)
        except Exception as exc:
            logger.warning(
                "hook_failed name=%s fn=%s err=%s",
                name, getattr(fn, "__qualname__", str(fn)), exc,
            )
            results.append(None)
    return results


def call_hooks_sync(name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """sync 버전 — 비동기 hook 은 무시."""
    results: list[Any] = []
    for fn in _HOOKS.get(name, []):
        if asyncio.iscoroutinefunction(fn):
            logger.debug("hook_skipped_async name=%s fn=%s",
                         name, getattr(fn, "__qualname__", str(fn)))
            continue
        try:
            results.append(fn(*args, **kwargs))
        except Exception as exc:
            logger.warning("hook_failed name=%s err=%s", name, exc)
            results.append(None)
    return results
