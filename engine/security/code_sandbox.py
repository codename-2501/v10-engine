"""
engine/security/code_sandbox.py
CodeVerificationSandbox — 3단계 코드 검증.
Stage 1: AST 파싱 (구문 오류)
Stage 2: 정적 위험 분석 (금지 패턴)
Stage 3: Docker 격리 실행 (선택적)
subprocess+ulimit 미사용 — 크로스 플랫폼 호환.
"""

from __future__ import annotations

import ast
import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# 결과 타입
# ---------------------------------------------------------------------------

@dataclass
class RiskAnalysis:
    imports: list[str]
    matched: list[str]      # 금지 패턴 + 미허용 모듈
    score: int
    is_blocked: bool


@dataclass
class VerificationResult:
    stage: str              # 'AST' | 'STATIC' | 'DOCKER'
    passed: bool
    output: dict            # stage별 세부 정보

    def to_db_json(self) -> str:
        import json
        return json.dumps({
            "stage": self.stage,
            "passed": self.passed,
            **self.output,
        })


@dataclass
class ArtifactForVerification:
    """CodeVerificationSandbox.verify()에 전달하는 DTO."""
    content: str            # 파이썬 소스코드
    test_content: str = ""  # 테스트 코드 (Stage 3 실행용)
    file_type: str = "py"


# ---------------------------------------------------------------------------
# 샌드박스
# ---------------------------------------------------------------------------

class CodeVerificationSandbox:
    """
    3단계 코드 검증 샌드박스.
    비코드 산출물(md, json 등)에는 Stage 1~2만 적용 (AST 파싱 스킵).
    """

    ALLOWED_MODULES: frozenset[str] = frozenset({
        # 표준 라이브러리
        "os", "sys", "json", "re", "math", "datetime", "pathlib",
        "typing", "collections", "itertools", "functools", "dataclasses",
        "abc", "enum", "asyncio", "hashlib", "base64", "uuid",
        "contextlib", "logging", "time", "copy", "io",
        # 허용 외부 패키지 (pip 7개)
        "fastapi", "uvicorn", "aiohttp", "jwt", "cryptography",
        "structlog", "prometheus_client", "pydantic",
        # Anthropic SDK
        "anthropic",
    })

    BLOCKED_PATTERNS: list[str] = [
        r"subprocess",
        r"\beval\s*\(",
        r"\bexec\s*\(",
        r"__import__\s*\(",
        r"open\s*\(.*['\"]w['\"]",     # 쓰기 모드 파일 오픈
        r"open\s*\(.*['\"]a['\"]",     # 추가 쓰기 모드
        r"os\.system\s*\(",
        r"os\.popen\s*\(",
        r"shutil\.rmtree",
        r"\bsocket\s*\.",
        r"importlib\.import_module",
        r"ctypes",
        r"pickle\.loads",
        r"marshal\.loads",
    ]

    # Docker 이미지 (Stage 3)
    DOCKER_IMAGE = "python:3.12-slim"
    DOCKER_TIMEOUT = 30  # 초

    async def verify(self, artifact: ArtifactForVerification) -> VerificationResult:
        """
        비코드 파일(py 아닌 경우) → Stage 2(패턴 검사)만.
        Python 파일 → Stage 1 → Stage 2 → (Stage 3: Docker 선택적).
        """
        if artifact.file_type != "py":
            # 비코드: 패턴 검사만
            matched = self._pattern_scan(artifact.content)
            score = len(matched) * 20
            passed = score < 20
            return VerificationResult(
                stage="STATIC",
                passed=passed,
                output={"matched_patterns": matched, "risk_score": score},
            )

        # Stage 1: AST 파싱
        try:
            tree = ast.parse(artifact.content)
        except SyntaxError as exc:
            return VerificationResult(
                stage="AST",
                passed=False,
                output={"error": str(exc), "lineno": exc.lineno},
            )

        # Stage 2: 정적 위험 분석
        risk = self._analyze_ast(tree, artifact.content)
        if risk.is_blocked:
            return VerificationResult(
                stage="STATIC",
                passed=False,
                output={
                    "blocked_patterns": risk.matched,
                    "risk_score": risk.score,
                },
            )

        # Stage 3: Docker (선택적 — 테스트 코드 + Docker 사용 가능 시)
        if artifact.test_content and await self._docker_available():
            return await self._run_in_docker(artifact)

        return VerificationResult(
            stage="STATIC",
            passed=True,
            output={"risk_score": risk.score, "modules_used": risk.imports},
        )

    # ------------------------------------------------------------------
    # 내부 분석
    # ------------------------------------------------------------------

    def _analyze_ast(self, tree: ast.AST, source: str) -> RiskAnalysis:
        """AST 워크 → import 목록 + 금지 패턴 매칭."""
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module.split(".")[0])

        blocked_imports = [m for m in imports if m not in self.ALLOWED_MODULES]
        matched_patterns = self._pattern_scan(source)

        score = len(blocked_imports) * 10 + len(matched_patterns) * 20
        return RiskAnalysis(
            imports=imports,
            matched=matched_patterns + blocked_imports,
            score=score,
            is_blocked=(score >= 20),
        )

    def _pattern_scan(self, source: str) -> list[str]:
        """금지 패턴 매칭. 매칭된 패턴 목록 반환."""
        return [p for p in self.BLOCKED_PATTERNS if re.search(p, source)]

    # ------------------------------------------------------------------
    # Docker
    # ------------------------------------------------------------------

    async def _docker_available(self) -> bool:
        """Docker CLI 사용 가능 여부 확인."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except (FileNotFoundError, OSError):
            return False

    async def _run_in_docker(
        self, artifact: ArtifactForVerification
    ) -> VerificationResult:
        """
        Docker 격리 컨테이너에서 테스트 실행.
        --network=none: 네트워크 차단
        --read-only: 읽기 전용 파일시스템
        --memory=256m / --cpus=0.5: 리소스 제한
        """
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "code.py"
            test_file = Path(tmpdir) / "test_code.py"
            src_file.write_text(artifact.content)
            test_file.write_text(artifact.test_content)

            cmd = [
                "docker", "run", "--rm",
                "--network=none",
                "--read-only",
                "--memory=256m",
                "--cpus=0.5",
                "--tmpfs", "/tmp:size=64m",
                "-v", f"{tmpdir}:/workspace:ro",
                "-w", "/workspace",
                self.DOCKER_IMAGE,
                "python", "-m", "pytest", "test_code.py", "-x", "-q",
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.DOCKER_TIMEOUT
                )
                exit_code = proc.returncode
                passed = exit_code == 0
                return VerificationResult(
                    stage="DOCKER",
                    passed=passed,
                    output={
                        "docker_exit_code": exit_code,
                        "stdout": stdout.decode(errors="replace")[:2000],
                        "stderr": stderr.decode(errors="replace")[:1000],
                    },
                )
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                return VerificationResult(
                    stage="DOCKER",
                    passed=False,
                    output={"error": f"Docker 실행 타임아웃 ({self.DOCKER_TIMEOUT}초)"},
                )
