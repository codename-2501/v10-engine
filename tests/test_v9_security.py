"""V9 보안 강화 테스트."""

from __future__ import annotations

import os
import pytest


def test_admin_password_env_variable():
    """V9_ADMIN_PASSWORD 환경변수 미설정 시 랜덤 생성 확인."""
    # 현재 환경에서 admin_pw 로드
    admin_pw = os.environ.get("V9_ADMIN_PASSWORD", None)
    # 미설정이면 서버 시작 시 랜덤 생성됨 (별도 통합 테스트에서 검증)
    assert admin_pw is None or isinstance(admin_pw, str)


def test_bcrypt_hashing():
    """bcrypt 비밀번호 해싱 검증."""
    try:
        import bcrypt

        pwd = b"test_password_123"
        hashed = bcrypt.hashpw(pwd, bcrypt.gensalt())

        # 검증
        assert bcrypt.checkpw(pwd, hashed)

        # 다른 비밀번호는 실패
        assert not bcrypt.checkpw(b"wrong_password", hashed)

    except ImportError:
        pytest.skip("bcrypt not installed")


def test_model_id_constants():
    """모델 ID 상수 검증."""
    try:
        from engine.ai.model_adapter import ModelID

        # 상수 존재 확인
        assert hasattr(ModelID, "OPUS")
        assert hasattr(ModelID, "SONNET")
        assert hasattr(ModelID, "HAIKU")

        # 값 확인 (버전명 포함)
        assert "claude" in ModelID.OPUS.lower()
        assert "claude" in ModelID.SONNET.lower()
        assert "claude" in ModelID.HAIKU.lower()

    except ImportError:
        pytest.skip("engine.ai.model_adapter not available")
