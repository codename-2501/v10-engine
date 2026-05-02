"""F4 — hook_registry 단위 검증."""
import asyncio
import pytest

from engine.core.hook_registry import (
    register_hook, unregister_hook, list_hooks, clear_hooks,
    call_hooks, call_hooks_sync,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_hooks()
    yield
    clear_hooks()


def test_register_and_list():
    def fn(): return "a"
    register_hook("evt", fn)
    assert fn in list_hooks("evt")


def test_register_idempotent():
    def fn(): return "a"
    register_hook("evt", fn)
    register_hook("evt", fn)
    assert len(list_hooks("evt")) == 1


def test_unregister():
    def fn(): return "a"
    register_hook("evt", fn)
    assert unregister_hook("evt", fn) is True
    assert fn not in list_hooks("evt")
    assert unregister_hook("evt", fn) is False  # 두 번째는 False


@pytest.mark.asyncio
async def test_call_hooks_async_chain():
    results_seen = []
    async def a(x): results_seen.append(("a", x)); return f"a:{x}"
    async def b(x): results_seen.append(("b", x)); return f"b:{x}"
    register_hook("evt", a)
    register_hook("evt", b)
    out = await call_hooks("evt", 42)
    assert out == ["a:42", "b:42"]
    assert results_seen == [("a", 42), ("b", 42)]


@pytest.mark.asyncio
async def test_call_hooks_mixed_sync_async():
    def s(x): return f"s:{x}"
    async def a(x): return f"a:{x}"
    register_hook("evt", s)
    register_hook("evt", a)
    out = await call_hooks("evt", 7)
    assert out == ["s:7", "a:7"]


@pytest.mark.asyncio
async def test_call_hooks_swallows_exception():
    def boom(x): raise RuntimeError("boom")
    def ok(x): return "ok"
    register_hook("evt", boom)
    register_hook("evt", ok)
    out = await call_hooks("evt", 1)
    assert out == [None, "ok"]  # 첫 hook 실패해도 두 번째 호출


def test_call_hooks_sync_skips_async():
    async def a(): return "a"
    def s(): return "s"
    register_hook("evt", a)
    register_hook("evt", s)
    out = call_hooks_sync("evt")
    assert out == ["s"]  # async 는 skip


@pytest.mark.asyncio
async def test_no_hooks_returns_empty():
    out = await call_hooks("nonexistent")
    assert out == []


def test_clear_named():
    def fn(): pass
    register_hook("a", fn)
    register_hook("b", fn)
    clear_hooks("a")
    assert list_hooks("a") == []
    assert fn in list_hooks("b")
