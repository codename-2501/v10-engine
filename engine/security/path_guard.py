"""
engine/security/path_guard.py
PathGuard — 경로 순회(Path Traversal) 공격 방어.
ALLOWED_ROOTS 외부 경로 접근 시 SecurityError 발생.
"""

from __future__ import annotations

import os
from pathlib import Path


class SecurityError(Exception):
    """PathGuard 또는 보안 정책 위반."""


class PathGuard:
    """
    허용된 루트 디렉토리 내 경로만 통과.
    ../../ 등 상위 이동 시도 차단.
    """

    # 기본 허용 루트 (절대 경로로 resolve)
    # 런타임에 configure_roots()로 재설정 가능
    _roots: list[Path] = []

    @classmethod
    def configure_roots(cls, base_dir: str | Path, *extra: str | Path) -> None:
        """
        허용 루트 설정 (앱 시작 시 한 번 호출).
        base_dir 기준 artifacts/, sandboxes/, backups/ + 추가 경로.
        """
        base = Path(base_dir).resolve()
        cls._roots = [
            (base / "artifacts").resolve(),
            (base / "sandboxes").resolve(),
            (base / "backups").resolve(),
        ]
        for extra_path in extra:
            cls._roots.append(Path(extra_path).resolve())

    @classmethod
    def _get_roots(cls) -> list[Path]:
        if not cls._roots:
            # 기본값: 현재 작업 디렉토리 기준
            cwd = Path.cwd()
            return [
                (cwd / "artifacts").resolve(),
                (cwd / "sandboxes").resolve(),
                (cwd / "backups").resolve(),
            ]
        return cls._roots

    @classmethod
    def validate(cls, path: str | Path) -> Path:
        """
        경로 유효성 검사.
        허용된 루트 내에 있으면 resolve된 Path 반환.
        아니면 SecurityError 발생.
        """
        try:
            resolved = Path(path).resolve()
        except (TypeError, ValueError) as exc:
            raise SecurityError(f"유효하지 않은 경로: {path!r}") from exc

        for root in cls._get_roots():
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue

        raise SecurityError(
            f"경로 순회 공격 차단: {path!r} → {resolved}\n"
            f"허용 루트: {[str(r) for r in cls._get_roots()]}"
        )

    @classmethod
    def safe_join(cls, root: str | Path, *parts: str) -> Path:
        """
        root + parts 결합 후 validate.
        외부 입력을 경로로 조합할 때 사용.
        """
        joined = Path(root).joinpath(*parts)
        return cls.validate(joined)

    @classmethod
    def is_safe(cls, path: str | Path) -> bool:
        """예외 없이 True/False 반환하는 편의 메서드."""
        try:
            cls.validate(path)
            return True
        except SecurityError:
            return False
