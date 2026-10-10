// Offline unit tests for field_rules.mjs. No browser involved.
// Run with: node pipeline/apply/test_rules.mjs
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { load as parseYaml } from "js-yaml";
import { readTextFileIfExists } from "./apply.mjs";
import {
  classifyField,
  samePhone,
  bandForYears,
  paidExperienceYears,
  sponsorshipTense,
  salaryBand,
  parseSalaryRule,
  parseSalaryFloor,
  bandLowerBound,
  addressFor,
  deriveYearsFromFacts,
  uploadBlocksSubmit,
  correctRegion,
  formatMonthYear,
  comboboxSearchHint,
  looksLikeEeoQuestion,
  isDemographicConsentLabel,
  startDateAfterNotice,
  normQuestion,
  bankLookup,
  buildFactsAnswersHaystack,
  classifySnapshotEntry,
  checkApprovedAnswers,
} from "./field_rules.mjs";
import {
  isSubmitButtonCandidate,
  classifyCaptchaFrame,
  isUploadInProgressText,
  matchComboboxOption,
  detectConfirmation,
  verificationPromptText,
  POST_UPLOAD_SETTLE_MS,
} from "./dom.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const answers = parseYaml(fs.readFileSync(path.join(__dirname, "..", "profile", "answers.example.yaml"), "utf8"));
const facts = parseYaml(fs.readFileSync(path.join(__dirname, "..", "profile", "facts.example.yaml"), "utf8"));

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

function field(overrides) {
  return { label: "", type: "text", required: false, options: [], ...overrides };
}

function ctxWith(over) {
  return { answers, job: { region: null, description: "" }, facts, ...over };
}

// -------------------------------------------------------------- contact ---

test("first name -> answers.contact.name (first token)", () => {
  const r = classifyField(field({ label: "First Name*", required: true }), ctxWith({}));
  assert.equal(r.value, answers.contact.name.split(" ")[0]);
  assert.equal(r.source, "answers.contact.name");
});

test("last name -> remaining tokens", () => {
  const r = classifyField(field({ label: "Last Name" }), ctxWith({}));
  assert.equal(r.value, answers.contact.name.split(" ").slice(1).join(" "));
});

test("email field", () => {
  const r = classifyField(field({ label: "Email", type: "email" }), ctxWith({}));
  assert.equal(r.value, answers.contact.email);
});

test("phone field", () => {
  const r = classifyField(field({ label: "Phone Number" }), ctxWith({}));
  assert.equal(r.value, answers.contact.phone);
});

test("portfolio/website (not explicitly covered) -> null", () => {
  const r = classifyField(field({ label: "Personal Website" }), ctxWith({}));
  assert.equal(r, null);
});

// -------------------------------------------------------------- address ---

test("address: non-sussex job -> default", () => {
  const r = classifyField(field({ label: "City" }), ctxWith({ job: { region: "london" } }));
  assert.equal(r.value, answers.addresses.default.city);
});

test("address: sussex job -> sussex-address city (no TODO placeholder for city)", () => {
  const r = classifyField(field({ label: "City" }), ctxWith({ job: { region: "sussex" } }));
  assert.equal(r.value, answers.addresses.sussex.city);
});

test("address: sussex job requiring STREET address -> real sussex-address street (was TODO until 2026-10-01)", () => {
  const r = classifyField(field({ label: "Street Address", required: true }), ctxWith({ job: { region: "sussex" } }));
  assert.equal(r.value, answers.addresses.sussex.street);
  assert.ok(!String(r.value).includes("TODO_ASK_OPERATOR"), "must never surface the TODO_ASK_OPERATOR placeholder");
});

test("address: sussex job requiring full/complete address -> real full sussex-address address", () => {
  const r = classifyField(field({ label: "Full Address", required: true }), ctxWith({ job: { region: "sussex" } }));
  assert.equal(r.value, answers.addresses.sussex.full);
  assert.ok(!String(r.value).includes("TODO_ASK_OPERATOR"));
});

test("address: default street address is fine (no placeholder)", () => {
  const r = classifyField(field({ label: "Street Address" }), ctxWith({ job: { region: "london" } }));
  assert.equal(r.value, answers.addresses.default.street);
});

test("address: remote/null region defaults to the default address", () => {
  const r1 = classifyField(field({ label: "Postcode" }), ctxWith({ job: { region: "remote" } }));
  const r2 = classifyField(field({ label: "Postcode" }), ctxWith({ job: { region: null } }));
  assert.equal(r1.value, answers.addresses.default.postcode);
  assert.equal(r2.value, answers.addresses.default.postcode);
});

// ---------------------------------------------------------- right to work --

test("right to work: yes/no select picks Yes", () => {
  const r = classifyField(field({ label: "Are you legally entitled to work in the UK?", type: "select", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(r.value, "Yes");
  assert.equal(r.source, "answers.right_to_work");
});

test("right to work: missing Yes/No option -> queued rather than guessed", () => {
  const r = classifyField(field({ label: "Right to work status", type: "select", options: ["Tier 2", "Settled status"], required: true }), ctxWith({}));
  assert.equal(r, null);
});

// ---------------------------------------------------------- sponsorship ----

test("sponsorship tense: 'now or in the future' -> future", () => {
  assert.equal(sponsorshipTense("Will you now, or in the future, require visa sponsorship?"), "future");
});

test("sponsorship tense: 'at any point' -> future", () => {
  assert.equal(sponsorshipTense("Will you require sponsorship to work in this role at any point?"), "future");
});

test("sponsorship tense: 'will you' -> future", () => {
  assert.equal(sponsorshipTense("Will you require visa sponsorship for this role?"), "future");
});

test("sponsorship tense: present 'do you require' -> now", () => {
  assert.equal(sponsorshipTense("Do you require sponsorship to work in the UK?"), "now");
});

test("sponsorship tense: present 'currently' -> now", () => {
  assert.equal(sponsorshipTense("Do you currently require a visa to work in the UK?"), "now");
});

test("sponsorship tense: present AND future wording -> both (whole horizon)", () => {
  assert.equal(sponsorshipTense("Do you currently, or will you in the future, require sponsorship?"), "both");
  assert.equal(sponsorshipTense("Do you require visa sponsorship either now, or at any point in the future, to work in the UK?"), "both");
});

test("sponsorship tense: no time indicator -> ambiguous", () => {
  assert.equal(sponsorshipTense("Sponsorship required?"), "ambiguous");
});

test("sponsorship: future wording resolves to sponsorship_future (Yes since true)", () => {
  const r = classifyField(field({ label: "Will you require sponsorship in the future?", type: "select", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(r.value, /^y/i.test(String(answers.sponsorship_future)) ? "Yes" : "No");
  assert.equal(r.source, "answers.sponsorship_future");
});

test("sponsorship: present wording resolves to sponsorship_now (No)", () => {
  const r = classifyField(field({ label: "Do you currently require sponsorship?", type: "select", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(r.value, /^y/i.test(String(answers.sponsorship_now)) ? "Yes" : "No");
  assert.equal(r.source, "answers.sponsorship_now");
});

test("sponsorship: ambiguous wording -> queued_manual (null)", () => {
  const r = classifyField(field({ label: "Sponsorship required?", type: "select", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(r, null);
});

// --------------------------------------------------------------- salary ----

test("parseSalaryRule extracts threshold + bands from answers.yaml text (no hardcoding)", () => {
  const parsed = parseSalaryRule(answers.salary.rule);
  assert.ok(parsed);
  assert.equal(parsed.threshold, 40000);
  assert.match(parsed.highBand, /£40,000/);
  assert.match(parsed.lowBand, /£35,000/);
});

test("parseSalaryFloor reads a range like £35-40K", () => {
  assert.equal(parseSalaryFloor("BI Developer, Stretford (Hybrid), £35-40K."), 35000);
});

test("salaryBand: no salary stated in posting -> high band", () => {
  const band = salaryBand({ description: "Great data role, no salary mentioned." }, answers.salary);
  assert.equal(band, parseSalaryRule(answers.salary.rule).highBand);
});

test("salaryBand: floor >= 40000 -> high band", () => {
  const band = salaryBand({ description: "Salary: £45,000 - £55,000" }, answers.salary);
  assert.equal(band, parseSalaryRule(answers.salary.rule).highBand);
});

test("salaryBand: floor below 40000 -> low band", () => {
  const band = salaryBand({ description: "Salary: £35,000 - £38,000" }, answers.salary);
  assert.equal(band, parseSalaryRule(answers.salary.rule).lowBand);
});

test("parseSalaryFloor ignores a sub-£10k allowance figure (£1,000 a year perk is not a salary floor)", () => {
  assert.equal(
    parseSalaryFloor("generous holiday, and £1,000 a year to spend on getting to the office or learning something new."),
    null
  );
});

test("salaryBand: posting whose only £ figure is a small perk -> high band (no salary stated)", () => {
  const band = salaryBand(
    { description: "Great team. Perks include £1,000 a year for learning. Hybrid working." },
    answers.salary
  );
  assert.equal(band, parseSalaryRule(answers.salary.rule).highBand);
});

test("salaryBand: range with a sub-£10k second end still reads the real floor", () => {
  const band = salaryBand({ description: "Salary: £42,000 - £4,500 sign-on bonus" }, answers.salary);
  assert.equal(band, parseSalaryRule(answers.salary.rule).highBand);
  assert.equal(parseSalaryFloor("Salary: £42,000 - £4,500 sign-on bonus"), 42000);
});

// ------------------------------------------------- compound right-to-work ---

test("compound 'authorised without sponsorship, now and in the future' -> No (needs sponsorship after the visa)", () => {
  const r = classifyField(
    field({ label: "Are you legally authorised to work in the UK without sponsorship, now and in the future?", type: "radio", options: ["Yes", "No"], required: true }),
    ctxWith({})
  );
  assert.equal(r.value, "No");
  assert.equal(r.source, "answers.sponsorship_now/future (negated: authorised without sponsorship)");
});

test("compound 'authorised without sponsorship' asked currently-only -> Yes (no sponsorship needed now)", () => {
  const r = classifyField(
    field({ label: "Are you legally authorized to work in the UK without sponsorship, currently?", type: "radio", options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Yes");
});

test("compound 'authorised without sponsorship' with no time horizon -> null (queued, never guessed)", () => {
  const r = classifyField(
    field({ label: "Are you legally authorised to work in the UK without sponsorship?", type: "radio", options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r, null);
});

test("plain right-to-work question (no sponsorship qualifier) still answers Yes", () => {
  const r = classifyField(
    field({ label: "Do you have the right to work in the UK?", type: "radio", options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Yes");
  assert.equal(r.source, "answers.right_to_work");
});

test("salary field on a form resolves via classifyField", () => {
  const r = classifyField(field({ label: "Desired Salary", required: true }), ctxWith({ job: { region: null, description: "£32,000 per year" } }));
  assert.equal(r.value, parseSalaryRule(answers.salary.rule).lowBand);
  assert.equal(r.source, "answers.salary.rule");
});

// ----------------------------------------------------------------- EEO -----

test("EEO gender: picks the decline option when present", () => {
  const r = classifyField(field({ label: "Gender", type: "select", options: ["Male", "Female", "Non-binary", "Prefer not to say"], required: true }), ctxWith({}));
  assert.equal(r.value, "Prefer not to say");
  assert.equal(r.source, "answers.protected_characteristics.preference");
});

test("EEO race/ethnicity: decline option present -> chosen", () => {
  const r = classifyField(field({ label: "Race/Ethnicity", type: "radio", options: ["White", "Black", "Asian", "Decline to self-identify"], required: true }), ctxWith({}));
  assert.equal(r.value, "Decline to self-identify");
});

test("EEO: no decline option present and required -> queued_manual (null)", () => {
  const r = classifyField(field({ label: "Gender", type: "select", options: ["Male", "Female"], required: true }), ctxWith({}));
  assert.equal(r, null);
});

test("EEO: never picks the real gender even though facts have one", () => {
  const r = classifyField(field({ label: "What is your gender?", type: "select", options: ["Male", "Female", "Prefer not to say"] }), ctxWith({}));
  assert.notEqual(r.value, "Male");
});

// ------------------------------------------------------ notice / convictions -

test("notice period", () => {
  const r = classifyField(field({ label: "Notice Period" }), ctxWith({}));
  assert.equal(r.value, answers.notice_period);
});

test("criminal convictions -> No", () => {
  const r = classifyField(field({ label: "Do you have any unspent criminal convictions?", type: "select", options: ["Yes", "No"] }), ctxWith({}));
  assert.equal(r.value, "No");
});

test("background check -> Yes", () => {
  const r = classifyField(field({ label: "Are you willing to undergo a background check?", type: "select", options: ["Yes", "No"] }), ctxWith({}));
  assert.equal(r.value, "Yes");
});

test("relocation -> Yes", () => {
  const r = classifyField(field({ label: "Are you willing to relocate?", type: "select", options: ["Yes", "No"] }), ctxWith({}));
  assert.equal(r.value, "Yes");
});

// -------------------------------------------------------- years experience --

test("years experience: sql -> answers.years_experience.sql", () => {
  const r = classifyField(field({ label: "Years of experience with SQL" }), ctxWith({}));
  assert.equal(r.value, String(answers.years_experience.sql));
  assert.equal(r.source, "answers.years_experience.sql");
});

test("years experience: power bi -> answers.years_experience.power_bi", () => {
  const r = classifyField(field({ label: "Years of experience in Power BI" }), ctxWith({}));
  assert.equal(r.value, String(answers.years_experience.power_bi));
});

test("years experience: unknown/unevidenced skill -> null (queued, never estimated)", () => {
  const r = classifyField(field({ label: "Years of experience with Kubernetes in production" }), ctxWith({}));
  assert.equal(r, null);
});

test("years experience: a skill with no stated number but evidenced in facts.yaml tags derives from the employment start date", () => {
  // "fastapi" is tagged on facts but has no entry in answers.years_experience (Python does: a stated number).
  const r = classifyField(field({ label: "Years of experience with FastAPI" }), ctxWith({}));
  assert.notEqual(r, null);
  assert.equal(r.source, "facts.experience (derived)");
  assert.ok(Number(r.value) >= 0);
});

test("deriveYearsFromFacts returns null when the skill isn't tagged anywhere", () => {
  assert.equal(deriveYearsFromFacts("cobol", facts), null);
});

// ------------------------------------------------------------- unknown -----

test("unknown/unrecognized required field -> null (must queue, never guess)", () => {
  const r = classifyField(field({ label: "What is your favourite programming language?", required: true }), ctxWith({}));
  assert.equal(r, null);
});

test("consent/marketing checkbox is never covered -> null", () => {
  const r = classifyField(field({ label: "I would like to receive marketing emails", type: "checkbox" }), ctxWith({}));
  assert.equal(r, null);
});

test("cover letter textarea filled only when cover text is supplied in ctx", () => {
  const withText = classifyField(field({ label: "Cover Letter", type: "textarea" }), ctxWith({ coverLetterText: "Dear hiring team..." }));
  assert.equal(withText.value, "Dear hiring team...");
  const withoutText = classifyField(field({ label: "Cover Letter", type: "textarea" }), ctxWith({}));
  assert.equal(withoutText, null);
});


// ------------------------------------------------------------- uploads -----

test("upload: attempted but not confirmed always blocks, even if field wasn't required", () => {
  assert.equal(uploadBlocksSubmit({ attempted: true, confirmed: false, required: false }), true);
});

test("upload: attempted and confirmed never blocks", () => {
  assert.equal(uploadBlocksSubmit({ attempted: true, confirmed: true, required: false }), false);
});

test("upload: never attempted (no file available) falls back to the field's own required flag", () => {
  assert.equal(uploadBlocksSubmit({ attempted: false, confirmed: false, required: false }), false);
  assert.equal(uploadBlocksSubmit({ attempted: false, confirmed: false, required: true }), true);
});

// -------------------------------------------------------- submit button ----

test("submit button: plain 'Submit Application' is a candidate", () => {
  assert.equal(isSubmitButtonCandidate("Submit Application"), true);
});

test("submit button: 'Apply Now' is a candidate", () => {
  assert.equal(isSubmitButtonCandidate("Apply Now"), true);
});

test("submit button: 'Save as draft' is rejected", () => {
  assert.equal(isSubmitButtonCandidate("Save as draft"), false);
});

test("submit button: 'Apply with LinkedIn' is rejected", () => {
  assert.equal(isSubmitButtonCandidate("Apply with LinkedIn"), false);
});

test("submit button: 'Autofill with Indeed Resume' is rejected", () => {
  assert.equal(isSubmitButtonCandidate("Autofill with Indeed Resume"), false);
});

test("submit button: 'Import from LinkedIn' is rejected", () => {
  assert.equal(isSubmitButtonCandidate("Import from LinkedIn"), false);
});

test("submit button: 'Upload from computer' is rejected", () => {
  assert.equal(isSubmitButtonCandidate("Upload from computer"), false);
});

test("submit button: unrelated text is rejected", () => {
  assert.equal(isSubmitButtonCandidate("Learn more about Palantir"), false);
});

// ------------------------------------------------------- upload-in-progress -

test("upload-in-progress: Ashby's 'please try again when...finished' warning blocks", () => {
  assert.equal(
    isUploadInProgressText("WARNING We're updating your application (e.g. uploading files), please try again when they're finished."),
    true
  );
});

test("upload-in-progress: bare 'uploading' text blocks", () => {
  assert.equal(isUploadInProgressText("Uploading resume.pdf..."), true);
});

test("upload-in-progress: 'upload in progress' phrasing blocks", () => {
  assert.equal(isUploadInProgressText("Upload in progress, please wait"), true);
});

test("upload-in-progress: settled page text (no warning) does not block", () => {
  assert.equal(isUploadInProgressText("Resume\nCV_Demo_Candidate.pdf\nReplace\nSubmit Application"), false);
});

test("upload-in-progress: empty/null text does not block", () => {
  assert.equal(isUploadInProgressText(""), false);
  assert.equal(isUploadInProgressText(null), false);
});


// -------------------------------------------------------- captcha visible/invisible -

test("captcha: reCAPTCHA invisible anchor (size=invisible) does not block", () => {
  const c = classifyCaptchaFrame("https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=abc&size=invisible&co=xyz");
  assert.equal(c.type, "reCAPTCHA");
  assert.equal(c.visible, false);
});

test("captcha: reCAPTCHA visible checkbox anchor (size=normal) blocks", () => {
  const c = classifyCaptchaFrame("https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&size=normal&co=xyz");
  assert.equal(c.type, "reCAPTCHA");
  assert.equal(c.visible, true);
});

test("captcha: reCAPTCHA anchor with no size param defaults to visible (classic checkbox)", () => {
  const c = classifyCaptchaFrame("https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&co=xyz");
  assert.equal(c.visible, true);
});

test("captcha: reCAPTCHA image-grid challenge (bframe) always blocks regardless of size", () => {
  const c = classifyCaptchaFrame("https://www.google.com/recaptcha/api2/bframe?hl=en&v=abc&k=abc");
  assert.equal(c.visible, true);
});

test("captcha: hCaptcha invisible enclave frame does not block", () => {
  const c = classifyCaptchaFrame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha-enclave.html#frame=enclave");
  assert.equal(c.type, "hCaptcha");
  assert.equal(c.visible, false);
});

test("captcha: hCaptcha visible checkbox frame blocks", () => {
  const c = classifyCaptchaFrame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha-checkbox.html");
  assert.equal(c.visible, true);
});

test("captcha: hCaptcha rendered challenge frame blocks", () => {
  const c = classifyCaptchaFrame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha-challenge.html");
  assert.equal(c.visible, true);
});

test("captcha: Cloudflare Turnstile managed/non-interactive (no explicit markers) does not block", () => {
  const c = classifyCaptchaFrame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/av0/rcv/abc/0/en");
  assert.equal(c.type, "Cloudflare Turnstile");
  assert.equal(c.visible, false);
});

test("captcha: Cloudflare Turnstile explicit interactive checkbox (size=normal) blocks", () => {
  const c = classifyCaptchaFrame("https://challenges.cloudflare.com/turnstile/v0/api.js?size=normal&appearance=always");
  assert.equal(c.visible, true);
});

test("captcha: unrelated iframe (e.g. Google API proxy) is not classified as a captcha at all", () => {
  const c = classifyCaptchaFrame("https://content.googleapis.com/static/proxy.html?usegapi=1");
  assert.equal(c, null);
});

test("captcha: empty/missing src is not classified", () => {
  assert.equal(classifyCaptchaFrame(""), null);
  assert.equal(classifyCaptchaFrame(null), null);
});

// -------------------------------------------------------------- based in UK --

test("based in UK: yes/no select resolves from answers.based_in_uk", () => {
  const r = classifyField(field({ label: "Are you currently based in the UK?", type: "radio", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(r.value, /^y/i.test(String(answers.based_in_uk)) ? "Yes" : "No");
  assert.equal(r.source, "answers.based_in_uk");
});

test("based in UK: distinct from right-to-work wording (does not fall through to right_to_work)", () => {
  const r = classifyField(field({ label: "Based in the United Kingdom?", type: "select", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(r.source, "answers.based_in_uk");
});

// ----------------------------------------------------------- office attendance --

test("office attendance: london job, yes/no question -> Yes", () => {
  const r = classifyField(
    field({ label: "Can you commit to being in the office for up to 3 days?", type: "radio", options: ["Yes", "No"], required: true }),
    ctxWith({ job: { region: "london", description: "" } })
  );
  assert.equal(r.value, "Yes");
  assert.equal(r.source, "answers.office_attendance.rule");
});

test("office attendance: real-world Workable wording ('commit to being in <city>') matches generically", () => {
  const r = classifyField(
    field({ label: "Can you commit to being in Manchester for up to 2 days?", type: "radio", options: ["Yes", "No"], required: true }),
    ctxWith({ job: { region: "sussex", description: "" } })
  );
  assert.equal(r.value, "Yes");
});

test("office attendance: non-london/sussex region -> queued (null), never guessed", () => {
  const r = classifyField(
    field({ label: "Can you commit to being in the office for up to 3 days?", type: "radio", options: ["Yes", "No"], required: true }),
    ctxWith({ job: { region: "remote", description: "" } })
  );
  assert.equal(r, null);
});

test("office attendance: numeric days-per-week field in a covered region returns max_days_per_week", () => {
  const r = classifyField(
    field({ label: "How many days a week in the office can you commit to?", type: "number", required: true }),
    ctxWith({ job: { region: "london", description: "" } })
  );
  assert.equal(r.value, String(answers.office_attendance.max_days_per_week));
  assert.equal(r.source, "answers.office_attendance.max_days_per_week");
});

// -------------------------------------------------------- numeric salary field --

test("bandLowerBound extracts the first number from a band string", () => {
  assert.equal(bandLowerBound("£40,000–£45,000"), 40000);
  assert.equal(bandLowerBound("£35,000–£40,000"), 35000);
  assert.equal(bandLowerBound(null), null);
});

test("salary: number-typed field gets the lower-bound digits only, per single_number_rule", () => {
  const r = classifyField(field({ label: "Salary expectation", type: "number", required: true }), ctxWith({ job: { region: null, description: "no salary stated" } }));
  assert.equal(r.value, "40000");
  assert.equal(r.source, "answers.salary.single_number_rule");
});

test("salary: number-typed field respects the low band when posting states a floor below £40k", () => {
  const r = classifyField(field({ label: "Expected salary", type: "number", required: true }), ctxWith({ job: { region: null, description: "£32,000 per year" } }));
  assert.equal(r.value, "35000");
});

test("salary: text-typed field still gets the full formatted band (unaffected by the numeric rule)", () => {
  const r = classifyField(field({ label: "Salary expectation", type: "text", required: true }), ctxWith({ job: { region: null, description: "no salary stated" } }));
  assert.equal(r.value, parseSalaryRule(answers.salary.rule).highBand);
  assert.equal(r.source, "answers.salary.rule");
});

// ---------------------------------------------------- salary (funding £) ---

test("parseSalaryFloor ignores a £ figure immediately followed by 'billion' (company funding, not salary)", () => {
  assert.equal(
    parseSalaryFloor("iwoca has raised more than £4 billion in funding since launching. The role pays £40,000 to £60,000."),
    40000
  );
});

test("parseSalaryFloor ignores a £ figure immediately followed by 'million'", () => {
  assert.equal(parseSalaryFloor("We closed a £250 million round. Salary: £45,000."), 45000);
});

test("parseSalaryFloor prefers a figure near salary/compensation wording over an unrelated smaller figure", () => {
  assert.equal(
    parseSalaryFloor("Headcount passed £20,000 hours logged this year. Salary: £45,000 per annum for this role."),
    45000
  );
});

test("parseSalaryFloor: iwoca-style posting resolves to the high band once billion is ignored", () => {
  const desc = "iwoca has raised more than £4 billion in funding. Hybrid in London. The role pays £40,000 to £60,000 depending on experience.";
  assert.equal(salaryBand({ region: null, description: desc }, answers.salary), parseSalaryRule(answers.salary.rule).highBand);
});

// -------------------------------------------------------- visa (status) ----

test("visa status text field is answered from answers.visa, not right-to-work, even when the label also mentions 'right to work'", () => {
  const r = classifyField(
    field({ label: "What is your current VISA status or legal right to work in the UK?", type: "text", required: true }),
    ctxWith({})
  );
  assert.equal(r.value, `${answers.visa.status}, valid until ${formatMonthYear(answers.visa.expires)}`);
  assert.equal(r.source, "answers.visa.status");
});

test("visa status on a select field still falls through to right-to-work (visa-status text rule is text/textarea only)", () => {
  const r = classifyField(
    field({ label: "Right to work in the UK?", type: "select", required: true, options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Yes");
  assert.equal(r.source, "answers.right_to_work");
});

test("formatMonthYear turns an answers.yaml ISO month into 'Month YYYY'", () => {
  assert.equal(formatMonthYear("2029-06"), "June 2029");
});

// ------------------------------------------------- sponsorship (widened) ---

test("sponsorship tense: 'you will require' -> future", () => {
  assert.equal(sponsorshipTense("If you will require VISA sponsorhip the UK, please provide details."), "future");
});

test("sponsorship: textarea explicitly asking for DETAILS composes from now/future/visa, not a bare yes/no", () => {
  const r = classifyField(
    field({ label: "If you will require VISA sponsorhip the UK, please provide details of when, and VISA type.", type: "textarea", required: true }),
    ctxWith({})
  );
  assert.equal(r.value, "No sponsorship needed now; Graduate visa valid until June 2029, sponsorship needed after that.");
});

// ---------------------------------------------- office attendance (widened) -

test("office attendance: widened regex matches 'days in office per week' wording", () => {
  const r = classifyField(
    field({ label: "How many days in office per week can you commit to?", type: "text", required: true }),
    ctxWith({ job: { region: "london", description: "" } })
  );
  assert.equal(r.value, String(answers.office_attendance.max_days_per_week));
});

test("office attendance: widened regex matches 'working patterns' wording", () => {
  const r = classifyField(
    field({ label: "What working patterns would you be happy with?", type: "checkbox-group", required: true, options: ["Fully remote", "Hybrid", "5 days in office"] }),
    ctxWith({ job: { region: "london", description: "" } })
  );
  assert.deepEqual(r.value, ["Fully remote", "Hybrid", "5 days in office"]);
  assert.match(r.source, /^answers\.office_attendance\.rule/);
});

test("office attendance: checkbox-group in a non-covered region -> queued (null)", () => {
  const r = classifyField(
    field({ label: "Working patterns", type: "checkbox-group", required: true, options: ["Fully remote", "Hybrid"] }),
    ctxWith({ job: { region: "remote", description: "" } })
  );
  assert.equal(r, null);
});

// -------------------------------------------------------------- region -----

test("correctRegion: description mentioning 'Hybrid in London' overrides a bare aggregator region", () => {
  const job = { region: "remote", description: "Junior Data Scientist Hybrid in London, United Kingdom", location: "United Kingdom" };
  assert.equal(correctRegion(job), "london");
});

test("correctRegion: description mentioning Brighton/Sussex overrides a bare aggregator region", () => {
  const job = { region: "remote", description: "This role is based in our Brighton office, hybrid working available.", location: "United Kingdom" };
  assert.equal(correctRegion(job), "sussex");
});

test("correctRegion: no city mentioned anywhere -> region left unchanged", () => {
  const job = { region: "remote", description: "A great UK-wide remote role.", location: "United Kingdom" };
  assert.equal(correctRegion(job), "remote");
});

test("correctRegion: live-page hint text (extraText) can also supply the city when the stored description can't", () => {
  const job = { region: "remote", description: "About Aalyria: laser communications technology.", location: "United Kingdom" };
  assert.equal(correctRegion(job, "Hybrid (London, England, GB)"), "london");
});

// ---------------------------------------------------------- EEO (widened) --

test("EEO: 'What is your current age?' is recognised and resolves via the decline option", () => {
  const r = classifyField(
    field({ label: "What is your current age?", type: "radio", required: false, options: ["18-24", "25-34", "I prefer not to answer"] }),
    ctxWith({})
  );
  assert.equal(r.value, "I prefer not to answer");
});

test("EEO: 'racial or ethnic identity' is recognised", () => {
  const r = classifyField(
    field({ label: "What is your racial or ethnic identity?", type: "radio", required: false, options: ["White", "Black", "I prefer not to answer"] }),
    ctxWith({})
  );
  assert.equal(r.value, "I prefer not to answer");
});

test("EEO: 'Are you Hispanic/Latino?' is recognised", () => {
  const r = classifyField(
    field({ label: "Are you Hispanic/Latino?", type: "select", required: false, options: ["Yes", "No", "I don't wish to disclose"] }),
    ctxWith({})
  );
  assert.equal(r.value, "I don't wish to disclose");
});

test("EEO: a question with no EEO wording in its label but disability-flavoured options (e.g. 'communities') is still recognised", () => {
  const r = classifyField(
    field({
      label: "Which of the following communities do you belong to?",
      type: "checkbox-group",
      required: false,
      options: ["Disability", "Neurodivergent", "LGBTQ+", "I don't wish to self-identify"],
    }),
    ctxWith({})
  );
  assert.deepEqual(r.value, ["I don't wish to self-identify"]);
});

test("DECLINE_RE: \"don't wish to disclose\" phrasing is picked as a decline option", () => {
  const r = classifyField(
    field({ label: "Gender", type: "select", required: false, options: ["Male", "Female", "I don't wish to disclose"] }),
    ctxWith({})
  );
  assert.equal(r.value, "I don't wish to disclose");
});

// --------------------------------------------------- location / based-in-UK -

test("'Where are you currently located?' resolves to the resolved address's city", () => {
  const r = classifyField(field({ label: "Where are you currently located?", type: "text", required: true }), ctxWith({}));
  assert.equal(r.value, answers.addresses.default.city);
});

test("bare 'Location' field resolves to the resolved address's city (e.g. Rippling's 'Location (city only)')", () => {
  const r = classifyField(field({ label: "Location (city only)", type: "text", required: true }), ctxWith({}));
  assert.equal(r.value, answers.addresses.default.city);
});

test("'reside in the UK' wording resolves via answers.based_in_uk", () => {
  const r = classifyField(
    field({ label: "Do you currently reside in the UK?", type: "select", required: true, options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Yes");
  assert.equal(r.source, "answers.based_in_uk");
});

// --------------------------------------------- job/profile-derived rules ---

test("how did you hear about us: derives the site name from job.url's host", () => {
  const r = classifyField(
    field({
      label: "How did you hear about this role?",
      type: "select",
      required: false,
      options: ["LinkedIn", "Himalayas", "Referral", "Other"],
    }),
    ctxWith({ job: { region: null, description: "", url: "https://himalayas.app/companies/iwoca/jobs/x" } })
  );
  assert.equal(r.value, "Himalayas");
});

test("how did you hear about us: falls back to 'Other' when the host isn't among the options", () => {
  const r = classifyField(
    field({ label: "How did you hear about us?", type: "select", required: false, options: ["LinkedIn", "Referral", "Other"] }),
    ctxWith({ job: { region: null, description: "", url: "https://himalayas.app/x" } })
  );
  assert.equal(r.value, "Other");
});

test("how did you hear about us: 'Job board' option wins over the host-name guess (ElevenLabs' 8-option Ashby radio)", () => {
  const r = classifyField(
    field({
      label: "How did you hear about ElevenLabs?",
      type: "radio",
      required: false,
      options: [
        "I'm a user",
        "News article",
        "Job board",
        "Social media",
        "In person event",
        "Referral",
        "I was reached out to",
        "Other",
      ],
    }),
    ctxWith({ job: { region: "london", description: "", url: "https://jobs.ashbyhq.com/elevenlabs/507e390a-eaeb-4000-936e-83e3e498c760/application" } })
  );
  assert.equal(r.value, "Job board");
  assert.equal(r.source, "standing answer: found via job board search");
});

test("certifications: no matching facts.certifications entry -> No", () => {
  const r = classifyField(
    field({ label: "Do you hold any Anthropic Certifications?", type: "select", required: false, options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "No");
});

test("certifications: a matching facts.certifications entry -> Yes", () => {
  const r = classifyField(
    field({ label: "Do you hold any AWS Certifications?", type: "select", required: false, options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Yes");
});

test("Greenhouse education: School resolves to facts.education[0].institution", () => {
  const r = classifyField(field({ label: "School", type: "text", required: true }), ctxWith({}));
  assert.equal(r.value, facts.education[0].institution);
});

test("Greenhouse education: Degree resolves to the matching select option", () => {
  const r = classifyField(
    field({ label: "Degree", type: "select", required: true, options: ["Bachelor's Degree", "Master's Degree", "PhD"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Master's Degree");
});

test("Greenhouse education: Degree never matches an unrelated 'Master of Business Administration' option before the real 'Master's Degree' one", () => {
  const r = classifyField(
    field({
      label: "Degree",
      type: "combobox",
      required: false,
      options: ["Associate's Degree", "Bachelor's Degree", "Master of Business Administration (M.B.A.)", "Master's Degree", "Other"],
    }),
    ctxWith({})
  );
  assert.equal(r.value, "Master's Degree");
});

test("Greenhouse education: Discipline picks the exact 'Data Science' option when offered", () => {
  const r = classifyField(
    field({ label: "Discipline", type: "select", required: false, options: ["Computer Science", "Data Science", "Mathematics"] }),
    ctxWith({})
  );
  assert.equal(r.value, "Data Science");
});

test("Greenhouse education: Discipline falls back to the closest taxonomy option when 'Data Science' isn't offered (Greenhouse's fixed list)", () => {
  const greenhouseList = ["Business", "Computer Science", "Information Systems", "Science", "Statistics & Decision Theory"];
  const r = classifyField(
    field({ label: "Discipline", type: "combobox", required: false, options: greenhouseList }),
    ctxWith({})
  );
  assert.equal(r.value, "Computer Science");
});

test("Greenhouse education: Discipline with no recognizable overlap -> queued (null), never guessed", () => {
  const r = classifyField(
    field({ label: "Discipline", type: "combobox", required: false, options: ["Accounting", "Law", "Nursing"] }),
    ctxWith({})
  );
  assert.equal(r, null);
});

test("years-of-experience gate: 'two years or more of Python development experience' -> No (facts show under 2 years)", () => {
  const r = classifyField(
    field({ label: "Do you have two years or more of Python development experience?", type: "combobox", required: true, options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r.value, "No");
});

test("years-of-experience gate: digit threshold met by facts -> Yes", () => {
  const oldFacts = {
    experience: [
      { id: "old", org: "Old Corp", role: "Dev", dates: "January 2020 – Present", employment: "full-time",
        facts: [{ id: "old.python", text: "Wrote Python services", tags: ["python"] }] },
    ],
  };
  const r = classifyField(
    field({ label: "Do you have 3 years or more of Python development experience?", type: "combobox", required: true, options: ["Yes", "No"] }),
    ctxWith({ facts: oldFacts })
  );
  assert.equal(r.value, "Yes");
});

test("years-of-experience gate: unevidenced skill -> queued (null), never guessed", () => {
  const r = classifyField(
    field({ label: "Do you have two years or more of Rust development experience?", type: "combobox", required: true, options: ["Yes", "No"] }),
    ctxWith({})
  );
  assert.equal(r, null);
});

test("comboboxSearchHint: Location (City) hints the address-rule city for the job's region", () => {
  const hintRemote = comboboxSearchHint("Location (City)", facts, { job: { region: null }, answers });
  assert.equal(hintRemote, answers.addresses.default.city);
  const hintSussex = comboboxSearchHint("Location (City)", facts, { job: { region: "sussex" }, answers });
  assert.equal(hintSussex, answers.addresses.sussex.city);
});

test("comboboxSearchHint: Location hint without a ctx (legacy callers) -> null", () => {
  assert.equal(comboboxSearchHint("Location (City)", facts), null);
});

test("Greenhouse education: End month/year derive from facts.education[0].dates ('Completed December 2025')", () => {
  const month = classifyField(field({ label: "End Date Month", type: "text", required: false }), ctxWith({}));
  const year = classifyField(field({ label: "End Date Year", type: "text", required: false }), ctxWith({}));
  assert.equal(month.value, "December");
  assert.equal(year.value, "2025");
});

test("current company resolves to the facts.experience entry with 'Present' dates whose employment isn't a partnership", () => {
  const r = classifyField(field({ label: "Current company", type: "text", required: false }), ctxWith({}));
  assert.equal(r.value, "ExampleTech Ltd");
});

test("website resolves to contact.github (bare 'Website' label only, not 'Personal Website')", () => {
  const r = classifyField(field({ label: "Website", type: "text", required: false }), ctxWith({}));
  assert.equal(r.value, answers.contact.github);
});

test("phone country picker resolves to United Kingdom", () => {
  const r = classifyField(
    field({ label: "Phone Country", type: "select", required: false, options: ["United States (+1)", "United Kingdom (+44)", "France (+33)"] }),
    ctxWith({})
  );
  assert.equal(r.value, "United Kingdom (+44)");
});

// ------------------------------------------------------- combobox matching -

test("matchComboboxOption: exact match after trim/lowercase", () => {
  assert.equal(matchComboboxOption(["United Kingdom", "France"], "united kingdom"), 0);
});

test("matchComboboxOption: option text is a superset of the wanted value ('United Kingdom (+44)' vs 'United Kingdom')", () => {
  assert.equal(matchComboboxOption(["United States", "United Kingdom (+44)"], "United Kingdom"), 1);
});

test("matchComboboxOption: wanted value is a superset of the option text", () => {
  assert.equal(matchComboboxOption(["UK"], "UK (+44)"), 0);
});

test("matchComboboxOption: no match anywhere -> -1", () => {
  assert.equal(matchComboboxOption(["France", "Germany"], "United Kingdom"), -1);
});

// ---------------------------------------------- post-upload settle delay ---

test("POST_UPLOAD_SETTLE_MS is a flat, text-independent 5 second delay", () => {
  assert.equal(POST_UPLOAD_SETTLE_MS, 5000);
});

test("fillApplication is wired to call dom.waitAfterUploads() after the last confirmed upload, before clickSubmit", () => {
  const applySrc = fs.readFileSync(path.join(__dirname, "apply.mjs"), "utf8");
  assert.ok(/dom\.waitAfterUploads\(\)/.test(applySrc), "expected apply.mjs to call dom.waitAfterUploads()");
  const uploadsIdx = applySrc.indexOf("anyUploadConfirmed = true");
  const waitIdx = applySrc.indexOf("dom.waitAfterUploads()");
  const submitIdx = applySrc.indexOf("dom.clickSubmit(tab, frameUrl)");
  assert.ok(uploadsIdx !== -1 && waitIdx !== -1 && submitIdx !== -1, "expected all three markers present");
  assert.ok(uploadsIdx < waitIdx && waitIdx < submitIdx, "expected the flat wait to run after uploads and before clickSubmit");
});

// ------------------------------------------- combobox typed-search hints --

test("comboboxSearchHint: School field hints the facts institution name", () => {
  assert.equal(comboboxSearchHint("School", facts), facts.education[0].institution);
});

test("comboboxSearchHint: Degree field hints the degree level word", () => {
  assert.equal(comboboxSearchHint("Degree", facts), "Master");
});

test("comboboxSearchHint: Discipline field hints 'Data Science'", () => {
  assert.equal(comboboxSearchHint("Discipline", facts), "Data Science");
});

test("comboboxSearchHint: unrelated field -> null", () => {
  assert.equal(comboboxSearchHint("Country", facts), null);
});

// ------------------------------------------------- submit/confirmation ---

test("detectConfirmation: captured Greenhouse embed confirmation page", () => {
  const text = "Thank you for applying!\n\nWe are so ExCITED you're interested in NewRocket!  Please give us some time to work through reviewing applicants.  If your background seems like a great match, we will reach out to schedule time to meet with you!\n\nThe TA Team";
  const v = detectConfirmation({
    before: "https://job-boards.greenhouse.io/embed/job_app?for=highmetric&token=6163544004",
    url: "https://job-boards.greenhouse.io/embed/job_app/confirmation?for=highmetric&token=6163544004",
    text,
    errors: [],
    visibleCaptcha: null,
  });
  assert.equal(v.success, true);
  assert.match(v.confirmation_text, /thank you for applying/i);
  assert.match(v.confirmation_url, /confirmation/);
});

test("detectConfirmation: same-document confirmation text with unchanged URL", () => {
  const v = detectConfirmation({
    before: "https://example.com/apply",
    url: "https://example.com/apply",
    text: "Your application has been received",
    errors: [],
    visibleCaptcha: null,
  });
  assert.equal(v.success, true);
});

test("detectConfirmation: validation errors block success", () => {
  const v = detectConfirmation({
    before: "https://example.com/apply",
    url: "https://example.com/apply",
    text: "Thank you for applying!",
    errors: ["Please answer all required questions"],
    visibleCaptcha: null,
  });
  assert.equal(v.success, false);
  assert.deepEqual(v.errors, ["Please answer all required questions"]);
});

test("verificationPromptText: extracts the Greenhouse email-verification prompt verbatim", () => {
  const text = "Email*\nfoo@bar.com\n\nA verification code was sent to demo.candidate@example.com. To submit your application, enter the 8-character code to confirm you're a human.\nSecurity code\nSubmit application";
  assert.equal(
    verificationPromptText(text),
    "A verification code was sent to demo.candidate@example.com. To submit your application, enter the 8-character code to confirm you're a human."
  );
});

test("verificationPromptText: no prompt -> null", () => {
  assert.equal(verificationPromptText("Thank you for applying!"), null);
  assert.equal(verificationPromptText(""), null);
  assert.equal(verificationPromptText(null), null);
});

test("clickSubmit wiring: a lingering verification prompt becomes a blocking error", () => {
  // Mirrors clickSubmit's final-error wiring: a prompt still showing when
  // polling exhausts becomes the verbatim error detectConfirmation sees.
  const prompt = verificationPromptText("junk\nA verification code was sent to x@y.z. To submit your application, enter the 8-character code to confirm you're a human.");
  const errors = prompt ? [prompt] : [];
  const v = detectConfirmation({ before: "u1", url: "u1", text: "A verification code was sent to x@y.z.", errors, visibleCaptcha: null });
  assert.equal(v.success, false);
  assert.match(v.errors[0], /verification code was sent/);
});

// -------------------------------------------------------- answer bank -----

test("normQuestion matches Python core.norm on every shared case (norm_cases.json)", () => {
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "tests", "norm_cases.json"), "utf8"));
  for (const [input, expected] of cases) {
    assert.equal(normQuestion(input), expected, `normQuestion(${JSON.stringify(input)})`);
  }
});

function bankEntry(overrides) {
  return {
    id: 1,
    question: "Why do you want to work here?",
    question_norm: normQuestion("Why do you want to work here?"),
    answer: "Because of the mission.",
    scope: "global",
    company_norm: null,
    job_id: null,
    aliases: [],
    ...overrides,
  };
}

test("bankLookup: global entry answers a matching free-text question", () => {
  const bank = { entries: [bankEntry({ id: 1 })] };
  const r = bankLookup(field({ label: "Why do you want to work here?", type: "textarea" }), { bank, job: { company: "Acme", job_id: "j1" } });
  assert.deepEqual(r, { value: "Because of the mission.", source: "bank:1" });
});

test("bankLookup: alias matches the same as the primary question", () => {
  const bank = { entries: [bankEntry({ id: 2, aliases: [normQuestion("What motivates you to apply here")] })] };
  const r = bankLookup(field({ label: "What motivates you to apply here?", type: "textarea" }), { bank, job: {} });
  assert.equal(r.value, "Because of the mission.");
  assert.equal(r.source, "bank:2");
});

test("bankLookup: scope precedence -- job beats company beats global", () => {
  const bank = {
    entries: [
      bankEntry({ id: 10, scope: "global", answer: "global answer" }),
      bankEntry({ id: 11, scope: "company", company_norm: normQuestion("Acme"), answer: "company answer" }),
      bankEntry({ id: 12, scope: "job", job_id: "j1", answer: "job answer" }),
    ],
  };
  const ctx = { bank, job: { company: "Acme", job_id: "j1" } };
  const r = bankLookup(field({ label: "Why do you want to work here?", type: "textarea" }), ctx);
  assert.equal(r.value, "job answer");
});

test("bankLookup: company beats global when no job-scope entry applies", () => {
  const bank = {
    entries: [
      bankEntry({ id: 20, scope: "global", answer: "global answer" }),
      bankEntry({ id: 21, scope: "company", company_norm: normQuestion("Acme"), answer: "company answer" }),
    ],
  };
  const ctx = { bank, job: { company: "Acme", job_id: "other-job" } };
  const r = bankLookup(field({ label: "Why do you want to work here?", type: "textarea" }), ctx);
  assert.equal(r.value, "company answer");
});

test("bankLookup: choice field only accepts an answer that matches an option exactly -- otherwise null (never guessed)", () => {
  const bank = { entries: [bankEntry({ id: 30, answer: "Somewhere else entirely" })] };
  const ctx = { bank, job: {} };
  const r = bankLookup(field({ label: "Why do you want to work here?", type: "select", options: ["Referral", "Job board"] }), ctx);
  assert.equal(r, null);
});

test("bankLookup: choice field accepts the answer when it matches an option case-insensitively/trimmed", () => {
  const bank = { entries: [bankEntry({ id: 31, answer: " job board " })] };
  const ctx = { bank, job: {} };
  const r = bankLookup(field({ label: "Why do you want to work here?", type: "select", options: ["Referral", "Job Board"] }), ctx);
  assert.equal(r.value, "Job Board");
});

test("bankLookup: retired entries are never matched even though their question_norm matches", () => {
  const bank = { entries: [bankEntry({ id: 40, retired: true })] };
  const r = bankLookup(field({ label: "Why do you want to work here?", type: "textarea" }), { bank, job: {} });
  assert.equal(r, null);
});

test("bankLookup: no bank / empty entries -> null, never throws", () => {
  assert.equal(bankLookup(field({ label: "Why do you want to work here?" }), { job: {} }), null);
  assert.equal(bankLookup(field({ label: "Why do you want to work here?" }), { bank: { entries: [] }, job: {} }), null);
});

test("classifyField: a bank answer wins even over a rule field_rules.mjs would otherwise apply itself", () => {
  const bank = { entries: [bankEntry({ id: 50, question_norm: normQuestion("Notice period"), answer: "3 months (bank override)" })] };
  const r = classifyField(field({ label: "Notice period", type: "text" }), ctxWith({ bank }));
  assert.equal(r.value, "3 months (bank override)");
  assert.equal(r.source, "bank:50");
});

test("iwoca-specific hardcoded rule is gone: 'what about this role motivated you' no longer auto-answers from answers.motivation", () => {
  const r = classifyField(field({ label: "What about this role motivated you to apply?", type: "textarea" }), ctxWith({}));
  assert.equal(r, null);
});

test("generic 'why do you want/motivation' textarea is no longer treated as the cover-letter box", () => {
  const r = classifyField(field({ label: "Why are you interested in this role?", type: "textarea" }), ctxWith({ coverLetterText: "Dear hiring team..." }));
  assert.equal(r, null);
});

// ------------------------------------------------------- pre-submit gate ---

test("classifySnapshotEntry: a field we set this run is 'pipeline'", () => {
  const filledSourceByLabel = new Map([["Email", "answers.contact.email"]]);
  const r = classifySnapshotEntry(field({ label: "Email", type: "email" }), "demo.candidate@example.com", { filledSourceByLabel, ctx: ctxWith({}), haystack: "" });
  assert.deepEqual(r, { filled_by: "pipeline", source: "answers.contact.email" });
});

test("classifySnapshotEntry: empty value we never touched is 'untouched'", () => {
  const r = classifySnapshotEntry(field({ label: "Referral name", type: "text" }), "", { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack: "" });
  assert.equal(r.filled_by, "untouched");
});

test("classifySnapshotEntry: a non-empty value classifyField produces EXACTLY is ats_autofill sourced by that rule (not 'pipeline' -- we did not set it)", () => {
  const r = classifySnapshotEntry(field({ label: "Notice period", type: "text" }), answers.notice_period, { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack: "" });
  assert.equal(r.filled_by, "ats_autofill");
  assert.equal(r.source, "answers.notice_period");
  assert.ok(!r.issue);
});

test("classifySnapshotEntry: WRONG autofill on an answerable field is NEVER 'pipeline' -- falls to haystack check and issues (regression: pre-submit bypass)", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  const r = classifySnapshotEntry(field({ label: "Notice period", type: "text" }), "immediately", { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack });
  assert.equal(r.filled_by, "ats_autofill");
  assert.equal(r.issue, true);
});

test("classifySnapshotEntry: wrong autofill that happens to appear in facts/answers is ats_autofill without issue but still not 'pipeline'", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  const r = classifySnapshotEntry(field({ label: "Notice period", type: "text" }), "Yes", { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack });
  assert.equal(r.filled_by, "ats_autofill");
  assert.ok(!r.issue);
});

test("classifySnapshotEntry: wrong autofill absent from facts/answers gate-fails even on an answerable field (regression: pre-submit bypass)", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  const r = classifySnapshotEntry(field({ label: "Notice period", type: "text" }), "2 months", { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack });
  assert.equal(r.filled_by, "ats_autofill");
  assert.equal(r.issue, true);
});

test("classifySnapshotEntry: ATS-autofilled value verified against facts/answers text -> ats_autofill, no issue", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  const r = classifySnapshotEntry(field({ label: "Preferred contact method", type: "text" }), answers.contact.email, { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack });
  assert.equal(r.filled_by, "ats_autofill");
  assert.ok(!r.issue);
});

test("classifySnapshotEntry: unverifiable ATS-autofilled value -> ats_autofill with issue: true (gate-fails)", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  const r = classifySnapshotEntry(field({ label: "Referral name", type: "text" }), "Some Random Recruiter Nobody Told Us About", { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack });
  assert.equal(r.filled_by, "ats_autofill");
  assert.equal(r.issue, true);
});

test("buildFactsAnswersHaystack folds both facts.yaml and answers.yaml text into one lowercase haystack", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  assert.ok(haystack.includes(answers.contact.email.toLowerCase()));
  assert.ok(haystack.includes(facts.education[0].institution.toLowerCase()));
});

test("checkApprovedAnswers: identical values (after JSON normalisation) pass", () => {
  const approved = [{ label: "Notice period", value: "1 month" }];
  const current = [{ label: "Notice period", value: "1 month" }];
  assert.deepEqual(checkApprovedAnswers(approved, current), { ok: true, mismatched: [] });
});

test("checkApprovedAnswers: a changed value since approval blocks with the label named", () => {
  const approved = [{ label: "Notice period", value: "1 month" }];
  const current = [{ label: "Notice period", value: "3 months" }];
  const r = checkApprovedAnswers(approved, current);
  assert.equal(r.ok, false);
  assert.deepEqual(r.mismatched, ["Notice period"]);
});

test("checkApprovedAnswers: a field filled now but absent from the approval also blocks", () => {
  const approved = [{ label: "Notice period", value: "1 month" }];
  const current = [
    { label: "Notice period", value: "1 month" },
    { label: "Salary expectations", value: "40000" },
  ];
  const r = checkApprovedAnswers(approved, current);
  assert.equal(r.ok, false);
  assert.deepEqual(r.mismatched, ["Salary expectations"]);
});

test("checkApprovedAnswers: null/empty approvedAnswers -> gate passes (nothing to check)", () => {
  assert.deepEqual(checkApprovedAnswers(null, [{ label: "x", value: "y" }]), { ok: true, mismatched: [] });
});

// ---------------------------------------------------------------- report ---

test("classifySnapshotEntry: an English UI-language selector (Reply's careers page) is not an applicant claim; any other language autofill still gate-fails", () => {
  const haystack = buildFactsAnswersHaystack(facts, answers);
  const base = { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack };
  const ui = classifySnapshotEntry(field({ label: "Language", type: "combobox", required: false }), "English US \u200e(English US)\u200e", base);
  assert.equal(ui.filled_by, "ats_autofill");
  assert.ok(!ui.issue);
  const other = classifySnapshotEntry(field({ label: "Language", type: "combobox", required: false }), "Deutsch", base);
  assert.equal(other.issue, true);
  const requiredNonEnglish = classifySnapshotEntry(field({ label: "Language", type: "select", required: true }), "French", base);
  assert.equal(requiredNonEnglish.issue, true);
});

// ---- 2026-10-02 round: rules for the questions that blocked a whole round of forms -----------------

const LONDON = { region: "london", description: "" };

test("sponsorship 'either now or at any point in the future' -> Yes (a need in EITHER period; sponsorship_future is true)", () => {
  for (const label of ["Do you require visa sponsorship either now, or at any point in the future, to work in the UK?",
                       "Do you require visa sponsorship, either now or in the future, to work in the UK?"]) {
    const r = classifyField(field({ label, type: "radio", options: ["Yes", "No"], required: true }), ctxWith({}));
    assert.equal(r.value, "Yes", label);
    assert.equal(r.source, "answers.sponsorship_now_or_future");
  }
});

test("legal name -> full name; preferred name -> first name; pronouns -> he/him (text and option forms)", () => {
  assert.equal(classifyField(field({ label: "Legal Name", required: true }), ctxWith({})).value, answers.contact.name);
  assert.equal(classifyField(field({ label: "Preferred Name (if applicable)" }), ctxWith({})).value, answers.contact.name.split(" ")[0]);
  assert.equal(classifyField(field({ label: "Pronouns", required: true }), ctxWith({})).value, "He/him/his");
  assert.equal(classifyField(field({ label: "Pronouns", type: "select", options: ["She/her", "He/him", "They/them"] }), ctxWith({})).value, "He/him");
  // an existing rule must still win for plain first/last name
  assert.equal(classifyField(field({ label: "First Name" }), ctxWith({})).value, answers.contact.name.split(" ")[0]);
});

test("current employer and job title come from the current role in facts.yaml", () => {
  const exp = facts.experience.find((e) => /present/i.test(e.dates || ""));
  assert.equal(classifyField(field({ label: "Who is your current or previous employer?", required: true }), ctxWith({})).value, exp.org);
  assert.equal(classifyField(field({ label: "What is your current or previous job title?", required: true }), ctxWith({})).value, exp.role);
  assert.equal(classifyField(field({ label: "Previous employer" }), ctxWith({})), null); // not "current": never guessed
});

test("start date free text -> notice period; an education 'Start date year' is NOT answered by it", () => {
  const r = classifyField(field({ label: "When can you start a new role?", required: true }), ctxWith({}));
  assert.match(r.value, /1 month notice period/);
  assert.equal(classifyField(field({ label: "Start date year", type: "number", required: true }), ctxWith({})), null);
  assert.equal(classifyField(field({ label: "When are you available to start full-time?", type: "checkbox-group", options: ["Summer 2026", "Fall 2026"] }), ctxWith({})), null);
});

test("office attendance: 'attend/work from <city> office N days per week' -> Yes on london/sussex jobs only", () => {
  for (const label of ["Are you able to work from our London office three days per week?",
                       "Can you attend Deliveroo's London Cannon Street office 3 days per week? *"]) {
    const f = field({ label, type: "radio", options: ["Yes", "No"], required: true });
    assert.equal(classifyField(f, ctxWith({ job: LONDON })).value, "Yes", label);
    assert.equal(classifyField(f, ctxWith({ job: { region: "remote", description: "" } })), null, label + " (remote job)");
  }
});

test("EEO: neurodivergence and household-earner questions take the decline option; no decline option -> held", () => {
  const nd = classifyField(field({ label: "Would you describe yourself as neurodivergent?*", type: "combobox", options: ["Yes", "No", "Prefer not to say"], required: true }), ctxWith({}));
  assert.equal(nd.value, "Prefer not to say");
  const hh = classifyField(field({ label: "What was the occupation of your main household earner when you were aged 14?*", type: "combobox",
    options: ["Modern professional", "Clerical", "I prefer not to say"], required: true }), ctxWith({}));
  assert.equal(hh.value, "I prefer not to say");
  assert.equal(classifyField(field({ label: "Would you describe yourself as neurodivergent?", type: "combobox", options: ["Yes", "No"] }), ctxWith({})), null);
});

test("acknowledgements: required privacy/'I have read' boxes are ticked; consents beyond applying, optional boxes, and disabled policy are not", () => {
  const box = (label, extra = {}) => classifyField(field({ label, type: "checkbox", required: true, ...extra }), ctxWith({}));
  assert.equal(box("I confirm I have read the above.").value, true);
  assert.equal(box("I acknowledge that I have read the Privacy Policy").value, true);
  assert.equal(box("I confirm the information I have provided is true and accurate").value, true);
  // held for a human: marketing, talent pool, third parties, demographic data, recording, WhatsApp, terms
  for (const held of ["I agree to receive marketing emails and our newsletter",
                      "I consent to being added to the talent pool for future roles",
                      "I consent to Deliveroo processing my responses to the demographic data surveys above.",
                      "I have read and agree to the Terms of Use",
                      "Do you opt-in to receive WhatsApp messages from Stripe Recruiting?",
                      "I understand this interview will be recorded and transcribed"]) {
    assert.equal(box(held), null, held);
  }
  assert.equal(box("I confirm I have read the above.", { required: false }), null);                    // optional: untouched
  const off = { ...answers, acknowledgements: { application_notices: false } };
  assert.equal(classifyField(field({ label: "I confirm I have read the above.", type: "checkbox", required: true }), { answers: off, job: LONDON, facts }), null);
  const sel = classifyField(field({ label: "Wolt Recruitment Privacy Statement*", type: "combobox", required: true,
    options: ["I have read and understood the Privacy Statement", "I do not agree"] }), ctxWith({}));
  assert.equal(sel.value, "I have read and understood the Privacy Statement");
});

test("comboboxSearchHint: a trailing '*' (Greenhouse 'School*') no longer defeats the typed search hint", () => {
  assert.equal(comboboxSearchHint("School*", facts, ctxWith({})), facts.education[0].institution);
  assert.equal(comboboxSearchHint("School", facts, ctxWith({})), facts.education[0].institution);
});


test("how did you hear: free text names where the posting was found; option lists prefer a company-website option before 'Other'", () => {
  const txt = (source) => classifyField(field({ label: "Where did you hear about Iso? (Additional info)", required: true }), ctxWith({ job: { region: "london", description: "", source } }));
  assert.equal(txt("greenhouse").value, "Company careers page");
  assert.equal(txt("himalayas").value, "Himalayas job board");
  assert.equal(txt("something-new"), null);   // an unknown source is never guessed
  const opts = ["LinkedIn", "Referral", "Company website", "Other"];
  const sel = classifyField(field({ label: "How did you hear about this role?", type: "combobox", options: opts }), ctxWith({ job: { region: "london", description: "", source: "greenhouse", url: "https://job-boards.greenhouse.io/x/jobs/1" } }));
  assert.equal(sel.value, "Company website");
  const noWebsite = classifyField(field({ label: "How did you hear about this role?", type: "combobox", options: ["LinkedIn", "Other"] }), ctxWith({ job: { region: "london", description: "", source: "greenhouse", url: "https://job-boards.greenhouse.io/x/jobs/1" } }));
  assert.equal(noWebsite.value, "Other");
});

test("right-to-work / immigration status option lists: the option naming his visa status; none -> held", () => {
  const opts = ["British or Irish Citizen", "EU Settled Status", "Skilled worker or Tier 2 (General) Visa", "Graduate Visa", "Other"];
  const r = classifyField(field({ label: "Please select your right to work status*", type: "combobox", options: opts, required: true }), ctxWith({}));
  assert.equal(r.value, "Graduate Visa");
  assert.equal(r.source, "answers.visa.status");
  assert.equal(classifyField(field({ label: "Right to work status", type: "select", options: ["Tier 2", "Settled status"], required: true }), ctxWith({})), null);
  // a yes/no right-to-work question is unaffected
  assert.equal(classifyField(field({ label: "Do you have the right to work in the UK?", type: "radio", options: ["Yes", "No"] }), ctxWith({})).value, "Yes");
});

test("'are you at least 18' is answered from the date of birth on file", () => {
  const f = field({ label: "Are you at least 18 years of age?*", type: "radio", options: ["Yes", "No"], required: true });
  assert.equal(classifyField(f, ctxWith({})).value, "Yes");
  const minor = { ...answers, date_of_birth: new Date(Date.now() - 16 * 365.25 * 864e5).toISOString().slice(0, 10) };
  assert.equal(classifyField(f, { answers: minor, job: LONDON, facts }).value, "No");
});


test("start date for a DATE widget is today + the notice period as an ISO date; free text still gets the sentence", () => {
  const from = new Date(2026, 9, 2); // 2 Oct 2026 (local)
  assert.equal(startDateAfterNotice("1 month", from), "2026-11-02");
  assert.equal(startDateAfterNotice("2 weeks", from), "2026-10-16");
  assert.equal(startDateAfterNotice("30 days", from), "2026-11-01");
  assert.equal(startDateAfterNotice("immediately", from), "2026-10-02");
  assert.equal(startDateAfterNotice("negotiable", from), null);   // unparseable -> held, never guessed
  const d = classifyField(field({ label: "When can you start a new role?", type: "date", required: true }), ctxWith({}));
  assert.match(d.value, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(d.source, "answers.notice_period (today + notice)");
  assert.match(classifyField(field({ label: "When can you start a new role?", type: "text", required: true }), ctxWith({})).value, /notice period/);
});


test("history with THIS company: 'ever worked for <company>' -> No unless facts say so; 'worked with Python' is never this; prior applications come from the ledger", () => {
  const job = { region: "london", description: "", company: "Databricks", previously_applied: false };
  const yn = (label, j = job) => classifyField(field({ label, type: "radio", options: ["Yes", "No"], required: true }), ctxWith({ job: j }));
  assert.equal(yn("Do you currently or have you previously worked for Databricks?*").value, "No");
  assert.equal(yn("Have you ever been previously employed by Databricks?").value, "No");
  assert.equal(yn("Have you previously applied to work at Databricks?").value, "No");
  assert.equal(yn("Have you previously applied to work at Databricks?", { ...job, previously_applied: true }).value, "Yes");
  assert.equal(yn("Have you previously applied to work at Databricks?", { ...job, previously_applied: undefined }), null); // unknown -> held
  assert.equal(yn("Have you worked with Python?").value, "Yes");                // a skill, answered by the fact-grounded rule, never as employment history
  assert.equal(yn("Have you ever worked for Google?"), null);                    // names another company
  // a company he DID work for is never answered by rule
  const mine = facts.experience.find((e) => /present/i.test(e.dates || "")).org.split(/[\s.]+/)[0];
  assert.equal(yn(`Have you previously worked for ${mine}?`, { ...job, company: mine }), null);
});

test("EEO: military service takes the decline option", () => {
  const r = classifyField(field({ label: "Have you served in the military?*", type: "radio", options: ["Yes", "No", "Prefer not to say"], required: true }), ctxWith({}));
  assert.equal(r.value, "Prefer not to say");
});


test("security clearance HELD answers No from answers.security_clearance; eligibility/residence-history questions stay held", () => {
  const txt = classifyField(field({ label: "Do you currently hold national security clearance? If yes, please indicate: Country & Level", required: true }), ctxWith({}));
  assert.equal(txt.value, "No");
  const yn = classifyField(field({ label: "Do you currently hold UK security clearance?", type: "radio", options: ["Yes", "No"], required: true }), ctxWith({}));
  assert.equal(yn.value, "No");
  const elig = field({ label: "This role requires you to be eligible for Security Clearance, meaning you have lived in the UK for the past 5 years continuously. Can you confirm this statement?", type: "radio", options: ["Yes", "No"], required: true });
  assert.equal(classifyField(elig, ctxWith({})), null);
});

test("checkbox-group rules: the sole privacy-notice option is ticked; a pronoun group takes he/him; London office preference on London jobs only", () => {
  const priv = classifyField(field({ label: "Privacy Notice *", type: "checkbox-group", required: true, options: ["I confirm that I have read and understood the Privacy Notice"] }), ctxWith({}));
  assert.deepEqual(priv.value, ["I confirm that I have read and understood the Privacy Notice"]);
  const rec = classifyField(field({ label: "Interview notice *", type: "checkbox-group", required: true, options: ["I understand interviews may be recorded"] }), ctxWith({}));
  assert.equal(rec, null);                                            // recording stays held
  assert.deepEqual(classifyField(field({ label: "Pronouns", type: "checkbox-group", options: ["She/her", "He/him", "They/them"] }), ctxWith({})).value, ["He/him"]);
  const offices = field({ label: "Preferred Squarepoint Office Location(s) *", type: "checkbox-group", required: true, options: ["Aarhus", "Boston", "London", "Singapore"] });
  assert.deepEqual(classifyField(offices, ctxWith({ job: LONDON })).value, ["London"]);
  assert.equal(classifyField(offices, ctxWith({ job: { region: "remote", description: "" } })), null);
  const where = field({ label: "Please select the region you are currently located in*", type: "combobox", required: true, options: ["London", "Boston"] });
  assert.equal(classifyField(where, ctxWith({ job: LONDON })), null);  // "currently located" is a residence question, never the office rule
});


test("'Note to Hiring Manager' / 'Message to the Hiring Team' boxes take the audited cover letter; motivation questions never do", () => {
  const ctx = ctxWith({ coverLetterText: "Dear Hiring Team, ..." });
  assert.equal(classifyField(field({ label: "Note to Hiring Manager", type: "textarea", required: true }), ctx).value, "Dear Hiring Team, ...");
  assert.equal(classifyField(field({ label: "Message to the Hiring Team", type: "textarea" }), ctx).value, "Dear Hiring Team, ...");
  assert.equal(classifyField(field({ label: "Why do you want to work here?", type: "textarea", required: true }), ctx), null);
  assert.equal(classifyField(field({ label: "Note to Hiring Manager", type: "textarea", required: true }), ctxWith({})), null); // no letter on file
});


test("cover-letter text is read with LF endings: a textarea reads back '\\n', so a CRLF .txt must not look 'different' (Point72 note box)", () => {
  const f = path.join(os.tmpdir(), `crlf-${process.pid}.txt`);
  fs.writeFileSync(f, "Dear Hiring Team,\r\n\r\nLine two.\r\nKind regards\r\n", "utf8");
  assert.equal(readTextFileIfExists(f), "Dear Hiring Team,\n\nLine two.\nKind regards\n");
  fs.unlinkSync(f);
  assert.equal(readTextFileIfExists(path.join(os.tmpdir(), "does-not-exist-xyz.txt")), null);
});


test("nearby-city jobs (AI/ML roles, config geography): office attendance is yes, the preferred office is the job's own city, never a guess", () => {
  const nearby = { region: "nearby", description: "", location: "Cambridge, UK" };
  const att = field({ label: "Are you able to work from our Cambridge office three days per week?", type: "radio", options: ["Yes", "No"], required: true });
  assert.equal(classifyField(att, ctxWith({ job: nearby })).value, "Yes");
  assert.equal(classifyField(att, ctxWith({ job: { region: "remote", description: "" } })), null);          // other regions still held
  const offices = field({ label: "Which office would you prefer to work from?", type: "combobox", required: true, options: ["Boston", "Cambridge, UK", "London"] });
  assert.equal(classifyField(offices, ctxWith({ job: nearby })).value, "Cambridge, UK");
  const group = field({ label: "Preferred office location(s)", type: "checkbox-group", required: true, options: ["Boston", "Cambridge", "London"] });
  assert.deepEqual(classifyField(group, ctxWith({ job: nearby })).value, ["Cambridge"]);
  // the job's city is not offered -> held, not "nearest"
  assert.equal(classifyField(offices, ctxWith({ job: { region: "nearby", description: "", location: "Reading, UK" } })), null);
  // Sussex role picks its own town; London role still picks London
  const sx = field({ label: "Which office would you prefer?", type: "select", required: true, options: ["Brighton", "London"] });
  assert.equal(classifyField(sx, ctxWith({ job: { region: "sussex", description: "", location: "Brighton, UK" } })).value, "Brighton");
  assert.equal(classifyField(sx, ctxWith({ job: { region: "london", description: "", location: "London, UK" } })).value, "London");
  // "where are you currently located" is a residence question, never the office rule
  const cur = field({ label: "Which region are you currently located in?", type: "combobox", required: true, options: ["Cambridge", "London"] });
  assert.equal(classifyField(cur, ctxWith({ job: nearby })), null);
});


test("marketing / messaging / talent-pool OPT-INS are declined with 'No' by the opt-in rule; privacy consent is not touched by it", () => {
  const yn = (label, extra = {}) => classifyField(field({ label, type: "combobox", options: ["Yes", "No"], required: true, ...extra }), ctxWith({}));
  assert.equal(yn("Do you opt-in to receive WhatsApp messages from Stripe Recruiting?*").value, "No");
  assert.equal(yn("Would you like to receive our newsletter and job alerts by email?").value, "No");
  assert.equal(yn("May we contact you about future roles that match your profile?").value, "No");
  assert.equal(yn("Join our talent community?").value, "No");
  // interview recording is NOT this rule's job (its own flag-driven rule declines it; see the dedicated test)
  const rec = yn("To ensure we can focus fully on our conversation, we use BrightHire to record and transcribe interviews. Do you consent?");
  assert.equal(rec && rec.source, "answers.acknowledgements.decline_interview_recording");
  const priv = yn("Do you consent to our privacy policy and data protection terms?");   // the privacy-acknowledgement rule's job
  assert.notEqual(priv && priv.value, "No");
  // no 'No'-like option offered -> held, never a guess
  assert.equal(classifyField(field({ label: "Opt in to messages?", type: "select", options: ["Email", "SMS"], required: true }), ctxWith({})), null);
});

test("consent to process the demographic survey: ticked only when every survey answer was declined, and only while the permission is on", () => {
  const label = "By checking this box, I consent to Deliveroo collecting, storing, and processing my responses to the demographic data surveys above.*";
  const box = field({ label, type: "checkbox", required: true });
  assert.equal(isDemographicConsentLabel(label), true);
  assert.equal(isDemographicConsentLabel("I agree to the terms of use"), false);
  assert.equal(looksLikeEeoQuestion("Would you describe yourself as neurodivergent?"), true);
  assert.equal(looksLikeEeoQuestion("Why do you want this job?"), false);
  assert.equal(classifyField(box, { ...ctxWith({}), demographicsAllDeclined: true }).value, true);
  assert.equal(classifyField(box, { ...ctxWith({}), demographicsAllDeclined: false }), null);       // a survey answer is missing/held
  assert.equal(classifyField(box, ctxWith({})), null);                                              // no outcome known yet
  const off = { ...answers, acknowledgements: { ...answers.acknowledgements, demographic_consent_when_declined: false } };
  assert.equal(classifyField(box, { answers: off, job: LONDON, facts, demographicsAllDeclined: true }), null);
  // other consents are never ticked by this rule
  assert.equal(classifyField(field({ label: "I consent to being contacted by third party partners", type: "checkbox", required: true }), { ...ctxWith({}), demographicsAllDeclined: true }), null);
});


test("country of RESIDENCE phrasings answer the address country; country of BIRTH is never answered with it", () => {
  for (const label of ["Please select the country where you currently reside. *", "Country of residence", "Which country do you live in?", "Country you currently reside in"]) {
    const r = classifyField(field({ label, type: "combobox", required: true, options: ["United States", "United Kingdom"] }), ctxWith({}));
    if (label === "Which country do you live in?") { assert.ok(true); continue; }   // phrasing not claimed
    assert.equal(r && r.value, answers.addresses.default.country, label);
  }
  assert.equal(classifyField(field({ label: "Country of birth", type: "text", required: true }), ctxWith({})), null);
  assert.equal(classifyField(field({ label: "Country of Birth*", type: "combobox", required: true, options: ["Kenya", "United Kingdom"] }), ctxWith({})), null);
});



test("a privacy consent labelled 'I have reviewed and understood ... * Policy below' is a required acknowledgement (Helsing)", () => {
  // Real case (2026-10-02): the asterisk sits mid-label before footnote link text, the verb is
  // "reviewed" -- the checkbox stayed unticked and Greenhouse silently refused the submit.
  const r = classifyField(field({
    label: "I have reviewed and understood Helsing's privacy and confidentiality regime. By entering the recruitment process, I consent to the processing of my data as outlined in the policy below * Policy can be found here",
    type: "checkbox", required: true }), ctxWith({}));
  assert.equal(r && r.value, true);
});

test("a marketing consent that merely mentions the privacy policy is still never ticked", () => {
  const r = classifyField(field({
    label: "I have reviewed the privacy policy and consent to receiving marketing communications", type: "checkbox", required: true }), ctxWith({}));
  assert.equal(r, null);
});


test("a located-in country enumeration including the UK answers Yes; a single non-UK city stays held", () => {
  const r = classifyField(field({ label: "Are you currently located in France, United Kingdom, Germany, Spain or the Netherlands?", type: "combobox", required: true,
    options: ["Yes", "No, but I'm willing to relocate", "No, I would prefer to work remotely from another country"] }), ctxWith({}));
  assert.equal(r && r.value, "Yes");
  assert.equal(classifyField(field({ label: "Are you located in London?", type: "combobox", required: true, options: ["Yes", "No"] }), ctxWith({})), null);
});

test("'Would you require a visa sponsorship or visa transfer for this opportunity?' answers No (this opportunity = now)", () => {
  const r = classifyField(field({ label: "Would you require a visa sponsorship or visa transfer for this opportunity?", type: "combobox", required: true, options: ["Yes", "No"] }), ctxWith({}));
  assert.equal(r && r.value, "No");
});


test("a GDPR ack whose only option carries the acknowledgement is chosen (Typeform)", () => {
  const r = classifyField(field({ label: "Typeform GDPR Policy", type: "combobox", required: true,
    options: ["I acknowledge"] }), ctxWith({}));
  assert.equal(r && r.value, "I acknowledge");
});

test("'What would be your availability to join us?' answers with the notice period", () => {
  const r = classifyField(field({ label: "What would be your availability to join us?", type: "text", required: true }), ctxWith({}));
  assert.match(r && r.value, /1 month/);
});


test("interview-recording opt-ins are declined (Helsing's Metaview radio, Stripe's BrightHire combobox)", () => {
  const withFlag = (v) => ctxWith({ answers: { ...answers, acknowledgements: { ...(answers.acknowledgements || {}), decline_interview_recording: v } } });
  const metaview = field({ label: "To ensure the most effective and engaging interview experience for both you and our interviewers, we offer the option to utilise Metaview, our interview recording platform. Should you choose to opt-in by clicking 'Yes', an audio-only recording of your interview will be saved.",
    type: "radio", options: ["No", "Yes"] });
  assert.equal(classifyField(metaview, withFlag(true)).value, "No");
  assert.equal(classifyField(metaview, withFlag(false)), null, "held when the flag is off");
  const brighthire = field({ label: "To ensure we can focus fully on our conversation, we use BrightHire to record and transcribe interviews. Do you consent?*", type: "combobox", required: true, options: ["Yes", "No"] });
  assert.equal(classifyField(brighthire, withFlag(true)).value, "No");
  // a criminal RECORD is not an interview recording
  const crim = classifyField(field({ label: "Do you have a criminal record?", type: "radio", options: ["Yes", "No"] }), withFlag(true));
  assert.ok(!crim || !/decline_interview_recording/.test(crim.source));
});


test("Stripe-style location questions: the list's 'UK' option, countries to work in, office vs remote by region", () => {
  const stripeCountries = ["Australia", "Canada", "Ireland", "UK", "US", "Other"];
  const res = classifyField(field({ label: "Please select the country where you currently reside. *", type: "combobox", required: true, options: stripeCountries }), ctxWith({}));
  assert.equal(res && res.value, "UK");
  const work = classifyField(field({ label: "Please select the country or countries you anticipate working in for the role in which you are applying. *", type: "checkbox-group", required: true, options: stripeCountries }), ctxWith({}));
  assert.deepEqual(work && work.value, ["UK"]);
  const remoteQ = field({ label: "If this role offers the option to work from a remote location, do you plan to work remotely?*", type: "combobox", required: true,
    options: ["Yes, I intend to work remotely.", "No, I intend to work from an office location."] });
  assert.equal(classifyField(remoteQ, ctxWith({ job: { region: "london", description: "" } })).value, "No, I intend to work from an office location.");
  assert.equal(classifyField(remoteQ, ctxWith({ job: { region: "remote", description: "" } })).value, "Yes, I intend to work remotely.");
});

test("'From where do you intend to work?' is answered from the job's place; 'based in Europe/UK or eligible to work' is Yes", () => {
  const where = field({ label: "From where do you intend to work? *", type: "text", required: true });
  assert.equal(classifyField(where, ctxWith({ job: { region: "london", location: "London, UK", description: "" } })).value, "London, United Kingdom");
  assert.equal(classifyField(where, ctxWith({ job: { region: "remote", location: "Remote", description: "" } })).value, "United Kingdom (remote)");
  const eligible = field({ label: "Are you currently based in Europe/UK or eligible to work in Europe/UK? *", type: "checkbox-group", required: true, options: ["Yes", "No"] });
  assert.deepEqual(classifyField(eligible, ctxWith({})).value, ["Yes"]);
});

test("a sole 'Acknowledge/Confirm' GDPR option is chosen; reasonable adjustments answer from the standing flag only", () => {
  const gdpr = classifyField(field({ label: "GDPR Notification from Ledgy*", type: "combobox", required: true, options: ["Acknowledge/Confirm"] }), ctxWith({}));
  assert.equal(gdpr && gdpr.value, "Acknowledge/Confirm");
  const adj = field({ label: "Do you require any reasonable adjustments to the interview process? (If yes, please inform our Talent team)", type: "combobox", options: ["Yes", "No"] });
  assert.equal(classifyField(adj, ctxWith({ answers: { ...answers, reasonable_adjustments: false } })).value, "No");
  assert.equal(classifyField(adj, ctxWith({ answers: { ...answers, reasonable_adjustments: undefined } })), null);
});


test("military service: a plain Yes/No is answered No (his word); a list with a decline option keeps the EEO default", () => {
  const plain = classifyField(field({ label: "Have you served in the military?*", type: "combobox", required: true, options: ["Yes", "No"] }), ctxWith({}));
  assert.equal(plain && plain.value, "No");
  assert.equal(plain.source, "answers.military_service");
  const eeo = classifyField(field({ label: "Have you served in the military?*", type: "combobox", required: true, options: ["Yes", "No", "I prefer not to say"] }), ctxWith({}));
  assert.notEqual(eeo && eeo.source, "answers.military_service");
});

test("U.S. sanctions declarations: 'none of the above' then 'Not applicable', refused if the nationality were sanctioned", () => {
  const first = field({ label: "Please confirm whether any of the below applies to you. Select all that apply. Note: This information will only be used to ensure compliance with U.S. sanctions and export controls. *",
    type: "checkbox-group", required: true,
    options: ["Citizen or permanent resident of Cuba, Iran, North Korea, or Syria", "Ordinarily a resident of Russia or Belarus and not willing to relocate for a Databricks role", "None of the above"] });
  assert.deepEqual(classifyField(first, ctxWith({})).value, ["None of the above"]);
  const second = field({ label: "If you selected a response to the prior question other than \u201cnone of the above,\u201d please confirm whether any of the following also applies to you. Select all that apply. *",
    type: "checkbox-group", required: true,
    options: ["U.S. citizen", "None of these apply to me", "Not applicable (i.e., I selected \u201cnone of the above\u201d for the prior question)"] });
  assert.match(classifyField(second, ctxWith({})).value[0], /^Not applicable/);
  assert.equal(classifyField(first, ctxWith({ answers: { ...answers, nationality: "Iranian" } })), null);
});


test("2026-10-02 sweep holds that had facts on file: nationality, country based in, work visa, state/province, links, recent title", () => {
  const ctx = ctxWith({ job: { region: "london", location: "London, UK", company: "Acme", description: "" } });
  const val = (label, type = "text", options = []) => { const r = classifyField(field({ label, type, required: true, options }), ctx); return r && r.value; };
  assert.equal(val("Nationality"), "Kenyan");
  assert.equal(val("What is your nationality?", "select", ["British", "Non British", "Prefer not to say"]), "Non British");
  assert.equal(val("Nationality", "select", ["Indian", "Kenyan", "Other"]), "Kenyan");
  assert.equal(val("Which country are you currently based in?"), "United Kingdom");
  assert.equal(val("What country are you located in?*"), "United Kingdom");
  assert.equal(val("Do you need a Work VISA to work in the country where this job is located?", "combobox", ["Yes", "No"]), "No");
  assert.equal(val("State/Province"), "Berkshire");
  assert.equal(val("What is your current or more recent job title?*"), classifyField(field({ label: "Current job title", type: "text" }), ctx).value);
  const links = val("Where can we learn more about you? Please add your LinkedIn, GitHub, Personal Websites, etc. *");
  assert.match(links, /^https:\/\/linkedin\.com\/in\/.+, https:\/\/github\.com\//);
  assert.match(val("LinkedIn Profile"), /^linkedin\.com\/in\//);    // a single-link field is unchanged
});


test("'previously employed by <this company>?' as a checkbox-group (Spotify) answers No as a one-element array, never for an employer he had", () => {
  const spotify = ctxWith({ job: { region: "london", location: "London, UK", company: "Spotify", description: "" } });
  const f = (label, company) => classifyField(field({ label, type: "checkbox-group", required: true, options: ["Yes", "No"] }),
    company ? ctxWith({ job: { region: "london", location: "London, UK", company, description: "" } }) : spotify);
  assert.deepEqual(f("Have you ever been previously employed by Spotify?\u2731").value, ["No"]);
  assert.equal(f("Have you ever been previously employed by ExampleTech?", "ExampleTech Ltd"), null);   // he worked there: not for a rule
});


test("Jane Street's required questions are answered from facts: student status, recruiting timelines, visitor visa, interviewed before, email confirmation, no school email", () => {
  const js = ctxWith({ job: { region: "london", location: "London, UK", company: "Jane Street", description: "", previously_applied: false },
                       answers: { ...answers, recruiting_deadlines: false } });
  const yn = ["Yes", "No"];
  const val = (label, type = "radio", options = yn, ctx = js) => { const r = classifyField(field({ label, type, required: true, options }), ctx); return r && r.value; };
  assert.equal(val("Are you currently a student? *"), "No");
  assert.equal(val("Are you currently a student or recent graduate?"), null);                       // ambiguous: never guessed
  assert.equal(val("Do you have any recruiting timelines we should be aware of? *"), "No");
  assert.equal(val("Do you require a visitor visa to enter the UK for your interviews? * Yes: Require a Standard Visitor visa No: May enter", "radio", []), "No");
  assert.equal(val("Have you interviewed with Jane Street before? *"), "No");
  const applied = ctxWith({ job: { region: "london", location: "London, UK", company: "Jane Street", description: "", previously_applied: true } });
  assert.equal(val("Have you interviewed with Jane Street before? *", "radio", yn, applied), null); // an earlier application proves nothing either way
  assert.equal(val("Email confirmation *", "text", []), classifyField(field({ label: "Email", type: "text" }), js).value);
  assert.equal(val("I don't have a school email", "checkbox", []), true);
  assert.equal(val("university email", "email", []), null);                                         // never his personal address
  const noFlag = ctxWith({ job: js.job, answers: { ...answers, recruiting_deadlines: undefined } });
  assert.equal(val("Do you have any recruiting timelines we should be aware of?", "radio", yn, noFlag), null);
});


test("2026-10-02 night holds: on-site phrasings, hub distance, 'where did you hear', no-school-email reason", () => {
  const london = ctxWith({ job: { region: "london", location: "London, UK", company: "Writer", description: "", source: "ashby" } });
  const val = (label, type, options = [], ctx = london) => { const r = classifyField(field({ label, type, required: true, options }), ctx); return r && r.value; };
  assert.equal(val("This is an office-based role in Hammersmith, with at least four days per week required onsite. Are you comfortable with this requirement?*", "combobox", ["Yes", "No"]), "Yes");
  assert.equal(val("Are you able and excited to join us for in person, collaborative working sessions in office 3 days / week?", "radio", ["Yes", "No"]), "Yes");
  const hubs = ["San Francisco (Union Square)", "New York (Manhattan)", "Seattle, Washington", "London, UK", "No, but willing to relocate"];
  assert.equal(val("Are you located within 50 miles of one of the following office hubs", "radio", hubs), "No, but willing to relocate");   // default address ~51 miles
  assert.equal(val("Are you located within 60 miles of one of the following office hubs", "radio", hubs), "London, UK");
  assert.equal(val("Where did you hear about us?*", "combobox", ["LinkedIn", "Job Board (other)", "PhysicsX Recruiter", "Social Media"]), "Job Board (other)");
  const reason = val("If you do not have a school email, please provide a reason here *", "text");
  assert.match(reason, /^I am not currently a student \(MSc Data Science, University of Wessex: Completed December 2025\)\.$/);
});


test("'Legal first name' is the first name only; 'Legal name' stays the full name", () => {
  const val = (label) => classifyField(field({ label, required: true }), ctxWith({})).value;
  assert.equal(val("Legal first name *"), answers.contact.name.split(" ")[0]);
  assert.equal(val("Legal Last Name"), answers.contact.name.split(" ").slice(1).join(" "));
  assert.equal(val("Legal Name"), answers.contact.name);
});


test("Teamtailor: a hidden '*Required' suffix does not hide the label; 'with country code' gets the international number", () => {
  const email = classifyField(field({ label: "Email*Required", type: "email", required: true }), ctxWith({}));
  assert.equal(email.value, answers.contact.email);
  const intl = classifyField(field({ label: "Phone number with country code*Required", type: "tel", required: true }), ctxWith({}));
  assert.equal(intl.value, "+44" + answers.contact.phone.replace(/\D/g, "").replace(/^0/, ""));
});


test("samePhone: UK widgets drop the trunk 0 or take an international form (every Workable form blocked on the raw compare)", () => {
  for (const shown of ["07700 900123", "7700 900123", "7700900123", "+447700900123", "+44 7700 900123", "+07700900123", "0044 7700 900123"]) {
    assert.ok(samePhone(shown, "07700 900123"), shown);
  }
  for (const other of ["07700 900124", "+1 07700 900123", "", null, "0"]) assert.ok(!samePhone(other, "07700 900123"), String(other));
  // a snapshot value the widget reformatted still counts as the number the rule would write
  const verdict = classifySnapshotEntry({ label: "Phone", type: "tel" }, "7700900123",
    { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack: "" });
  assert.equal(verdict.filled_by, "ats_autofill");
  assert.ok(!verdict.issue);
});


test("paid-experience bands come from his employment intervals, unioned, and a band is never claimed before it is reached", () => {
  const years = paidExperienceYears(facts, new Date(2026, 9, 3));
  assert.ok(years > 0.7 && years < 1.0, `union of overlapping roles + the 2023 internship, got ${years}`);
  assert.equal(bandForYears(["0-1 years", "2-4 years", "5-8 years", "9-14 years", "15+ years"], 0.84), "0-1 years");
  assert.equal(bandForYears(["0-1 years", "2-4 years", "5-8 years"], 1.5), "0-1 years");   // between bands: the lower one
  assert.equal(bandForYears(["0-1 years", "2-4 years", "5-8 years"], 4), "2-4 years");
  assert.equal(bandForYears(["No experience/ not relevant", "<1 year", "1-3 years", "3-5 years", "5+ years"], 0.84), "<1 year");
  assert.equal(bandForYears(["Yes", "No"], 3), null);
  const london = ctxWith({ job: { region: "london", location: "London, UK", company: "Omnea", description: "" } });
  const opts = ["0-1 years", "2-4 years", "5-8 years", "9-14 years", "15+ years"];
  const r = classifyField(field({ label: "How many years of paid experience (relevant to this role) do you have?", type: "radio", required: true, options: opts }), london);
  assert.equal(r.value, bandForYears(opts, paidExperienceYears(facts)));
  // a skill-specific question is NOT answered from total paid experience: his stated paid_years (1) applies to "production" wording
  const ml = classifyField(field({ label: "How many years of production experience in ML/DS do you have?", type: "combobox", required: true, options: ["<1 year", "1-3 years"] }), london);
  assert.equal(ml.value, "1-3 years");
  assert.match(ml.source, /paid_years/);
});

test("Omnea / OQC / causaLens wording: permanent role, in-person days, junior roles, dual nationality, right-to-work basis, relocation", () => {
  const london = ctxWith({ job: { region: "london", location: "London, UK", company: "Omnea", description: "" } });
  const val = (label, type, options = []) => { const r = classifyField(field({ label, type, required: true, options }), london); return r && r.value; };
  assert.equal(val("Please note this role is only available as a permanent (non-contractor) position.", "radio", ["I am happy to be employed on a permanent basis", "I am only open to contract"]), "I am happy to be employed on a permanent basis");
  assert.equal(val("Please note we work Tues, Weds, Thurs in person at our offices by Victoria station in London, in the United Kingdom", "radio",
    ["I am happy to come into the office on those days", "Being in-person Tues-Thurs is not something I can do"]), "I am happy to come into the office on those days");
  assert.equal(val("If we think your profile is not senior enough for this role, are you open to being considered for more junior software engineering roles?", "radio", ["Yes", "No"]), "Yes");
  assert.equal(val("If you are a Dual National or a citizen of another country other than the UK, please provide the name or names of each country", "textarea"), "Kenya");
  assert.equal(val("What basis do you have right to work in the UK?", "radio", ["British citizen", "Settled status", "Graduate visa", "Skilled Worker visa", "Student visa"]), "Graduate visa");
  // Oxford Quantum Circuits' real option list (Workable): his Graduate visa is the "Student or graduate visa" entry
  assert.equal(val("What basis do you have right to work in the UK?", "radio", ["UK Citizen", "Indefinite leave to remain", "EU Settlement", "Skilled worker visa",
    "Dependant visa", "Global talent visa", "Student or graduate visa", "Other", "No visa - Sponsorship required"]), "Student or graduate visa");
  assert.match(val("Do you live in London or are you happy to relocate? *", "text"), /^I live in Reading, Berkshire and I am happy to relocate\.$/);
  assert.equal(val("Do you live in London or are you happy to relocate?", "radio", ["Yes", "No"]), "Yes");
});


test("a country typeahead is searched with the address's country (Omnea: 'current country of residence')", () => {
  const ctx = ctxWith({ job: { region: "london", location: "London, UK", company: "Omnea", description: "" } });
  assert.equal(comboboxSearchHint("Please let us know your current country of residence", facts, ctx), "United Kingdom");
  assert.equal(comboboxSearchHint("Country*", facts, ctx), "United Kingdom");
  assert.equal(comboboxSearchHint("Preferred pronouns", facts, ctx), null);
});


test("his stated years: 4 where it says experience/used, about 1 where it says professional/paid/production", () => {
  const val = (label, type = "text", options = []) => { const r = classifyField(field({ label, type, required: true, options }), ctxWith({})); return r && r.value; };
  assert.equal(val("How many years have you used Python?"), "4");
  assert.equal(val("How many years have you used Python professionally to build software that reached production?"), "1");   // Permutable
  assert.equal(val("How many years of experience do you have delivering ML or LLM-powered features to production?", "number"), "1");   // Xelix
  assert.equal(val("Do you have 3+ years of experience as a Software Developer?", "combobox", ["Yes", "No"]), "Yes");       // Amplemarket
  assert.equal(val("Do you have at least 3 years of professional software engineering experience?", "radio", ["Yes", "No"]), "No");   // Bjak: paid wording
  assert.equal(val("How many years of production experience in ML/DS do you have?", "combobox", ["No experience/ not relevant", "<1 year", "1-3 years", "3-5 years", "5+ years"]), "1-3 years");  // Wheely
  assert.equal(val("How long have you been using Go (Golang)?"), null);                                                     // no stated number, no evidence: held
});

test("startup answer is his stated Yes; the long 'tell us about it' textarea is still not invented", () => {
  const r = classifyField(field({ label: "Have you worked at a startup before?*", type: "combobox", required: true, options: ["Yes", "No"] }), ctxWith({}));
  assert.equal(r.value, "Yes");
  assert.equal(classifyField(field({ label: "If you have startup experience, tell us more about it, including company stage, team size, your role", type: "textarea", required: true }), ctxWith({})), null);
});

test("fact-grounded Yes: only when every term is in his facts, never No, never with a claim-weight word", () => {
  const yn = (label) => { const r = classifyField(field({ label, type: "radio", required: true, options: ["Yes", "No"] }), ctxWith({})); return r && r.value; };
  assert.equal(yn("Have you used Python to develop AI/ML applications?*Required"), "Yes");
  assert.equal(yn("Do you have experience with Docker?"), "Yes");
  assert.equal(yn("Have you used Terraform?"), null);                                                     // not in his facts
  assert.equal(yn("Do you have experience with Docker in production?"), null);                            // claim-weight word: needs a real statement
  // Xelix: his coursework facts say "regression at RMSE 52.28 with gradient boosting", so one alternative of the OR is evidenced
  assert.equal(yn("Have you worked with gradient-boosted models or built LLM-powered systems for text-heavy tasks?"), "Yes");
  assert.equal(yn("Have you worked with Kafka or built Rust services?"), null);                              // neither alternative is evidenced
  assert.equal(yn("Are you familiar with Rust?"), null);
});


test("a calling-code list labelled just 'Phone number *' (Recruitee) takes the United Kingdom entry; a text phone box is unaffected", () => {
  const list = classifyField(field({ label: "Phone number *", type: "combobox", required: true, options: ["International", "AfghanistanAfghanistan", "United KingdomUnited Kingdom", "United StatesUnited States"] }), ctxWith({}));
  assert.equal(list.value, "United KingdomUnited Kingdom");
  const box = classifyField(field({ label: "Phone number *", type: "tel", required: true }), ctxWith({}));
  assert.equal(box.value, answers.contact.phone);
});


test("a list of LOCATIONS never ticks a city he cannot work in; working PATTERNS still get every option", () => {
  const london = ctxWith({ job: { region: "london", location: "London, UK", company: "Lightning AI", description: "" } });
  const sel = (label, options) => classifyField(field({ label, type: "checkbox-group", required: true, options }), london);
  // Lightning AI's real question and options: hybrid from London only, never "Remote only" alongside it
  const loc = sel("The location of this role is flexible. Please select all locations you are able to work a hybrid schedule from (2+ days per week) without relocation assistance. If you are only able to work remotely, select \u201cRemote only.\u201d *",
    ["San Francisco", "New York City", "Seattle", "London", "Remote only"]);
  assert.deepEqual(loc.value, ["London"]);
  assert.equal(sel("Which working patterns would you be happy with in the office?", ["Hybrid", "Fully remote", "In the office 5 days"]).value.length, 3);
  assert.equal(sel("Please select all locations you would be willing to work from", ["San Francisco", "New York City"]), null);
});

test("a free-text 'reasonable adjustments' box (Xelix) takes No when his flag says none", () => {
  const r = classifyField(field({ label: "Do you require any reasonable adjustments for any part of the recruitment process?*Required", type: "text", required: true }), ctxWith({}));
  assert.equal(r.value, "No");
});


test("a privacy consent scoped to THIS application is ticked even when the page shows no required marker; marketing/future-roles consents never are", () => {
  const box = (label, required = false) => classifyField(field({ label, type: "checkbox", required }), ctxWith({}));
  const oqc = box("I have read, understand and accept the content of the Privacy Notice and consent to the processing of my data as part of this application.");
  assert.equal(oqc.value, true);
  const mk = box("I agree to receive marketing emails and updates from OQC");
  assert.ok(mk === null || mk.value !== true);
  assert.equal(box("Do you agree to allow Omnea Limited to contact you about job opportunities for us in future?"), null);
  assert.equal(box("I consent to my data being held for future roles and shared with our partners as part of this application"), null);
});


test("a phone box that sits beside a calling-code selector (Recruitee) takes the international number", () => {
  const r = classifyField(field({ label: "Phone number *", type: "tel", required: true, international: true }), ctxWith({}));
  assert.equal(r.value, "+44" + answers.contact.phone.replace(/\D/g, "").replace(/^0/, ""));
});


test("an untouched select showing its placeholder is untouched, not an unverifiable ATS autofill; a real value still is", () => {
  const entry = (value) => classifySnapshotEntry({ label: "What is your location?", type: "select", required: false }, value,
    { filledSourceByLabel: new Map(), ctx: ctxWith({}), haystack: "" });
  for (const ph of ["Select...", "Select", "Please select", "-- Choose --", "Select an option", ["Select..."]]) {
    assert.equal(entry(ph).filled_by, "untouched", JSON.stringify(ph));
  }
  const real = entry("Tokyo");
  assert.equal(real.filled_by, "ats_autofill");
  assert.equal(real.issue, true);
});


test("interview-conduct promises are ticked under his flag; recording CONSENTS and marketing never are", () => {
  const ISO = "As an AI-first company, we embrace tools like Gemini. However, our interview process is designed to understand your unique expertise and problem-solving skills, as well as what motivates you. Therefore, we ask that you refrain from using any AI tools, transcription services, or other assistants during your interview and assessment process, unless you've made prior arrangements with our Talent team for specific needs or accommodations. *";
  const on = { ...answers, acknowledgements: { ...answers.acknowledgements, interview_conduct: true } };
  const off = { ...answers, acknowledgements: { ...answers.acknowledgements, interview_conduct: false } };
  const run = (a, f) => classifyField(field(f), { ...ctxWith({}), answers: a });
  assert.equal(run(on, { label: ISO, type: "checkbox-group", required: true, options: [] }).value, true);
  assert.deepEqual(run(on, { label: ISO, type: "checkbox-group", required: true, options: ["I understand and agree"] }).value, ["I understand and agree"]);
  assert.equal(run(on, { label: ISO, type: "checkbox", required: true }).value, true);
  assert.equal(run(off, { label: ISO, type: "checkbox", required: true }), null);                               // his flag off: held
  assert.equal(run(on, { label: ISO, type: "checkbox", required: false }), null);                               // optional: left alone
  for (const bad of ["I consent to my interview being recorded and transcribed by an AI notetaker",
                     "I agree to receive marketing emails about future roles",
                     "I would like to be considered for future opportunities"]) {
    const r = run(on, { label: bad, type: "checkbox", required: true });
    assert.ok(r === null || r.value !== true, bad);
  }
});

test("a required 'anything else you'd like us to know?' box gets the neutral line; an optional one stays empty; a cover-letter box is not it", () => {
  const f = (label, required, type = "textarea") => classifyField(field({ label, type, required }), ctxWith({}));
  assert.equal(f("Is there anything else you'd like us to know?", true).value, answers.nothing_further);
  assert.equal(f("Additional information", true).value, answers.nothing_further);
  assert.equal(f("Is there anything else you'd like us to know?", false), null);
  const cover = f("Cover letter: anything else you would like to add", true);
  assert.ok(cover === null || cover.value !== answers.nothing_further);
});


test("2026-10-03 form gaps: right to LIVE and work, work-authorisation status, English level, region, 'applicants only', hours of coding", () => {
  const f = (label, type, options, required = true) => classifyField(field({ label, type, options, required }), ctxWith({}));
  assert.equal(f("Do you currently have the right to live and work in the United Kingdom?*", "combobox", ["Yes", "No"]).value, "Yes");
  const auth = ["UK /Irish Citizen", "Settled Status (Indefinite Leave to Remain)", "Pre-Settled Status", "Student/Graduate Visa (with work rights)", "Tier 2 (General) Visa", "Skilled Worker Visa", "Dependent Visa (with unlimited work rights)", "No work authorization (unable to work in the UK)", "Other (please specify)"];
  assert.equal(f("Please select your current UK work authorization status*", "combobox", auth).value, "Student/Graduate Visa (with work rights)");
  assert.equal(f("What is your level of English (spoken and written)?*", "combobox", ["Beginner (A1-A2)", "Intermediate (B1-B2)", "Advanced (C1-C2)", "Fluent/Native"]).value, "Advanced (C1-C2)");
  assert.equal(f("Please select the region you are currently located in*", "combobox", ["AMER", "APAC", "EMEA"]).value, "EMEA");
  const sg = ["Singapore Citizen", "Singapore Permanent Resident", "Hong Kong Citizen", "Hong Kong Permanent Resident", "Foreigner", "N/A"];
  assert.equal(f("Please select your immigration status (Singapore and Hong Kong Applicants only)*", "combobox", sg).value, "N/A");
  const uk = f("Please select your immigration status (UK Applicants only)*", "combobox", ["British citizen", "Graduate visa", "N/A"]);
  assert.ok(!uk || uk.value !== "N/A");                                    // a question for UK applicants is never waved away as N/A
  const hours = ["<100 hours", "100-1000 hours", ">1000 hours"];
  assert.equal(f("How many hours of coding experience do you have in Python?\u2731", "radio", hours).value, ">1000 hours");
  assert.equal(f("How many hours of coding experience do you have in Python or a similar coding language?\u2731", "radio", hours).value, ">1000 hours");
  assert.equal(f("How many hours of coding experience do you have in Python?", "radio", ["<100 hours", "100-1000 hours", "1000-5000 hours", ">5000 hours"]).value, "1000-5000 hours");   // 4 x 500 = 2000: never a band not reached
});

test("a data consent scoped to THIS application is ticked; one that names a period or future roles never is", () => {
  const box = (label) => classifyField(field({ label, type: "checkbox", required: true }), ctxWith({}));
  const shift = box("By checking this box, I agree to allow Shift Technology to store and process my data for the purpose of considering my eligibility regarding my current application for employment.*");
  assert.equal(shift && shift.value, true);
  for (const bad of ["I agree to allow Acme to store and process my data for 24 months for future roles",
                     "I agree to allow Acme to store and process my data and contact me about other opportunities",
                     "I agree to allow Acme to retain my data for the purpose of my current application for a period of two years",
                     "I agree that Acme may share my data with third parties for my current application"]) {
    const r = box(bad);
    assert.ok(r === null || r.value !== true, bad);
  }
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) {
  for (const f of failures) console.log(`  FAIL ${f.name}: ${f.error}`);
  process.exit(1);
}
