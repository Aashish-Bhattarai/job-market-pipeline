// Fixture-based extraction test: runs the same pure `extractFieldsFromDocument`
// walker that dom.mjs feeds into `page.evaluate` against synthetic, server-rendered
// application-form HTML (no browser/JS execution needed to render these forms).
// Run with: node test_extraction.mjs
//
// Fixtures: three synthetic Lever-style `/apply` postings for a fictional "Example
// Corp", modelled on Lever's real server-rendered form markup (inputs, selects,
// textareas, checkbox groups, and the custom-question schema are all present in
// the raw HTML), so they are a faithful target for the offline extraction walker.
//
// Greenhouse (job-boards.greenhouse.io) and Ashby (jobs.ashbyhq.com) were also
// investigated as fixture sources:
//   - Greenhouse: the modern job-boards.greenhouse.io platform hydrates the
//     entire application form client-side from a JS bundle, and the legacy
//     server-rendered `/embed/job_app` endpoint 404s for boards migrated to
//     that platform -- there is no static HTML form to extract fields from.
//   - Ashby (jobs.ashbyhq.com/<org>/<postingId>): the posting page is a React
//     SPA shell; the application form only exists after client-side render.
//   - Workable (apply.workable.com/<org>): the board index page is a ~7KB SPA
//     shell with no server-rendered job links, so no workable `/apply` HTML
//     could be captured either.
// For these three, `dom.mjs`'s live extraction still works against the real
// rendered DOM once Puppeteer has executed the page's JS (see the live
// submit:false smoke tests) — it just cannot be exercised offline with a
// static-HTML fixture the way Lever-style markup can.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { parseHTML } from "linkedom";
import { extractFieldsFromDocument, isFileUploadConfirmed, isApplyCtaText, looksLikeApplicationForm, postingClosedReason, classifyCaptchaFrame, settleSaves, detectConfirmation, stripBlobs, pickSubmitCandidate, isSubmitButtonCandidate, pickLocationSuggestion, captchaBlockedSubmit } from "./dom.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = [
  "fixtures/example_apply_1.html",
  "fixtures/example_apply_2.html",
  "fixtures/example_apply_3.html",
];

let pass = 0;
let fail = 0;
const failures = [];
function test(name, fn) {
  try {
    fn();
    pass++;
  } catch (e) {
    fail++;
    failures.push({ name, error: e.message });
  }
}

for (const rel of FIXTURES) {
  const html = fs.readFileSync(path.join(__dirname, rel), "utf8");
  const { document } = parseHTML(html);

  test(`${rel}: extraction runs and finds fields`, () => {
    const fields = extractFieldsFromDocument(document, document.querySelector("form") || document.body);
    assert.ok(fields.length > 10, `expected >10 fields, got ${fields.length}`);
  });

  test(`${rel}: finds the standard Lever contact fields`, () => {
    const fields = extractFieldsFromDocument(document, document.querySelector("form") || document.body);
    const labels = fields.map((f) => f.label.toLowerCase());
    assert.ok(labels.some((l) => l.includes("name")), "expected a name field");
    assert.ok(fields.some((f) => f.type === "file"), "expected a resume file upload field");
  });

  test(`${rel}: required custom questions are flagged required`, () => {
    const fields = extractFieldsFromDocument(document, document.querySelector("form") || document.body);
    const required = fields.filter((f) => f.required);
    assert.ok(required.length > 0, "expected at least one required field to be detected");
  });

  test(`${rel}: checkbox groups (e.g. language skills) are grouped with options, not exploded into lonely checkboxes`, () => {
    const fields = extractFieldsFromDocument(document, document.querySelector("form") || document.body);
    const groups = fields.filter((f) => f.type === "checkbox-group");
    assert.ok(groups.length > 0, "expected at least one checkbox-group");
    const withOptions = groups.find((g) => g.options.length > 1);
    assert.ok(withOptions, "expected a checkbox-group with multiple options");
  });

  test(`${rel}: selects expose their option text`, () => {
    const fields = extractFieldsFromDocument(document, document.querySelector("form") || document.body);
    const selects = fields.filter((f) => f.type === "select");
    assert.ok(selects.length > 0, "expected at least one <select>");
    assert.ok(selects.every((s) => Array.isArray(s.options) && s.options.length > 0), "every select must expose options");
  });
}

// Synthetic case (not a captured fixture): Greenhouse's embedded
// `/embed/job_app` form labels every upload slot with the identical
// visually-hidden caption "Attach", distinguished only by the underlying
// input's id ("resume" / "cover_letter"). apply.mjs's file-upload matcher
// keys purely off `field.label`, so two fields both labelled "Attach"
// would be indistinguishable and both silently dropped as unknown uploads.
test("generic duplicate 'Attach' file labels fall back to id, not caption text", () => {
  const { document } = parseHTML(`<!doctype html><html><body><form>
    <div><button type="button">Attach</button><label class="visually-hidden" for="resume">Attach</label>
      <input id="resume" type="file"></div>
    <div><button type="button">Attach</button><label class="visually-hidden" for="cover_letter">Attach</label>
      <input id="cover_letter" type="file"></div>
  </form></body></html>`);
  const fields = extractFieldsFromDocument(document, document.querySelector("form"));
  const files = fields.filter((f) => f.type === "file");
  assert.equal(files.length, 2, "expected both file inputs to be extracted");
  const labels = files.map((f) => f.label.toLowerCase());
  assert.ok(labels.includes("resume"), `expected a field labelled "resume", got: ${JSON.stringify(labels)}`);
  assert.ok(labels.includes("cover letter"), `expected a field labelled "cover letter", got: ${JSON.stringify(labels)}`);
});

// Some ATS widgets (e.g. Greenhouse's embedded form) unmount the original
// <input type=file> and replace it with a "file attached" display once an
// upload succeeds, so the element at the original data-omp-field selector
// is gone by the time the confirmation check runs. A missing element must
// never be misread as "upload failed" when the filename is genuinely
// visible elsewhere on the page.
test("isFileUploadConfirmed: confirms via visible filename after the tagged input is unmounted", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <div class="file-upload__wrapper"><p>CV_Demo_Candidate.pdf</p><button aria-label="Remove file"></button></div>
  </body></html>`);
  const confirmed = isFileUploadConfirmed(document, '[data-omp-field="11"]', "CV_Demo_Candidate.pdf");
  assert.equal(confirmed, true, "expected confirmation via visible filename text despite missing element");
});

test("isFileUploadConfirmed: still reports false when the filename never appears anywhere", () => {
  const { document } = parseHTML(`<!doctype html><html><body><div>no upload happened</div></body></html>`);
  const confirmed = isFileUploadConfirmed(document, '[data-omp-field="11"]', "CV_Demo_Candidate.pdf");
  assert.equal(confirmed, false, "expected no false positive when filename is absent");
});

// Ashby's MultiValueSelect questions render as N independently-named/id'd
// checkboxes with no shared `name` -- before this fix they fell to the
// lonely-checkbox path and lost the question text entirely. They must be
// grouped into one checkbox-group question via their shared question
// container instead.
test("checkboxes sharing a question container (no shared name/id pattern) are grouped into one checkbox-group", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <div class="question">
      <div class="question-text">What working patterns would you be happy with?</div>
      <label><input type="checkbox" id="wp1" value="Fully remote">Fully remote</label>
      <label><input type="checkbox" id="wp2" value="Hybrid">Hybrid</label>
      <label><input type="checkbox" id="wp3" value="Full time in office">Full time in office</label>
    </div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const groups = fields.filter((f) => f.type === "checkbox-group");
  assert.equal(groups.length, 1, "expected the three checkboxes to collapse into a single checkbox-group");
  assert.equal(groups[0].label, "What working patterns would you be happy with?");
  assert.equal(groups[0].options.length, 3);
  assert.equal(fields.filter((f) => f.type === "checkbox").length, 0, "no lonely checkboxes should remain");
});

// Ashby's required marker for a grouped question sits on the wider
// question-container element, not the small div/label wrapping one
// radio/checkbox option -- the nearest-div check alone must never be
// trusted for groups.
test("required detection on a grouped field checks the wider question-container's class, not just the nearest small option wrapper", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <fieldset class="question required">
      <legend>Which working patterns would you be happy with?</legend>
      <div class="option-row"><input type="checkbox" id="wp1" value="Fully remote"><label for="wp1">Fully remote</label></div>
      <div class="option-row"><input type="checkbox" id="wp2" value="Hybrid"><label for="wp2">Hybrid</label></div>
    </fieldset>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const group = fields.find((f) => f.type === "checkbox-group");
  assert.ok(group, "expected a checkbox-group to be extracted");
  assert.equal(group.required, true, "expected the fieldset's own 'required' class to flag the group as required");
});

test("required detection also finds a standalone required marker element inside the wider question container", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <div class="question-wrapper">
      <div class="question-text">Which communities do you belong to</div>
      <span class="required">Required</span>
      <div class="option-row"><input type="checkbox" id="c1" value="Disability"><label for="c1">Disability</label></div>
      <div class="option-row"><input type="checkbox" id="c2" value="Other"><label for="c2">Other</label></div>
    </div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const group = fields.find((f) => f.type === "checkbox-group");
  assert.ok(group, "expected a checkbox-group to be extracted");
  assert.equal(group.required, true, "expected the sibling '.required' marker inside the wider question container to flag it required");
});

// Rippling-style custom radio widgets: no native <input type=radio> at all,
// just role=radiogroup/role=radio divs -- these were previously ignored
// entirely by the radio extraction pass.
test("[role=radiogroup]/[role=radio] widgets with no native <input> are extracted as a radio question", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <div>
      <div id="q1-label">Do you require export control clearance?</div>
      <div role="radiogroup" aria-labelledby="q1-label" aria-required="true">
        <div role="radio" aria-checked="false" id="opt-yes">Yes</div>
        <div role="radio" aria-checked="false" id="opt-no">No</div>
      </div>
    </div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const radio = fields.find((f) => f.type === "radio");
  assert.ok(radio, "expected a radio-type field from the role=radiogroup widget");
  assert.equal(radio.label, "Do you require export control clearance?");
  assert.equal(radio.required, true);
  assert.deepEqual(radio.options, ["Yes", "No"]);
});

// Rippling's opaque `customQuestions.<hash>.<uniqueKey>` name must never be
// reported as the field's label verbatim -- the real question title should
// be resolved from the page's own embedded __NEXT_DATA__ payload first.
test("an opaque Rippling customQuestions name resolves to the real question title via __NEXT_DATA__", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <script id="__NEXT_DATA__" type="application/json">${JSON.stringify({
      props: {
        pageProps: {
          activeJobApplication: {
            additionalQuestions: [
              {
                uniqueKey: "1ac2a765-4552-412a-9908-d36efa0ad900",
                title: "Please list all nationalities and citizenships you currently hold.",
              },
            ],
          },
        },
      },
    })}</script>
    <div><input type="text" name="customQuestions.69aa1a8a23287cf62f784c66.1ac2a765-4552-412a-9908-d36efa0ad900"></div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const textField = fields.find((f) => f.type === "text");
  assert.ok(textField, "expected the opaque-named input to still be extracted");
  assert.equal(textField.label, "Please list all nationalities and citizenships you currently hold.");
});

// Rippling's real DOM stores the opaque id in `data-testid` on the
// [role=radiogroup] wrapper itself (not `name`/`placeholder`, which don't
// exist on a plain <div>) -- the testId fallback must be resolved the same
// way, not trusted verbatim just because it's a semantic QA hook.
test("an opaque Rippling customQuestions data-testid on a role=radiogroup resolves via __NEXT_DATA__", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <script id="__NEXT_DATA__" type="application/json">${JSON.stringify({
      props: {
        pageProps: {
          apiData: {
            jobPost: {
              activeJobApplication: {
                additionalQuestions: [
                  {
                    form: {
                      questions: [
                        {
                          uniqueKey: "27142e42-e916-4c86-b237-d154884dc66c",
                          title: "Do you require export control clearance?",
                        },
                      ],
                    },
                  },
                ],
              },
            },
          },
        },
      },
    })}</script>
    <div class="css-1oic1sx" data-testid="customQuestions.69aa1a8a23287cf62f784c66.27142e42-e916-4c86-b237-d154884dc66c" role="radiogroup">
      <div role="radio" aria-checked="false">Yes</div>
      <div role="radio" aria-checked="false">No</div>
    </div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const radio = fields.find((f) => f.type === "radio");
  assert.ok(radio, "expected a radio-type field from the role=radiogroup widget");
  assert.equal(radio.label, "Do you require export control clearance?");
});


test("an opaque name with no matching __NEXT_DATA__ entry falls back to the raw name (never crashes)", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <div><input type="text" name="customQuestions.deadbeefdeadbeefdeadbeef.00000000-0000-0000-0000-000000000000"></div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const textField = fields.find((f) => f.type === "text");
  assert.ok(textField, "expected the field to still be extracted");
  assert.ok(textField.label.length > 0, "expected a non-empty fallback label");
});

// Ashby's application form renders required yes/no questions with NO native
// <input> and NO role attributes: a pair of <button data-option="yes|no"
// aria-pressed> toggles inside the question container (markup captured live
// from jobs.ashbyhq.com, 2026-09-27). The extractor must surface these as a
// radio-type group with Yes/No options or the question is silently skipped
// and the ATS rejects the application as incomplete.
test("Ashby aria-pressed yes/no button pair extracts as a required radio group with Yes/No options", () => {
  const { document } = parseHTML(`<!doctype html><html><body>
    <div class="ashby-application-form-field-entry" data-field-path="53ae50f6-9245-4f30-aaa9-f9b2070be940">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 ashby-application-form-question-title" for="53ae50f6-9245-4f30-aaa9-f9b2070be940">Are you currently based in the UK?&nbsp;</label>
      <div class="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno">
        <button class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes">Yes</button>
        <button class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no">No</button>
      </div>
      <input type="checkbox" name="53ae50f6-9245-4f30-aaa9-f9b2070be940" value="on" hidden>
    </div>
  </body></html>`);
  const fields = extractFieldsFromDocument(document, document.body);
  const yesno = fields.find((f) => f.type === "radio");
  assert.ok(yesno, "expected the yes/no button pair to be extracted as a radio group");
  assert.equal(yesno.label, "Are you currently based in the UK?");
  assert.deepEqual(yesno.options, ["Yes", "No"]);
  assert.equal(yesno.required, true, "the question label carries the _required_ class, so the group must be required");
  // The hidden native checkbox behind the widget must NOT surface as a
  // separate lonely checkbox field alongside the group.
  const checkboxes = fields.filter((f) => f.type === "checkbox");
  assert.equal(checkboxes.length, 0, "the widget's hidden native checkbox must never double-report");
});


test("isApplyCtaText: unambiguous apply controls qualify, third-party sign-ins and look-alikes never do", () => {
  for (const ok of ["Apply", "Apply now", "Apply Online Now", "Apply for this job", "Start application", "I'm interested", "Apply here »"]) {
    assert.equal(isApplyCtaText(ok), true, ok);
  }
  for (const no of ["Apply with LinkedIn", "Apply with Indeed", "Sign in to apply", "Save job", "Share this job", "Create job alert",
                    "Refer a friend", "Application status", "Apply for jobs at our other offices and teams around the world", ""]) {
    assert.equal(isApplyCtaText(no), false, no);
  }
});

test("looksLikeApplicationForm: a job page with only a search box and cookie toggles is not a form; contact or upload fields are", () => {
  assert.equal(looksLikeApplicationForm([{ type: "text", label: "Search jobs" }, { type: "checkbox", label: "Analytics cookies" }]), false);
  assert.equal(looksLikeApplicationForm([]), false);
  assert.equal(looksLikeApplicationForm([{ type: "email", label: "Email address" }]), true);
  assert.equal(looksLikeApplicationForm([{ type: "file", label: "Upload" }]), true);
  assert.equal(looksLikeApplicationForm([{ type: "text", label: "First name" }]), true);
});


test("postingClosedReason: a closed posting says so; an open one does not", () => {
  assert.match(postingClosedReason("Graduate Data Scientist Sheffield. No longer accepting applications. Job ID 43797"), /Posting is closed/);
  assert.match(postingClosedReason("Sorry, this position has been filled."), /Posting is closed/);
  assert.match(postingClosedReason("Applications are now closed for this role"), /Posting is closed/);
  for (const live of ["The job you are looking for is no longer open. Current openings at Deliveroo",
                      "Page not found The job board you were viewing is no longer active.",
                      "Sorry, we couldn't find anything here The job posting you're looking for might have closed, or it has been removed. (404 error)",
                      "Job not found The job you requested was not found. View all open positions"]) {
    assert.match(postingClosedReason(live), /Posting is closed/, live);
  }
  assert.equal(postingClosedReason("This role involves closed loop control; applications close on 6 November."), null);
  assert.equal(postingClosedReason("We are accepting applications until 6 November. Apply now."), null);
  assert.equal(postingClosedReason(""), null);
});

test("classifyCaptchaFrame: DataDome's block frame (SmartRecruiters) is a visible human-verification wall", () => {
  assert.deepEqual(classifyCaptchaFrame("https://geo.captcha-delivery.com/captcha/?initialCid=AHrl&hash=E5A9"), { type: "DataDome", visible: true });
  assert.equal(classifyCaptchaFrame("https://example.com/embed"), null);
});

test("extraction: radio groups and a lone 'Yes' checkbox with no fieldset/legend take the question text above them (Helsing's Tailwind markup)", () => {
  const html = `<form><div class="grid">
    <div class="flex flex-col"><div>Are you open to relocation?</div>
      <div class="row"><label><input type="radio" name="q1" value="0">No</label><label><input type="radio" name="q1" value="1">Yes</label></div></div>
    <div class="flex flex-col"><div>Do you hold the right to work in the UK? *</div>
      <div class="row"><label><input type="radio" name="q2" value="0">No</label><label><input type="radio" name="q2" value="1">Yes</label></div></div>
    <div class="flex flex-col"><div>I have reviewed and understood the privacy regime of this employer.</div>
      <div class="row"><label><input type="checkbox" name="q3">Yes</label></div></div>
  </div></form>`;
  const { document } = parseHTML(html);
  const fields = extractFieldsFromDocument(document, document.querySelector("form"));
  const radios = fields.filter((f) => f.type === "radio");
  assert.deepEqual(radios.map((f) => f.label), ["Are you open to relocation?", "Do you hold the right to work in the UK? *"]);
  assert.deepEqual(radios[0].options, ["No", "Yes"]);
  const box = fields.find((f) => f.type === "checkbox");
  assert.match(box.label, /^I have reviewed and understood the privacy regime/);
});

test("extraction: groups sharing one parent never borrow each other's question (stay labelled by their own option instead)", () => {
  // no per-group wrapper: the only text before the second group's options belongs to the FIRST group
  const html = `<form><div>
    <p>First question?</p><label><input type="radio" name="a" value="0">No</label><label><input type="radio" name="a" value="1">Yes</label>
    <label><input type="radio" name="b" value="0">Maybe</label><label><input type="radio" name="b" value="1">Sure</label>
  </div></form>`;
  const { document } = parseHTML(html);
  const radios = extractFieldsFromDocument(document, document.querySelector("form")).filter((f) => f.type === "radio");
  assert.equal(radios.length, 2);
  assert.notEqual(radios[1].label, "First question?");
});


// ---- Ashby save-settling (2026-10-02): the fill must wait for in-flight ApiSetFormValue writes ----------
import { EventEmitter } from "node:events";
const SAVE_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSetFormValue";
const fakeReq = (url = SAVE_URL, method = "POST") => ({ method: () => method, url: () => url });
const fakePage = (url) => Object.assign(new EventEmitter(), { url: () => url });
const settle = await (async () => {
  const out = {};
  // a non-Ashby page with no saves seen never waits
  let t0 = Date.now();
  out.otherHostFast = (await settleSaves(fakePage("https://boards.greenhouse.io/x/jobs/1"), { quietMs: 400, maxMs: 2000 })) === false && Date.now() - t0 < 200;
  // an Ashby page with nothing in flight resolves after the quiet window
  t0 = Date.now();
  out.idleAshby = (await settleSaves(fakePage("https://jobs.ashbyhq.com/x/1/application"), { quietMs: 150, maxMs: 2000 })) === true;
  // an in-flight save holds it until the request finishes (+ quiet window)
  const p = fakePage("https://jobs.ashbyhq.com/x/1/application");
  await settleSaves(p, { quietMs: 0, maxMs: 1 });          // installs the tracker, like apply.mjs does
  const r = fakeReq(); p.emit("request", r);
  t0 = Date.now();
  const waiting = settleSaves(p, { quietMs: 100, maxMs: 3000 });
  setTimeout(() => p.emit("requestfinished", r), 400);
  const ok = await waiting;
  out.heldUntilFinished = ok === true && Date.now() - t0 >= 400;
  // an unrelated request never holds it
  const q = fakePage("https://jobs.ashbyhq.com/x/1/application");
  await settleSaves(q, { quietMs: 0, maxMs: 1 });
  q.emit("request", fakeReq("https://jobs.ashbyhq.com/api/images/logo.png", "GET"));
  t0 = Date.now();
  out.unrelatedIgnored = (await settleSaves(q, { quietMs: 100, maxMs: 3000 })) === true && Date.now() - t0 < 600;
  // an Ashby form EMBEDDED in a company site (non-Ashby URL) is recognised once a save has been seen
  const e = fakePage("https://www.company.example/careers/job/1");
  await settleSaves(e, { quietMs: 0, maxMs: 1 });
  const er = fakeReq(); e.emit("request", er);
  t0 = Date.now();
  const w2 = settleSaves(e, { quietMs: 50, maxMs: 3000 });
  setTimeout(() => e.emit("requestfinished", er), 300);
  out.embeddedHeld = (await w2) === true && Date.now() - t0 >= 300;
  // a hung save cannot stall the fill forever
  const h = fakePage("https://jobs.ashbyhq.com/x/1/application");
  await settleSaves(h, { quietMs: 0, maxMs: 1 });
  h.emit("request", fakeReq());
  t0 = Date.now();
  out.hungGivesUp = (await settleSaves(h, { quietMs: 50, maxMs: 500 })) === false && Date.now() - t0 < 1500;
  return out;
})();
test("settleSaves: waits for in-flight Ashby writes (also embedded), ignores other hosts/requests, never hangs", () => {
  assert.deepEqual(settle, { otherHostFast: true, idleAshby: true, heldUntilFinished: true, unrelatedIgnored: true, embeddedHeld: true, hungGivesUp: true });
});



// ---- 2026-10-02: an asterisk anywhere in the composed label marks required ------------------
// Helsing's consent label is "...policy below * Policy can be found here": the asterisk sits
// mid-label before footnote link text, so the end-anchored check left the required consent
// unticked and Greenhouse silently refused the submit ("no success confirmation").
for (const rel of FIXTURES) {
  const html2 = fs.readFileSync(path.join(__dirname, rel), "utf8");
  const { document: doc2 } = parseHTML(html2);
  test(`${rel}: mid-label asterisk marks a field required`, () => {
    const form = doc2.querySelector("form") || doc2.body;
    const probe = doc2.createElement("div");
    probe.innerHTML = `<div><label>By entering, I consent to the processing of my data as outlined in the policy below * Policy</label>` +
      `<input type="checkbox" name="consent_probe"></div>`;
    form.appendChild(probe);
    const fields = extractFieldsFromDocument(doc2, form);
    const hit = fields.find((f) => (f.label || "").includes("consent to the processing"));
    assert.ok(hit, "probe checkbox extracted");
    assert.equal(hit.required, true);
    probe.remove();
  });
}


// ---- 2026-10-02: Headless UI listbox buttons are combobox fields -------------
// Helsing's own form renders "Which office would you be applying to?" as
// <button aria-haspopup="listbox"><span>Please select</span></button> with no
// <select>/input anywhere; the server rejects the submit with "Please select".
{
  const html = `<!doctype html><html><body><form>
    <div class="flex flex-col"><span>Which office would you be applying to? <br><span><p>Please let us know the closest office</p></span></span>
      <div class="w-full"><div class="relative"><button class="form-like-select" type="button" aria-haspopup="listbox" aria-expanded="false"><span>Please select</span></button></div></div>
    </div>
    <div class="flex flex-col"><span>Team</span><select name="team"><option value="">-</option><option>Deployed AI</option></select></div>
  </form></body></html>`;
  const { document: doc3 } = parseHTML(html);
  test("headless-ui listbox button extracted as a combobox with the block label", () => {
    const fields = extractFieldsFromDocument(doc3, doc3.querySelector("form"));
    const office = fields.find((f) => /which office/i.test(f.label || ""));
    assert.ok(office, "office field extracted");
    assert.equal(office.type, "combobox");
    assert.match(office.label, /which office would you be applying to/i);
    assert.equal(fields.filter((f) => f.type === "combobox" && /office/i.test(f.label || "")).length, 1);
    assert.ok(fields.some((f) => f.type === "select" && /team/i.test(f.label || "")), "native select still extracted");
  });
}


// ---- 2026-10-02: a ~650-character question above a bare radio pair still labels the group ----------
{
  const long = "To ensure the most effective and engaging interview experience for both you and our interviewers, we offer the option to utilise Metaview, our interview recording platform. " +
    "Should you choose to opt-in by clicking 'Yes', an audio-only recording of your interview will be saved, allowing our team to review discussions thoroughly and maintain focus. " +
    "You retain the right to opt-out of this feature at any time. Please be assured that any video feed will not be recorded or stored. Your participation is entirely voluntary and will not impact your candidacy.";
  const { document: doc4 } = parseHTML(`<!doctype html><html><body><form><div class="q">${long}
    <div><label><input type="radio" name="question_1" value="0"> No</label></div>
    <div><label><input type="radio" name="question_1" value="1"> Yes</label></div></div></form></body></html>`);
  test("a ~650-char paragraph above a radio pair labels the group (not 'No')", () => {
    const fields = extractFieldsFromDocument(doc4, doc4.querySelector("form"));
    const g = fields.find((f) => f.type === "radio");
    assert.ok(g, "radio group extracted");
    assert.match(g.label, /Metaview/);
  });
}


// ---- 2026-10-02: aria-required="false" is authoritative -------------------------------------------------
{
  const { document: doc5 } = parseHTML(`<!doctype html><html><body><form>
    <div class="field required"><label>Why do you want to join us?*</label><textarea aria-required="true" name="why"></textarea></div>
    <div class="field required"><label>Additional Information</label><textarea aria-required="false" name="extra"></textarea></div>
    <div class="field required"><label>Nickname</label><input type="text" name="nick"></div>
  </form></body></html>`);
  test("aria-required=false overrides a 'required' container; no aria attribute keeps the container heuristic", () => {
    const fields = extractFieldsFromDocument(doc5, doc5.querySelector("form"));
    const by = (re) => fields.find((f) => re.test(f.label || ""));
    assert.equal(by(/why do you want/i).required, true);
    assert.equal(by(/additional information/i).required, false);
    assert.equal(by(/nickname/i).required, true);
  });
}


test("a hidden base64 field neither hides a success message nor passes for the page (Writer, ElevenLabs 2026-10-02)", () => {
  const blob = "wr/DsE0YF8OZREIqwpLDvsKPw75Lwo".repeat(60);
  const url = "https://jobs.ashbyhq.com/x/1/application";
  const ok = detectConfirmation({ before: url, url, text: `${blob}\nYour application was successfully submitted.\n${blob}`, errors: [], visibleCaptcha: null });
  assert.equal(ok.success, true);
  assert.equal(stripBlobs(`a ${blob} b`).replace(/\s+/g, " "), "a b");
  const refused = detectConfirmation({ before: url, url, text: `${blob} We couldn't submit your application ${blob}`, errors: ["We couldn't submit your application"], visibleCaptcha: null });
  assert.equal(refused.success, false);
});


test("Workable's 'SVGs not supported by this browser.' fallback text never becomes an option or a label", () => {
  const html = `<form><fieldset><legend>Are you happy to work remotely?</legend>
    <label for="a"><input type="radio" id="a" name="remote" value="YES"><svg><text>SVGs not supported by this browser.</text></svg>YES</label>
    <label for="b"><input type="radio" id="b" name="remote" value="NO"><svg><text>SVGs not supported by this browser.</text></svg>NO</label></fieldset></form>`;
  const { document } = parseHTML(html);
  const fields = extractFieldsFromDocument(document, document.querySelector("form"));
  const radio = fields.find((f) => f.type === "radio");
  assert.ok(radio, "radio group found");
  assert.deepEqual(radio.options, ["YES", "NO"]);
  assert.ok(!/SVGs/.test(radio.label), radio.label);
});


test("Workable: role-radio options come from their aria-labelledby sibling; a custom select's aria-hidden backing input is not a text field", () => {
  const html = `<form>
    <fieldset role="radiogroup" aria-labelledby="g_label"><legend id="g_label">What basis do you have right to work in the UK?</legend>
      <label role="presentation"><div role="radio" aria-checked="false" aria-labelledby="g_label r1" tabindex="0"><input type="radio" name="CA_1" value="1" aria-hidden="true" tabindex="-1" required><svg><text>SVGs not supported by this browser.</text></svg></div><span id="r1">Graduate visa</span></label>
      <label role="presentation"><div role="radio" aria-checked="false" aria-labelledby="g_label r2" tabindex="0"><input type="radio" name="CA_1" value="2" aria-hidden="true" tabindex="-1" required><svg><text>SVGs not supported by this browser.</text></svg></div><span id="r2">British citizen</span></label>
    </fieldset>
    <div data-ui="CA_2"><span id="CA_2_label">Are you happy to work remotely?</span>
      <label><input id="input_CA_2" role="combobox" aria-labelledby="CA_2_label" readonly value="Yes" type="text"></label>
      <input name="CA_2" required aria-hidden="true" tabindex="-1" value="373696" type="text"></div>
  </form>`;
  const { document } = parseHTML(html);
  const fields = extractFieldsFromDocument(document, document.querySelector("form"));
  const radios = fields.filter((f) => f.type === "radio");
  assert.equal(radios.length, 1, JSON.stringify(radios.map((r) => r.label)));
  assert.deepEqual(radios[0].options, ["Graduate visa", "British citizen"]);
  assert.ok(!fields.some((f) => /CA[ _]\d/i.test(f.label)), fields.map((f) => f.label).join(" | "));
});


test("Recruitee: a CV input labelled 'fileInputField' is identified by its slot name", () => {
  const html = `<form><label>Full name <input type="text" name="name"></label>
    <div><input type="file" id="input-candidate.cv-9" name="candidate.cv" aria-label="fileInputField" required></div>
    <div><input type="file" id="input-candidate.coverLetterFile-15" name="candidate.coverLetterFile"></div></form>`;
  const { document } = parseHTML(html);
  const files = extractFieldsFromDocument(document, document.querySelector("form")).filter((f) => f.type === "file");
  assert.equal(files.length, 2);
  assert.ok(/cv/i.test(files[0].label) && !/fileInputField/.test(files[0].label), files[0].label);
  assert.ok(/cover/i.test(files[1].label), files[1].label);
});


test("the submit button is the bottom one, not a page-top 'Apply Now' that only scrolls (Wheely: two silent failed submits)", () => {
  const wheely = [{ text: "Apply Now", inForm: false }, { text: "Create alert", inForm: false }, { text: "Submit application", inForm: true }];
  assert.equal(pickSubmitCandidate(wheely), 2);
  assert.equal(pickSubmitCandidate([{ text: "Apply Now", inForm: false }, { text: "Apply now", inForm: false }]), 1);      // only weak ones: the last
  assert.equal(pickSubmitCandidate([{ text: "Submit", inForm: false }, { text: "Send application", inForm: true }]), 1);      // strong in the form wins
  assert.equal(pickSubmitCandidate([]), -1);
  // Recruitee (causaLens): the real button is just "Send"; "Apply" at the top only scrolls
  assert.ok(isSubmitButtonCandidate("Send") && !isSubmitButtonCandidate("Write it here instead"));
  assert.equal(pickSubmitCandidate([{ text: "Apply", inForm: false }, { text: "Send", inForm: true }]), 1);
});


test("Recruitee: a tel box beside a 'Select country calling code' button is flagged international; a plain tel box is not", () => {
  const html = `<form><div class="PhoneInput"><div><button type="button" aria-label="Select country calling code: United Kingdom">UK</button></div><input type="tel" id="p1" aria-label="Phone number"></div>
    <div><input type="tel" id="p2" aria-label="Mobile"></div></form>`;
  const { document } = parseHTML(html);
  const tels = extractFieldsFromDocument(document, document.querySelector("form")).filter((f) => f.type === "tel");
  assert.equal(tels.length, 2);
  assert.equal(tels[0].international, true);
  assert.ok(!tels[1].international);
});


test("Lever's location typeahead: the UK suggestion that starts with what was typed is chosen, never a same-named place abroad", () => {
  const riverton = ["Riverton, NS, CAN", "Riverton, VT, USA", "Riverton, Essex, England, GBR", "Riverton, Nelson Mandela Bay, Eastern Cape, ZAF"];
  assert.equal(pickLocationSuggestion(riverton, "Riverton"), 2);
  assert.equal(pickLocationSuggestion(["London, ON, CAN", "London, England, GBR"], "london"), 1);
  assert.equal(pickLocationSuggestion(["Paris, France", "Parish, LA, USA"], "Paris"), 0);     // no UK match: first that starts with it
  assert.equal(pickLocationSuggestion(["Springfield, MA, USA"], "Boston"), 0);               // nothing starts with it: the first
  assert.equal(pickLocationSuggestion([], "x"), -1);
});

test("a typeahead widget's status text is not part of the field label", () => {
  const html = `<form><div class="application-field"><label for="location-input">Current location <span>✱</span>
    <div class="dropdown-no-results">No location found. Try entering a different location</div><div>Loading</div></label>
    <input id="location-input" name="location" type="text"><input type="hidden" id="selected-location" name="selectedLocation"></div></form>`;
  const { document } = parseHTML(html);
  const f = extractFieldsFromDocument(document, document.querySelector("form")).find((x) => x.type === "text");
  assert.ok(f && /^Current location/.test(f.label) && !/No location found|Loading/.test(f.label), f && f.label);
});


test("a phone input's country selector is not a form field, a real listbox button still is", () => {
  const html = `<form><div class="x"><div class="PhoneInput"><button type="button" aria-haspopup="listbox" aria-label="Select country calling code: United Kingdom">United KingdomUnited Kingdom</button><input type="tel" aria-label="Phone number"></div></div>
    <div><label>Which office would you be applying to?</label><button type="button" aria-haspopup="listbox"><span>Please select</span></button></div></form>`;
  const { document } = parseHTML(html);
  const combos = extractFieldsFromDocument(document, document.querySelector("form")).filter((f) => f.type === "combobox");
  assert.equal(combos.length, 1);
  assert.match(combos[0].label, /Which office/);
});

test("a captcha challenge with no accepted application POST is a wall; an accepted POST is not", () => {
  const hc = { url: "https://api.hcaptcha.com/getcaptcha/abc", status: 200 };
  assert.equal(captchaBlockedSubmit([hc]), true);                                                              // Spotify
  assert.equal(captchaBlockedSubmit([hc, { url: "https://jobs.lever.co/spotify/x/apply", status: 200 }]), false); // invisible captcha passed, application sent
  assert.equal(captchaBlockedSubmit([hc, { url: "https://jobs.lever.co/x/apply", status: 400 }]), true);
  assert.equal(captchaBlockedSubmit([{ url: "https://captcha-base.recruiteecdn.com/getcaptcha/d111bc04", status: 200 }]), true);   // causaLens / Recruitee
  assert.equal(captchaBlockedSubmit([]), false);
  assert.equal(captchaBlockedSubmit(null), false);
});


test("an Ashby Yes/No question whose text is ~290 characters is still labelled (Thought Machine: 3 days in the office)", () => {
  const html = `<form><div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" data-field-path="1deecac8-7f5e-4d69-99dc-43688e066564" data-field-entry-id="2a18a545-0b9f-468c-9618-644336065e55_1deecac8-7f5e-4d69-99dc-43688e066564"><label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 ashby-application-form-question-title" for="1deecac8-7f5e-4d69-99dc-43688e066564">Thought Machine embraces a modern, hybrid work environment that balances collaborative time in the office with the flexibility of remote work. We require a minimum of 3 days a week in the office per week. Does this working model align with your expectations?&nbsp;</label><div class="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno"><button class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes">Yes</button><button class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no">No</button><input type="checkbox" tabindex="-1" name="1deecac8-7f5e-4d69-99dc-43688e066564"></div></div></form>`;
  const { document } = parseHTML(html);
  const f = extractFieldsFromDocument(document, document.querySelector("form")).find((x) => x.options && x.options.join() === "Yes,No");
  assert.ok(f, "no yes/no group extracted");
  assert.match(f.label, /Does this working model align with your expectations/);
  assert.ok(f.label.length > 200);
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) {
  for (const f of failures) console.log(`  FAIL ${f.name}: ${f.error}`);
  process.exit(1);
}
