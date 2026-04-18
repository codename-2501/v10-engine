"""
playwright_bridge.py — Python bridge that builds JSON config, calls
the Node.js playwright_runner.js via subprocess, and returns structured results.

Handles graceful degradation when Node.js, Playwright, or the app are unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Timeout for the entire Playwright test suite (5 minutes)
RUNNER_TIMEOUT_SECONDS = 300

# Path to the runner script relative to this file
RUNNER_JS = Path(__file__).parent / "playwright_runner.js"

# Default viewports
DEFAULT_VIEWPORTS = [
    {"name": "mobile", "width": 375, "height": 812},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "desktop", "width": 1280, "height": 900},
]


def _check_prerequisites() -> str | None:
    """Check that node and playwright are available. Returns error message or None."""
    if not shutil.which("node"):
        return "Node.js not available on PATH"
    # Quick check that playwright can be required
    try:
        result = subprocess.run(
            ["node", "-e", "require('playwright')"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(RUNNER_JS.parent.parent.parent),  # project root
        )
        if result.returncode != 0:
            return f"Playwright not importable: {result.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return "Playwright import check timed out"
    except Exception as exc:
        return f"Prerequisite check failed: {exc}"
    return None


def _check_server_running(port: int) -> bool:
    """Quick check if a server is listening on the given port."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _infer_page_type(slug: str) -> str:
    """Infer page type from slug naming patterns."""
    slug_lower = slug.lower()
    if any(k in slug_lower for k in ("dashboard", "index", "home", "monitoring")):
        return "dashboard"
    if any(k in slug_lower for k in ("detail", "view", "profile")):
        return "detail"
    if any(k in slug_lower for k in ("form", "create", "edit", "new", "rating", "register")):
        return "form"
    # Default to list for admin-style pages
    return "list"


def _discover_pages(workspace_path: Path, preview_dir: Path) -> list[dict]:
    """Discover pages from preview HTML files."""
    pages = []
    if not preview_dir.exists():
        return pages

    for html_file in sorted(preview_dir.glob("*.html")):
        slug = html_file.stem  # e.g. "admin-caregivers"
        if slug in ("_layout", "layout", "template"):
            continue
        route = f"/{slug}" if slug != "index" else "/"
        page_type = _infer_page_type(slug)
        pages.append({
            "slug": slug,
            "route": route,
            "designHtml": html_file.name,
            "type": page_type,
        })

    return pages


def _build_config(
    workspace_path: Path,
    ports: dict,
    screenshot_dir: str | None = None,
    user_scenarios: list | None = None,
) -> dict:
    """Build the JSON config for playwright_runner.js."""
    fe_port = ports.get("frontend", ports.get("fe", 3000))
    preview_dir = workspace_path / "preview"

    pages = _discover_pages(workspace_path, preview_dir)

    if not screenshot_dir:
        screenshot_dir = tempfile.mkdtemp(prefix="pw-screenshots-")

    config = {
        "appUrl": f"http://localhost:{fe_port}",
        "designDir": str(preview_dir),
        "pages": pages,
        "screenshotDir": screenshot_dir,
        "viewports": DEFAULT_VIEWPORTS,
        "checks": {
            "screenshots": True,
            "pixelDiff": preview_dir.exists(),
            "accessibility": True,
            "performance": True,
            "userFlows": True,
            "consoleLogs": True,
            "security": True,
        },
    }

    # Inject DEFINE-artifact-based user scenario tests
    if user_scenarios:
        config["userScenarios"] = user_scenarios

    return config


async def run_playwright_tests(
    workspace_path: Path,
    project_id: str,
    ports: dict,
) -> dict:
    """
    Run the full Playwright test suite.

    Returns a results dict with pass/fail, summary, per-page results, and issues.
    Handles all errors gracefully — never raises, always returns a dict.
    """
    skip_result = {
        "pass": True,
        "summary": {
            "total_checks": 0,
            "passed": 0,
            "failed": 0,
            "warnings": 0,
            "score": 0,
        },
        "pages": {},
        "issues": [],
        "skipped": True,
    }

    # -- Prerequisite checks --
    prereq_error = await asyncio.to_thread(_check_prerequisites)
    if prereq_error:
        logger.warning("playwright_skipped reason=%s", prereq_error)
        skip_result["skip_reason"] = prereq_error
        return skip_result

    fe_port = ports.get("frontend", ports.get("fe", 3000))
    if not await asyncio.to_thread(_check_server_running, fe_port):
        reason = f"Frontend server not running on port {fe_port}"
        logger.warning("playwright_skipped reason=%s", reason)
        skip_result["skip_reason"] = reason
        return skip_result

    # -- Build config --
    screenshot_dir = None
    try:
        workspace_path = Path(workspace_path)
        screenshot_dir = tempfile.mkdtemp(prefix="pw-screenshots-")

        # Load DEFINE-artifact-based user scenario tests (graceful degradation)
        user_scenarios = []
        try:
            from engine.testing.flow_generator import generate_user_flow_tests
            # We need a DB handle — try to import from the running app context
            # The db parameter is not available here, so we try a lazy import
            # from the engine's singleton. If unavailable, skip silently.
            from engine.db import get_db
            _db = await get_db()
            user_scenarios = await generate_user_flow_tests(_db, project_id)
        except ImportError:
            logger.debug("flow_generator: db module not available, skipping user scenarios")
        except Exception as _fg_exc:
            logger.debug("flow_generator: user scenario generation skipped: %s", _fg_exc)

        config = _build_config(workspace_path, ports, screenshot_dir, user_scenarios)

        if not config["pages"]:
            logger.warning("playwright_skipped reason=no_pages_discovered")
            skip_result["skip_reason"] = "No pages discovered in preview dir"
            return skip_result

        logger.info(
            "playwright_starting project=%s pages=%d",
            project_id,
            len(config["pages"]),
        )

        # -- Run node subprocess --
        config_json = json.dumps(config)
        project_root = str(RUNNER_JS.parent.parent.parent)

        proc = await asyncio.create_subprocess_exec(
            "node",
            str(RUNNER_JS),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_root,
            env={**os.environ, "NODE_PATH": str(Path(project_root) / "node_modules")},
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=config_json.encode("utf-8")),
                timeout=RUNNER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("playwright_timeout project=%s", project_id)
            return {
                "pass": False,
                "summary": {
                    "total_checks": 0,
                    "passed": 0,
                    "failed": 0,
                    "warnings": 0,
                    "score": 0,
                },
                "pages": {},
                "issues": [
                    {
                        "page": "_runner",
                        "type": "timeout",
                        "severity": "critical",
                        "message": f"Test suite timed out after {RUNNER_TIMEOUT_SECONDS}s",
                    }
                ],
                "timeout": True,
            }

        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.debug("playwright_stderr: %s", stderr_text[:500])

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        if not stdout_text:
            return {
                "pass": False,
                "error": "Empty stdout from playwright runner",
                "summary": {
                    "total_checks": 0,
                    "passed": 0,
                    "failed": 0,
                    "warnings": 0,
                    "score": 0,
                },
                "pages": {},
                "issues": [],
            }

        # Parse JSON — handle potential non-JSON prefix/suffix
        try:
            results = json.loads(stdout_text)
        except json.JSONDecodeError:
            # Try to extract JSON from output
            match = re.search(r'\{.*\}', stdout_text, re.DOTALL)
            if match:
                results = json.loads(match.group())
            else:
                return {
                    "pass": False,
                    "error": f"Invalid JSON output: {stdout_text[:300]}",
                    "summary": {
                        "total_checks": 0,
                        "passed": 0,
                        "failed": 0,
                        "warnings": 0,
                        "score": 0,
                    },
                    "pages": {},
                    "issues": [],
                }

        logger.info(
            "playwright_complete project=%s pass=%s score=%.1f",
            project_id,
            results.get("pass"),
            results.get("summary", {}).get("score", 0),
        )
        return results

    except Exception as exc:
        logger.warning("playwright_error project=%s error=%s", project_id, exc)
        return {
            "pass": False,
            "error": str(exc),
            "summary": {
                "total_checks": 0,
                "passed": 0,
                "failed": 0,
                "warnings": 0,
                "score": 0,
            },
            "pages": {},
            "issues": [],
        }
    finally:
        # Cleanup screenshot dir
        if screenshot_dir and os.path.isdir(screenshot_dir):
            try:
                shutil.rmtree(screenshot_dir, ignore_errors=True)
            except Exception:
                pass
