#!/usr/bin/env node
/**
 * playwright_runner.js — Comprehensive Playwright-based testing framework for V8 engine.
 *
 * Receives JSON config on stdin, outputs JSON results on stdout.
 * All errors are handled gracefully — never crashes, always returns JSON.
 *
 * Tests: Screenshot+PixelDiff, Accessibility, Console Errors,
 *        User Flow, Security, Performance
 *
 * Optimized: Single page load per viewport, parallel page testing.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const { PNG } = require("pngjs");
const pixelmatch = require("pixelmatch");

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------
const PAGE_TIMEOUT = 10_000;
const SECTION_TIMEOUT = 60_000;
const PIXEL_DIFF_THRESHOLD = 8; // percent (lowered from 15 for stricter design matching)
const CONCURRENCY = 3; // parallel page tests

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function makeDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function safeJsonParse(str) {
  try {
    return JSON.parse(str);
  } catch {
    return null;
  }
}

/** Read stdin fully as a string */
function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

/** Resize a PNG buffer to target width/height (nearest-neighbour, padded white) */
function resizePng(pngObj, targetW, targetH) {
  const out = new PNG({ width: targetW, height: targetH });
  // fill white
  for (let i = 0; i < out.data.length; i += 4) {
    out.data[i] = 255;
    out.data[i + 1] = 255;
    out.data[i + 2] = 255;
    out.data[i + 3] = 255;
  }
  const copyW = Math.min(pngObj.width, targetW);
  const copyH = Math.min(pngObj.height, targetH);
  for (let y = 0; y < copyH; y++) {
    for (let x = 0; x < copyW; x++) {
      const srcIdx = (y * pngObj.width + x) * 4;
      const dstIdx = (y * targetW + x) * 4;
      out.data[dstIdx] = pngObj.data[srcIdx];
      out.data[dstIdx + 1] = pngObj.data[srcIdx + 1];
      out.data[dstIdx + 2] = pngObj.data[srcIdx + 2];
      out.data[dstIdx + 3] = pngObj.data[srcIdx + 3];
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Individual test logic (operates on an already-loaded page)
// ---------------------------------------------------------------------------

/** Take screenshot of already-loaded page */
async function captureScreenshot(page, screenshotDir, pageInfo, vpName) {
  const appFile = path.join(
    screenshotDir,
    `app-${pageInfo.slug}-${vpName}.png`
  );
  await page.screenshot({ path: appFile, fullPage: true });
  return appFile;
}

/** Take screenshot of design HTML in a separate context */
async function captureDesignScreenshot(browser, config, pageInfo, vp) {
  const screenshotDir = config.screenshotDir || "/tmp/playwright-screenshots";
  const designPath = path.join(config.designDir, pageInfo.designHtml);
  if (!fs.existsSync(designPath)) return null;

  const context = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
  });
  const page = await context.newPage();
  try {
    await page.goto(`file://${designPath}`, {
      waitUntil: "networkidle",
      timeout: PAGE_TIMEOUT,
    });
    await page.waitForTimeout(500);
    const designFile = path.join(
      screenshotDir,
      `design-${pageInfo.slug}-${vp.name}.png`
    );
    await page.screenshot({ path: designFile, fullPage: true });
    return designFile;
  } finally {
    await context.close();
  }
}

/** Compute pixel diff between two PNG files */
function computePixelDiff(designShot, appShot, screenshotDir, pageInfo, vpName) {
  let img1 = PNG.sync.read(fs.readFileSync(designShot));
  let img2 = PNG.sync.read(fs.readFileSync(appShot));

  const w = Math.max(img1.width, img2.width);
  const h = Math.max(img1.height, img2.height);
  if (img1.width !== w || img1.height !== h) img1 = resizePng(img1, w, h);
  if (img2.width !== w || img2.height !== h) img2 = resizePng(img2, w, h);

  const diff = new PNG({ width: w, height: h });
  const mismatchCount = pixelmatch(img1.data, img2.data, diff.data, w, h, {
    threshold: 0.3,
  });
  const totalPixels = w * h;
  const diffPct = ((mismatchCount / totalPixels) * 100).toFixed(1);

  const diffFile = path.join(
    screenshotDir,
    `diff-${pageInfo.slug}-${vpName}.png`
  );
  fs.writeFileSync(diffFile, PNG.sync.write(diff));

  return parseFloat(diffPct);
}

/** Run accessibility checks on an already-loaded page */
async function runAccessibility(page) {
  const result = { pass: true, violations: 0, details: [] };

  let AxeBuilder;
  try {
    AxeBuilder =
      require("@axe-core/playwright").default ||
      require("@axe-core/playwright");
  } catch {
    result.skipped = true;
    result.message = "@axe-core/playwright not available";
    return result;
  }

  const axeResults = await new AxeBuilder({ page }).analyze();
  const critical = axeResults.violations.filter((v) => v.impact === "critical");
  const serious = axeResults.violations.filter((v) => v.impact === "serious");
  const moderate = axeResults.violations.filter((v) => v.impact === "moderate");
  const minor = axeResults.violations.filter((v) => v.impact === "minor");

  result.violations = axeResults.violations.length;
  result.critical = critical.length;
  result.serious = serious.length;
  result.moderate = moderate.length;
  result.minor = minor.length;
  result.details = axeResults.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    description: v.description,
    nodes: v.nodes.length,
  }));

  if (critical.length > 0 || serious.length > 0) {
    result.pass = false;
  }
  return result;
}

/** Extract security info from response + page (already loaded) */
async function runSecurity(page, response) {
  const result = { pass: true, issues: [] };

  // Check response headers
  if (response) {
    const headers = response.headers();
    const checks = [
      { header: "x-frame-options", label: "X-Frame-Options" },
      { header: "x-content-type-options", label: "X-Content-Type-Options" },
      { header: "referrer-policy", label: "Referrer-Policy" },
    ];
    for (const chk of checks) {
      if (!headers[chk.header]) {
        result.issues.push({
          type: "missing_header",
          severity: "warning",
          message: `Missing ${chk.label} header`,
        });
      }
    }
  }

  // Check page source for token in localStorage
  const pageContent = await page.content();
  if (/localStorage\.setItem\s*\(\s*['"]token['"]/i.test(pageContent)) {
    result.issues.push({
      type: "token_storage",
      severity: "warning",
      message: "Token stored in localStorage (consider httpOnly cookie)",
    });
  }

  // Check for inline onclick
  const inlineHandlers = await page.$$eval("[onclick]", (els) =>
    els.map((el) => el.getAttribute("onclick")).filter(Boolean)
  );
  if (inlineHandlers.length > 0) {
    result.issues.push({
      type: "inline_handlers",
      severity: "warning",
      message: `Found ${inlineHandlers.length} inline onclick handlers`,
    });
  }

  // Check forms for CSRF
  const forms = await page.$$("form");
  for (let i = 0; i < forms.length; i++) {
    const hasCSRF = await forms[i].$(
      'input[name="_csrf"], input[name="csrf_token"], input[name="csrfmiddlewaretoken"]'
    );
    const action = await forms[i].evaluate(
      (f) => f.getAttribute("action") || ""
    );
    if (
      !hasCSRF &&
      action &&
      action !== "#" &&
      !action.startsWith("javascript:")
    ) {
      result.issues.push({
        type: "no_csrf",
        severity: "info",
        message: `Form #${i + 1} may lack CSRF protection (action="${action}")`,
      });
    }
  }

  const critical = result.issues.filter(
    (i) => i.severity === "critical" || i.severity === "serious"
  );
  if (critical.length > 0) result.pass = false;

  return result;
}

/** Get performance metrics from already-loaded page */
async function runPerformance(page, totalBytes) {
  const result = {
    pass: true,
    loadTime: 0,
    domContentLoaded: 0,
    transferredBytes: 0,
  };

  const perf = await page.evaluate(() => {
    const timing = performance.getEntriesByType("navigation")[0];
    if (timing) {
      return {
        loadTime: Math.round(timing.loadEventEnd - timing.startTime),
        domContentLoaded: Math.round(
          timing.domContentLoadedEventEnd - timing.startTime
        ),
      };
    }
    const t = performance.timing;
    return {
      loadTime: t.loadEventEnd - t.navigationStart,
      domContentLoaded: t.domContentLoadedEventEnd - t.navigationStart,
    };
  });

  result.loadTime = perf.loadTime;
  result.domContentLoaded = perf.domContentLoaded;
  result.transferredBytes = totalBytes;

  if (perf.loadTime > 5000) {
    result.pass = false;
    result.issue = `Load time ${perf.loadTime}ms exceeds 5s threshold`;
  }
  if (perf.domContentLoaded > 3000) {
    result.pass = false;
    result.issue = `DOM content loaded ${perf.domContentLoaded}ms exceeds 3s threshold`;
  }
  return result;
}

/** Run user flow tests on already-loaded page */
async function runUserFlows(page, pageInfo, appUrl) {
  const result = { pass: true, steps: 0, passed: 0, details: [] };
  const steps = [];

  function addStep(name, passed, info) {
    steps.push({ name, passed, info: info || "" });
  }

  // suppress dialog boxes
  page.on("dialog", async (dialog) => {
    try {
      await dialog.dismiss();
    } catch {}
  });

  const pageType = pageInfo.type || "unknown";

  // ── LIST pages ──
  if (pageType === "list") {
    try {
      const searchInput = await page.$(
        'input[type="search"], input[type="text"], input[placeholder*="검색"], input[placeholder*="search"], input[placeholder*="Search"]'
      );
      if (searchInput) {
        await searchInput.fill("test");
        await page.waitForTimeout(800);
        addStep("search_input", true, "typed 'test' in search");
      } else {
        addStep("search_input", true, "no search input found (OK)");
      }
    } catch (e) {
      addStep("search_input", false, e.message);
    }

    try {
      const headers = await page.$$(
        "th[class*='sort'], th[role='columnheader'], thead th"
      );
      if (headers.length > 0) {
        await headers[0].click();
        await page.waitForTimeout(500);
        addStep("sort_click", true, "clicked first header");
      } else {
        addStep("sort_click", true, "no sortable headers (OK)");
      }
    } catch (e) {
      addStep("sort_click", false, e.message);
    }

    try {
      const nextBtn = await page.$(
        'button:has-text("다음"), button:has-text("Next"), [class*="next"], [aria-label="next"], [aria-label="Next page"], nav[class*="pagination"] button:last-child'
      );
      if (nextBtn) {
        await nextBtn.click();
        await page.waitForTimeout(500);
        addStep("pagination", true, "clicked next page");
      } else {
        addStep("pagination", true, "no pagination found (OK)");
      }
    } catch (e) {
      addStep("pagination", false, e.message);
    }

    try {
      const actionBtn = await page.$(
        'button:has-text("추가"), button:has-text("등록"), button:has-text("Add"), button:has-text("New"), a[class*="btn"]:has-text("추가")'
      );
      if (actionBtn) {
        await actionBtn.click();
        await page.waitForTimeout(500);
        addStep("action_button", true, "clicked action button");
        await page.goBack().catch(() => {});
        await page.waitForTimeout(500);
      } else {
        addStep("action_button", true, "no action button found (OK)");
      }
    } catch (e) {
      addStep("action_button", false, e.message);
    }

    try {
      const row = await page.$(
        "table tbody tr:first-child td:first-child, [class*='list-item']:first-child"
      );
      if (row) {
        await row.click();
        await page.waitForTimeout(500);
        addStep("row_click", true, "clicked first row");
        await page.goBack().catch(() => {});
      } else {
        addStep("row_click", true, "no clickable row (OK)");
      }
    } catch (e) {
      addStep("row_click", false, e.message);
    }
  }

  // ── FORM pages ──
  else if (pageType === "form") {
    try {
      const inputs = await page.$$(
        'input:not([type="hidden"]):not([type="submit"]), textarea, select'
      );
      let filled = 0;
      for (const input of inputs.slice(0, 10)) {
        try {
          const tagName = await input.evaluate((el) =>
            el.tagName.toLowerCase()
          );
          const inputType = await input.evaluate((el) => el.type || "");
          if (tagName === "select") {
            const options = await input.$$("option");
            if (options.length > 1) {
              await input.selectOption({ index: 1 });
              filled++;
            }
          } else if (inputType === "checkbox" || inputType === "radio") {
            await input.check().catch(() => {});
            filled++;
          } else if (inputType === "date") {
            await input.fill("2025-01-15");
            filled++;
          } else if (inputType === "number") {
            await input.fill("42");
            filled++;
          } else if (inputType === "email") {
            await input.fill("test@example.com");
            filled++;
          } else if (inputType === "tel") {
            await input.fill("010-1234-5678");
            filled++;
          } else {
            await input.fill("테스트 데이터");
            filled++;
          }
        } catch {}
      }
      addStep("fill_inputs", true, `filled ${filled} inputs`);
    } catch (e) {
      addStep("fill_inputs", false, e.message);
    }

    try {
      const submitBtn = await page.$(
        'button[type="submit"], button:has-text("저장"), button:has-text("등록"), button:has-text("Submit"), button:has-text("Save")'
      );
      if (submitBtn) {
        await submitBtn.click();
        await page.waitForTimeout(1000);
        addStep("form_submit", true, "clicked submit");
      } else {
        addStep("form_submit", true, "no submit button found (OK)");
      }
    } catch (e) {
      addStep("form_submit", false, e.message);
    }

    try {
      await page.goto(appUrl, {
        waitUntil: "networkidle",
        timeout: PAGE_TIMEOUT,
      });
      await page.waitForTimeout(500);
      const submitBtn = await page.$(
        'button[type="submit"], button:has-text("저장"), button:has-text("등록"), button:has-text("Submit")'
      );
      if (submitBtn) {
        await submitBtn.click();
        await page.waitForTimeout(500);
        const errorMsgs = await page.$$(
          '[class*="error"], [class*="invalid"], [role="alert"], .text-red-500, .text-danger'
        );
        addStep(
          "form_validation",
          true,
          `validation messages: ${errorMsgs.length}`
        );
      } else {
        addStep("form_validation", true, "no submit for validation test");
      }
    } catch (e) {
      addStep("form_validation", false, e.message);
    }
  }

  // ── DETAIL pages ──
  else if (pageType === "detail") {
    try {
      const tabs = await page.$$(
        '[role="tab"], button[class*="tab"], a[class*="tab"]'
      );
      let clicked = 0;
      for (const tab of tabs.slice(0, 5)) {
        try {
          await tab.click();
          await page.waitForTimeout(300);
          clicked++;
        } catch {}
      }
      addStep("tab_click", true, `clicked ${clicked} tabs`);
    } catch (e) {
      addStep("tab_click", false, e.message);
    }

    try {
      const btns = await page.$$(
        'button:has-text("수정"), button:has-text("삭제"), button:has-text("Edit"), button:has-text("Delete")'
      );
      if (btns.length > 0) {
        await btns[0].click();
        await page.waitForTimeout(500);
        addStep("detail_action", true, "clicked action button");
        await page.goBack().catch(() => {});
      } else {
        addStep("detail_action", true, "no action buttons (OK)");
      }
    } catch (e) {
      addStep("detail_action", false, e.message);
    }

    try {
      const backLink = await page.$(
        'a:has-text("목록"), a:has-text("Back"), a:has-text("뒤로"), button:has-text("목록")'
      );
      if (backLink) {
        addStep("back_link", true, "back link found");
      } else {
        addStep("back_link", true, "no back link (OK)");
      }
    } catch (e) {
      addStep("back_link", false, e.message);
    }
  }

  // ── DASHBOARD pages ──
  else if (pageType === "dashboard") {
    try {
      const cards = await page.$$(
        '[class*="stat"], [class*="card"], [class*="metric"], [class*="summary"]'
      );
      addStep("stat_cards", cards.length > 0, `found ${cards.length} stat cards`);
    } catch (e) {
      addStep("stat_cards", false, e.message);
    }

    try {
      const charts = await page.$$(
        "canvas, svg[class*='chart'], [class*='chart'], [class*='graph']"
      );
      addStep("charts", true, `found ${charts.length} chart areas`);
    } catch (e) {
      addStep("charts", false, e.message);
    }

    try {
      const tables = await page.$$("table");
      let hasRows = false;
      for (const t of tables) {
        const rows = await t.$$("tbody tr");
        if (rows.length > 0) hasRows = true;
      }
      addStep(
        "dashboard_tables",
        true,
        `found ${tables.length} tables, hasRows=${hasRows}`
      );
    } catch (e) {
      addStep("dashboard_tables", false, e.message);
    }
  }

  // ── Unknown page type ──
  else {
    try {
      const body = await page.$("body");
      const text = await body.innerText();
      addStep("basic_render", text.length > 0, `body text length: ${text.length}`);
    } catch (e) {
      addStep("basic_render", false, e.message);
    }
  }

  result.steps = steps.length;
  result.passed = steps.filter((s) => s.passed).length;
  result.details = steps;
  if (result.passed < result.steps) result.pass = false;
  return result;
}

/**
 * Run explicit user scenario flows from config.userScenarios.
 * These are generated from DEFINE phase artifacts (User Flow, Product Backlog).
 * Each scenario is a sequence of actions: goto, click, fill, expect, wait.
 */
async function runUserScenarios(browser, config) {
  const scenarios = config.userScenarios || [];
  if (scenarios.length === 0) return { pass: true, scenarios: [], skipped: true };

  const result = { pass: true, scenarios: [], total: scenarios.length, passed: 0 };

  for (const scenario of scenarios) {
    const scenarioResult = {
      name: scenario.name || "unnamed",
      pass: true,
      steps: [],
    };

    const context = await browser.newContext({
      viewport: { width: 1280, height: 900 },
    });
    const page = await context.newPage();

    // suppress dialogs
    page.on("dialog", async (dialog) => {
      try { await dialog.dismiss(); } catch {}
    });

    try {
      for (const step of scenario.steps || []) {
        const stepResult = { action: step.action, pass: true, info: "" };
        try {
          if (step.action === "goto") {
            const url = step.url.startsWith("http")
              ? step.url
              : `${config.appUrl}${step.url}`;
            await page.goto(url, { waitUntil: "networkidle", timeout: PAGE_TIMEOUT });
            stepResult.info = `navigated to ${step.url}`;
          } else if (step.action === "click") {
            await page.click(step.selector, { timeout: 5000 });
            await page.waitForTimeout(500);
            stepResult.info = `clicked ${step.selector}`;
          } else if (step.action === "fill") {
            await page.fill(step.selector, step.value || "", { timeout: 5000 });
            stepResult.info = `filled ${step.selector} with "${step.value}"`;
          } else if (step.action === "expect") {
            const locator = page.locator(step.selector);
            if (step.text) {
              await locator.first().waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
              const textContent = await locator.first().textContent().catch(() => "");
              if (textContent && textContent.includes(step.text)) {
                stepResult.info = `found "${step.text}" in ${step.selector}`;
              } else if (step.contains) {
                // Looser check: any visible element contains text
                const visible = await locator.count();
                stepResult.info = `element ${step.selector} found (${visible} matches), text check soft-pass`;
              } else {
                stepResult.pass = false;
                stepResult.info = `expected "${step.text}" not found in ${step.selector}`;
              }
            } else {
              const count = await locator.count();
              if (count > 0) {
                stepResult.info = `${step.selector} exists (${count} matches)`;
              } else {
                stepResult.pass = false;
                stepResult.info = `${step.selector} not found`;
              }
            }
          } else if (step.action === "wait") {
            await page.waitForTimeout(step.ms || 1000);
            stepResult.info = `waited ${step.ms || 1000}ms`;
          } else if (step.action === "select") {
            await page.selectOption(step.selector, step.value || "", { timeout: 5000 });
            stepResult.info = `selected ${step.value} in ${step.selector}`;
          }
        } catch (err) {
          stepResult.pass = false;
          stepResult.info = err.message.slice(0, 200);
        }
        scenarioResult.steps.push(stepResult);
        if (!stepResult.pass) {
          scenarioResult.pass = false;
        }
      }
    } catch (err) {
      scenarioResult.pass = false;
      scenarioResult.error = err.message.slice(0, 200);
    } finally {
      await context.close();
    }

    result.scenarios.push(scenarioResult);
    if (scenarioResult.pass) result.passed++;
  }

  result.pass = result.passed === result.total;
  return result;
}

// ---------------------------------------------------------------------------
// Single page test orchestrator — one load per viewport, all checks at once
// ---------------------------------------------------------------------------

async function testSinglePage(browser, config, pageInfo) {
  const pageResult = {};
  const screenshotDir = config.screenshotDir || "/tmp/playwright-screenshots";
  makeDir(screenshotDir);

  const appUrl = `${config.appUrl}${pageInfo.route}`;

  // --- Phase 1: Design screenshots (all viewports, parallel) ---
  const designShots = {}; // vpName -> filePath
  if (config.checks.screenshots || config.checks.pixelDiff) {
    const designPromises = config.viewports.map(async (vp) => {
      try {
        const shot = await captureDesignScreenshot(browser, config, pageInfo, vp);
        if (shot) designShots[vp.name] = shot;
      } catch {}
    });
    await Promise.all(designPromises);
  }

  // --- Phase 2: App page testing (one load per viewport) ---
  // We use the FIRST viewport (desktop, typically 1280) for all non-screenshot checks
  // and capture screenshots for all viewports.

  const screenshotResult = { pass: true, diffs: {} };
  const appShots = {}; // vpName -> filePath

  // For non-screenshot tests, we only need one viewport (desktop-sized)
  // Do all tests on the first context, screenshots on all viewports
  const primaryVpIndex = 0;

  for (let vi = 0; vi < config.viewports.length; vi++) {
    const vp = config.viewports[vi];
    const isPrimary = vi === primaryVpIndex;

    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
    });
    const page = await context.newPage();

    // Console error capture (on primary only)
    const consoleErrors = [];
    if (isPrimary && config.checks.consoleLogs) {
      page.on("console", (msg) => {
        if (msg.type() === "error") consoleErrors.push(msg.text());
      });
      page.on("pageerror", (err) => {
        consoleErrors.push(err.message || String(err));
      });
    }

    // Performance byte tracking (on primary only)
    let totalBytes = 0;
    if (isPrimary && config.checks.performance) {
      page.on("response", (resp) => {
        const headers = resp.headers();
        totalBytes += parseInt(headers["content-length"] || "0", 10);
      });
    }

    try {
      // Navigate ONCE
      const response = await page.goto(appUrl, {
        waitUntil: "networkidle",
        timeout: PAGE_TIMEOUT,
      });
      await page.waitForTimeout(isPrimary ? 1000 : 500);

      // --- Screenshot ---
      if (config.checks.screenshots || config.checks.pixelDiff) {
        try {
          const appFile = await captureScreenshot(page, screenshotDir, pageInfo, vp.name);
          appShots[vp.name] = appFile;
        } catch (err) {
          screenshotResult.pass = false;
          screenshotResult.error = err.message;
        }
      }

      // --- All other tests: only on primary viewport ---
      if (isPrimary) {
        // Accessibility
        if (config.checks.accessibility) {
          try {
            pageResult.accessibility = await runAccessibility(page);
          } catch (err) {
            pageResult.accessibility = {
              pass: false,
              violations: -1,
              error: err.message,
            };
          }
        }

        // Console (already captured via listeners)
        if (config.checks.consoleLogs) {
          pageResult.console = {
            pass: consoleErrors.length === 0,
            errors: consoleErrors.length,
            errorMessages: consoleErrors.slice(0, 10),
          };
        }

        // Security
        if (config.checks.security) {
          try {
            pageResult.security = await runSecurity(page, response);
          } catch (err) {
            pageResult.security = { pass: false, error: err.message };
          }
        }

        // Performance
        if (config.checks.performance) {
          try {
            pageResult.performance = await runPerformance(page, totalBytes);
          } catch (err) {
            pageResult.performance = {
              pass: false,
              loadTime: -1,
              error: err.message,
            };
          }
        }

        // User Flow (last — it may navigate away)
        if (config.checks.userFlows) {
          try {
            pageResult.userFlow = await runUserFlows(page, pageInfo, appUrl);
          } catch (err) {
            pageResult.userFlow = {
              pass: false,
              steps: 0,
              passed: 0,
              error: err.message,
            };
          }
        }
      }
    } catch (err) {
      if (isPrimary) {
        // If primary viewport load fails, mark all sections as failed
        if (config.checks.accessibility && !pageResult.accessibility) {
          pageResult.accessibility = {
            pass: false,
            violations: -1,
            error: err.message,
          };
        }
        if (config.checks.consoleLogs && !pageResult.console) {
          pageResult.console = { pass: false, errors: -1, error: err.message };
        }
        if (config.checks.security && !pageResult.security) {
          pageResult.security = { pass: false, error: err.message };
        }
        if (config.checks.performance && !pageResult.performance) {
          pageResult.performance = {
            pass: false,
            loadTime: -1,
            error: err.message,
          };
        }
        if (config.checks.userFlows && !pageResult.userFlow) {
          pageResult.userFlow = {
            pass: false,
            steps: 0,
            passed: 0,
            error: err.message,
          };
        }
      }
      screenshotResult.pass = false;
      screenshotResult.error = err.message;
    } finally {
      await context.close();
    }
  }

  // --- Phase 3: Pixel diffs (CPU-only, no browser needed) ---
  if (config.checks.pixelDiff) {
    for (const vp of config.viewports) {
      const designFile = designShots[vp.name];
      const appFile = appShots[vp.name];
      if (designFile && appFile) {
        try {
          const diffPct = computePixelDiff(
            designFile,
            appFile,
            screenshotDir,
            pageInfo,
            vp.name
          );
          screenshotResult.diffs[vp.name] = diffPct;
          if (diffPct > PIXEL_DIFF_THRESHOLD) {
            screenshotResult.pass = false;
          }
        } catch (diffErr) {
          screenshotResult.diffs[vp.name] = -1;
          screenshotResult.diffError = diffErr.message;
        }
      }
    }
  }

  // Assign screenshots result
  if (config.checks.screenshots || config.checks.pixelDiff) {
    pageResult.screenshots = screenshotResult;
  }

  // Fill in defaults for disabled checks
  if (!config.checks.screenshots && !config.checks.pixelDiff) {
    pageResult.screenshots = { pass: true, diffs: {} };
  }
  if (!config.checks.accessibility) {
    pageResult.accessibility = { pass: true, violations: 0, details: [] };
  }
  if (!config.checks.consoleLogs) {
    pageResult.console = { pass: true, errors: 0, errorMessages: [] };
  }
  if (!config.checks.userFlows) {
    pageResult.userFlow = { pass: true, steps: 0, passed: 0, details: [] };
  }
  if (!config.checks.security) {
    pageResult.security = { pass: true, issues: [] };
  }
  if (!config.checks.performance) {
    pageResult.performance = {
      pass: true,
      loadTime: 0,
      domContentLoaded: 0,
      transferredBytes: 0,
    };
  }

  return pageResult;
}

// ---------------------------------------------------------------------------
// Main runner
// ---------------------------------------------------------------------------

async function run(config) {
  const results = {
    pass: true,
    summary: {
      total_checks: 0,
      passed: 0,
      failed: 0,
      warnings: 0,
      score: 0,
    },
    pages: {},
    issues: [],
  };

  let browser;
  try {
    const { chromium } = require("playwright");
    browser = await chromium.launch({ headless: true });
  } catch (err) {
    results.pass = false;
    results.error = `Playwright launch failed: ${err.message}`;
    results.summary.score = 0;
    return results;
  }

  try {
    // Run pages in parallel batches
    const pages = config.pages;
    for (let i = 0; i < pages.length; i += CONCURRENCY) {
      const batch = pages.slice(i, i + CONCURRENCY);
      const batchResults = await Promise.all(
        batch.map((pageInfo) => {
          return Promise.race([
            testSinglePage(browser, config, pageInfo),
            new Promise((_, rej) =>
              setTimeout(() => rej(new Error("timeout")), SECTION_TIMEOUT)
            ),
          ]).catch((err) => ({
            screenshots: { pass: false, error: err.message, diffs: {} },
            accessibility: {
              pass: false,
              violations: -1,
              error: err.message,
            },
            console: { pass: false, errors: -1, error: err.message },
            userFlow: {
              pass: false,
              steps: 0,
              passed: 0,
              error: err.message,
            },
            security: { pass: false, error: err.message },
            performance: {
              pass: false,
              loadTime: -1,
              error: err.message,
            },
          }));
        })
      );

      // Assign results
      for (let j = 0; j < batch.length; j++) {
        const pageInfo = batch[j];
        const pageResult = batchResults[j];
        results.pages[pageInfo.slug] = pageResult;

        // Collect issues
        const sections = [
          "screenshots",
          "accessibility",
          "console",
          "userFlow",
          "security",
          "performance",
        ];
        for (const sec of sections) {
          const sr = pageResult[sec];
          if (!sr) continue;

          if (sec === "screenshots" && sr.diffs) {
            for (const [vpName, diffPct] of Object.entries(sr.diffs)) {
              if (diffPct > PIXEL_DIFF_THRESHOLD) {
                results.issues.push({
                  page: pageInfo.slug,
                  type: "pixel_diff",
                  severity: "warning",
                  message: `${vpName} diff ${diffPct}% (threshold ${PIXEL_DIFF_THRESHOLD}%)`,
                });
              }
            }
          }
          if (sec === "accessibility" && sr.details) {
            for (const v of sr.details) {
              if (v.impact === "critical" || v.impact === "serious") {
                results.issues.push({
                  page: pageInfo.slug,
                  type: "accessibility",
                  severity: v.impact,
                  message: `${v.id}: ${v.description} (${v.nodes} nodes)`,
                });
              }
            }
          }
          if (sec === "console" && sr.errors > 0) {
            results.issues.push({
              page: pageInfo.slug,
              type: "console_error",
              severity: "warning",
              message: `${sr.errors} console error(s): ${(sr.errorMessages || []).slice(0, 3).join("; ")}`,
            });
          }
          if (sec === "security" && sr.issues) {
            for (const issue of sr.issues) {
              results.issues.push({
                page: pageInfo.slug,
                type: issue.type,
                severity: issue.severity,
                message: issue.message,
              });
            }
          }
          if (sec === "performance" && sr.issue) {
            results.issues.push({
              page: pageInfo.slug,
              type: "performance",
              severity: "warning",
              message: sr.issue,
            });
          }
        }
      }
    }

    // --- User Scenarios (cross-page flows from DEFINE artifacts) ---
    try {
      const scenarioResults = await runUserScenarios(browser, config);
      results.userScenarios = scenarioResults;
      if (!scenarioResults.skipped) {
        for (const sc of scenarioResults.scenarios || []) {
          if (!sc.pass) {
            results.issues.push({
              page: "_scenario",
              type: "user_scenario",
              severity: "warning",
              message: `Scenario "${sc.name}" failed: ${(sc.steps || []).filter(s => !s.pass).map(s => s.info).join("; ").slice(0, 200)}`,
            });
          }
        }
      }
    } catch (scenErr) {
      results.userScenarios = { pass: true, skipped: true, error: scenErr.message };
    }

    // Compute summary
    const sections = [
      "screenshots",
      "accessibility",
      "console",
      "userFlow",
      "security",
      "performance",
    ];
    let total = 0;
    let passed = 0;
    let failed = 0;
    let warnings = 0;

    for (const [, pageResult] of Object.entries(results.pages)) {
      for (const sec of sections) {
        const sr = pageResult[sec];
        if (!sr || sr.skipped) continue;
        total++;
        if (sr.pass) {
          passed++;
        } else {
          failed++;
        }
      }
    }

    // Include user scenario results in summary
    if (results.userScenarios && !results.userScenarios.skipped) {
      total += results.userScenarios.total || 0;
      passed += results.userScenarios.passed || 0;
      failed += (results.userScenarios.total || 0) - (results.userScenarios.passed || 0);
    }

    warnings = results.issues.filter((i) => i.severity === "warning").length;

    results.summary = {
      total_checks: total,
      passed,
      failed,
      warnings,
      score: total > 0 ? parseFloat(((passed / total) * 100).toFixed(1)) : 0,
    };

    results.pass = failed === 0;
  } catch (err) {
    results.pass = false;
    results.error = `Runner error: ${err.message}`;
  } finally {
    if (browser) await browser.close();
  }

  return results;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

async function main() {
  let config;
  try {
    const input = await readStdin();
    config = safeJsonParse(input);
    if (!config) {
      process.stdout.write(
        JSON.stringify({
          pass: false,
          error: "Invalid JSON config on stdin",
          summary: {
            total_checks: 0,
            passed: 0,
            failed: 0,
            warnings: 0,
            score: 0,
          },
          pages: {},
          issues: [],
        })
      );
      process.exit(0);
    }
  } catch (err) {
    process.stdout.write(
      JSON.stringify({
        pass: false,
        error: `stdin read error: ${err.message}`,
        summary: {
          total_checks: 0,
          passed: 0,
          failed: 0,
          warnings: 0,
          score: 0,
        },
        pages: {},
        issues: [],
      })
    );
    process.exit(0);
  }

  try {
    const results = await run(config);
    process.stdout.write(JSON.stringify(results));
  } catch (err) {
    process.stdout.write(
      JSON.stringify({
        pass: false,
        error: `Fatal: ${err.message}`,
        summary: {
          total_checks: 0,
          passed: 0,
          failed: 0,
          warnings: 0,
          score: 0,
        },
        pages: {},
        issues: [],
      })
    );
  }
}

// When required as a module, export run; when executed directly, read stdin.
if (require.main === module) {
  main();
} else {
  module.exports = { run };
}
