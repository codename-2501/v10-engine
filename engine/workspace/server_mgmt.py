"""
engine/workspace/server_mgmt.py
Stack detection, dependency install/build, server start/stop, process management.

Extracted from auto_deploy.py for modularity.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import asyncio
from pathlib import Path
from typing import Any

from engine.workspace.config_gen import (
    _generate_express_package_json,
    _generate_express_tsconfig,
    _generate_dev_env,
    _generate_frontend_env,
    _generate_nextjs_package_json,
    _generate_nextjs_tsconfig,
)
from engine.workspace.paths import _npm_safe_name

logger = logging.getLogger("engine.workspace.server_mgmt")


def _detect_stack(workspace_path: Path) -> dict:
    """워크스페이스 파일 분석 → 프론트/백엔드 스택 감지. 모든 주요 스택 지원."""
    stack: dict[str, Any] = {}
    fe = workspace_path / "frontend"
    be = workspace_path / "backend"

    # ── 프론트엔드 감지 ──
    if fe.is_dir():
        has_next_config = (fe / "next.config.js").is_file() or (fe / "next.config.mjs").is_file() or (fe / "next.config.ts").is_file()
        has_nuxt_config = (fe / "nuxt.config.ts").is_file() or (fe / "nuxt.config.js").is_file()
        has_vue = any(fe.rglob("*.vue"))
        has_svelte = any(fe.rglob("*.svelte"))
        has_tsx = any(fe.rglob("*.tsx")) or any(fe.rglob("*.jsx"))

        if has_next_config or (has_tsx and not has_vue and not has_svelte):
            stack["frontend"] = "nextjs"
        elif has_nuxt_config or has_vue:
            stack["frontend"] = "nuxt" if has_nuxt_config else "vue"
        elif has_svelte:
            stack["frontend"] = "svelte"
        elif (fe / "package.json").is_file():
            # package.json에서 프레임워크 감지
            try:
                pkg = json.loads((fe / "package.json").read_text())
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "next" in deps:
                    stack["frontend"] = "nextjs"
                elif "nuxt" in deps or "vue" in deps:
                    stack["frontend"] = "nuxt" if "nuxt" in deps else "vue"
                elif "svelte" in deps or "@sveltejs/kit" in deps:
                    stack["frontend"] = "svelte"
                elif "react" in deps:
                    stack["frontend"] = "react"  # CRA or Vite+React
                else:
                    stack["frontend"] = "static"
            except Exception:
                stack["frontend"] = "static"
        else:
            stack["frontend"] = "static"

    # ── 백엔드 감지 ──
    if be.is_dir():
        has_prisma = (be / "prisma").is_dir()
        has_ts = any(f for f in be.rglob("*.ts") if ".venv" not in str(f) and "node_modules" not in str(f))
        has_py = any(f for f in be.rglob("*.py") if ".venv" not in str(f))
        has_java = any(be.rglob("*.java"))
        has_go = any(be.rglob("*.go"))

        if has_prisma or has_ts:
            stack["backend"] = "express"
            stack["orm"] = "prisma" if has_prisma else "none"
        elif has_py:
            # FastAPI vs Django vs Flask
            has_django = any(f for f in be.rglob("*.py") if ".venv" not in str(f) and "manage.py" in f.name) or (be / "manage.py").is_file()
            if has_django:
                stack["backend"] = "django"
                stack["orm"] = "django-orm"
            else:
                stack["backend"] = "fastapi"
                stack["orm"] = "sqlalchemy"
        elif has_java:
            stack["backend"] = "spring"
            stack["orm"] = "jpa"
        elif has_go:
            stack["backend"] = "go"
            stack["orm"] = "none"
        else:
            stack["backend"] = "unknown"

    return stack


async def _run_cmd(
    cmd: list[str],
    cwd: Path,
    label: str = "",
    timeout: int = 300,
) -> None:
    """서브프로세스 실행 + 로깅."""
    logger.info("workspace_cmd_start label=%s cmd=%s cwd=%s", label, " ".join(cmd), cwd)
    try:
        result = await asyncio.to_thread(subprocess.run, 
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "workspace_cmd_warn label=%s rc=%s stderr=%s",
                label, result.returncode, result.stderr[-500:] if result.stderr else "",
            )
            # build 실패는 치명적이지 않을 수 있음 (타입 에러 등)
            if "error" in (result.stderr or "").lower() and "build" in label:
                raise RuntimeError(f"{label} 실패: {result.stderr[-300:]}")
        else:
            logger.info("workspace_cmd_done label=%s", label)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} 타임아웃 ({timeout}초)")


def _install_and_build(
    workspace_path: Path,
    stack: dict,
    fe_port: int = 3000,
    be_port: int = 4000,
) -> None:
    """의존성 설치 + 빌드. 포트에 맞춰 .env 자동 생성."""
    fe = workspace_path / "frontend"
    be = workspace_path / "backend"

    # --- Backend (.env 먼저 — 프론트 빌드 시 API URL 필요) ---
    if be.is_dir():
        if stack.get("backend") == "express":
            if not (be / "package.json").is_file():
                _generate_express_package_json(be)
            if not (be / "tsconfig.json").is_file():
                _generate_express_tsconfig(be)
            # .env — 항상 포트 반영 (기존 파일이 있어도 CORS/PORT 업데이트)
            _generate_dev_env(be, be_port=be_port, fe_port=fe_port)
            # npm install
            _run_cmd(["npm", "install"], cwd=be, label="backend npm install")
            # prisma (BUILD 프롬프트가 SQLite 호환 스키마 직접 생성)
            if (be / "prisma" / "schema.prisma").is_file():
                _run_cmd(["npx", "prisma", "generate"], cwd=be, label="prisma generate")
                _run_cmd(["npx", "prisma", "db", "push"], cwd=be, label="prisma db push")
            # tsc
            _run_cmd(["npm", "run", "build"], cwd=be, label="backend build")

        elif stack.get("backend") in ("fastapi", "django"):
            if (be / "requirements.txt").is_file():
                # uvicorn이 requirements.txt에 없으면 추가
                req_content = (be / "requirements.txt").read_text(encoding="utf-8")
                if "uvicorn" not in req_content and stack.get("backend") == "fastapi":
                    with open(be / "requirements.txt", "a") as f:
                        f.write("\nuvicorn[standard]>=0.27.0\n")
                _run_cmd(["pip3", "install", "-r", "requirements.txt"], cwd=be, label="pip install")
            elif (be / "Pipfile").is_file():
                _run_cmd(["pipenv", "install"], cwd=be, label="pipenv install")
            elif (be / "pyproject.toml").is_file():
                _run_cmd(["pip3", "install", "-e", "."], cwd=be, label="pip install -e .")
            else:
                # requirements.txt 없으면 최소 의존성 설치
                if stack.get("backend") == "fastapi":
                    _run_cmd(["pip3", "install", "fastapi", "uvicorn[standard]"], cwd=be, label="pip install fastapi+uvicorn")
            # CORS 동적 포트 반영
            _patch_fastapi_cors(be, fe_port)
            # Django migrate
            if stack.get("backend") == "django":
                _run_cmd(["python3", "manage.py", "migrate", "--run-syncdb"], cwd=be, label="django migrate")

        elif stack.get("backend") == "spring":
            # application.properties CORS + 포트 설정
            _patch_spring_config(be, fe_port, be_port)
            if (be / "gradlew").is_file():
                _run_cmd(["./gradlew", "build", "-x", "test"], cwd=be, label="gradle build")
            elif (be / "mvnw").is_file():
                _run_cmd(["./mvnw", "package", "-DskipTests"], cwd=be, label="maven build")

        elif stack.get("backend") == "go":
            _patch_go_cors(be, fe_port)
            _run_cmd(["go", "build", "./..."], cwd=be, label="go build")

    # --- Frontend ---
    fe_type = stack.get("frontend", "")

    if fe.is_dir() and fe_type in ("nextjs", "nuxt", "vue", "svelte", "react"):
        # package.json 없으면 생성 (Next.js만 — 나머지는 이미 있어야 함)
        if fe_type == "nextjs" and not (fe / "package.json").is_file():
            _generate_nextjs_package_json(fe)
        if fe_type == "nextjs" and not (fe / "tsconfig.json").is_file():
            _generate_nextjs_tsconfig(fe)
        if fe_type == "nextjs" and not (fe / "next.config.js").is_file():
            (fe / "next.config.js").write_text(
                "/** @type {import('next').NextConfig} */\n"
                "module.exports = {\n"
                "  typescript: { ignoreBuildErrors: true },\n"
                "  eslint: { ignoreDuringBuilds: true },\n"
                "}\n"
            )

        # .env.local — 백엔드 API URL 자동 연결
        api_prefix = "/api/v1" if stack.get("backend") == "express" else "/api"
        if fe_type == "nextjs":
            _generate_frontend_env(fe, be_port=be_port, api_prefix=api_prefix)
        elif fe_type in ("vue", "nuxt"):
            (fe / ".env").write_text(f"VITE_API_URL=http://localhost:{be_port}{api_prefix}\n")
        elif fe_type == "svelte":
            (fe / ".env").write_text(f"VITE_API_URL=http://localhost:{be_port}{api_prefix}\n")
        elif fe_type == "react":
            (fe / ".env").write_text(f"REACT_APP_API_URL=http://localhost:{be_port}{api_prefix}\n")

        # npm install + build
        _run_cmd(["npm", "install"], cwd=fe, label="frontend npm install")
        if fe_type == "nextjs":
            _run_cmd(["npm", "run", "build"], cwd=fe, label="frontend build")
        elif (fe / "package.json").is_file():
            try:
                pkg = json.loads((fe / "package.json").read_text())
                if "build" in pkg.get("scripts", {}):
                    _run_cmd(["npm", "run", "build"], cwd=fe, label="frontend build")
            except Exception:
                pass


def _start_servers(
    workspace_path: Path,
    stack: dict,
    fe_port: int,
    be_port: int,
) -> None:
    """백그라운드 서버 프로세스 기동."""
    fe = workspace_path / "frontend"
    be = workspace_path / "backend"
    pids_file = workspace_path / ".pids"
    pids = {}

    # Backend
    be_type = stack.get("backend", "")
    if be.is_dir() and be_type:
        env = {**os.environ, "PORT": str(be_port), "NODE_ENV": "development"}
        log_out = open(workspace_path / "backend.log", "w")
        proc = None

        if be_type == "express":
            # dist/server.js 또는 src/server.ts (ts-node)
            if (be / "dist" / "server.js").is_file():
                proc = subprocess.Popen(["node", "dist/server.js"], cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT)
            elif (be / "src" / "server.ts").is_file():
                proc = subprocess.Popen(["npx", "ts-node", "src/server.ts"], cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT)
        elif be_type == "fastapi":
            # uvicorn이 PATH에 없을 수 있으므로 python3 -m 폴백
            if shutil.which("uvicorn"):
                _uvicorn_cmd = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(be_port)]
            else:
                _uvicorn_cmd = ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(be_port)]
            proc = subprocess.Popen(
                _uvicorn_cmd,
                cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT,
            )
        elif be_type == "django":
            proc = subprocess.Popen(
                ["python3", "manage.py", "runserver", f"0.0.0.0:{be_port}"],
                cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT,
            )
        elif be_type == "spring":
            # gradle bootRun 또는 jar 실행
            jar = list(be.rglob("*.jar"))
            if jar:
                proc = subprocess.Popen(
                    ["java", "-jar", str(jar[0]), f"--server.port={be_port}"],
                    cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT,
                )
            elif (be / "gradlew").is_file():
                proc = subprocess.Popen(
                    ["./gradlew", "bootRun", f"--args=--server.port={be_port}"],
                    cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT,
                )
        elif be_type == "go":
            go_bin = list(be.glob("main")) or list(be.glob("server"))
            if go_bin:
                proc = subprocess.Popen([str(go_bin[0])], cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT)
            else:
                proc = subprocess.Popen(["go", "run", "."], cwd=be, env=env, stdout=log_out, stderr=subprocess.STDOUT)

        if proc:
            pids["backend"] = proc.pid
            logger.info("backend_started type=%s port=%s pid=%s", be_type, be_port, proc.pid)

    # Frontend
    fe_type = stack.get("frontend", "")
    if fe.is_dir() and fe_type:
        env = {**os.environ, "PORT": str(fe_port)}
        log_out = open(workspace_path / "frontend.log", "w")
        proc = None

        if fe_type == "nextjs":
            proc = subprocess.Popen(["npx", "next", "dev", "-p", str(fe_port)], cwd=fe, env=env, stdout=log_out, stderr=subprocess.STDOUT)
        elif fe_type == "nuxt":
            proc = subprocess.Popen(["npx", "nuxi", "dev", "--port", str(fe_port)], cwd=fe, env=env, stdout=log_out, stderr=subprocess.STDOUT)
        elif fe_type in ("vue", "react", "svelte"):
            # Vite 기반: npm run dev -- --port PORT
            proc = subprocess.Popen(["npm", "run", "dev", "--", "--port", str(fe_port)], cwd=fe, env=env, stdout=log_out, stderr=subprocess.STDOUT)
        elif fe_type == "static":
            # 정적 파일: npx serve
            proc = subprocess.Popen(["npx", "serve", "-l", str(fe_port)], cwd=fe, env=env, stdout=log_out, stderr=subprocess.STDOUT)
        pids["frontend"] = proc.pid
        logger.info("frontend_started port=%s pid=%s", fe_port, proc.pid)

    # PID 파일 저장 (나중에 stop할 때 사용)
    pids_file.write_text(json.dumps(pids))


def _wait_for_server(
    port: int,
    timeout: int = 30,
    log_file: Path | None = None,
    ready_markers: tuple[str, ...] = (),
    fatal_markers: tuple[str, ...] = (),
) -> None:
    """서버 기동 검증 (HTTP 폴링 + 로그 패턴 스캔).

    포트 리슨만으로는 Next.js가 크래시 중인데도 잠깐 열린 상태를 PASS로
    잘못 판정하므로, log_file이 주어지면 그 안의 ready/fatal 마커까지 함께
    검사한다. 타임아웃이나 fatal 매치 시 RuntimeError를 raise (기존 warning
    경로 폐기).

    - ready_markers 중 하나가 로그에 있으면 즉시 PASS.
    - fatal_markers 중 하나가 로그에 있으면 즉시 RAISE.
    - HTTP 응답 200/3xx/4xx도 PASS (서버가 살아 라우팅 응답하는 상태).
    """
    import time
    import urllib.request
    deadline = time.time() + timeout
    interval = 1.0

    def _scan_log() -> tuple[bool, str | None]:
        if log_file is None or not log_file.is_file():
            return False, None
        try:
            text = log_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False, None
        for fm in fatal_markers:
            if fm in text:
                return False, fm
        for rm in ready_markers:
            if rm in text:
                return True, None
        return False, None

    while time.time() < deadline:
        ready, fatal = _scan_log()
        if fatal is not None:
            raise RuntimeError(
                f"server_fatal_in_log port={port} marker={fatal!r}"
            )
        if ready:
            logger.info("health_check_ok_log port=%d", port)
            return
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            logger.info("health_check_ok_http port=%d", port)
            return
        except Exception:
            time.sleep(interval)
            interval = min(interval * 1.5, 5.0)

    # 타임아웃: 마지막 로그 tail을 포함해 RAISE (기존 warning→RAISE 승격).
    tail = ""
    if log_file is not None and log_file.is_file():
        try:
            tail = log_file.read_text(
                encoding="utf-8", errors="ignore"
            )[-500:]
        except Exception:
            tail = ""
    raise RuntimeError(
        f"health_check_timeout port={port} timeout={timeout}s log_tail={tail!r}"
    )


def _cleanup_processes(workspace_path: Path) -> None:
    """실패한 배포의 프로세스 정리."""
    pids_file = workspace_path / ".pids"
    if pids_file.is_file():
        try:
            pids = json.loads(pids_file.read_text())
            for name, pid in pids.items():
                try:
                    os.kill(pid, 9)
                    logger.info("cleanup_killed name=%s pid=%d", name, pid)
                except ProcessLookupError:
                    pass
            pids_file.unlink()
        except Exception:
            pass


def _port_in_use(port: int) -> bool:
    """포트 사용중 여부 확인."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _patch_go_cors(be: Path, fe_port: int) -> None:
    """Go 프로젝트의 CORS 설정에 프론트 포트 추가."""
    fe_url = f"http://localhost:{fe_port}"
    for go_file in be.rglob("*.go"):
        try:
            content = go_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "AllowOrigins" in content or "Access-Control-Allow-Origin" in content:
            if fe_url not in content:
                content = content.replace(
                    "AllowOrigins:",
                    f'AllowOrigins: // includes {fe_url}\n\t\t\t',
                )
                # 직접 헤더 설정 방식
                content = re.sub(
                    r'(Access-Control-Allow-Origin["\s,]+)([^"]*?)(")',
                    lambda m: m.group(1) + fe_url + m.group(3) if fe_url not in m.group(0) else m.group(0),
                    content,
                )
                go_file.write_text(content, encoding="utf-8")
                logger.info("go_cors_patched file=%s", go_file)
            return
    # .env 파일에 CORS_ORIGIN 추가 (환경변수 방식)
    env_file = be / ".env"
    if env_file.is_file():
        content = env_file.read_text(encoding="utf-8")
        if "CORS_ORIGIN" not in content:
            content += f"\nCORS_ORIGIN={fe_url}\n"
            env_file.write_text(content, encoding="utf-8")
    else:
        env_file.write_text(f"CORS_ORIGIN={fe_url}\nPORT=4000\n")
    logger.info("go_cors_env_set fe_url=%s", fe_url)


def _patch_spring_config(be: Path, fe_port: int, be_port: int) -> None:
    """Spring Boot application.properties에 CORS + 포트 설정."""
    for props_file in be.rglob("application.properties"):
        content = props_file.read_text(encoding="utf-8")
        if f"server.port={be_port}" not in content:
            content += f"\nserver.port={be_port}\n"
        if "allowed-origins" not in content:
            content += f"spring.web.cors.allowed-origins=http://localhost:{fe_port}\n"
        props_file.write_text(content, encoding="utf-8")
        logger.info("spring_config_patched file=%s", props_file)
        return
    # application.yml
    for yml_file in be.rglob("application.yml"):
        content = yml_file.read_text(encoding="utf-8")
        if str(be_port) not in content:
            content += f"\nserver:\n  port: {be_port}\n"
        yml_file.write_text(content, encoding="utf-8")
        logger.info("spring_config_patched file=%s", yml_file)
        return


def _patch_fastapi_cors(be: Path, fe_port: int) -> None:
    """FastAPI main.py의 CORS origins에 프론트엔드 포트를 추가."""
    for py_file in be.rglob("main.py"):
        if ".venv" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if "CORSMiddleware" not in content:
            continue
        fe_url = f"http://localhost:{fe_port}"
        if fe_url in content:
            return  # 이미 있음
        # allow_origins 배열에 추가
        content = re.sub(
            r'(allow_origins\s*=\s*\[)([^\]]*?)(\])',
            lambda m: m.group(1) + m.group(2).rstrip().rstrip(",") + f', "{fe_url}"' + m.group(3),
            content,
        )
        py_file.write_text(content, encoding="utf-8")
        logger.info("fastapi_cors_patched file=%s fe_port=%d", py_file, fe_port)
        return
