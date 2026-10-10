// Standalone plain-Node runner: `node pipeline/apply/runner.mjs <order.json>`.
//
// This is NOT run inside the omp eval JS sandbox (js-yaml resolution and
// puppeteer-core both need plain Node module resolution) -- it is invoked
// by pipeline/run/batch.py as a child process. It reads the work order
// (pipeline v2 contract), drives the operator's own Chromium-based browser over CDP through a
// NEW tab it opens itself, calls apply.mjs's fillApplication unchanged,
// and writes the attempt record + screenshots the ledger then ingests.
//
// Exit codes (batch.py depends on these exactly):
//   0 = an attempt was written (status carries the real outcome)
//   3 = the browser lock is held by another live runner process
//   4 = Brave is not reachable on the CDP port (never launched here)
//   2 = the order.json itself couldn't be read/parsed
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { load as parseYaml } from "js-yaml";
import puppeteer from "puppeteer-core";
import { fillApplication } from "./apply.mjs";

const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const PIPELINE_ROOT = path.join(moduleDir, "..");
const LOCK_PATH = path.join(PIPELINE_ROOT, "data", "browser.lock");
const CDP_URL = "http://127.0.0.1:9223";

function fail(code, message) {
  console.error(message);
  // process.exit() below terminates the process immediately -- it does not
  // unwind to the `finally` in main(), so release the lock (if we hold it)
  // here explicitly rather than relying on the next run's dead-pid takeover.
  releaseLock();
  process.exit(code);
}

function nowIso() {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------
// Exclusive lock: pipeline/data/browser.lock. A dead pid is taken over
// (the previous runner crashed without releasing it); a live pid means a
// real concurrent run is in progress and this run must refuse to touch
// the browser at all.
// ---------------------------------------------------------------------
function pidIsAlive(pid) {
  if (!pid || !Number.isFinite(pid)) return false;
  try {
    // Signal 0: existence check only, no actual signal delivered. Works on
    // Windows (via OpenProcess under the hood) as well as POSIX.
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e && e.code === "EPERM"; // exists but we can't signal it -> still alive
  }
}

function acquireLock() {
  fs.mkdirSync(path.dirname(LOCK_PATH), { recursive: true });
  const payload = () => JSON.stringify({ pid: process.pid, started_at: nowIso() });
  for (;;) {
    try {
      const fd = fs.openSync(LOCK_PATH, "wx");
      fs.writeSync(fd, payload());
      fs.closeSync(fd);
      return;
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
      let existing = null;
      try {
        existing = JSON.parse(fs.readFileSync(LOCK_PATH, "utf8"));
      } catch {
        existing = null;
      }
      const existingPid = existing && existing.pid;
      if (pidIsAlive(existingPid)) {
        fail(
          3,
          `Refusing to touch the browser: pipeline/data/browser.lock is held by live pid ${existingPid}` +
            (existing && existing.started_at ? ` (started ${existing.started_at})` : "") +
            ". Another apply run is in progress -- wait for it to finish."
        );
      }
      // Dead pid: take over. Remove and loop to re-create with 'wx' so a
      // genuine race with another runner still fails safely instead of
      // both processes believing they hold the lock.
      try {
        fs.unlinkSync(LOCK_PATH);
      } catch {
        // Another process may have already removed/re-created it; loop
        // will re-check.
      }
    }
  }
}

function releaseLock() {
  try {
    const existing = JSON.parse(fs.readFileSync(LOCK_PATH, "utf8"));
    if (existing && existing.pid === process.pid) fs.unlinkSync(LOCK_PATH);
  } catch {
    // Lock already gone or unreadable -- nothing to release.
  }
}

// ---------------------------------------------------------------------
// order.json / answers / bank loading
// ---------------------------------------------------------------------
function loadOrder(orderPath) {
  let raw;
  try {
    raw = fs.readFileSync(orderPath, "utf8");
  } catch (e) {
    fail(2, `Could not read order file "${orderPath}": ${e.message}`);
  }
  try {
    return JSON.parse(raw);
  } catch (e) {
    fail(2, `order.json at "${orderPath}" is not valid JSON: ${e.message}`);
  }
}

function loadAnswers(answersPath) {
  try {
    return parseYaml(fs.readFileSync(answersPath, "utf8")) || {};
  } catch (e) {
    fail(2, `Could not read/parse answers.yaml at "${answersPath}": ${e.message}`);
  }
}

function loadBank(bankPath) {
  if (!bankPath) return null;
  try {
    return JSON.parse(fs.readFileSync(bankPath, "utf8"));
  } catch {
    // A missing/unreadable bank export is never fatal -- classifyField's
    // bankLookup treats ctx.bank == null as "no bank entries available".
    return null;
  }
}

async function portIsUp() {
  try {
    const res = await fetch(`${CDP_URL}/json/version`, { signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function main() {
  const orderPath = process.argv[2];
  if (!orderPath) {
    fail(2, "Usage: node pipeline/apply/runner.mjs <order.json>");
  }
  const order = loadOrder(path.resolve(orderPath));

  acquireLock();

  const startedAt = nowIso();
  let browser = null;
  let page = null;
  try {
    if (!(await portIsUp())) {
      fail(
        4,
        `Brave is not reachable on ${CDP_URL} -- refusing to launch it. ` +
          "Start Brave with --remote-debugging-port=9223 (see brave_connect.mjs) and retry."
      );
    }

    try {
      browser = await puppeteer.connect({ browserURL: CDP_URL, defaultViewport: null });
    } catch (e) {
      fail(4, `Failed to connect to Brave over CDP at ${CDP_URL}: ${e.message}`);
    }

    // Adapter so dom.mjs (written against the omp eval `tab.run(fn, {args})`
    // convention) works unchanged here: dom.mjs's call sites always invoke
    // `tab.run(fn, { args: [x] })` expecting `fn({page}, x)` -- spreading
    // opts.args as positional arguments after the context object reproduces
    // that exactly.
    page = await browser.newPage();
    const tab = { run: (fn, opts = {}) => fn({ page }, ...(opts.args || [])) };

    const applyUrl = (order.job && (order.job.apply_url || order.job.url)) || null;
    if (!applyUrl) {
      fail(2, `order.json has no job.apply_url/job.url to navigate to (job_id ${order.job_id})`);
    }

    await page.goto(applyUrl, { waitUntil: "networkidle2", timeout: 45000 });
    // Let any client-side render (React/Vue app shells) finish settling
    // beyond the network going idle.
    await new Promise((r) => setTimeout(r, 1500));

    const answers = loadAnswers(order.answers_path);
    const bank = loadBank(order.bank_path);

    const attemptDir = order.attempt_dir;
    fs.mkdirSync(attemptDir, { recursive: true });
    const prefillPath = path.join(attemptDir, "prefill.png");
    const confirmPath = path.join(attemptDir, "confirm.png");
    let prefillWritten = false;
    let confirmWritten = false;

    const result = await fillApplication(tab, order.job, order.files, answers, {
      submit: order.mode === "submit",
      bank,
      approvedAnswers: order.approved_answers || null,
      onBeforeSubmit: async () => {
        await page.screenshot({ path: prefillPath, fullPage: true });
        prefillWritten = true;
      },
      onConfirmed: async () => {
        await page.screenshot({ path: confirmPath, fullPage: true });
        confirmWritten = true;
      },
      onSubmitFailed: async () => {
        await page.screenshot({ path: path.join(attemptDir, "after_submit_failed.png"), fullPage: true });
      },
    });

    const finishedAt = nowIso();
    const attempt = {
      schema: 1,
      job_id: order.job_id,
      mode: order.mode,
      started_at: startedAt,
      finished_at: finishedAt,
      apply_url: applyUrl,
      final_url: page.url(),
      status: result.status,
      gate_reasons: result.gate_reasons || [],
      fields: result.fields || [],
      unanswered: result.unanswered || [],
      snapshot: result.snapshot || [],
      autofill_issues: result.autofill_issues || [],
      uploads: result.uploads || [],
      screenshots: { prefill: prefillWritten ? "prefill.png" : null, confirm: confirmWritten ? "confirm.png" : null },
      confirmation_text: result.confirmation_text || null,
      confirmation_url: result.confirmation_url || null,
      post_submit_text: result.post_submit_text || null,
      post_submit_network: result.post_submit_network || null,
      errors: result.errors || [],
      bank_sha256: order.bank_sha256 || null,
    };
    fs.writeFileSync(path.join(attemptDir, "attempt.json"), JSON.stringify(attempt, null, 2), "utf8");
    console.log(`attempt.json written: status=${attempt.status} job_id=${order.job_id}`);
  } finally {
    // Never touch other tabs, never close the browser itself -- only the
    // page this run opened, then hand the CDP connection back.
    try {
      if (page) await page.close();
    } catch {
      // Best-effort: a page that failed to open/already closed is fine.
    }
    try {
      if (browser) browser.disconnect();
    } catch {
      // disconnect() should not throw, but never let cleanup crash the exit.
    }
    releaseLock();
  }
}

main().catch((e) => {
  releaseLock();
  fail(1, `runner.mjs crashed: ${(e && e.stack) || e}`);
});
