"""
engine/platform/sleep_guard.py
SleepGuard — 크로스 플랫폼 절전 방지.
macOS:   caffeinate -i (내장)
Linux:   systemd-inhibit (선택적, 없으면 스킵)
Windows: SetThreadExecutionState Win32 API
비동기 컨텍스트 매니저로 사용.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


class SleepGuard:
    """
    사용법:
        async with SleepGuard():
            await engine.run()
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._os = platform.system()

    async def __aenter__(self) -> "SleepGuard":
        if self._os == "Darwin":
            try:
                self._process = subprocess.Popen(
                    ["caffeinate", "-i"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("sleep_guard_active method=%s", "caffeinate")
            except FileNotFoundError:
                logger.warning("sleep_guard_caffeinate_not_found")

        elif self._os == "Linux":
            try:
                self._process = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=idle",
                        "--who=AI-SI-Platform",
                        "--why=engine-running",
                        "--mode=block",
                        "sleep", "infinity",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("sleep_guard_active method=%s", "systemd-inhibit")
            except FileNotFoundError:
                logger.info("sleep_guard_skipped reason=%s", "systemd-inhibit not found")

        elif self._os == "Windows":
            self._win_prevent_sleep()
            logger.info("sleep_guard_active method=%s", "SetThreadExecutionState")

        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._process:
            self._process.terminate()
            self._process = None

        if self._os == "Windows":
            self._win_allow_sleep()

        logger.info("sleep_guard_released")

    # ------------------------------------------------------------------
    # Windows: SetThreadExecutionState
    # ------------------------------------------------------------------

    def _win_prevent_sleep(self) -> None:
        try:
            import ctypes
            ES_CONTINUOUS      = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
        except Exception as exc:
            logger.warning("sleep_guard_windows_failed error=%s", str(exc))

    def _win_allow_sleep(self) -> None:
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception as exc:
            logger.warning("sleep_guard_windows_release_failed error=%s", str(exc))
