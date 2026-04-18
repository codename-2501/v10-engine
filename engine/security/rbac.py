"""
engine/security/rbac.py
RBAC — 역할 3계층 (ADMIN / SENIOR_DESIGNER / DESIGNER).
권한 검사는 순수 함수 — DB 의존 없음.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    ADMIN           = "ADMIN"
    SENIOR_DESIGNER = "SENIOR_DESIGNER"
    DESIGNER        = "DESIGNER"


class Permission(str, Enum):
    # 시스템 관리
    MANAGE_SYSTEM           = "MANAGE_SYSTEM"
    MANAGE_CREDENTIALS      = "MANAGE_CREDENTIALS"
    ACTIVATE_CONSTITUTION   = "ACTIVATE_CONSTITUTION"

    # 프로젝트
    CREATE_PROJECT          = "CREATE_PROJECT"
    DELETE_PROJECT          = "DELETE_PROJECT"
    VIEW_PROJECT            = "VIEW_PROJECT"
    UPDATE_PROJECT          = "UPDATE_PROJECT"

    # 게이트 / 에스컬레이션
    APPROVE_GATE            = "APPROVE_GATE"
    HANDLE_ESCALATION       = "HANDLE_ESCALATION"

    # 노드
    RETRY_NODE              = "RETRY_NODE"
    SUSPEND_NODE            = "SUSPEND_NODE"

    # 산출물
    VIEW_ARTIFACT           = "VIEW_ARTIFACT"
    DELETE_ARTIFACT         = "DELETE_ARTIFACT"

    # 인테이크
    MANAGE_INTAKE           = "MANAGE_INTAKE"


# 역할 → 권한 매핑 (불변)
_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),  # 모든 권한

    Role.SENIOR_DESIGNER: frozenset({
        Permission.CREATE_PROJECT,
        Permission.DELETE_PROJECT,
        Permission.VIEW_PROJECT,
        Permission.UPDATE_PROJECT,
        Permission.APPROVE_GATE,
        Permission.HANDLE_ESCALATION,
        Permission.RETRY_NODE,
        Permission.SUSPEND_NODE,
        Permission.VIEW_ARTIFACT,
        Permission.DELETE_ARTIFACT,
        Permission.MANAGE_INTAKE,
    }),

    Role.DESIGNER: frozenset({
        Permission.VIEW_PROJECT,
        Permission.VIEW_ARTIFACT,
        Permission.RETRY_NODE,
    }),
}


class PermissionDeniedError(Exception):
    def __init__(self, role: str, permission: str) -> None:
        super().__init__(f"권한 없음: 역할={role}, 필요={permission}")


class RBAC:
    """
    역할 기반 접근 제어.
    모든 메서드는 순수 함수 — 상태 없음.
    """

    @staticmethod
    def has_permission(role: str, permission: Permission | str) -> bool:
        """특정 역할이 해당 권한을 가지는지 확인."""
        try:
            r = Role(role)
        except ValueError:
            return False
        perm = Permission(permission) if isinstance(permission, str) else permission
        return perm in _ROLE_PERMISSIONS.get(r, frozenset())

    @staticmethod
    def require(role: str, permission: Permission | str) -> None:
        """권한 없으면 PermissionDeniedError 발생."""
        if not RBAC.has_permission(role, permission):
            raise PermissionDeniedError(role, str(permission))

    @staticmethod
    def permissions_for(role: str) -> frozenset[Permission]:
        """역할의 전체 권한 목록."""
        try:
            r = Role(role)
        except ValueError:
            return frozenset()
        return _ROLE_PERMISSIONS.get(r, frozenset())

    @staticmethod
    def is_admin(role: str) -> bool:
        return role == Role.ADMIN

    @staticmethod
    def can_approve_gate(role: str) -> bool:
        return RBAC.has_permission(role, Permission.APPROVE_GATE)
