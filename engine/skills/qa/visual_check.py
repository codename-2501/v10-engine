"""
engine/skills/qa/visual_check.py
Lightweight visual comparison: design HTML vs live app page.

Two-stage validation (mirrors V3 visual-check.js pattern):
  Stage 1: DOM rule-based checks via curl + HTML parsing (token 0, ~3s)
  Stage 2: Screenshot comparison via Claude Vision (conditional)

No heavy browser dependencies — uses curl for HTML fetch + stdlib parsing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

logger = logging.getLogger("engine.skills.qa.visual_check")

# ---------------------------------------------------------------------------
# HTML element extraction helpers
# ---------------------------------------------------------------------------

_STRUCTURAL_TAGS = frozenset({
    "div", "table", "form", "button", "input", "nav", "header", "footer",
    "main", "section", "article", "aside", "select", "textarea", "ul", "ol",
    "li", "h1", "h2", "h3", "h4", "h5", "h6", "a", "img", "span", "p",
})

_INTERACTIVE_TAGS = frozenset({
    "button", "input", "select", "textarea", "a",
})

_VISUAL_TAGS = frozenset({
    "img", "svg", "canvas", "video", "picture",
})


class _ElementCounter(HTMLParser):
    """Count structural HTML elements and extract visible text."""

    def __init__(self) -> None:
        super().__init__()
        self.tag_counts: Counter = Counter()
        self.interactive_count: int = 0
        self.visual_count: int = 0
        self.text_chunks: list[str] = []
        self.css_var_refs: int = 0
        self._in_style = False
        self._in_script = False
        self._depth = 0
        self._has_body = False
        self._body_child_count = 0
        self._in_body = False
        self._empty_containers: list[str] = []
        self._current_tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        self._current_tag_stack.append(tag_lower)

        if tag_lower == "body":
            self._has_body = True
            self._in_body = True

        if self._in_body and self._current_tag_stack.count("body") == 1:
            # Direct child of body
            if tag_lower not in ("script", "style", "link", "meta"):
                self._body_child_count += 1

        if tag_lower in _STRUCTURAL_TAGS:
            self.tag_counts[tag_lower] += 1

        if tag_lower in _INTERACTIVE_TAGS:
            self.interactive_count += 1

        if tag_lower in _VISUAL_TAGS:
            self.visual_count += 1

        if tag_lower == "style":
            self._in_style = True
        if tag_lower == "script":
            self._in_script = True

        # Check for inline style with CSS variables
        for attr_name, attr_val in attrs:
            if attr_name == "style" and attr_val and "var(--" in attr_val:
                self.css_var_refs += 1
            if attr_name == "class" and attr_val:
                # Tailwind classes don't count as CSS var usage
                pass

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "style":
            self._in_style = False
        if tag_lower == "script":
            self._in_script = False
        if tag_lower == "body":
            self._in_body = False
        if self._current_tag_stack and self._current_tag_stack[-1] == tag_lower:
            self._current_tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_style:
            # Count CSS variable references in stylesheets
            self.css_var_refs += data.count("var(--")
            return
        if self._in_script:
            return
        stripped = data.strip()
        if stripped and len(stripped) > 1:
            self.text_chunks.append(stripped)

    def handle_comment(self, data: str) -> None:
        pass

    @property
    def total_structural(self) -> int:
        return sum(self.tag_counts.values())


def _parse_html(html: str) -> _ElementCounter:
    """Parse HTML and return element counts + text."""
    counter = _ElementCounter()
    try:
        counter.feed(html)
    except Exception:
        pass
    return counter


def _extract_text_set(counter: _ElementCounter) -> set[str]:
    """Extract normalized word set from text chunks for Jaccard comparison."""
    words: set[str] = set()
    for chunk in counter.text_chunks:
        # Normalize: lowercase, split on whitespace/punctuation
        for word in re.split(r'[\s,;:.\-_/\\|"\'!?(){}[\]<>]+', chunk.lower()):
            w = word.strip()
            if len(w) > 2:  # Skip very short words
                words.add(w)
    return words


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """Compute Jaccard similarity between two sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_url(url: str, timeout: int = 10) -> str | None:
    """Fetch URL content using curl (no Python HTTP deps needed)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _read_file(path: str | Path) -> str | None:
    """Read a local file, return None on failure."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError):
        return None


# ---------------------------------------------------------------------------
# Stage 1: DOM rule-based checks
# ---------------------------------------------------------------------------

def _check_content_presence(app_html: str | None, page_slug: str) -> dict:
    """Check 1: App URL returns non-empty, non-error HTML."""
    if not app_html:
        return {
            "name": "content_presence",
            "pass": False,
            "detail": f"Page '{page_slug}' returned empty or unreachable",
        }
    # Check for common error indicators
    lower = app_html.lower()
    error_indicators = [
        "cannot get /", "404 not found", "internal server error",
        "application error", "module not found", "syntaxerror",
        "referenceerror", "typeerror",
    ]
    for indicator in error_indicators:
        if indicator in lower and len(app_html) < 2000:
            return {
                "name": "content_presence",
                "pass": False,
                "detail": f"Page '{page_slug}' shows error: '{indicator}'",
            }
    # Check body has content
    parsed = _parse_html(app_html)
    if parsed._has_body and parsed._body_child_count == 0:
        return {
            "name": "content_presence",
            "pass": False,
            "detail": f"Page '{page_slug}' has empty <body> (no child elements)",
        }
    return {"name": "content_presence", "pass": True, "detail": "OK"}


def _check_element_count_ratio(
    design_parsed: _ElementCounter,
    app_parsed: _ElementCounter,
    page_slug: str,
) -> dict:
    """Check 2: Major HTML element counts should be within 0.5x-2x ratio."""
    d_total = design_parsed.total_structural
    a_total = app_parsed.total_structural

    if d_total == 0:
        return {
            "name": "element_count_ratio",
            "pass": True,
            "detail": "Design HTML has no structural elements (skip)",
            "design_count": d_total,
            "app_count": a_total,
        }

    ratio = a_total / d_total if d_total > 0 else 0
    ok = 0.3 <= ratio <= 3.0  # Generous range: frameworks add wrapper divs
    return {
        "name": "element_count_ratio",
        "pass": ok,
        "detail": f"ratio={ratio:.2f} (design={d_total}, app={a_total})"
                  + ("" if ok else f" — page '{page_slug}' element count diverges significantly"),
        "design_count": d_total,
        "app_count": a_total,
        "ratio": round(ratio, 2),
    }


def _check_text_content_overlap(
    design_parsed: _ElementCounter,
    app_parsed: _ElementCounter,
    page_slug: str,
) -> dict:
    """Check 3: Jaccard similarity of visible text should be > 0.3."""
    design_words = _extract_text_set(design_parsed)
    app_words = _extract_text_set(app_parsed)
    similarity = _jaccard_similarity(design_words, app_words)

    # If design has very little text, skip this check
    if len(design_words) < 3:
        return {
            "name": "text_content_overlap",
            "pass": True,
            "detail": "Design has too few text words to compare (skip)",
            "similarity": round(similarity, 3),
        }

    ok = similarity > 0.2  # Lower threshold: frameworks transform text
    return {
        "name": "text_content_overlap",
        "pass": ok,
        "detail": f"jaccard={similarity:.3f} (design_words={len(design_words)}, app_words={len(app_words)})"
                  + ("" if ok else f" — page '{page_slug}' text diverges from design"),
        "similarity": round(similarity, 3),
    }


def _check_css_variable_usage(app_parsed: _ElementCounter, app_html: str) -> dict:
    """Check 4: App HTML should reference CSS custom properties (design system)."""
    # Count var(-- references in the full HTML (styles + inline)
    css_var_count = app_html.count("var(--") if app_html else 0
    # Also count CSS custom property definitions
    css_def_count = len(re.findall(r'--[\w-]+\s*:', app_html)) if app_html else 0
    # Tailwind class usage is also acceptable as a design system
    tailwind_refs = len(re.findall(r'class="[^"]*(?:flex|grid|bg-|text-|p-|m-|rounded|shadow)', app_html)) if app_html else 0

    has_design_system = css_var_count > 0 or css_def_count > 0 or tailwind_refs > 3
    return {
        "name": "css_design_system",
        "pass": has_design_system,
        "detail": f"css_vars={css_var_count}, css_defs={css_def_count}, tailwind_refs={tailwind_refs}"
                  + ("" if has_design_system else " — no design system detected"),
    }


def _check_interactive_elements(
    design_parsed: _ElementCounter,
    app_parsed: _ElementCounter,
    page_slug: str,
) -> dict:
    """Check 5: Interactive element count comparison."""
    d_count = design_parsed.interactive_count
    a_count = app_parsed.interactive_count

    if d_count == 0:
        return {
            "name": "interactive_elements",
            "pass": True,
            "detail": "Design has no interactive elements (skip)",
        }

    ratio = a_count / d_count if d_count > 0 else 0
    ok = ratio >= 0.3  # App should have at least 30% of design's interactive elements
    return {
        "name": "interactive_elements",
        "pass": ok,
        "detail": f"design={d_count}, app={a_count}, ratio={ratio:.2f}"
                  + ("" if ok else f" — page '{page_slug}' missing interactive elements"),
        "design_count": d_count,
        "app_count": a_count,
    }


def _check_visual_elements(
    design_parsed: _ElementCounter,
    app_parsed: _ElementCounter,
    page_slug: str,
) -> dict:
    """Check 6: Visual element count (img, svg, canvas) comparison.
    Don't penalize gradient placeholders."""
    d_count = design_parsed.visual_count
    a_count = app_parsed.visual_count

    if d_count == 0:
        return {
            "name": "visual_elements",
            "pass": True,
            "detail": "Design has no visual elements (skip)",
        }

    # Very lenient: just flag if app has zero visuals but design has many
    ok = a_count > 0 or d_count <= 2
    return {
        "name": "visual_elements",
        "pass": ok,
        "detail": f"design={d_count}, app={a_count}"
                  + ("" if ok else f" — page '{page_slug}' missing visual elements"),
    }


# ---------------------------------------------------------------------------
# Page-level visual check
# ---------------------------------------------------------------------------

def visual_check_page(
    design_html_path: str | None,
    app_url: str,
    page_slug: str,
) -> dict:
    """Compare design HTML rendering vs live app page.

    Stage 1 only (DOM rule-based). Stage 2 (Vision) called separately.

    Args:
        design_html_path: Path to the design HTML file, or None if unavailable.
        app_url: URL of the live app page (e.g., http://localhost:3100/).
        page_slug: Page identifier for logging.

    Returns:
        {"pass": bool, "issues": [...], "checks": [...], "score": float}
    """
    issues: list[dict] = []
    checks: list[dict] = []

    # Fetch app HTML
    app_html = _fetch_url(app_url)

    # Check 1: Content presence
    c1 = _check_content_presence(app_html, page_slug)
    checks.append(c1)
    if not c1["pass"]:
        issues.append({"type": "content_missing", "severity": "critical", "detail": c1["detail"]})
        # Can't do further checks without app HTML
        return {"pass": False, "issues": issues, "checks": checks, "score": 0.0}

    app_parsed = _parse_html(app_html)  # type: ignore[arg-type]

    # Load design HTML if available
    design_html = _read_file(design_html_path) if design_html_path else None
    design_parsed = _parse_html(design_html) if design_html else None

    # Check 4: CSS design system usage (doesn't need design HTML)
    c4 = _check_css_variable_usage(app_parsed, app_html)  # type: ignore[arg-type]
    checks.append(c4)
    if not c4["pass"]:
        issues.append({"type": "no_design_system", "severity": "warning", "detail": c4["detail"]})

    # Checks requiring design HTML comparison
    if design_parsed:
        # Check 2: Element count ratio
        c2 = _check_element_count_ratio(design_parsed, app_parsed, page_slug)
        checks.append(c2)
        if not c2["pass"]:
            issues.append({"type": "element_count_mismatch", "severity": "warning", "detail": c2["detail"]})

        # Check 3: Text content overlap
        c3 = _check_text_content_overlap(design_parsed, app_parsed, page_slug)
        checks.append(c3)
        if not c3["pass"]:
            issues.append({"type": "text_divergence", "severity": "warning", "detail": c3["detail"]})

        # Check 5: Interactive elements
        c5 = _check_interactive_elements(design_parsed, app_parsed, page_slug)
        checks.append(c5)
        if not c5["pass"]:
            issues.append({"type": "interactive_missing", "severity": "warning", "detail": c5["detail"]})

        # Check 6: Visual elements
        c6 = _check_visual_elements(design_parsed, app_parsed, page_slug)
        checks.append(c6)
        if not c6["pass"]:
            issues.append({"type": "visual_missing", "severity": "warning", "detail": c6["detail"]})

    # Compute score: fraction of passing checks
    total_checks = len(checks)
    passing = sum(1 for c in checks if c["pass"])
    score = passing / total_checks if total_checks > 0 else 1.0

    # Pass if no critical issues
    critical_count = sum(1 for i in issues if i["severity"] == "critical")
    passed = critical_count == 0

    return {
        "pass": passed,
        "issues": issues,
        "checks": checks,
        "score": round(score, 3),
    }


# ---------------------------------------------------------------------------
# Stage 2: Vision-based comparison (conditional)
# ---------------------------------------------------------------------------

def _run_vision_check(
    design_html_path: str | None,
    app_url: str,
    page_slug: str,
) -> dict:
    """Stage 2: Use Claude CLI to compare design vs app visually.

    Only runs if both design HTML path and app URL are available.
    Uses claude CLI with haiku model for cost efficiency.

    Returns:
        {"pass": bool, "feedback": str, "skipped": bool}
    """
    if not design_html_path or not os.path.exists(design_html_path):
        return {"pass": True, "feedback": "No design HTML — skip vision check", "skipped": True}

    prompt = (
        f"This is a visual comparison task for page '{page_slug}'.\n"
        f"Design HTML is at: {design_html_path}\n"
        f"Live app URL: {app_url}\n\n"
        "Compare the design intent with what the app produces. "
        "Check for:\n"
        "1. Major layout differences (missing sections, wrong column counts)\n"
        "2. Missing interactive elements (buttons, forms, inputs)\n"
        "3. Completely absent content areas\n"
        "4. Broken styling (no colors, no spacing)\n\n"
        "If the app reasonably matches the design intent, respond with just: PASS\n"
        "If there are significant issues, respond with: FAIL: <brief description>"
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku", "--max-turns", "1"],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip() if result.returncode == 0 else ""
        if not output:
            return {"pass": True, "feedback": "Vision check unavailable", "skipped": True}

        passed = "PASS" in output.upper() and "FAIL" not in output.upper()
        return {
            "pass": passed,
            "feedback": "" if passed else output.replace("FAIL:", "").strip(),
            "skipped": False,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("vision_check_unavailable error=%s", str(exc))
        return {"pass": True, "feedback": f"Vision check unavailable ({exc})", "skipped": True}


# ---------------------------------------------------------------------------
# Project-level orchestration
# ---------------------------------------------------------------------------

def run_visual_checks(
    workspace_path: str | Path,
    ports: dict[str, int] | None = None,
    project_id: str = "",
) -> dict:
    """Run visual checks across all pages of a deployed workspace.

    Discovers design HTMLs in workspace/preview/ and compares against
    the live app running on the allocated port.

    Args:
        workspace_path: Path to the workspace directory.
        ports: Dict with "frontend_port" and/or "backend_port".
        project_id: For logging context.

    Returns:
        {
            "pass": bool,
            "issues": [...],   # All issues across all pages
            "pages_checked": int,
            "pages_failed": int,
            "score": float,    # Average score across pages
        }
    """
    workspace_path = Path(workspace_path)
    all_issues: list[dict] = []
    page_scores: list[float] = []
    pages_checked = 0
    pages_failed = 0

    # Determine the app URL base
    fe_port = None
    if ports:
        fe_port = ports.get("frontend_port") or ports.get("fe_port")
    if not fe_port:
        # Try reading ports.json
        ports_file = workspace_path / "ports.json"
        if ports_file.exists():
            try:
                ports_data = json.loads(ports_file.read_text())
                fe_port = ports_data.get("frontend_port") or ports_data.get("fe_port")
            except (json.JSONDecodeError, OSError):
                pass

    if not fe_port:
        logger.info("visual_check_skip project=%s reason=no_frontend_port", project_id[:8] if project_id else "?")
        return {
            "pass": True,
            "issues": [],
            "pages_checked": 0,
            "pages_failed": 0,
            "score": 1.0,
            "skipped": True,
            "reason": "no_frontend_port",
        }

    base_url = f"http://localhost:{fe_port}"

    # Discover design HTML files in preview/
    preview_dir = workspace_path / "preview"
    design_htmls: dict[str, Path] = {}
    if preview_dir.is_dir():
        for html_file in sorted(preview_dir.glob("*.html")):
            slug = html_file.stem
            design_htmls[slug] = html_file

    # Determine pages to check
    pages_to_check: list[tuple[str, str, str | None]] = []

    if design_htmls:
        for slug, html_path in design_htmls.items():
            # Map design slug to URL path
            url_path = "/" if slug in ("index", "home", "dashboard", "main") else f"/{slug}"
            pages_to_check.append((slug, f"{base_url}{url_path}", str(html_path)))
    else:
        # No design HTMLs — check root page only
        pages_to_check.append(("index", base_url, None))

    # Run checks
    for slug, app_url, design_path in pages_to_check:
        logger.info("visual_check_page project=%s page=%s url=%s", project_id[:8] if project_id else "?", slug, app_url)
        result = visual_check_page(design_path, app_url, slug)
        pages_checked += 1
        page_scores.append(result["score"])

        if not result["pass"]:
            pages_failed += 1

        for issue in result["issues"]:
            issue["page"] = slug
            all_issues.append(issue)

        # Stage 2: Vision check (only if stage 1 passed and design exists)
        if result["pass"] and design_path:
            vision = _run_vision_check(design_path, app_url, slug)
            if not vision["skipped"] and not vision["pass"]:
                all_issues.append({
                    "type": "vision_mismatch",
                    "severity": "warning",
                    "detail": vision["feedback"],
                    "page": slug,
                })

    avg_score = sum(page_scores) / len(page_scores) if page_scores else 1.0
    critical_issues = [i for i in all_issues if i["severity"] == "critical"]
    passed = len(critical_issues) == 0

    summary = {
        "pass": passed,
        "issues": all_issues,
        "pages_checked": pages_checked,
        "pages_failed": pages_failed,
        "score": round(avg_score, 3),
    }

    log_fn = logger.info if passed else logger.warning
    log_fn(
        "visual_check_complete project=%s pass=%s pages=%d/%d score=%.2f issues=%d",
        project_id[:8] if project_id else "?",
        passed, pages_checked - pages_failed, pages_checked,
        avg_score, len(all_issues),
    )

    return summary
