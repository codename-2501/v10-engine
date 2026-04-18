"""
engine/security/crypto.py
AES-256-GCM 암호화 — 환경변수, API 키, OAuth 설정 등 민감 데이터 보호.
의존: cryptography (pip)
"""

from __future__ import annotations

import base64
import os
import secrets


class CryptoError(Exception):
    """암호화/복호화 실패."""


class AES256GCM:
    """
    AES-256-GCM 대칭 암호화.
    - nonce: 12바이트 랜덤 (매 암호화마다 새로 생성)
    - 인증 태그: AESGCM이 자동 처리 (16바이트)
    - 저장 형식: base64(nonce[12] + ciphertext + tag[16])
    """

    KEY_BYTES = 32  # 256 bit

    @staticmethod
    def generate_key() -> str:
        """
        새 256-bit 키 생성.
        반환: hex 문자열 (64자).
        운영 환경에서는 환경변수(PLATFORM_ENCRYPT_KEY)로 관리.
        """
        return secrets.token_hex(AES256GCM.KEY_BYTES)

    @staticmethod
    def encrypt(plaintext: str, key_hex: str) -> str:
        """
        평문 → base64 인코딩된 암호문.
        key_hex: 64자 hex 문자열 (32바이트).
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise CryptoError("cryptography 패키지 미설치. pip install cryptography") from exc

        try:
            key = bytes.fromhex(key_hex)
        except ValueError as exc:
            raise CryptoError(f"유효하지 않은 키 형식: {exc}") from exc

        if len(key) != AES256GCM.KEY_BYTES:
            raise CryptoError(
                f"키 길이 오류: {len(key)}바이트 (필요: {AES256GCM.KEY_BYTES}바이트)"
            )

        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        except Exception as exc:
            raise CryptoError(f"암호화 실패: {exc}") from exc

        return base64.b64encode(nonce + ciphertext).decode("ascii")

    @staticmethod
    def decrypt(encrypted_b64: str, key_hex: str) -> str:
        """
        base64 암호문 → 평문.
        복호화 실패(키 불일치, 변조 등) 시 CryptoError 발생.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise CryptoError("cryptography 패키지 미설치. pip install cryptography") from exc

        try:
            key = bytes.fromhex(key_hex)
        except ValueError as exc:
            raise CryptoError(f"유효하지 않은 키 형식: {exc}") from exc

        try:
            raw = base64.b64decode(encrypted_b64)
        except Exception as exc:
            raise CryptoError(f"base64 디코딩 실패: {exc}") from exc

        if len(raw) < 12 + 16:
            raise CryptoError("암호문 길이 부족 (nonce 12B + 최소 태그 16B 필요)")

        nonce, ciphertext = raw[:12], raw[12:]
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
        except Exception as exc:
            raise CryptoError(
                "복호화 실패 — 키 불일치 또는 데이터 변조"
            ) from exc

    @staticmethod
    def key_from_env(env_var: str = "PLATFORM_ENCRYPT_KEY") -> str:
        """
        환경변수에서 암호화 키 로드.
        미설정 시 RuntimeError.
        """
        key = os.environ.get(env_var, "")
        if not key:
            raise RuntimeError(
                f"암호화 키 환경변수 미설정: {env_var}\n"
                f"  → python -c \"import secrets; print(secrets.token_hex(32))\" 로 생성 후 설정"
            )
        if len(key) != 64:
            raise RuntimeError(
                f"{env_var} 길이 오류: {len(key)}자 (필요: 64자 hex)"
            )
        return key
