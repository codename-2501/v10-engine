"""
engine/ai/model_adapter.py
Model Provider Adapter — Anthropic Claude API 호출 추상화.
CredentialProvider: api_key / OAuth 방식 모두 지원.
에이전트 루프 완전 금지 — Python 엔진이 단일 API 호출 후 결과 파싱.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

from engine.security.crypto import AES256GCM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 모델 ID 상수
# ---------------------------------------------------------------------------

class ModelID:
    OPUS   = "claude-opus-4-7"
    SONNET = "claude-sonnet-4-6"
    HAIKU  = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Stage 2-A: API 레벨 세마포어 (코어 DAGAdvancer 불변, API 호출만 제한)
# ---------------------------------------------------------------------------
# DAGAdvancer MAX_CONCURRENT_NODES=5 는 코어라 수정 불가. API 호출 자체의
# 동시성을 3(기본)로 제한해 Max CLI proxy 과부하 → 529 연쇄를 억제한다.
# V8_API_CONCURRENCY=0 이면 세마포어 bypass (rollback 플래그).

_API_CONCURRENCY = int(os.environ.get("V8_API_CONCURRENCY", "3"))
_api_semaphore: asyncio.Semaphore | None = (
    asyncio.Semaphore(_API_CONCURRENCY) if _API_CONCURRENCY > 0 else None
)


async def _acquire_api_slot():
    """세마포어 acquire. bypass 시 즉시 통과."""
    if _api_semaphore is not None:
        await _api_semaphore.acquire()


def _release_api_slot() -> None:
    """세마포어 release. bypass 시 no-op."""
    if _api_semaphore is not None:
        _api_semaphore.release()


# ---------------------------------------------------------------------------
# Stage 2-C: In-memory cache 지표 트래커 (frontend endpoint 에서 소비)
# ---------------------------------------------------------------------------
# budget_enforcer 는 코어라 수정 불가. cache_read/creation tokens 는 별도
# 카운터로 추적해 /api/v1/metrics/cache 에서 노출. 프로세스 재시작 시 리셋.

class _CacheMetrics:
    def __init__(self) -> None:
        self.total_input_tokens: int = 0
        self.total_cache_read: int = 0
        self.total_cache_write: int = 0
        self.call_count: int = 0

    def record(self, input_tokens: int, cache_read: int, cache_write: int) -> None:
        self.total_input_tokens += input_tokens
        self.total_cache_read += cache_read
        self.total_cache_write += cache_write
        self.call_count += 1

    def snapshot(self) -> dict:
        total_billed = self.total_input_tokens  # API 가 보고하는 input 은 이미 cache_read 제외
        total_would_have_been = total_billed + self.total_cache_read
        hit_ratio = (
            self.total_cache_read / max(1, total_would_have_been)
        )
        return {
            "call_count": self.call_count,
            "total_input_tokens_billed": total_billed,
            "total_cache_read_tokens": self.total_cache_read,
            "total_cache_write_tokens": self.total_cache_write,
            "cache_hit_ratio": round(hit_ratio, 4),
            "tokens_saved_by_cache": self.total_cache_read,
        }


CACHE_METRICS = _CacheMetrics()


# ---------------------------------------------------------------------------
# API 응답
# ---------------------------------------------------------------------------

@dataclass
class APIResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str     # 'end_turn' | 'max_tokens' | 'stop_sequence'
    cache_read_tokens: int = 0    # Prompt Caching: 캐시 히트 읽기 토큰
    cache_write_tokens: int = 0   # Prompt Caching: 캐시 신규 쓰기 토큰
    tool_used: bool = False        # Tool Use: 응답이 tool_use 블록인지 여부

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Anthropic API 호출 실패 (4xx/5xx)."""
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"API 오류 [{status}]: {message}")
        self.status = status


class RateLimitError(APIError):
    """HTTP 429 Rate Limit."""
    def __init__(self) -> None:
        super().__init__(429, "Rate Limit 초과")


# ---------------------------------------------------------------------------
# 과부하 자동 재시도 유틸
# ---------------------------------------------------------------------------

_OVERLOAD_STATUSES = {429, 529, 503}
# 네트워크 일시 장애도 동일 backoff 정책으로 재시도 (retry_count 비소비).
# 502/504는 일시적 게이트웨이 이슈 (영구 4xx와 구분).
_TRANSIENT_HTTP_STATUSES = _OVERLOAD_STATUSES | {502, 504}
# 환경변수로 오버라이드 가능 (개발: 낮게, 프로덕션: 기본값 유지)
_OVERLOAD_BACKOFFS = tuple(
    int(x) for x in
    os.environ.get("V9_RETRY_BACKOFFS", "30,60,120").split(",")
)

# 라이브러리·OS 레벨 일시 오류 화이트리스트
# - aiohttp: ClientConnectionError / ServerDisconnectedError / ClientOSError
# - socket: gaierror (ENOTFOUND), timeout
# - 기타: ConnectionResetError, ConnectionRefusedError
_TRANSIENT_EXCEPTIONS: tuple = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
    socket.gaierror,
    socket.timeout,
    ConnectionResetError,
    ConnectionRefusedError,
    ConnectionAbortedError,
    OSError,  # generic network OS error — 구체 subclass 아닌 경우 대비 (맨 끝)
)

# 영구 오류 패턴 (문자열 매치) — 이건 재시도해도 의미 없음. 즉시 raise.
_PERMANENT_ERROR_PATTERNS = (
    "hit your limit", "quota", "insufficient", "invalid api",
    "unauthori", "forbidden", "authentication", "billing",
    "invalid_request", "invalid argument",
)


def _is_transient_error(exc: Exception) -> tuple[bool, str]:
    """exc가 일시적 오류인지 판정. (True, reason)/ (False, reason)."""
    # 1) APIError status 기반
    if isinstance(exc, APIError):
        status = getattr(exc, "status", 0)
        if status in _TRANSIENT_HTTP_STATUSES:
            return True, f"http_{status}"
        msg_l = str(exc).lower()
        if any(p in msg_l for p in _PERMANENT_ERROR_PATTERNS):
            return False, "permanent_api"
        # 5xx 이외는 재시도 대상 아님
        return False, f"http_{status}_permanent"

    # 2) 일시 예외 화이트리스트
    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        # OSError 중 ENOTFOUND (errno 8, -2, -3) 또는 일반 연결 에러만
        if isinstance(exc, OSError) and not isinstance(
            exc,
            (ConnectionResetError, ConnectionRefusedError,
             ConnectionAbortedError, socket.gaierror, socket.timeout),
        ):
            # 일반 OSError는 보수적으로 transient 인정 안 함
            return False, "generic_oserror"
        return True, type(exc).__name__

    # 3) 문자열 매치 (nested exception 대비)
    msg_l = str(exc).lower()
    if "enotfound" in msg_l or "getaddrinfo failed" in msg_l:
        return True, "dns_enotfound"
    if "timed out" in msg_l or "timeout" in msg_l:
        return True, "timeout"
    if "connection reset" in msg_l or "connection refused" in msg_l:
        return True, "conn_reset"
    if any(p in msg_l for p in _PERMANENT_ERROR_PATTERNS):
        return False, "permanent_match"

    return False, "unknown"


async def _retry_on_transient(coro_factory, max_retries=3, backoffs=_OVERLOAD_BACKOFFS):
    """일시 오류(429/529/503/502/504 + 네트워크 예외) 시 지수 백오프 재시도.

    기존 _retry_on_overload 확장판. retry_count 비소비(엔진 레벨 retry 카운트에
    영향 없음 — model_adapter 내부 재시도).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            is_trans, reason = _is_transient_error(exc)
            if is_trans and attempt < max_retries:
                base_wait = backoffs[min(attempt, len(backoffs) - 1)]
                # ±25% jitter — 동시 재시도 thundering herd 방지
                wait = base_wait * (0.75 + random.random() * 0.5)
                logger.warning(
                    "transient_retry reason=%s attempt=%d/%d wait=%.1fs",
                    reason, attempt + 1, max_retries, wait,
                )
                await asyncio.sleep(wait)
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc


# Backwards-compat alias — 기존 호출부가 _retry_on_overload로 호출해도 동일 동작.
_retry_on_overload = _retry_on_transient


# ---------------------------------------------------------------------------
# CredentialProvider (추상)
# ---------------------------------------------------------------------------

class CredentialProvider(ABC):
    """API 인증 헤더 제공 인터페이스."""

    @abstractmethod
    async def get_headers(self) -> dict[str, str]: ...


class AnthropicAPIKeyProvider(CredentialProvider):
    """
    AES-256-GCM으로 암호화된 API 키를 복호화해서 헤더 제공.
    key_encrypted: DB의 provider_credentials.key_encrypted 값.
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, key_encrypted: str) -> None:
        self._key_encrypted = key_encrypted

    async def get_headers(self) -> dict[str, str]:
        encrypt_key = AES256GCM.key_from_env("PLATFORM_ENCRYPT_KEY")
        api_key = AES256GCM.decrypt(self._key_encrypted, encrypt_key)
        return {
            "x-api-key": api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "anthropic-beta": "prompt-caching-2024-07-31",
            "content-type": "application/json",
        }


class AnthropicPlaintextKeyProvider(CredentialProvider):
    """
    평문 API 키 (개발/테스트 환경).
    운영 환경에서는 AnthropicAPIKeyProvider 사용.
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    async def get_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 환경변수 미설정")
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "content-type": "application/json",
        }


class OAuthProvider(CredentialProvider):
    """
    OAuth Bearer 토큰 자동 갱신.
    oauth_config_encrypted: DB의 provider_credentials.oauth_config_encrypted.
    """

    _REFRESH_TIMEOUT = aiohttp.ClientTimeout(
        total=int(os.environ.get("V9_OAUTH_TIMEOUT", "30")),
        connect=int(os.environ.get("V9_OAUTH_CONNECT_TIMEOUT", "10")),
    )

    def __init__(
        self,
        oauth_config_encrypted: str,
        token_expires_at: str | None,
    ) -> None:
        self._config_encrypted = oauth_config_encrypted
        self._token_expires_at = token_expires_at
        self._access_token: str | None = None
        self._refresh_lock = asyncio.Lock()

    async def get_headers(self) -> dict[str, str]:
        if self._is_expired():
            async with self._refresh_lock:
                if self._is_expired():
                    await self._refresh_token()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "content-type": "application/json",
            # S3-2: 다른 두 provider 와 일관성 — prompt caching 활성화
            "anthropic-beta": "prompt-caching-2024-07-31",
        }

    def _is_expired(self) -> bool:
        if not self._access_token or not self._token_expires_at:
            return True
        try:
            exp = datetime.fromisoformat(self._token_expires_at)
            return datetime.now(timezone.utc) >= exp
        except ValueError:
            return True

    async def _refresh_token(self) -> None:
        encrypt_key = AES256GCM.key_from_env("PLATFORM_ENCRYPT_KEY")
        import json
        config = json.loads(AES256GCM.decrypt(self._config_encrypted, encrypt_key))

        async with aiohttp.ClientSession(timeout=self._REFRESH_TIMEOUT) as session:
            async with session.post(
                config["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": config["refresh_token"],
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                },
            ) as resp:
                if resp.status != 200:
                    raise APIError(resp.status, "OAuth 토큰 갱신 실패")
                data = await resp.json()
                self._access_token = data["access_token"]
                self._token_expires_at = data.get("expires_at")


# ---------------------------------------------------------------------------
# ModelAdapter — 단일 API 호출 (에이전트 루프 금지)
# ---------------------------------------------------------------------------

class ModelAdapter:
    """
    Anthropic Messages API 단일 호출 래퍼.
    Rule: 에이전트 루프 절대 금지 — Python 엔진이 결과를 받아 다음 액션 결정.
    재시도 로직: 호출자(BudgetEnforcer/Orchestrator)가 담당.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    _API_TIMEOUT = aiohttp.ClientTimeout(
        connect=int(os.environ.get("V9_LLM_CONNECT_TIMEOUT", "10")),
        sock_read=int(os.environ.get("V9_LLM_SOCK_READ_TIMEOUT", "300")),
    )

    def __init__(self, credential_provider: CredentialProvider) -> None:
        self._creds = credential_provider

    async def call(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str = "",
        temperature: float = 0.3,
        tools: list | None = None,
        tool_choice: str | dict | None = None,
    ) -> APIResponse:
        """
        단일 Messages API 호출.
        max_tokens: BudgetEnforcer L2가 주입하는 하드 제한.

        Prompt Caching 적용 (anthropic-beta: prompt-caching-2024-07-31):
          - system prompt: cache_control {"type": "ephemeral"} 블록으로 전달
          - 첫 번째 user message (large context): cache_control 적용
          - 캐싱 조건: system 1024+ tokens, user message 1024+ tokens
        """
        headers = await self._creds.get_headers()

        # Prompt Caching: system prompt를 cache_control 블록으로 구성
        # system이 충분히 길 때(≥1024 tokens ≈ 4096자)만 캐싱 적용
        # 짧은 system은 단순 문자열로 전달 (캐싱 오버헤드 방지)
        if system:
            if len(system) >= 4096:
                # 긴 system prompt: cache_control 블록으로 캐시 적용
                body_system: list | str = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                body_system = system
        else:
            body_system = ""

        # 첫 번째 user message: 대형 컨텍스트(≥4096자)일 때 캐시 적용
        if len(prompt) >= 4096:
            user_content: list | str = [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            user_content = prompt

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user_content}],
        }
        if body_system:
            body["system"] = body_system
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or {"type": "auto"}

        async def _call_once():
            # Stage 2-A: API 세마포어 acquire — 실제 HTTP 호출 동시성 제한
            await _acquire_api_slot()
            try:
                async with aiohttp.ClientSession(timeout=self._API_TIMEOUT) as session:
                    async with session.post(
                        self.API_URL,
                        headers=headers,
                        json=body,
                    ) as resp:
                        if resp.status == 429:
                            raise RateLimitError()
                        if resp.status >= 400:
                            text = await resp.text()
                            raise APIError(resp.status, text[:500])

                        data = await resp.json()
            finally:
                _release_api_slot()

            usage = data.get("usage", {})
            first_block = data["content"][0]
            if first_block["type"] == "tool_use":
                import json
                content = json.dumps(first_block["input"])
                tool_used = True
            else:
                content = first_block["text"]
                tool_used = False

            return APIResponse(
                content=content,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                model=data["model"],
                stop_reason=data.get("stop_reason", "end_turn"),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
                tool_used=tool_used,
            )

        result = await _retry_on_overload(_call_once)
        # S3-2: cache hit/miss 를 logger 로 노출 (events.py 와 별도 — 토큰 효율 관측)
        if result.cache_read_tokens or result.cache_write_tokens:
            logger.info(
                "prompt_cache model=%s read=%d write=%d input=%d (saved≈%.0f%%)",
                model[:16], result.cache_read_tokens, result.cache_write_tokens,
                result.input_tokens,
                100 * result.cache_read_tokens
                / max(1, result.cache_read_tokens + result.input_tokens),
            )
        # Stage 2-C: 전역 cache 지표 업데이트 (/api/v1/metrics/cache 에서 소비)
        CACHE_METRICS.record(
            input_tokens=result.input_tokens,
            cache_read=result.cache_read_tokens,
            cache_write=result.cache_write_tokens,
        )
        return result

    async def call_stream(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str = "",
        temperature: float = 0.3,
        tools: list | None = None,
        tool_choice: str | dict | None = None,
    ):
        """
        async generator — LLM 응답을 토큰 단위로 yield.
        완료 시 None yield. Anthropic Messages API SSE streaming.
        """
        headers = await self._creds.get_headers()

        if system:
            if len(system) >= 4096:
                body_system: list | str = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                body_system = system
        else:
            body_system = ""

        if len(prompt) >= 4096:
            user_content: list | str = [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            user_content = prompt

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user_content}],
            "stream": True,
        }
        if body_system:
            body["system"] = body_system
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or {"type": "auto"}

        await _acquire_api_slot()
        try:
            async with aiohttp.ClientSession(timeout=self._API_TIMEOUT) as session:
                async with session.post(
                    self.API_URL,
                    headers=headers,
                    json=body,
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise APIError(resp.status, text[:500])

                    async for raw_line in resp.content:
                        line = raw_line.strip()
                        if not line or not line.startswith(b"data: "):
                            continue
                        data_str = line[6:]
                        if data_str == b"[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    yield delta.get("text", "")
                            elif event.get("type") == "message_stop":
                                break
                        except json.JSONDecodeError:
                            pass
            # 정상 종료 신호 — try 블록 내부 (finally 내 yield는 GeneratorExit 충돌)
            yield None
        finally:
            _release_api_slot()


# ---------------------------------------------------------------------------
# CLIProxyAdapter — Claude Code CLI를 프록시로 사용 (Pro/Max 구독)
# ---------------------------------------------------------------------------

class CLIProxyAdapter:
    """
    Claude Code CLI(claude --print)를 서브프로세스로 호출.
    Pro/Max 구독 크레딧 사용 — API Key 불필요.
    ModelAdapter와 동일한 call() 인터페이스.

    config_dir: CLAUDE_CONFIG_DIR 경로. None이면 기본 ~/.claude 사용.
                여러 계정(Pro/Max)을 각각 다른 config_dir로 분리 가능.
    """

    def __init__(self, cli_path: str = "claude", config_dir: str | None = None) -> None:
        self._cli = cli_path
        if config_dir:
            import pathlib
            config_path = pathlib.Path(config_dir).resolve()
            home = pathlib.Path.home()
            try:
                config_path.relative_to(home)
            except ValueError:
                raise ValueError(
                    f"config_dir는 사용자 홈 디렉터리({home}) 하위여야 합니다. "
                    f"입력값: {config_path}"
                )
            self._config_dir: str | None = str(config_path)
        else:
            self._config_dir = None

    async def call(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str = "",
        temperature: float = 0.3,
        tools: list | None = None,
        tool_choice: str | dict | None = None,
    ) -> APIResponse:
        """Claude Code CLI --print 모드로 단일 호출 (stdin 전달 — 임시 파일 없음). tools는 무시됨."""
        import json

        # system prompt + user prompt 결합 → stdin으로 직접 전달
        full_prompt = f"{system}\n\n---\n\n{prompt}" if system else prompt

        cmd = [
            self._cli,
            "--print",
            "--model", model,
            "--output-format", "json",
            "--max-turns", "1",
            "--no-session-persistence",
            "--tools", "",                      # 모든 도구 비활성화 (파일/셸 접근 차단)
            "--strict-mcp-config",              # 사용자 MCP 서버 로드 차단
            "--mcp-config", '{"mcpServers":{}}',
        ]

        # 민감한 환경변수 제거 (서버 크리덴셜이 서브프로세스로 노출되지 않도록)
        _deny_patterns = (
            "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PWD",
            "CREDENTIAL", "AUTH", "DATABASE_URL", "DB_URL",
        )
        env = {
            k: v for k, v in os.environ.items()
            if not any(p in k.upper() for p in _deny_patterns)
        }
        if self._config_dir:
            env["CLAUDE_CONFIG_DIR"] = self._config_dir

        async def _call_once():
            # Stage 2-A: CLI proxy 에도 세마포어 적용 (API 레벨 동시성 통합 제어)
            await _acquire_api_slot()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=full_prompt.encode("utf-8")),
                    timeout=1200,
                )
            finally:
                _release_api_slot()

            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace")[:500]
                out_msg = stdout.decode("utf-8", errors="replace")[:500]
                combined = f"{err_msg} {out_msg}".lower()
                # 과부하 패턴 감지 → APIError(529)로 raise하여 재시도 대상
                if any(p in combined for p in ("overloaded", "server busy", "503", "529", "capacity", "hit your limit", "hit_your_limit")):
                    raise APIError(529, f"CLI 과부하: {err_msg or out_msg}")
                logger.error("cli_failed rc=%d stderr=%s stdout=%s", proc.returncode, repr(err_msg), repr(out_msg))
                raise APIError(proc.returncode, f"CLI 오류: {err_msg or out_msg}")

            raw = stdout.decode("utf-8").strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return APIResponse(
                    content=raw,
                    input_tokens=0,
                    output_tokens=len(raw) // 4,
                    model=model,
                    stop_reason="end_turn",
                )

            # CLI가 is_error=true로 반환한 경우 (API 오류를 result에 담아서 반환)
            if data.get("is_error"):
                err_result = str(data.get("result", ""))
                if any(p in err_result.lower() for p in ("overloaded", "529", "503", "hit your limit", "limit")):
                    raise APIError(429, f"CLI 한도 초과: {err_result[:500]}")
                raise APIError(1, f"CLI 오류: {err_result[:500]}")

            result = data.get("result", raw)

            # CLI json 모드에서 result가 이스케이프된 JSON 문자열일 수 있음
            if isinstance(result, str) and (r'\"' in result or r'\n' in result):
                try:
                    unescaped = json.loads(f'"{result}"')
                    result = unescaped
                except (json.JSONDecodeError, ValueError):
                    pass

            usage = data.get("usage", data.get("modelUsage", {}).get(model, {}))
            return APIResponse(
                content=result,
                input_tokens=usage.get("input_tokens", usage.get("inputTokens", 0)),
                output_tokens=usage.get("output_tokens", usage.get("outputTokens", 0)),
                model=model,
                stop_reason=data.get("stop_reason", "end_turn"),
            )

        return await _retry_on_overload(_call_once)

    async def call_stream(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str = "",
        temperature: float = 0.3,
        tools: list | None = None,
        tool_choice: str | dict | None = None,
    ):
        """CLI는 streaming 미지원. call()을 1회 실행하고 결과를 단일 청크로 yield."""
        result = await self.call(
            model=model,
            prompt=prompt,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        yield result.content
        yield None
