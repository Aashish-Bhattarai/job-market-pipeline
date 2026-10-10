import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import {
  classifyField,
  uploadBlocksSubmit,
  correctRegion,
  comboboxSearchHint,
  buildFactsAnswersHaystack,
  classifySnapshotEntry,
  checkApprovedAnswers, looksLikeEeoQuestion, isDemographicConsentLabel, samePhone } from "./field_rules.mjs";
import * as dom from "./dom.mjs";

const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const FACTS_PATH = path.join(moduleDir, "..", "profile", "facts.yaml");

let cachedFacts = null;
// js-yaml's bare `import("js-yaml")` fails to resolve inside the OMP eval
// JS sandbox (a Bun-specific module-resolution limitation of that managed
// environment, reported via xd://report_issue) even though it resolves
// fine under plain `node` -- and this loader must work identically in
// both hosts, since fillApplication is invoked directly from eval sessions
// driving a live browser tab as well as from plain-node scripts. Shelling
// out to Python's stdlib-adjacent `pyyaml` (already a hard dependency of
// this whole pipeline, see CONTRACTS.md) sidesteps the JS package
// resolution entirely and works the same everywhere `python` is on PATH.
function loadFacts() {
  if (cachedFacts) return cachedFacts;
  const py = "import sys,json,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1],encoding='utf-8'))))";
  const result = spawnSync("python", ["-c", py, FACTS_PATH], { encoding: "utf8" });
  if (result.error || result.status !== 0) {
    throw new Error(
      `loadFacts: failed to parse ${FACTS_PATH} via python: ` +
        (result.error ? result.error.message : `exit ${result.status}: ${result.stderr}`)
    );
  }
  cachedFacts = JSON.parse(result.stdout);
  return cachedFacts;
}

export function readTextFileIfExists(p) {
  if (!p) return null;
  try {
    // LF only: a textarea always reports "\n", so a CRLF cover letter (Windows .txt) read back as
    // "different" and the fill was held as unanswered / gate-blocked (Point72, 2026-10-02).
    return fs.readFileSync(p, "utf8").replace(/\r\n?/g, "\n");
  } catch {
    return null;
  }
}

function emptyResult(status, unanswered, errors) {
  return {
    status,
    fields: [],
    unanswered: unanswered || [],
    errors: errors || [],
    confirmation_text: null,
    confirmation_url: null,
    snapshot: [],
    autofill_issues: [],
    uploads: [],
    gate_reasons: [],
  };
}

async function fillApplicationInner(tab, job, files, answers, { submit = false, bank = null, approvedAnswers = null, onBeforeSubmit = null, onConfirmed = null, onSubmitFailed = null } = {}) {
  const fields = [];
  const unanswered = [];
  const errors = [];
  const uploads = [];

  // ---- 1. Blocker detection BEFORE touching anything -----------------
  let blockers;
  try {
    blockers = await dom.detectBlockers(tab);
  } catch (e) {
    return emptyResult("queued_manual", [{ label: "Page inspection failed", type: "system", required: true }], [String(e && e.message || e)]);
  }
  if (blockers.captcha) {
    return emptyResult("queued_manual", [{ label: `CAPTCHA present (${blockers.captcha})`, type: "system", required: true }], [`CAPTCHA detected: ${blockers.captcha}`]);
  }
  if (blockers.password || blockers.createAccount) {
    return emptyResult("queued_account", [{ label: "Account creation / password required", type: "system", required: true }], ["Form requires creating an account or setting a password"]);
  }
  if (blockers.idNumbers && blockers.idNumbers.length) {
    return emptyResult(
      "queued_manual",
      blockers.idNumbers.map((l) => ({ label: l, type: "text", required: true })),
      ["Government ID number field(s) detected: " + blockers.idNumbers.join("; ")]
    );
  }

  // ---- 2. Extract every control -----------------------------------------
  // Always check for a same-origin embedded ATS form iframe with strictly
  // more form controls than the top-level page (e.g. a Greenhouse job board
  // configured with a custom-domain redirect embeds the real form via
  // `/embed/job_app` on the company's own careers page, while the host page
  // itself may still have a handful of unrelated inputs like a newsletter
  // signup -- that must never win over the real, larger application form).
  let frameUrl = null;
  let descriptors;
  const extractAll = async () => {
    const embedded = await dom.findEmbeddedFormFrame(tab).catch(() => null);
    return { frameUrl: embedded || null, descriptors: await dom.extractFields(tab, embedded || null) };
  };
  try {
    ({ frameUrl, descriptors } = await extractAll());
    // JS-hydrated forms (Softwire's own React application) mount their controls
    // asynchronously: the first extraction can see a half-rendered form (13
    // fields, empty option lists) that never matches the real one (42). Wait
    // for the extraction to stabilise instead of racing the framework.
    for (let i = 0; i < 6; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const next = await extractAll().catch(() => null);
      if (!next) break;
      if (next.descriptors.length === descriptors.length) break;
      frameUrl = next.frameUrl;
      descriptors = next.descriptors;
    }
  } catch (e) {
    return emptyResult("queued_manual", [{ label: "Field extraction failed", type: "system", required: true }], [String(e && e.message || e)]);
  }

  // A job-description page (Gradcracker /out landings, SmartRecruiters, careers sites) shows the
  // posting and an Apply button but no form until it is clicked -- and the click can lead to a
  // second page (Reply's SuccessFactors: job page -> Apply -> "Sign in / create account"). Follow
  // the Apply control up to 3 hops, re-running every blocker check after each one (captcha, login,
  // account wall, government-ID field), stopping as soon as a form appears or a hop changes nothing.
  for (let hop = 0; hop < 3 && !dom.looksLikeApplicationForm(descriptors); hop++) {
    const urlBefore = await tab.run(async ({ page }) => page.url()).catch(() => null);
    const opened = await dom.openApplicationForm(tab).catch(() => null);
    if (!opened) break;
    const after = await dom.detectBlockers(tab).catch(() => null);
    if (after && after.captcha) {
      return emptyResult("queued_manual", [{ label: `CAPTCHA present (${after.captcha})`, type: "system", required: true }], [`CAPTCHA detected after opening the form: ${after.captcha}`]);
    }
    if (after && (after.password || after.createAccount)) {
      return emptyResult("queued_account", [{ label: "Account creation / password required", type: "system", required: true }], ["Form requires creating an account or setting a password (found after clicking Apply)"]);
    }
    if (after && after.idNumbers && after.idNumbers.length) {
      return emptyResult("queued_manual", after.idNumbers.map((l) => ({ label: l, type: "text", required: true })), ["Government ID number field(s) detected after opening the form: " + after.idNumbers.join("; ")]);
    }
    try {
      ({ frameUrl, descriptors } = await extractAll());
    } catch (e) {
      return emptyResult("queued_manual", [{ label: "Field extraction failed", type: "system", required: true }], [String(e && e.message || e)]);
    }
    if (opened.url === urlBefore && !dom.looksLikeApplicationForm(descriptors)) break; // the click changed nothing
  }

  if (!dom.looksLikeApplicationForm(descriptors)) {
    // Still no application form after trying the Apply control: if the page says the posting is
    // closed, say so (Arcadis's page reads "No longer accepting applications") rather than the
    // vague "no form fields" -- and it is not something a human can fix by reviewing it.
    const closed = dom.postingClosedReason(await dom.pageText(tab).catch(() => ""));
    if (closed) {
      return emptyResult("queued_manual", [{ label: closed, type: "system", required: true }], [closed]);
    }
  }

  if (!descriptors || descriptors.length === 0) {
    // No controls found at all: the page never rendered a real form (wrong
    // URL, still loading, or blocked/rate-limited by the site) -- never
    // report this as a successfully filled empty application.
    return emptyResult(
      "queued_manual",
      [{ label: "No form fields found on the page", type: "system", required: true }],
      ["extractFields returned 0 fields -- the application form likely never rendered (blocked, rate-limited, or wrong URL)"]
    );
  }

  const coverLetterText = readTextFileIfExists(files && files.cover_txt);
  // The discovery aggregator (Himalayas) frequently reports only a bare
  // country ("United Kingdom") for a job whose own ATS posting is explicit
  // about a specific hybrid city -- correct region here, from the job's own
  // description/location plus a live page-text hint, before it feeds the
  // office-attendance/address rules below.
  const regionHint = await dom.extractRegionHint(tab, frameUrl).catch(() => "");
  const correctedJob = { ...job, region: correctRegion(job, regionHint) };
  const ctx = { answers, job: correctedJob, facts: await loadFacts(), coverLetterText, bank };

    // ---- 3. Uploads ---------------------------------------------------------
  let anyUploadConfirmed = false;

  const fileFields = descriptors.filter((d) => d.type === "file");
  for (const d of fileFields) {
    const lower = (d.label || "").toLowerCase();
    let filePath = null;
    let source = null;
    let role = null;
    if (/resume|\bcv\b/i.test(lower)) {
      filePath = files && (files.cv_pdf || files.cv_docx);
      source = "files.cv_pdf";
      role = "cv";
    } else if (/cover/i.test(lower)) {
      filePath = files && (files.cover_pdf || files.cover_docx);
      source = "files.cover_pdf";
      role = "cover";
    } else {
      // Unknown file field (e.g. "additional documents"): don't guess.
      unanswered.push({ label: d.label, type: "file", required: d.required });
      continue;
    }
    if (!filePath) {
      unanswered.push({ label: d.label, type: "file", required: d.required });
      continue;
    }
    let confirmed = false;
    // Pre-check: if this exact file is already attached (e.g. this is a
    // fill/submit retry on an already-prepared form), do NOT re-fire the
    // file input -- re-uploading re-triggers the ATS's own asynchronous
    // resume parsing (observed: Ashby's autofill re-render), which
    // remounts/clears already-filled fields mid-fill.
    try {
      confirmed = await dom.isUploadConfirmed(tab, d.idx, filePath, frameUrl);
    } catch {
      confirmed = false;
    }
    let uploadPerformed = false;
    if (!confirmed) {
      uploadPerformed = true;
      try {
        confirmed = await dom.uploadFile(tab, d.idx, filePath, frameUrl);
      } catch (e) {
        // The ATS may re-render (remount) the file-input DOM node after an
        // earlier upload in this same pass completes -- observed on
        // Rippling, where successfully attaching the resume causes React to
        // unmount/remount every file-upload widget on the page, dropping the
        // `data-omp-field` tag this loop is still relying on for
        // not-yet-processed fields. A stale tag must never be read as "this
        // upload is impossible" -- re-extract fields once and retry by
        // matching the same label before giving up.
        try {
          const fresh = await dom.extractFields(tab, frameUrl);
          const freshField = fresh.find((f) => f.type === "file" && f.label === d.label);
          if (freshField) {
            confirmed = await dom.uploadFile(tab, freshField.idx, filePath, frameUrl);
          } else {
            errors.push(`Upload failed for "${d.label}": ${String((e && e.message) || e)}`);
          }
        } catch (e2) {
          errors.push(`Upload failed for "${d.label}": ${String((e2 && e2.message) || e2)}`);
        }
      }
    }
    // sha256 of the exact bytes sent, for the traceability record -- taken
    // regardless of whether the upload could be confirmed, so a failed
    // attempt's uploads[] entry still names exactly what was tried.
    let uploadSha256 = null;
    try {
      uploadSha256 = crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
    } catch {
      uploadSha256 = null;
    }
    if (!confirmed) {
      // We attempted this upload (had a real file path to send). Silently
      // dropping a resume/cover letter must never slip through as
      // "optional" just because the ATS itself didn't mark the field
      // required -- always block submission when an attempted upload
      // can't be confirmed.
      uploads.push({ label: d.label, role, file: filePath, sha256: uploadSha256, confirmed: false });
      unanswered.push({
        label: d.label,
        type: "file",
        required: uploadBlocksSubmit({ attempted: true, confirmed: false, required: d.required }),
      });
      errors.push(`Could not confirm "${path.basename(filePath)}" attached for "${d.label}"`);
      continue;
    }
    // Only a genuinely NEW upload triggers the ATS's asynchronous resume
    // parsing -- an already-confirmed attachment must not re-arm the
    // autofill race the settle dance below exists for.
    if (uploadPerformed) anyUploadConfirmed = true;
    uploads.push({ label: d.label, role, file: filePath, sha256: uploadSha256, confirmed: true });
    fields.push({ label: d.label, value: path.basename(filePath), source });
  }

  
  if (anyUploadConfirmed) {
    await dom.waitForUploadsToSettle(tab);
    // Ashby specific: wait for the asynchronous autofill to complete so it doesn't overwrite our manual inputs later.
    await tab.run(async ({ page }) => {
      await page.evaluate(() => {
         return new Promise(resolve => {
            if (document.body.innerText.includes("Autofill completed!")) return resolve();
            let count = 0;
            let interval = setInterval(() => {
               if (document.body.innerText.includes("Autofill completed!") || count > 40) {
                  clearInterval(interval);
                  resolve();
               }
               count++;
            }, 200); // Wait up to 8 seconds
         });
      });
    }).catch(() => {});
    await dom.waitAfterUploads();
    // Re-extract fields because ATS autofill (e.g. Ashby) frequently parses
    // the resume asynchronously and remounts/overwrites the form text inputs,
    // dropping their data-omp-field tags and clearing our values.
    descriptors = await dom.extractFields(tab, frameUrl).catch(() => descriptors);
  }

// ---- 4. Classify + fill each field -----------------------------------
  await dom.settleFormSaves(tab, { quietMs: 0, maxMs: 1 }).catch(() => null); // installs the Ashby save tracker before the first edit
  // The extractor lists checkboxes before comboboxes, so a "consent to process the demographic survey" box would be
  // judged before the survey answers exist. Fill those boxes last: ctx.demographicsAllDeclined then reflects the
  // real outcome of the survey questions.
  descriptors = [...descriptors.filter((d) => !(d.type === "checkbox" && isDemographicConsentLabel(d.label))),
                 ...descriptors.filter((d) => d.type === "checkbox" && isDemographicConsentLabel(d.label))];
  // A field can exist only after an earlier answer (Jane Street: ticking "I don't have a school email" reveals a required
  // "reason" box). Fill what appeared, up to two extra passes; anything already handled is skipped by type + label.
  const handledFields = new Set(descriptors.map((x) => `${x.type}|${x.label}`));
  // The same QUESTION is not new because a widget relabelled one of its controls (Recruitee: typing into "Phone number *" makes its
  // country selector appear as a second "Phone number *"): a label that already belongs to a handled field is skipped.
  const handledLabels = new Set(descriptors.map((x) => x.label));
  for (let pass = 0; pass < 3; pass++) {
    for (let d of descriptors) {
      // Ashby persists each edit server-side; let the previous field's write land before the next edit
      // (parallel writes raced and lost random fields -- see dom.mjs settleSaves). No-op on other ATS.
      await dom.settleFormSaves(tab).catch(() => null);
      if (d.type === "file") continue; // handled in the upload pass below
      // Consent-to-process-the-demographic-survey boxes are ticked only when the pipeline has answered at least one
      // demographic question (always with the decline option) and left none unanswered -- see field_rules.mjs.
      ctx.demographicsAllDeclined = fields.some((f) => /protected_characteristics/.test(f.source || ""))
        && !unanswered.some((u) => looksLikeEeoQuestion(u.label));

      if (d.type === "combobox") {
        // Some ATS platforms (observed: Greenhouse's embedded form) re-render
        // (remount) large portions of the DOM in response to an earlier
        // field's own interaction (typing into an async-search combobox,
        // picking an option elsewhere), dropping the `data-omp-field` tag a
        // LATER field in this same loop is still relying on. A stale idx
        // must never be silently treated as "this combobox has no options" --
        // re-extract once and match by label before giving up.
        const stillPresent = await dom
          .readValue(tab, d.idx, frameUrl)
          .then((v) => v !== null)
          .catch(() => false);
        if (!stillPresent) {
          const fresh = await dom.extractFields(tab, frameUrl).catch(() => []);
          const freshField = fresh.find((f) => f.type === "combobox" && f.label === d.label);
          if (freshField) d = { ...d, idx: freshField.idx };
        }
        let opts;
        try {
          opts = await dom.openCombobox(tab, d.idx, frameUrl);
        } catch (e) {
          opts = [];
        }
        let decision = opts.length ? classifyField({ ...d, type: "combobox", options: opts }, ctx) : null;
        // Some comboboxes (Greenhouse's School/Degree/Discipline pickers)
        // are virtualized/async-search widgets that only render a short
        // default slice of a huge option list until the user types to
        // filter it -- the real match can be entirely absent from the
        // untyped `opts`. Retry once by typing a plausible search hint
        // (derived from facts, independent of the options actually seen)
        // before re-collecting options.
        if (!opts.length || !decision || dom.matchComboboxOption(opts, decision.value) === -1) {
          const hint = comboboxSearchHint(d.label, ctx.facts, ctx);
          if (hint) {
            try {
              opts = await dom.openCombobox(tab, d.idx, frameUrl, hint);
            } catch (e) {
              // keep whatever opts we already had
            }
            
            // Fallback for Ashby "Location (Country)" fields when city hint yields nothing
            if (!opts.length && /^location\b/i.test(d.label)) {
              // Wait, we can't dynamic import easily here if top level throws.
              // We just use ctx.answers.addresses and find the country manually or rely on facts.contact.country!
              const addrList = ctx.answers && ctx.answers.addresses ? Object.values(ctx.answers.addresses) : [];
              const countryHint = addrList.length > 0 ? addrList[0].country : null; console.log("FALLBACK TRIGGERED", d.label, "countryHint:", countryHint);
              if (countryHint) {
                try {
                   opts = await dom.openCombobox(tab, d.idx, frameUrl, countryHint);
                } catch (e) {}
              }
            }
            
            console.log("OPTS returned:", opts);
            decision = opts.length ? classifyField({ ...d, type: "combobox", options: opts }, ctx) : decision;
          }
        }
        if (!opts.length) {
          await dom.closeCombobox(tab);
          unanswered.push({ label: d.label, type: d.type, required: d.required, reason: "no_options", options: [] });
          continue;
        }
        if (!decision) {
          await dom.closeCombobox(tab);
          unanswered.push({ label: d.label, type: d.type, required: d.required, reason: "no_rule", options: opts });
          continue;
        }
        const optionIndex = dom.matchComboboxOption(opts, decision.value);
        if (optionIndex === -1) {
          await dom.closeCombobox(tab);
          unanswered.push({ label: d.label, type: d.type, required: d.required, reason: "option_not_found", options: opts });
          continue;
        }
        await dom.pickComboboxOption(tab, optionIndex, frameUrl);
        // The widget's own re-render (React state update swapping in the
        // "selected value" display node) runs asynchronously after the
        // mousedown/mouseup/click sequence resolves -- a single immediate
        // read races that update and can read a genuinely successful pick
        // as failed. Poll briefly instead of checking exactly once.
        let after = null;
        let afterOk = false;
        // Widgets frequently display a transformed or abbreviated form of
        // the picked option once collapsed -- Greenhouse's "Country" picker
        // (a phone dial-code selector in disguise) shows only a flag and
        // "+44" for the picked "United Kingdom +44". Accept the read-back
        // when it matches EITHER the decision value OR the picked option's
        // own text, in either substring direction (empty read-backs never
        // count as a match).
        const wantedTexts = [String(decision.value), String(opts[optionIndex])]
          .filter(Boolean)
          .map((s) => s.trim().toLowerCase());
        const pickDeadline = Date.now() + 1500;
        while (Date.now() < pickDeadline) {
          after = await dom.readValue(tab, d.idx, frameUrl).catch(() => null);
          const rb = typeof after === "string" ? after.trim().toLowerCase() : "";
          afterOk = rb.length > 0 && wantedTexts.some((w) => w.includes(rb) || rb.includes(w));
          if (afterOk) break;
          await new Promise((r) => setTimeout(r, 150));
        }
        if (!afterOk) {
          unanswered.push({ label: d.label, type: d.type, required: d.required, reason: "option_not_found", options: opts });
          errors.push(`Combobox "${d.label}" did not reflect selection "${decision.value}" after picking (read back "${after}")`);
          continue;
        }
        fields.push({ label: d.label, value: decision.value, source: decision.source });
        continue;
      }

      // Lazy selects (Softwire's wizard populates <option>s only after the
      // widget is opened): open it once and re-read the freshly rendered
      // options before deciding the field is unanswerable.
      if (d.type === "select" && !(d.options || []).length) {
        await dom.openCombobox(tab, d.idx, frameUrl).catch(() => {});
        const fresh = (await dom.extractFields(tab, frameUrl).catch(() => null) || [])
          .find((f) => f.type === "select" && f.label === d.label && (f.options || []).length);
        if (fresh) d = fresh;
      }

      const decision = classifyField(d, ctx);
      if (!decision) {
        // keep the options: a held checkbox group / radio is undiagnosable without them
        unanswered.push({ label: d.label, type: d.type, required: d.required, options: d.options || [] });
        continue;
      }

      if (d.type === "radio" || d.type === "checkbox-group") {
        // A checkbox-group question can resolve to an array (multi-select,
        // e.g. office-attendance "which working patterns" or a one-element
        // EEO decline array) -- a radio question always resolves to a
        // single scalar. Normalize to a list of options to tick either way.
        const values = Array.isArray(decision.value) ? decision.value : [decision.value];
        let allOk = true;
        let idx = d.idx;
        for (const v of values) {
          await dom.setOptionState(tab, idx, v, true, frameUrl);
          let checked = await dom.readOptionState(tab, idx, v, frameUrl).catch(() => null);
          if (checked !== true) {
            // Ashby re-renders its form after earlier answers (observed after
            // the CV upload), destroying the data-omp-field tags captured at
            // extraction -- re-tag from the live DOM and retry once. The group
            // keeps the same index number after re-tagging, so the retry must
            // not depend on the index having changed.
            const fresh = (await dom.extractFields(tab, frameUrl).catch(() => null) || [])
              .find((f) => f.type === d.type && f.label === d.label);
            if (fresh) {
              idx = fresh.idx;
              await dom.setOptionState(tab, idx, v, true, frameUrl);
              checked = await dom.readOptionState(tab, idx, v, frameUrl).catch(() => null);
            }
          }
          if (checked !== true) {
            allOk = false;
            break;
          }
        }
        if (!allOk) {
          unanswered.push({ label: d.label, type: d.type, required: d.required });
          errors.push(`"${d.label}" option(s) did not verify as selected after setting`);
          continue;
        }
        fields.push({ label: d.label, value: decision.value, source: decision.source });
        continue;
      }

      if (d.type === "checkbox") {
        // Never tick unless the target state is explicitly true from answers.yaml.
        if (decision.value !== true) continue;
        await dom.setCheckbox(tab, d.idx, true, frameUrl).catch(() => {});
        const val = await dom.readValue(tab, d.idx, frameUrl).catch(() => null);
        if (val !== true) {
          unanswered.push({ label: d.label, type: d.type, required: d.required });
          errors.push(`Checkbox "${d.label}" did not verify as checked`);
          continue;
        }
        fields.push({ label: d.label, value: true, source: decision.source });
        continue;
      }

      if (d.type === "date") {
        const r = await dom.setDate(tab, d.idx, decision.value, frameUrl).catch((e) => ({ ok: false, reason: String(e && e.message || e) }));
        const shown = await dom.readValue(tab, d.idx, frameUrl).catch(() => null);
        if (!r || r.ok === false || !shown) {
          unanswered.push({ label: d.label, type: d.type, required: d.required });
          errors.push(`Date field "${d.label}" could not be set to ${decision.value} (${r && r.reason})`);
          continue;
        }
        fields.push({ label: d.label, value: decision.value, source: decision.source });
        continue;
      }

      // text / email / tel / textarea / select
      let set = await dom.setValue(tab, d.idx, decision.value, frameUrl);
      let after = await dom.readValue(tab, d.idx, frameUrl).catch(() => null);
      if (after === null || after === "") {
        // React forms (Lightning AI: the LinkedIn answer read back "" then null) re-render after an earlier answer and
        // drop our element tag or the value: re-tag by label and set once more, as radios and comboboxes already do.
        const fresh = (await dom.extractFields(tab, frameUrl).catch(() => null) || [])
          .find((f) => f.type === d.type && f.label === d.label);
        if (fresh) {
          d = { ...d, idx: fresh.idx };
          set = await dom.setValue(tab, d.idx, decision.value, frameUrl);
          after = await dom.readValue(tab, d.idx, frameUrl).catch(() => null);
        }
      }
      // Phone widgets frequently apply their own input mask (e.g. stripping
      // spaces/dashes) -- compare digits-only for anything phone-shaped, since
      // some ATS platforms use a plain `type=text` input for a phone field
      // rather than `type=tel`.
      const looksLikePhone = d.type === "tel" || /phone|mobile|telephone/i.test(d.label || "");
      const matches =
        looksLikePhone && typeof after === "string"
          ? samePhone(after, decision.value)
          : after === decision.value || (set && set.picked && after === set.picked);   // a typeahead shows the suggestion clicked
      if (!matches) {
        unanswered.push({ label: d.label, type: d.type, required: d.required });
        errors.push(`"${d.label}" read back "${after}" after setting "${decision.value}"`);
        continue;
      }
      fields.push({ label: d.label, value: (set && set.picked) || decision.value, source: decision.source });
    }
    if (pass === 2) break;
    const fresh = await dom.extractFields(tab, frameUrl).catch(() => []);
    const appeared = fresh.filter((f) => f.type !== "file" && !handledFields.has(`${f.type}|${f.label}`) && !handledLabels.has(f.label));
    if (!appeared.length) break;
    appeared.forEach((f) => { handledFields.add(`${f.type}|${f.label}`); handledLabels.add(f.label); });
    descriptors = appeared;
  }

  await dom.settleFormSaves(tab).catch(() => null);

  // ---- 5. Pre-submit snapshot: EVERY control, not just what we filled ---
  // Re-extract the whole form right before deciding anything (dry run or
  // submit) so an ATS autofill -- or a field that only appeared after an
  // earlier answer -- is captured and checked, never silently trusted.
  let snapshotDescriptors;
  try {
    snapshotDescriptors = await dom.extractFields(tab, frameUrl);
  } catch (e) {
    snapshotDescriptors = descriptors;
  }
  const filledSourceByLabel = new Map(fields.map((f) => [f.label, f.source]));
  const haystack = buildFactsAnswersHaystack(ctx.facts, answers);
  const snapshot = [];
  const autofillIssues = [];
  for (const d of snapshotDescriptors) {
    const value = await dom.getFieldValueForSnapshot(tab, d, frameUrl).catch(() => null);
    const cls = classifySnapshotEntry(d, value, { filledSourceByLabel, ctx, haystack });
    snapshot.push({
      idx: d.idx,
      label: d.label,
      type: d.type,
      required: d.required,
      value,
      options: d.options && d.options.length ? d.options : null,
      filled_by: cls.filled_by,
      source: cls.source || null,
    });
    if (cls.issue) {
      autofillIssues.push({ label: d.label, value, reason: "ATS-autofilled value not found verbatim in facts.yaml/answers.yaml" });
    }
  }

  // A required field left "untouched" in the FULL snapshot that the fill
  // loop above never even saw blocks the same as any other required-but-
  // unanswered field -- but never double-report one already queued above.
  const alreadyQueuedLabels = new Set(unanswered.map((u) => u.label));
  const newlyDiscoveredRequiredUntouched = snapshot
    .filter((s) => s.required && s.filled_by === "untouched" && !alreadyQueuedLabels.has(s.label))
    .map((s) => s.label);

  const gateReasons = [];
  if (newlyDiscoveredRequiredUntouched.length) {
    gateReasons.push(`required field(s) left unanswered: ${newlyDiscoveredRequiredUntouched.join("; ")}`);
  }
  if (autofillIssues.length) {
    gateReasons.push(`unverifiable ATS-autofilled value(s): ${autofillIssues.map((a) => a.label).join("; ")}`);
  }
  if (submit && approvedAnswers) {
    const currentFilled = fields.map((f) => ({ label: f.label, value: f.value }));
    const approvalCheck = checkApprovedAnswers(approvedAnswers, currentFilled);
    if (!approvalCheck.ok) {
      gateReasons.push(`answers changed since approval: ${approvalCheck.mismatched.join("; ")}`);
    }
  }

  const withGateData = (extra) => ({
    fields,
    unanswered,
    errors,
    snapshot,
    autofill_issues: autofillIssues,
    uploads,
    gate_reasons: gateReasons,
    confirmation_text: null,
    confirmation_url: null,
    ...extra,
  });
  // Screenshot hook (runner.mjs): full-page screenshot of the CURRENT state
  // -- right before a real submit click, or at the end of a dry run (this
  // is the only point every code path reaches, gate failure included, so
  // there's always a visual record of exactly what was on the page).
  if (onBeforeSubmit) {
    try {
      await onBeforeSubmit({ tab, frameUrl });
    } catch (e) {
      errors.push(`prefill screenshot hook failed: ${String((e && e.message) || e)}`);
    }
  }

  // Never submit on gate failure.
  if (gateReasons.length) {
    return withGateData({ status: "blocked_gate" });
  }

  // ---- 6. Decide status ---------------------------------------------------
  // The gate above passed, so the fresh snapshot holds no required-and-empty control. A fill-phase "unanswered required" entry
  // whose control is no longer a required control on the page was switched off by a LATER answer (Jane Street: ticking "I don't
  // have a school email" disables "university email"): it is not outstanding. Entries still present stay blocking.
  const requiredNow = new Set((snapshotDescriptors || []).filter((s) => s.required).map((s) => `${s.type}|${s.label}`));
  for (const u of unanswered) {
    if (u.required && u.type !== "file" && u.type !== "system" && snapshotDescriptors !== descriptors
        && !requiredNow.has(`${u.type}|${u.label}`)) {
      u.required = false;
      u.superseded = "no longer a required control on the page";
    }
  }
  const blockingUnanswered = unanswered.filter((u) => u.required);
  if (blockingUnanswered.length > 0) {
    return withGateData({ status: "queued_manual" });
  }

  if (!submit) {
    return withGateData({ status: "filled_not_submitted" });
  }

  await dom.settleFormSaves(tab).catch(() => null); // every write landed server-side before Submit reads it
  const control = await dom.findSubmitControl(tab, frameUrl);
  if (!control.found) {
    return withGateData({ status: "submit_failed", errors: [...errors, "No submit control found on the page"] });
  }

  await new Promise((r) => setTimeout(r, 2000));
  const result = await dom.clickSubmit(tab, frameUrl);
  const confirmation = dom.detectConfirmation(result);
  if (!confirmation.success) {
    // Keep the page's own words: the validation selectors above miss several ATS bundles, and
    // "no confirmation" alone can mean blocked-by-validation OR submitted-but-unrecognised.
    const page = dom.stripBlobs(result.text).replace(/\s+/g, " ").trim();
    const pageErrs = (page.match(/[^.!?]{0,80}(?:required|error|missing|invalid|must be complete)[^.!?]{0,80}/gi) || [])
      .map((s) => `page after submit: ${s.trim()}`).slice(0, 3);
    // centre the kept text on the outcome: a long posting page puts the form's fate thousands of
    // characters past its head
    const at = page.search(/thank you for|already applied|was (not )?(received|submitted)|required|error/i);
    const pageAround = at >= 0 ? page.slice(Math.max(0, at - 500), at + 900) : page.slice(-1200);
    // The page as it stands after the click (inline validation messages show where): a failed submit is otherwise a guess.
    if (onSubmitFailed) await onSubmitFailed({ tab, frameUrl }).catch(() => null);
    return withGateData({
      status: "submit_failed",
      errors: [...errors, ...confirmation.errors,
               ...(confirmation.errors.length ? [] : pageErrs.length ? pageErrs : ["No success confirmation detected after clicking submit"])],
      confirmation_text: confirmation.confirmation_text,
      confirmation_url: confirmation.confirmation_url,
      post_submit_text: pageAround || null,
      post_submit_network: result.network || null,
    });
  }

  if (onConfirmed) {
    try {
      await onConfirmed({ tab, frameUrl, confirmation });
    } catch (e) {
      errors.push(`confirm screenshot hook failed: ${String((e && e.message) || e)}`);
    }
  }
  return withGateData({
    status: "submitted",
    confirmation_text: confirmation.confirmation_text,
    confirmation_url: confirmation.confirmation_url,
    post_submit_network: result.network || null,
  });
}


// runner.mjs (Node → attempt.json) and the ledger replace the old
// answers.json auto-write: this returns the richer result (fields,
// unanswered, snapshot, autofill_issues, uploads, gate_reasons,
// confirmation) and never touches disk itself.
export async function fillApplication(tab, job, files, answersYaml, opts = {}) {
  return await fillApplicationInner(tab, job, files, answersYaml, opts);
}
