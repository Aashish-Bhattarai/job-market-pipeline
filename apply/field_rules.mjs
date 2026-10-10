// Pure logic: given a field descriptor + answers.yaml (parsed) + job.json (parsed)
// + optionally facts.yaml (parsed), decide what value (if any) belongs in the
// field. NEVER touches a browser/DOM. Fully unit-testable with `node`.
//
// Field descriptor shape (see dom.mjs for how the browser side builds these):
//   { label: string, type: 'text'|'email'|'tel'|'number'|'textarea'|'select'|'radio'
//                    |'checkbox'|'checkbox-group'|'combobox'|'file',
//     required: boolean, options?: string[] }
//
// classifyField(field, ctx) -> { value, source } | null
//   null means "do not know, must be queued" — callers must never invent a
//   fallback for a null result.


// Every posting this pipeline applies to was found via an automated
// job-board search (Greenhouse/Lever/Ashby/etc. listing pages) -- never a
// personal referral, an ad, or word of mouth. That's a fact about the
// APPLICATION CHANNEL, true for every job this worker submits, not a
// personal fact that belongs in answers.yaml/facts.yaml -- so it's a
// standing constant here rather than being read from either file or
// hardcoded per-company.
export const HOW_HEARD_JOB_BOARD_RE = /how did you (hear|find (out about|us)|learn)|where did you (hear|find out|learn)/i;
// Where the pipeline found a posting -> the truthful free-text answer to "how did you hear about us".
const HOW_HEARD_BY_SOURCE = {
  greenhouse: "Company careers page", lever: "Company careers page", ashby: "Company careers page",
  workable: "Company careers page", smartrecruiters: "Company careers page",
  himalayas: "Himalayas job board", reed: "Reed job board", gradcracker: "Gradcracker",
  hnhiring: "Hacker News 'Who is hiring'", jobsacuk: "jobs.ac.uk",
};
const COMPANY_BOARD_SOURCES = new Set(["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]);
const JOB_BOARD_OPTION_RE = /job board/i;
const DECLINE_RE = /prefer not to (say|answer|disclose)|decline to (state|answer|self.?identify|disclose)|do not (wish|want) to (disclose|answer|say)|don'?t wish to (answer|disclose|self.?identify)|not (specified|disclosed)|rather not say|choose not to (answer|disclose)/i;

// -------------------------------------------------------- answer bank -----

// `norm` = lowercase, replace every run of chars outside [a-z0-9] with one
// space, trim. MUST agree with Python's `core.norm` (shared cases in
// pipeline/tests/norm_cases.json; both test suites read it).
export function normQuestion(s) {
  return String(s == null ? "" : s)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

// Looks up a human-approved answer-bank entry for this exact question
// (or a known alias of it). ctx.bank is the parsed answer_bank.json export
// (schema: pipeline v2 contract). Precedence: job scope > company scope >
// global. Choice fields (select/radio/combobox/checkbox-group) only accept
// the bank answer if it equals an option exactly (case-insensitive,
// trimmed) -- never guessed onto an unrelated option. Retired entries are
// never in the export, but a defensive `retired` check is kept here too.
export function bankLookup(field, ctx) {
  const bank = ctx && ctx.bank;
  if (!bank || !Array.isArray(bank.entries) || !bank.entries.length) return null;
  const label = field && field.label;
  const qn = normQuestion(label);
  if (!qn) return null;
  const job = (ctx && ctx.job) || {};
  const companyNorm = job.company ? normQuestion(job.company) : null;
  const jobId = job.job_id || job.id || null;

  const matches = bank.entries.filter((e) => {
    if (!e || e.retired) return false;
    if (e.question_norm === qn) return true;
    return Array.isArray(e.aliases) && e.aliases.includes(qn);
  });
  if (!matches.length) return null;

  const jobMatch = jobId ? matches.find((e) => e.scope === "job" && e.job_id === jobId) : null;
  const companyMatch = companyNorm ? matches.find((e) => e.scope === "company" && e.company_norm === companyNorm) : null;
  const globalMatch = matches.find((e) => e.scope === "global");
  const picked = jobMatch || companyMatch || globalMatch;
  if (!picked) return null;

  let value = picked.answer;
  const isChoice = ["select", "radio", "combobox", "checkbox-group"].includes(field.type);
  if (isChoice) {
    const options = field.options || [];
    const wanted = String(value).trim().toLowerCase();
    const optMatch = options.find((o) => String(o).trim().toLowerCase() === wanted);
    if (!optMatch) return null; // never guess onto a non-matching option
    value = optMatch;
  }
  return { value, source: `bank:${picked.id}` };
}

function pickOption(options, patterns) {
  if (!options || !options.length) return null;
  for (const opt of options) {
    const o = String(opt).toLowerCase();
    for (const p of patterns) {
      if (p.test(o)) return opt;
    }
  }
  return null;
}

function pickDecline(options) {
  return pickOption(options, [DECLINE_RE]);
}

// Forms list the UK under many names ("UK" in Stripe's country list, "United Kingdom", "Great Britain", ...).
const UK_OPTION_RE = /^(united kingdom|u\.?k\.?|great britain|britain|england|gb|gbr)$/i;
function pickUkOption(options) {
  const list = options || [];
  return list.find((o) => UK_OPTION_RE.test(String(o).trim())) || list.find((o) => /united kingdom/i.test(String(o))) || null;
}

// A named place outside the UK: an option list of OFFICES/LOCATIONS must never tick one of these for him (no right to work there).
const FOREIGN_PLACE_RE = /\b(usa|united states|americas?|canada|mexico|brazil|argentina|germany|france|spain|italy|portugal|poland|netherlands|ireland|belgium|austria|switzerland|sweden|norway|denmark|finland|israel|india|singapore|australia|new zealand|japan|china|hong kong|korea|philippines|uae|dubai|nigeria|kenya|south africa|apac|latam|asia|north america|new york|nyc|san francisco|seattle|austin|boston|chicago|los angeles|toronto|vancouver|berlin|munich|paris|amsterdam|dublin|madrid|barcelona|lisbon|warsaw|zurich|sydney|melbourne|tokyo|bangalore|bengaluru|mumbai|tel aviv)\b/i;
const FOREIGN_US_RE = /\b(US|U\.S\.A?\.?)\b/;       // case-sensitive: "tell us" is not a country
const isForeignPlace = (o) => FOREIGN_PLACE_RE.test(String(o)) || FOREIGN_US_RE.test(String(o));

const SANCTIONED_RE = /\b(cuba|cuban|iran|iranian|north korea|korean \(dprk\)|syria|syrian|russia|russian|belarus|belarusian|crimea|donetsk|luhansk)\b/i;

function toBool(v) {
  if (typeof v === "boolean") return v;
  if (typeof v === "string") return /^y(es)?$/i.test(v.trim());
  return !!v;
}

function yesNoValue(field, rawValue) {
  const wantYes = toBool(rawValue);
  if (field.type === "select" || field.type === "radio" || field.type === "combobox" || field.type === "checkbox-group") {
    const yes = pickOption(field.options, [/^yes\b|^y$/i]);
    const no = pickOption(field.options, [/^no\b|^n$/i]);
    const picked = wantYes ? yes : no;
    if (!picked) return null;
    return field.type === "checkbox-group" ? [picked] : picked;   // a multi-select widget takes a one-element array
  }
  if (field.type === "checkbox") {
    // A standalone checkbox representing a single yes/no question (e.g.
    // "I have the right to work in the UK"). Only ever check it when the
    // desired answer is genuinely "yes"; never check a box to mean "no"
    // (unchecked already means no).
    return wantYes ? true : false;
  }
  // text/textarea fallback
  return wantYes ? "Yes" : "No";
}

// ---------------------------------------------------------------- salary ---

export function parseSalaryFloor(text) {
  if (!text) return null;
  // Matches "£40,000", "£40k", and ranges like "£35-40K" / "£35,000-£45,000"
  // where the trailing unit (k) applies to both sides of the range.
  const re = /£\s*([\d]{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?(?:\s*[-–to]{1,3}\s*£?\s*([\d]{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?)?/gi;
  const NEAR_SALARY_RE = /salary|compensation|remuneration|per annum|\bp\.?a\.?\b|\bpay\b/i;
  let m;
  const candidates = [];
  while ((m = re.exec(text))) {
    const matchEnd = m.index + m[0].length;
    // A currency figure immediately followed by "million"/"billion" is a
    // company funding/valuation figure (e.g. "£4 billion in funding"),
    // never a salary -- must never be picked as the floor.
    if (/^\s*(million|billion)\b/i.test(text.slice(matchEnd, matchEnd + 12))) continue;
    const secondHasK = !!m[4];
    let n1 = parseFloat(m[1].replace(/,/g, ""));
    if (m[2] || secondHasK) n1 *= 1000;
    // A figure below £10,000 is never a plausible UK annual salary floor
    // (e.g. "£1,000 a year to spend on getting to the office" perks,
    // referral bonuses, headcounts) -- skipping it leaves the posting with
    // no floor so salaryBand falls back to the "no salary stated" band.
    if (n1 < 10000) continue;
    const contextStart = Math.max(0, m.index - 40);
    const near = NEAR_SALARY_RE.test(text.slice(contextStart, matchEnd));
    candidates.push({ value: n1, near });
    if (m[3]) {
      let n2 = parseFloat(m[3].replace(/,/g, ""));
      if (m[4] || m[2]) n2 *= 1000;
      if (n2 >= 10000) candidates.push({ value: n2, near });
    }
  }
  if (!candidates.length) return null;
  // When any figure sits near explicit salary/compensation/pay wording,
  // only those are considered -- an unrelated number elsewhere in the
  // posting (funding round, headcount, revenue) must never win a job's
  // salary floor just by being numerically smaller.
  const near = candidates.filter((c) => c.near);
  const pool = near.length ? near : candidates;
  return Math.min(...pool.map((c) => c.value));
}

// Parses answers.yaml's salary.rule free text so the two bands and the
// threshold are read from the file, never hardcoded here.
export function parseSalaryRule(ruleText) {
  if (!ruleText) return null;
  const thresholdMatch = ruleText.match(/floor\s*>=\s*£([\d,]+)/i);
  const highBandMatch = ruleText.match(/>=\s*£[\d,]+,\s*quote\s*(£[\d,]+\s*[–\-]\s*£[\d,]+)/i);
  const lowBandMatch = ruleText.match(/floor below\s*£[\d,]+,\s*quote\s*(£[\d,]+\s*[–\-]\s*£[\d,]+)/i);
  if (!thresholdMatch || !highBandMatch || !lowBandMatch) return null;
  return {
    threshold: parseFloat(thresholdMatch[1].replace(/,/g, "")),
    highBand: highBandMatch[1].trim(),
    lowBand: lowBandMatch[1].trim(),
  };
}

export function salaryBand(job, salaryAnswers) {
  const parsed = parseSalaryRule(salaryAnswers && salaryAnswers.rule);
  if (!parsed) return null;
  const floor = parseSalaryFloor(job && job.description);
  if (floor === null || floor >= parsed.threshold) return parsed.highBand;
  return parsed.lowBand;
}

// Extracts the first (lower) number out of a band string like
// "£40,000–£45,000" -> 40000. Used only for number-typed fields that can't
// hold a formatted currency-range string, per answers.yaml's
// salary.single_number_rule (lower bound, digits only).
export function bandLowerBound(band) {
  if (!band) return null;
  const m = band.match(/([\d]{1,3}(?:,\d{3})*)/);
  if (!m) return null;
  return parseInt(m[1].replace(/,/g, ""), 10);
}

// --------------------------------------------------------------- address ---

export function addressFor(job, addresses) {
  if (!addresses) return null;
  const region = job && job.region;
  const addr = region === "sussex" ? addresses.sussex : addresses.default;
  if (!addr) return null;
  return addr;
}

function addressHasPlaceholder(addr, wantField) {
  const v = wantField ? addr[wantField] : addr.full;
  return typeof v === "string" && /TODO_ASK_OPERATOR/.test(v);
}

// --------------------------------------------------------------- region ---

// The aggregator (Himalayas) frequently reports only a bare country like
// "United Kingdom" even when the ATS's own posting is explicit about a
// specific hybrid city (Ashby's locationName/workplaceType, Rippling's
// workLocations, or simply the job description's own opening line). A bare
// aggregator country must never override a specific city mentioned in the
// posting's own text -- the office-attendance rule is gated on
// region === 'london' | 'sussex', so this drives a real
// answered-vs-unanswered difference. `extraText` lets callers fold in
// live-page-only signals (e.g. a scraped __NEXT_DATA__/visible-text
// location string) that never made it into the stored job.json snapshot.
const LONDON_RE = /\blondon\b/i;
const SUSSEX_RE = /\bsussex\b|\bbrighton\b|\bworthing\b|\bcrawley\b|\bhove\b/i;

export function correctRegion(job, extraText) {
  if (!job) return job;
  const text = [job.description, job.location, extraText].filter(Boolean).join(" ");
  if (LONDON_RE.test(text)) return "london";
  if (SUSSEX_RE.test(text)) return "sussex";
  return job.region;
}

// --------------------------------------------------------------- sponsor ---

// Formats an answers.yaml ISO "YYYY-MM" into "Month YYYY" for free-text
// visa/sponsorship answers (e.g. "2028-06" -> "June 2028").
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
export function formatMonthYear(iso) {
  if (!iso) return null;
  const m = String(iso).match(/^(\d{4})-(\d{2})$/);
  if (!m) return iso;
  const name = MONTH_NAMES[parseInt(m[2], 10) - 1];
  return name ? `${name} ${m[1]}` : iso;
}

const FUTURE_RE = /\bfuture\b|now or in the future|at any point|\bwill you\b|will you need|will you require|\byou will\b|\bwill\b[^.?!\n]{0,60}\brequire\b/i;
const PRESENT_RE = /do you (currently\s+)?(require|need)\b|currently require|currently need|\bcurrently\b|require sponsorship now|need sponsorship now/i;
const THIS_OPP_RE = /\bthis (opportunity|role|position)\b/i;

export function sponsorshipTense(label) {
  const future = FUTURE_RE.test(label);
  // "for this opportunity/role/position" is a now-horizon marker, but only when the label does not
  // already carry an explicit future horizon ("at any point", "now or in the future")
  const present = PRESENT_RE.test(label) || (THIS_OPP_RE.test(label) && !future);
  if (future && present) return "both";
  if (future) return "future";
  if (present) return "now";
  return "ambiguous";
}

function sponsorshipValue(field, answers) {
  const tense = sponsorshipTense(field.label);
  if (tense === "ambiguous") return null;
  // "either now or at any point in the future" covers the whole horizon: a need in EITHER period
  // makes the honest answer Yes (the same OR the right-to-work compound rule above applies).
  const boolValue = tense === "both" ? toBool(answers.sponsorship_now) || toBool(answers.sponsorship_future)
    : tense === "future" ? answers.sponsorship_future : answers.sponsorship_now;
  const val = yesNoValue(field, boolValue);
  if (val === null) return null;
  return { value: val, source: `answers.sponsorship_${tense === "both" ? "now_or_future" : tense}` };
}

// Builds a free-text answer for textarea/text fields that explicitly ask
// for sponsorship DETAILS (not a plain yes/no) -- composed from both time
// horizons plus the visa itself, e.g. "No sponsorship needed now; Graduate
// visa valid until June 2029, sponsorship needed after that."
function sponsorshipDetailsValue(answers) {
  const now = toBool(answers.sponsorship_now);
  const future = toBool(answers.sponsorship_future);
  const visa = answers.visa || {};
  const nowPart = now ? "Sponsorship needed now" : "No sponsorship needed now";
  let visaPart = visa.status || null;
  if (visaPart && visa.expires) visaPart += ` valid until ${formatMonthYear(visa.expires)}`;
  const futurePart = future ? "sponsorship needed after that" : "no sponsorship needed after that";
  const middle = visaPart ? `${visaPart}, ${futurePart}` : futurePart;
  return `${nowPart}; ${middle}.`;
}

// -------------------------------------------------------- years of exp. ---

function normalizeSkillKey(label) {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

const SKILL_ALIASES = {
  power_bi: ["power_bi", "powerbi", "power_bi_"],
  sql: ["sql"],
};

function matchesKnownSkill(label, key) {
  const norm = normalizeSkillKey(label);
  const aliases = SKILL_ALIASES[key] || [key];
  return aliases.some((a) => norm.includes(a));
}

const MONTHS = {
  jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
  jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
};
// Word-form thresholds used by the years-of-experience yes/no gate.
const WORD_NUMBERS = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
// Matches "two years or more of Python development experience" and the
// reordered "two or more years of Python development experience" phrasing.
const EXPERIENCE_GATE_RE = /(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:years?\s+or\s+more|or\s+more\s+years?)\s+(?:of\s+)?([a-z0-9 .+#/_-]+?)\s*(?:development\s+)?experience/i;

// "0-1 years" / "2-4 years" / "15+ years" / "<1 year" / "Less than 1 year" -> { lo, hi }
function parseBand(text) {
  const t = String(text).toLowerCase().replace(/\byears?\b|\byrs?\b/g, "").replace(/[\u2013\u2014]/g, "-").trim();
  let m;
  if ((m = t.match(/^(?:<|less than|under)\s*(\d+(?:\.\d+)?)/))) return { lo: 0, hi: Number(m[1]) };
  if ((m = t.match(/^(\d+(?:\.\d+)?)\s*\+/) || t.match(/^(?:>=?|\u2265|more than|over|at least)\s*(\d+(?:\.\d+)?)/))) return { lo: Number(m[1]), hi: Infinity };
  if ((m = t.match(/^(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)/))) return { lo: Number(m[1]), hi: Number(m[2]) };
  if (/^(no|none|0)\b/.test(t)) return { lo: 0, hi: 0 };
  return null;
}

// The band whose lower bound is the highest one NOT above `years` (1.5 years -> "0-1 years" when the next band starts at 2):
// a band is never claimed before it is reached.
export function bandForYears(options, years) {
  const bands = (options || []).map((o) => ({ o, b: parseBand(o) })).filter((x) => x.b);
  if (bands.length < 2) return null;
  const reached = bands.filter((x) => x.b.lo <= years && !(x.b.lo === 0 && x.b.hi === 0 && years > 0));
  if (!reached.length) return null;
  reached.sort((a, b) => b.b.lo - a.b.lo || a.b.hi - b.b.hi);
  return reached[0].o;
}

// Paid experience in years: the UNION of every facts.experience interval ("June 2026 - Present", "March 2026 - July 2026",
// "2023 (3 months)"), so overlapping part-time roles are not double counted.
export function paidExperienceYears(facts, now = new Date()) {
  if (!facts || !Array.isArray(facts.experience)) return null;
  const intervals = [];
  for (const exp of facts.experience) {
    const text = String(exp.dates || "");
    const stamps = [...text.matchAll(/([A-Za-z]{3,9})\s+(\d{4})/g)]
      .map((m) => (MONTHS[m[1].slice(0, 3).toLowerCase()] === undefined ? null : new Date(parseInt(m[2], 10), MONTHS[m[1].slice(0, 3).toLowerCase()], 1)))
      .filter(Boolean);
    let start = stamps[0] || null;
    let end = stamps.length > 1 ? stamps[stamps.length - 1] : null;
    if (start && !end && /present|current|now/i.test(text)) end = now;
    if (!start) {
      const y = text.match(/\b(20\d{2})\b/);
      const dur = text.match(/(\d+)\s+months?/i);
      if (y && dur) { start = new Date(parseInt(y[1], 10), 0, 1); end = new Date(parseInt(y[1], 10), parseInt(dur[1], 10), 1); }
    }
    if (start && end && end > start) intervals.push([start.getTime(), end.getTime()]);
  }
  if (!intervals.length) return null;
  intervals.sort((a, b) => a[0] - b[0]);
  let total = 0;
  let [curStart, curEnd] = intervals[0];
  for (const [s, e] of intervals.slice(1)) {
    if (s <= curEnd) curEnd = Math.max(curEnd, e);
    else { total += curEnd - curStart; [curStart, curEnd] = [s, e]; }
  }
  total += curEnd - curStart;
  return total / (1000 * 60 * 60 * 24 * 365.25);
}

function parseFactsStartDate(datesText) {
  if (!datesText) return null;
  const m = datesText.match(/([A-Za-z]{3,9})\s+(\d{4})/);
  if (!m) return null;
  const mon = MONTHS[m[1].slice(0, 3).toLowerCase()];
  if (mon === undefined) return null;
  return new Date(parseInt(m[2], 10), mon, 1);
}

// Looks across facts.yaml experience entries for a fact tagged with the
// given skill token; if found, derives whole years of experience from that
// experience entry's start date. Returns null if not evidenced anywhere.
export function deriveYearsFromFacts(skillLabel, facts, now = new Date()) {
  if (!facts || !Array.isArray(facts.experience)) return null;
  const norm = skillLabel.toLowerCase().replace(/[^a-z0-9]+/g, "");
  let earliest = null;
  for (const exp of facts.experience) {
    const tagHit = (exp.facts || []).some((f) =>
      (f.tags || []).some((t) => t.toLowerCase().replace(/[^a-z0-9]+/g, "").includes(norm))
    );
    if (!tagHit) continue;
    const start = parseFactsStartDate(exp.dates);
    if (start && (!earliest || start < earliest)) earliest = start;
  }
  if (!earliest) return null;
  const years = (now - earliest) / (1000 * 60 * 60 * 24 * 365.25);
  return Math.max(0, Math.floor(years));
}

// ---- stated years: answers.years_experience maps a skill to the number of years the operator has stated
// (kept consistent with facts.yaml). PAID_WORDING_RE distinguishes the professional/paid wording from the
// broader "experience/used" wording, which may carry a different number.
const STATED_SKILLS = [
  ["python", /\bpython\b/i],
  ["sql", /\bsql\b/i],
  ["power_bi", /power\s*bi/i],
  ["machine_learning", /\b(machine learning|ml|ai|a\.i\.|artificial intelligence|llms?|deep learning|data science|ml\/ds|ai\/ml)\b/i],
  ["software_development", /\b(software (development|engineering|developer|engineer)|programming|coding|back-?end|full[- ]?stack|web development)\b/i],
];
const PAID_WORDING_RE = /\b(professional(ly)?|paid|production|commercial(ly)?|industry|work(ing)? experience|employment)\b/i;
export function statedYears(field, answers) {
  const label = String(field.label || "");
  const lower = label.toLowerCase();
  if (!/\bhow many years\b|\bhow long have you\b|\byears? (of|have you)\b|\d+\s*\+?\s*years?\b|\bnumber of years\b/i.test(lower)) return null;
  if (/\byears? of age\b/.test(lower)) return null;
  const ye = (answers && answers.years_experience) || {};
  const skill = STATED_SKILLS.find(([, re]) => re.test(label));
  if (!skill) return null;
  const paid = PAID_WORDING_RE.test(label);
  const years = paid ? ye.paid_years : ye[skill[0]];
  if (typeof years !== "number") return null;
  const source = paid ? "answers.years_experience.paid_years (professional/paid/production wording)" : `answers.years_experience.${skill[0]}`;
  const opts = field.options || [];
  if (["radio", "select", "combobox"].includes(field.type) && opts.length) {
    const band = bandForYears(opts, years);
    if (band) return { value: band, source };
    const n = Number((lower.match(/(\d{1,2})\s*\+?\s*(?:years?|yrs?)/) || [])[1]);     // "Do you have 3+ years of ...?"
    if (n > 0) {
      const val = yesNoValue(field, years >= n);
      return val === null ? null : { value: val, source };
    }
    return null;
  }
  if (field.type === "checkbox" || field.type === "checkbox-group") return null;
  return { value: String(years), source };
}

// "How many hours of coding experience do you have in Python?": his stated years x answers.coding_hours_per_year (a deliberately low
// floor), then the band that number reaches -- never a band that has not been reached.
export function codingHours(field, answers) {
  const label = String(field.label || "");
  if (!/\bhow many hours\b[^?]{0,60}\b(coding|programming|python|code)\b/i.test(label)) return null;
  if (!["radio", "select", "combobox"].includes(field.type)) return null;
  const ye = (answers && answers.years_experience) || {};
  const years = /\bpython\b/i.test(label) ? ye.python : ye.software_development;
  const perYear = answers && answers.coding_hours_per_year;
  if (typeof years !== "number" || typeof perYear !== "number") return null;
  const band = bandForYears(field.options || [], years * perYear);
  return band ? { value: band, source: `answers.years_experience x answers.coding_hours_per_year (${years} x ${perYear} = ${years * perYear} h, a floor)` } : null;
}

// ---- fact-grounded Yes: "Have you used Python to develop AI/ML applications?" is answered Yes ONLY when
// every content term of the question appears in the facts on file. Never No; never when the question carries a claim-weight word
// (production, professional, scale, years, lead, expert ...) -- those need a real statement, not word coverage.
const CLAIM_WEIGHT_RE = /\b(production|professional(ly)?|commercial(ly)?|enterprise|at scale|large[- ]scale|senior|lead(ing)?|led|owned?|years?|expert|advanced|extensive|proven|certified|managed?)\b/i;
const FACT_STOP = new Set(["to", "a", "an", "the", "of", "in", "on", "for", "and", "or", "with", "any", "your", "you", "have", "has", "ever",
  "use", "used", "using", "work", "worked", "working", "build", "built", "develop", "developed", "developing", "create", "created",
  "implement", "implemented", "deploy", "deployed", "design", "designed", "write", "written", "apply", "applied", "do", "did", "are", "is", "previously"]);
const factStem = (w) => (w.length > 4 ? w.replace(/(ing|ed|es|s)$/, "") : w);
const factTokens = (text) => String(text == null ? "" : text).toLowerCase().split(/[^a-z0-9+#]+/).filter(Boolean).map(factStem);
export function factGroundedYes(field, facts) {
  if (!["radio", "select", "combobox"].includes(field.type) || !facts) return null;
  const opts = field.options || [];
  if (!opts.some((o) => /^yes\b/i.test(o)) || !opts.some((o) => /^no\b/i.test(o))) return null;
  const label = String(field.label || "").replace(/\*+\s*required\s*$/i, "").replace(/[\s*\u2731]+$/, "").trim();
  if (CLAIM_WEIGHT_RE.test(label)) return null;
  const m = label.match(/^(?:have you|do you have|did you|are you)\s+(?:ever\s+|previously\s+)?(?:used|worked (?:with|on)|built|developed|created|implemented|deployed|written|designed|experience (?:with|in|using|of)|familiar with|familiarity with|knowledge of|exposure to|hands[- ]on (?:experience )?(?:with|in))\s+(.+?)\??$/i);
  if (!m) return null;
  const known = new Set(factTokens(buildFactsAnswersHaystack(facts, {})));
  const covered = (alt) => {
    const toks = factTokens(alt).filter((t) => !FACT_STOP.has(t));
    return toks.length > 0 && toks.every((t) => known.has(t));
  };
  return m[1].split(/\s+or\s+/i).some(covered) ? yesNoValue(field, true) : null;
}

function yearsExperienceValue(field, answers, facts) {
  for (const key of Object.keys(SKILL_ALIASES)) {
    if (matchesKnownSkill(field.label, key)) {
      const val = answers.years_experience && answers.years_experience[key];
      if (val === undefined || val === null) return null;
      return { value: String(val), source: `answers.years_experience.${key}` };
    }
  }
  // Yes/no gate variant: "Do you have two years or more of Python
  // development experience?" -- compare the threshold in the question
  // against years derived from facts employment start dates. When the
  // skill isn't evidenced anywhere in facts, queue rather than guess.
  const gate = field.label.match(EXPERIENCE_GATE_RE);
  if (gate) {
    const nRaw = gate[1].toLowerCase();
    const threshold = WORD_NUMBERS[nRaw] !== undefined ? WORD_NUMBERS[nRaw] : parseInt(nRaw, 10);
    const skill = gate[2].trim();
    if (!(threshold > 0) || !skill || !facts) return null;
    const years = deriveYearsFromFacts(skill, facts);
    if (years === null) return null;
    const val = yesNoValue(field, years >= threshold);
    if (val === null) return null;
    return { value: val, source: "facts.experience (derived years vs question threshold)" };
  }
  // Unknown skill: try to derive from facts.yaml employment dates only if
  // evidenced by a fact tag; otherwise this must be queued, never guessed.
  const m = field.label.match(/years?\s+of\s+experience\s*(?:with|in|using)?\s*([a-z0-9 .+#/_-]+)/i);
  const skillGuess = m ? m[1].trim() : null;
  if (!skillGuess || !facts) return null;
  const years = deriveYearsFromFacts(skillGuess, facts);
  if (years === null) return null;
  return { value: String(years), source: "facts.experience (derived)" };
}

// --------------------------------------------------------------- EEO ------

const EEO_RE = /\bgender\b|\bsex\b(?!ual orientation)|\brace\b|\bracial\b|\bethnicity\b|\bethnic\b|\bage\b|hispanic|latino|\bdisabilit(y|ies)\b|\bveteran\b|sexual orientation|gender identity|transgender|neurodivergen\w*|neurodiverse|household earner|socio-?economic|\breligio\w*|\bbelief\b|marital status|civil partnership|caring responsibilit\w*|first[- ]generation|free school meals|\bmilitary\b|armed forces/i;

// Opt-in questions that are declined by default (see the rule in classifyField).
const OPTIN_RE = /\bopt[- ]?in\b|\bopt[- ]?out\b|receive\b[^?.]{0,60}\b(messages?|e-?mails?|sms|text messages?|whatsapp|updates?|newsletters?|marketing|communications?|job alerts?)\b|(contact|notify|keep|add) (me|you)\b[^?.]{0,60}\b(future|other|similar|new)\b[^?.]{0,30}\b(roles?|opportunit\w*|vacanc\w*|jobs?)\b|talent (pool|community|network)/i;
const OPTIN_EXCLUDE_RE = /record|transcri|interview (conduct|process)|\bai (tool|assistant|notetaker)|demographic|diversity|privacy|data protection|terms|background check/i;
const DEMOGRAPHIC_CONSENT_RE = /\b(consent|agree)\b[^.?!]{0,90}\b(demographic|diversity|equal opportunit|monitoring)\b[^.?!]{0,70}\b(survey|surveys|questionnaire|responses|data|information)\b|\bdemographic\b[^.?!]{0,60}\bconsent\b/i;

export function isDemographicConsentLabel(label) {
  return DEMOGRAPHIC_CONSENT_RE.test(String(label || "").toLowerCase().replace(/[\s*✱]+$/, "").trim());
}

// Acknowledgements that are part of applying at all (see the rule in classifyField).
const ACK_RE = /\bprivacy\b|\backnowledge\b|\bi (have )?(read|acknowledge|understand|reviewed)\b|\bi confirm\b[^.?!]{0,60}\b(read|true|accurate|correct|complete)\b|\b(information|details|answers)\b[^.?!]{0,50}\b(true|accurate|correct)\b/i;
const ACK_EXCLUDE_RE = /marketing|newsletter|talent (pool|community|network)|future (roles|opportunit|vacanc)|other (roles|opportunit)|whatsapp|\bsms\b|text messages?|third[- ]part|share (my|your) (data|information|details)|sensitive|special categor|demographic|diversity|equal opportunit|monitoring|record(ed|ing)?\b|transcri|background (check|screen)|credit check|will not use|promise|\bagree to (be )?bound|terms (of|and) (use|service|employment)|non-?compete|contract|graduation|visa|sponsor|relocat/i;

// A consent explicitly scoped to THIS application ("...consent to the processing of my data as part of this application", OQC/Workable):
// the site enforces it although the extractor saw no required marker ("Please accept the Privacy Notice to continue").
const APPLICATION_SCOPED_RE = /\b(as part of|for the purposes? of|in connection with|in relation to|regarding|for)\b[^.?!]{0,60}\b(this|my|the|your)(?: (?:current|job|role|recruitment))* (application|recruitment process|hiring process)\b/i;
// "I agree to allow X to store and process my data for the purpose of considering my eligibility regarding my current application"
// (Shift Technology): consent to handle the data for THIS application only. Never one that names a period or future roles.
const DATA_CONSENT_RE = /\b(store|process|processing|use|handle)\b[^.?!]{0,60}\b(my )?(personal )?(data|information|details)\b/i;
const RETENTION_RE = /\b(\d+|one|two|three|six|twelve|eighteen)\s*(months?|years?)\b|\bretain|retention|period of\b/i;

// "We ask that you refrain from using any AI tools, transcription services or other assistants during your interview and assessment
// process" (Isomorphic Labs): a promise about the applicant's OWN conduct. It needs an AI-ish term, an interview-ish term AND a
// conduct verb, so a consent to be recorded or transcribed ("I agree to my interview being recorded by an AI notetaker") never
// matches. Ticked only while answers.acknowledgements.interview_conduct is true (his decision, 2026-10-03).
const CONDUCT_AI_RE = /\bai\b|artificial intelligence|chatgpt|\bllms?\b|assistants?\b|transcription/i;
const CONDUCT_INTERVIEW_RE = /\binterview|assessment|screening/i;
const CONDUCT_VERB_RE = /refrain from|\b(do not|don'?t|will not|must not|not to|agree not to|never) (use|using)\b|without (the )?(use of |using )?(any )?(ai|assistants?)|prohibit/i;
export function isInterviewConductPromise(text) {
  const t = String(text || "");
  return CONDUCT_AI_RE.test(t) && CONDUCT_INTERVIEW_RE.test(t) && CONDUCT_VERB_RE.test(t);
}
const ANYTHING_ELSE_RE = /\b(anything|something) (else|more|further)\b[^?]{0,60}\b(know|add|share|mention|tell)\b|\badditional (information|comments?|details)\b/i;

// True when a question label is one of the demographic / protected-characteristic questions.
export function looksLikeEeoQuestion(label) {
  return EEO_RE.test(String(label || "").toLowerCase());
}

// Some EEO-style questions never mention gender/race/age/etc. in their own
// label (e.g. Rippling's "communities" MultiValueSelect just asks "Which
// of the following communities do you belong to?"), but their OPTIONS do
// (disability/neurodivergent choices sit alongside the decline option) --
// treat those as EEO too.
const EEO_OPTIONS_RE = /disabilit(y|ies)|neurodivergen|neurodiverse/i;

function eeoValue(field) {
  if (field.type !== "select" && field.type !== "radio" && field.type !== "checkbox-group" && field.type !== "combobox") {
    return null;
  }
  const opt = pickDecline(field.options);
  if (!opt) return null;
  // A checkbox-group is a multi-select widget (e.g. Rippling's
  // "communities" question) -- only the single decline option should ever
  // be ticked, but it must be sent as the one-element array apply.mjs
  // expects for multi-value questions.
  const value = field.type === "checkbox-group" ? [opt] : opt;
  return { value, source: "answers.protected_characteristics.preference" };
}

// ------------------------------------------------------------- dispatch ---

function labelSplit(fullName) {
  const parts = (fullName || "").trim().split(/\s+/);
  return { first: parts[0] || "", last: parts.slice(1).join(" ") || "" };
}

// An upload we genuinely attempted (had a real file path to send) must
// always block submission if it could not be confirmed attached afterward
// -- never treated as merely "optional" just because the ATS itself didn't
// mark that particular field required. Never attempted (no file path) just
// falls back to whatever the field's own required flag says.
export function uploadBlocksSubmit({ attempted, confirmed, required }) {
  if (!attempted) return !!required;
  return !confirmed;
}

// Greenhouse's discipline taxonomy is a fixed list (Accounting..
// Veterinary Medicine) with no "Data Science" option, so an exact
// field-of-study match would silently queue an answerable question.
// Known alias chains bridge a degree's field words to the closest option
// boards actually offer; beyond the aliases, fall back to shared-token
// overlap between the degree text and the offered options.
const DISCIPLINE_ALIASES = {
  "data science": ["data science", "computer science", "statistics & decision theory", "statistics", "mathematics"],
};

const DISCIPLINE_STOPWORDS = new Set([
  "msc", "bsc", "ma", "ba", "meng", "beng", "hons", "honours", "honors",
  "distinction", "merit", "first", "class", "second", "upper", "lower", "third",
  "degree", "of", "and", "the",
]);

function disciplineTokens(text) {
  return (text || "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t && !DISCIPLINE_STOPWORDS.has(t));
}

function disciplineOption(options, edu) {
  if (!options || !options.length || !edu) return null;
  const degree = (edu.degree || "").toLowerCase();
  for (const [field, prefs] of Object.entries(DISCIPLINE_ALIASES)) {
    if (!degree.includes(field)) continue;
    for (const p of prefs) {
      const opt = pickOption(options, [new RegExp(`^${p}$`, "i")]);
      if (opt) return { value: opt, source: `facts.education[0].degree (closest discipline: ${p})` };
    }
  }
  const tokens = disciplineTokens(edu.degree);
  if (!tokens.length) return null;
  let best = null;
  let bestScore = 0;
  for (const opt of options) {
    const optTokens = new Set(disciplineTokens(opt));
    const score = tokens.filter((t) => optTokens.has(t)).length;
    if (score > bestScore) {
      bestScore = score;
      best = opt;
    }
  }
  return best ? { value: best, source: "facts.education[0].degree (closest discipline by token overlap)" } : null;
}

// Some comboboxes (observed: Greenhouse's School/Degree/Discipline
// pickers) are virtualized/async-search widgets that only render a short
// default slice of a huge option list until the user types to filter it --
// collecting options without typing first can silently miss the real
// match entirely. Returns a plausible search string for a field whose
// eventual value is independent of the specific options offered (derived
// straight from facts.yaml), or null when this field isn't one of those.
// Callers use this to type into the widget BEFORE collecting options; the
// actual value still comes from classifyField against the real options.
export function comboboxSearchHint(label, facts, ctx) {
  // Same normalisation classifyField applies: Greenhouse labels carry a trailing "*" ("School*"),
  // which made the ^school$ hint below never match, so the async School list was collected
  // WITHOUT typing first, came back empty, and the question was left unanswered (Scale AI, Stripe).
  const lower = (label || "").toLowerCase().replace(/[\s*✱]+$/, "").trim();
  const edu = facts && Array.isArray(facts.education) && facts.education[0];
  // Location-style search comboboxes (Greenhouse's "Location (City)") only
  // render options once a typed query hits their geocoder -- type the city
  // the address rule already resolves for this job's region.
  if (/^location\b|current location|where are you (currently )?(located|based)/i.test(lower)) {
    if (!ctx || !ctx.job || !ctx.answers) return null;
    const addr = addressFor(ctx.job, ctx.answers.addresses);
    if (!addr || addressHasPlaceholder(addr, "city")) return null;
    return addr.city;
  }
  // A country typeahead ("Please let us know your current country of residence", Omnea): type the address's country.
  if (/country of residence|country (you )?(currently )?(live|reside|are based|are located)|^country$/i.test(lower)) {
    if (!ctx || !ctx.job || !ctx.answers) return null;
    const addr = addressFor(ctx.job, ctx.answers.addresses);
    return addr && addr.country ? addr.country : null;
  }
  if (!edu) return null;
  if (/^school$|school name|^institution$|^university$/i.test(lower)) return edu.institution;
  if (/^degree$|degree\/?qualification/i.test(lower)) {
    return /msc|master/i.test(edu.degree) ? "Master" : /bsc|bachelor/i.test(edu.degree) ? "Bachelor" : null;
  }
  if (/discipline|field of study|^major$/i.test(lower)) return "Data Science";
  return null;
}

// Earliest start as an ISO date: today + the standing notice period ("1 month", "2 weeks", "30 days");
// "immediately"/"none" -> today. Unparseable -> null (the question is then held, never guessed).
export function startDateAfterNotice(notice, from = new Date()) {
  const t = String(notice || "").toLowerCase();
  const d = new Date(Date.UTC(from.getFullYear(), from.getMonth(), from.getDate()));
  const m = t.match(/(\d+)\s*(day|week|month)/);
  if (m) {
    const n = Number(m[1]);
    if (m[2] === "day") d.setUTCDate(d.getUTCDate() + n);
    else if (m[2] === "week") d.setUTCDate(d.getUTCDate() + 7 * n);
    else d.setUTCMonth(d.getUTCMonth() + n);
  } else if (!/immediate|none|no notice|asap/.test(t)) {
    return null;
  }
  return d.toISOString().slice(0, 10);
}

export function classifyField(field, ctx) {
  const { answers, job, facts } = ctx;
  const label = (field.label || "").trim();
  // Teamtailor glues a visually hidden "Required" onto the label ("Email*Required").
  const lower = label.toLowerCase().replace(/\*+\s*required\s*$/i, "").replace(/[\s*✱]+$/, "").trim();
  const contact = answers.contact || {};
  const { first, last } = labelSplit(contact.name);

  if (!label) return null;

  const bankAnswer = bankLookup(field, ctx);
  if (bankAnswer) return bankAnswer;

  // Free-text contact/address rules only ever apply to text-like controls;
  // a checkbox or checkbox-group that merely mentions "email"/"address"
  // (e.g. a marketing-emails opt-in) must never be answered from here.
  const textLike = !["checkbox", "checkbox-group", "file"].includes(field.type);
  const regionKey = job && job.region === "sussex" ? "sussex" : "default";

  // ---- contact -----------------------------------------------------
  if (textLike && /^full\s*name$|^name\*?$|^your name$|^applicant name$/i.test(lower)) {
    return { value: contact.name, source: "answers.contact.name" };
  }
  // "Legal name" / "Preferred name" (OpenAI, Stripe): the full name / the first name.
  // "Legal first name" is the FIRST name only: a form once received the full name there.
  if (textLike && /^legal (first|given) name\b/i.test(lower)) {
    return { value: first, source: "answers.contact.name (legal first name)" };
  }
  if (textLike && /^(full )?legal name$|^legal name\b|name (as|exactly as) (it )?appears? on (your )?(passport|id)/i.test(lower)) {
    return { value: contact.name, source: "answers.contact.name (legal name)" };
  }
  if (textLike && /^preferred (first )?name\b|^nickname\b|^what (should|do) we call you|^name you go by|^known as\b/i.test(lower)) {
    return { value: first, source: "answers.contact.name (preferred name = first name)" };
  }
  // Free-text or option-based "Pronouns" (answers.pronouns, e.g. he/him).
  if (/^pronouns?\b/i.test(lower) && answers.pronouns) {
    if (["select", "radio", "combobox"].includes(field.type)) {
      const opt = pickOption(field.options, [/^he\s*[/,]\s*him/i, /^he\b/i]);
      return opt ? { value: opt, source: "answers.pronouns" } : null;
    }
    if (textLike) return { value: answers.pronouns, source: "answers.pronouns" };
  }
  if (/^pronouns?\b/i.test(lower) && answers.pronouns && field.type === "checkbox-group") {
    const opt = pickOption(field.options, [/^he\s*[/,]\s*him/i, /^he\b/i]);
    return opt ? { value: [opt], source: "answers.pronouns" } : null;
  }
  if (textLike && /first\s*name|given\s*name/i.test(lower)) return { value: first, source: "answers.contact.name" };
  if (textLike && /last\s*name|surname|family\s*name/i.test(lower)) return { value: last, source: "answers.contact.name" };
  if (textLike && /^e-?mail(\s*address)?$|your e-?mail/i.test(lower)) return { value: contact.email, source: "answers.contact.email" };
  // "Email confirmation" / "Confirm your email" (Jane Street): the same address typed again.
  if (textLike && /\b(confirm|re-?enter|re-?type|verify|repeat)\b[^?]{0,20}\be-?mail\b|\be-?mail\b[^?]{0,20}\b(confirm|confirmation|again)\b/i.test(lower)) {
    return contact.email ? { value: contact.email, source: "answers.contact.email (confirmation)" } : null;
  }
  // "I don't have a school email" (Jane Street): none is on file, so the honest tick; the university-email box itself is never
  // filled with his personal address.
  if (field.type === "checkbox" && /(don'?t|do not) have a (school|university|student|college) e-?mail/i.test(lower)) {
    return { value: true, source: "answers.contact (no university email on file)" };
  }
  if (textLike && /\b(no|not|n'?t)\b[^?]{0,30}\b(school|university|student|college)\s+e-?mail\b[^?]*\b(reason|why|explain)\b|\b(reason|why|explain)\b[^?]*\b(no|not|n'?t)\b[^?]{0,30}\b(school|university|student|college)\s+e-?mail\b/i.test(lower)) {
    const edu = (facts && facts.education) || [];
    if (!edu.length || edu.some((e) => /\b(present|current|ongoing|expected|in progress)\b/i.test(String(e.dates || "")))) return null;
    const e = edu[0];
    return { value: `I am not currently a student (${String(e.degree || "").split(",")[0]}, ${e.institution}: ${e.dates}).`, source: "facts.education (every entry completed)" };
  }
  if (/\b(university|school|student|college)\s+e-?mail\b/i.test(lower)) return null;
  if (textLike && /^website$/i.test(lower)) return contact.github ? { value: contact.github, source: "answers.contact.github" } : null;
  // (Recruitee's calling-code list is labelled just "Phone number *" and carries the country names as its options.)
  if (["select", "combobox", "radio"].includes(field.type) && (/phone country|country code|calling code/i.test(lower)
      || (/^phone( number)?$/i.test(lower) && (field.options || []).some((o) => /united kingdom/i.test(o))))) {
    const opt = pickOption(field.options, [/united kingdom/i, /^uk\b/, /\+44/]);
    if (!opt) return null;
    return { value: opt, source: "answers.contact.phone (country: United Kingdom)" };
  }
  if (textLike && /phone|mobile|telephone/i.test(lower)) {
    // "Phone number with country code" (Teamtailor): the international form, +44 and no trunk 0.
    if ((field.international || /country code|international (format|number)|\binclude\b[^?]*\bcountry\b/i.test(lower)) && typeof contact.phone === "string" && /^\s*0/.test(contact.phone)) {
      return { value: "+44" + contact.phone.replace(/\D/g, "").replace(/^0/, ""), source: "answers.contact.phone (international format, UK)" };
    }
    return { value: contact.phone, source: "answers.contact.phone" };
  }
  // "Where can we learn more about you? Please add your LinkedIn, GitHub, Personal Websites": every link on file
  if (textLike && /where can we learn more about you|linkedin[^?]{0,40}(github|website|portfolio)|(github|website|portfolio)[^?]{0,40}linkedin/i.test(lower)) {
    const links = [contact.linkedin, contact.github].filter(Boolean).map((u) => (/^https?:\/\//i.test(u) ? u : `https://${u}`));
    return links.length ? { value: links.join(", "), source: "answers.contact.linkedin+github" } : null;
  }
  if (textLike && /linkedin/i.test(lower)) return contact.linkedin ? { value: contact.linkedin, source: "answers.contact.linkedin" } : null;
  if (textLike && /github/i.test(lower)) return contact.github ? { value: contact.github, source: "answers.contact.github" } : null;

  // ---- address --------------------------------------------------------
  if (textLike && /street address|address line ?1|^address$|\bcity\b|\btown\b|\bcounty\b|state\s*[/,]\s*province|\bprovince\b|postcode|post code|\bzip\b/i.test(lower)) {
    const addr = addressFor(job, answers.addresses);
    if (!addr) return null;
    let value = null;
    let field_key = null;
    if (/street|address line ?1|^address$/i.test(lower)) { value = addr.street; field_key = "street"; }
    else if (/city|town/i.test(lower)) { value = addr.city; field_key = "city"; }
    else if (/county|province/i.test(lower)) { value = addr.county; field_key = "county"; }
    else if (/postcode|post code|\bzip\b/i.test(lower)) { value = addr.postcode; field_key = "postcode"; }
    if (value === undefined || value === null) return null;
    if (typeof value === "string" && /TODO_ASK_OPERATOR/.test(value)) return null;
    return { value, source: `answers.addresses.${regionKey}.${field_key}` };
  }
  if (/full address|complete address|mailing address|home address/i.test(lower)) {
    const addr = addressFor(job, answers.addresses);
    if (!addr || addressHasPlaceholder(addr)) return null;
    return { value: addr.full, source: `answers.addresses.${regionKey}.full` };
  }
  // NB: "country of BIRTH" is deliberately NOT here -- it used to be answered with the current (UK) address
  // country, a false statement (nothing on file says where he was born); it now stays held.
  if (textLike && /^country$|country of residence|current country|country (where )?you (currently )?(reside|live|are (currently )?(based|located|living))|country you live|(which|what) country (are you|do you) (currently )?(based|located|living|live|reside)/i.test(lower)) {
    const addr = addressFor(job, answers.addresses);
    if (!addr) return null;
    if (field.options && field.options.length && /united kingdom|^uk$|england|britain/i.test(addr.country)) {
      const uk = pickUkOption(field.options);   // Stripe lists "UK": returning "United Kingdom" found no option
      if (uk) return { value: uk, source: "answers.addresses.country (the list's UK option)" };
    }
    return { value: addr.country, source: "answers.addresses.country" };
  }
  // "Where are you currently located?" / bare "Location" (incl. Rippling's
  // real title "Location (city only)") -- distinct from the city/town/
  // address fields above, which require an exact "city"/"town"/"address"
  // word in the label.
  if (textLike && /where are you (currently )?(located|based)|current location|^location\b/i.test(lower)) {
    const addr = addressFor(job, answers.addresses);
    if (!addr || addressHasPlaceholder(addr, "city")) return null;
    if (field.type === "textarea") {
      const value = [addr.city, addr.county, addr.country].filter(Boolean).join(", ");
      return { value, source: `answers.addresses.city+county+country` };
    }
    if (field.type === "combobox" && field.options) {
      if (field.options.some(o => typeof o === "string" && o.toLowerCase().includes(addr.city.toLowerCase()))) {
        return { value: addr.city, source: `answers.addresses.city` };
      }
      if (field.options.some(o => typeof o === "string" && o.toLowerCase().includes(addr.country.toLowerCase()))) {
        return { value: addr.country, source: `answers.addresses.country` };
      }
    }
    return { value: addr.city, source: `answers.addresses.city` };
  }

  // ---- visa (checked BEFORE right-to-work: Ashby's "current VISA status
  // or legal right to work in the UK" text field must never be answered
  // with a bare right-to-work "Yes" when it's actually asking for the visa
  // type/status itself) -----------------------------------------------
  if ((field.type === "text" || field.type === "textarea") && /visa status|visa type|current visa/i.test(lower) && !/sponsor/i.test(lower)) {
    if (!answers.visa || !answers.visa.status) return null;
    const parts = [answers.visa.status];
    if (answers.visa.expires) parts.push(`valid until ${formatMonthYear(answers.visa.expires)}`);
    return { value: parts.join(", "), source: "answers.visa.status" };
  }

  // ---- nationality: a fact on file (answers.nationality). Free text gets it; a list gets the matching option, or the
  // "non-British / other" bucket when the list is British-vs-not (Softwire: British | Non British | Prefer not to say).
  if (/^nationality\b|\bwhat is your nationality|\byour nationality\b|\bnationality\??\*?\s*$/i.test(lower) && answers.nationality) {
    const nat = String(answers.nationality);
    if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length) {
      const stem = nat.toLowerCase().replace(/(ese|ian|i|an)$/, "");
      const exact = field.options.find((o) => String(o).toLowerCase().includes(stem));
      if (exact) return { value: exact, source: "answers.nationality" };
      if (!/british/i.test(nat)) {
        const other = pickOption(field.options, [/^non[- ]?british|^not british/i, /^other\b/i]);
        if (other) return { value: other, source: "answers.nationality (not British)" };
      }
      return null;
    }
    if (textLike) return { value: nat, source: "answers.nationality" };
  }

  // ---- where he will work (Stripe / Figma / Ledgy), derived from the job's own place and his UK right to work ----
  // "Please select the country or countries you anticipate working in": the UK.
  if (["checkbox-group", "select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && /countr(y|ies)\b[^?.]{0,80}\b(anticipate|plan|intend|expect)\w*\b[^?.]{0,30}\bwork/i.test(lower)) {
    const uk = pickUkOption(field.options);
    if (uk) return { value: field.type === "checkbox-group" ? [uk] : uk, source: "the UK (his right to work covers it)" };
  }
  // "If this role offers the option to work remotely, do you plan to work remotely?": office for the London /
  // Sussex / nearby roles (he can do 5 days in office), remote for the remote roles.
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && /work from a remote location|plan to work remotely|intend to work remotely|option to work remotely/i.test(lower) && job) {
    const remote = job.region === "remote";
    const opt = pickOption(field.options, remote ? [/remote/i] : [/office|on-?site|in[- ]person/i]);
    if (opt) return { value: opt, source: `job.region (${job.region}) + answers.office_attendance` };
  }
  // "From where do you intend to work?" (free text)
  if (textLike && /from where do you (intend|plan|expect) to work|where (do|will) you (intend|plan|expect) to (work|be working)|where will you be working from/i.test(lower) && job) {
    const city = String(job.location || "").split(/[,;(]/)[0].trim();
    const sussexCity = answers.addresses && answers.addresses.sussex && answers.addresses.sussex.city;
    const place = job.region === "london" ? "London, United Kingdom"
      : job.region === "sussex" && sussexCity ? `${sussexCity}, United Kingdom`
      : job.region === "nearby" && city ? `${city}, United Kingdom` : "United Kingdom (remote)";
    return { value: place, source: `job.region (${job.region})` };
  }
  // "Are you currently based in Europe/UK or eligible to work in Europe/UK?": he is, and is (Graduate visa).
  if (["select", "combobox", "radio", "checkbox-group"].includes(field.type) && field.options && field.options.length
      && /\b(based in|located in|eligible to work in|right to work in|authori[sz]ed to work in)\b[^?.]{0,40}\b(europe|uk|united kingdom|eea)\b/i.test(lower)
      && (field.type === "checkbox-group" || /\b(europe|eea)\b/i.test(lower))   // plain UK wording stays with the older based-in-UK / right-to-work rules
      && !/sponsor|visa/i.test(lower) && toBool(answers.right_to_work)) {
    const opt = pickOption(field.options, [/^yes\b/i, /^(i am|i'm)\b/i, /\b(based|eligible)\b/i]);
    if (opt && !/\bno\b|\bnot\b/i.test(opt)) return { value: field.type === "checkbox-group" ? [opt] : opt, source: "answers.based_in_uk + right_to_work" };
  }
  // "Do you require any reasonable adjustments to the interview process?" -- only when answers.reasonable_adjustments is set.
  if (["select", "combobox", "radio", "text", "textarea"].includes(field.type) && /reasonable adjustments?|need(s)? adjustments?|require (any )?adjustments?/i.test(lower)
      && answers.reasonable_adjustments !== undefined && answers.reasonable_adjustments !== null) {
    // a free-text box (Xelix) takes "No"; a Yes would need details only he can give
    if (field.type === "text" || field.type === "textarea") {
      return toBool(answers.reasonable_adjustments) ? null : { value: "No", source: "answers.reasonable_adjustments" };
    }
    const val = yesNoValue(field, answers.reasonable_adjustments);
    if (val !== null) return { value: val, source: "answers.reasonable_adjustments" };
  }

  // ---- right to work WITHOUT sponsorship (compound question) ----------
  // Ashby-style "Are you legally authorised to work in the UK without
  // sponsorship, now and in the future?" is NOT a plain right-to-work
  // question: it inverts the sponsorship polarity and bundles BOTH time
  // horizons, so the blanket right_to_work=yes would be recorded as
  // "will never need sponsorship" -- factually wrong for anyone on a
  // time-limited visa. Answer it from the sponsorship facts instead:
  // authorised-without-sponsorship is the NEGATION of needing
  // sponsorship over the horizon the question actually asks about.
  if (/authori[sz]ed to work|right to work|legally (eligible|entitled) to work/i.test(lower) && /without (a )?sponsorship|not require(ing)? sponsorship|no sponsorship (is )?required/i.test(lower)) {
    const future = FUTURE_RE.test(lower);
    const present = PRESENT_RE.test(lower);
    let needsSponsorship;
    if (future && present) needsSponsorship = toBool(answers.sponsorship_now) || toBool(answers.sponsorship_future);
    else if (future) needsSponsorship = toBool(answers.sponsorship_future);
    else if (present) needsSponsorship = toBool(answers.sponsorship_now);
    else return null; // horizon unclear -> must be queued, never guessed
    const val = yesNoValue(field, !needsSponsorship);
    if (val === null) return null;
    return { value: val, source: "answers.sponsorship_now/future (negated: authorised without sponsorship)" };
  }

  // ---- "(Singapore and Hong Kong applicants only)": a question for applicants elsewhere answers N/A (before the status rule, which would hold it)
  {
    const only = /\b([a-z][a-z ,&]{2,60}?)\s+applicants?\s+only\b/i.exec(lower);
    if (only && !/\b(uk|united kingdom|england|britain|europe|emea|worldwide|international)\b/i.test(only[1])
        && ["select", "combobox", "radio"].includes(field.type) && field.options) {
      const na = pickOption(field.options, [/^(n\/?a|not applicable)$/i]);
      if (na) return { value: na, source: `the question is for ${only[1].trim()} applicants only; he is not one` };
    }
  }

  // ---- right-to-work / immigration STATUS (an option list of statuses, not a yes/no) ---------------
  // "Please select your right to work status": British citizen / settled / Graduate Visa / ... Answer
  // with the option naming his actual status (answers.visa.status, "Graduate visa"); no matching
  // option -> held, never a nearest guess.
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && (/(right to work|work(ing)?( authori[sz]ation)?|immigration|visa|residency) status/i.test(lower)
          || /(basis|what (type|kind|permission)|how do you have)[^?]{0,40}right to work|right to work[^?]{0,40}(basis|based on|under|type)/i.test(lower))
      && !/sponsor/i.test(lower) && answers.visa && answers.visa.status) {
    const word = String(answers.visa.status).replace(/\s*visa\s*$/i, "").trim();
    let opt = word ? pickOption(field.options, [new RegExp(`^${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b.*visa|^${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*visa`, "i")]) : null;
    // "I have a Graduate Route visa", "Visa (e.g. Graduate)": any option naming his visa kind, never a citizen / settled one
    if (!opt && word) opt = field.options.find((o) => new RegExp(word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i").test(o) && !/british|irish|citizen|settled|indefinite|dependan|student(?!\s*(or|\/|and)\s*graduate)/i.test(o)) || null;
    return opt ? { value: opt, source: "answers.visa.status" } : null;
  }

  // ---- right to work ----------------------------------------------
  if (/right to (live and )?work|authori[sz]ed to (live and )?work|authori[sz]ation to work|legally (eligible|entitled) to (live and )?work|eligib(le|ility) to work/i.test(lower)) {
    const val = yesNoValue(field, answers.right_to_work);
    if (val === null) return null;
    return { value: val, source: "answers.right_to_work" };
  }

  // ---- "are you at least 18?" (from answers.date_of_birth) --------------------------------------
  if (/\b(at least|over|above|older than)\s+(the age of\s+)?18\b|\b18\s+(years?\s+(of age\s+)?)?(or|and)\s+(older|over|above)|\baged?\s+18\b|are you 18\b/i.test(lower)
      && answers.date_of_birth) {
    const dob = new Date(String(answers.date_of_birth));
    if (!Number.isNaN(dob.getTime())) {
      const now = new Date();
      let age = now.getFullYear() - dob.getFullYear();
      if (now.getMonth() < dob.getMonth() || (now.getMonth() === dob.getMonth() && now.getDate() < dob.getDate())) age--;
      const val = yesNoValue(field, age >= 18);
      if (val === null) return null;
      return { value: val, source: "answers.date_of_birth (age)" };
    }
  }

  // ---- based in the UK (distinct from right-to-work / sponsorship) -----
  // "Are you currently located in France, United Kingdom, Germany, Spain or the Netherlands?" --
  // an enumeration of allowed countries: Yes exactly when a UK term is among them. A single city
  // ("Are you located in London?") is NOT covered: a UK applicant outside that one city honestly answers No, so that stays held.
  const locatedList = /\b(?:currently )?located in\b([^?.]{0,180})/i.exec(lower);
  if (locatedList && /\bunited kingdom\b|\buk\b|\bengland\b|\bbritain\b/i.test(locatedList[1])) {
    const val = yesNoValue(field, answers.based_in_uk);
    if (val === null) return null;
    return { value: val, source: "answers.based_in_uk (located-in list includes the UK)" };
  }
  if (/based in the uk\b|based in the united kingdom|currently based in the uk|^are you (currently )?based in|reside in the uk|resident in the uk|live in the uk/i.test(lower)) {
    const val = yesNoValue(field, answers.based_in_uk);
    if (val === null) return null;
    return { value: val, source: "answers.based_in_uk" };
  }

  // ---- office attendance / commute (region-gated) -----------------------
  if (/commit to being (in|at)|able to (be|attend|work) (in|at) the office|days?\s*(a|per)\s*week\s*(in|at)|in-office|on-?site (attendance|presence|days)?|work from the office|come into the office|office attendance|hybrid (working|schedule|attendance)|days in office|in office per (week|month)|working patterns?|remote within the uk|\b(attend|work (from|in|at)|be (in|at)|come (in|into)|commute to)\b[^?.]{0,60}\boffice\b|office[- ]based|required (on-?site|in (the )?office)|in[- ]person[^?.]{0,80}\boffices?\b|\boffices?\b[^?.]{0,30}\d+\s*days?\s*(\/|a|per)\s*week/i.test(lower)) {
    const regionOk = job && (job.region === "london" || job.region === "sussex" || job.region === "nearby");
    if (!regionOk) return null;
    const office = answers.office_attendance || {};
    if (field.type === "text" || field.type === "number") {
      if (!office.max_days_per_week) return null;
      return { value: String(office.max_days_per_week), source: "answers.office_attendance.max_days_per_week" };
    }
    if (field.type === "checkbox-group") {
      // A "which working patterns would you be happy with?" style question
      // -- the operator accepts up to answers.office_attendance.max_days_per_week days in office, hybrid,
      // or fully remote, so every offered pattern option is genuinely acceptable.
      if (!field.options || !field.options.length) return null;
      // Working PATTERNS (hybrid / remote / office) are all acceptable, but a list of LOCATIONS must not tick a city he has no right
      // to work in: Lightning AI's "select all locations you would work from" listed San Francisco, New York City and Seattle.
      // When an option names THIS job's own city, that is the answer ("select all locations you can work a hybrid schedule from": London;
      // ticking "Remote only" as well would contradict it).
      const loc = String(job.location || "").toLowerCase();
      const own = field.options.filter((o) => {
        const t = String(o).toLowerCase().replace(/[^a-z ]/g, " ").trim();
        return t && !/remote only/i.test(o) && (loc.includes(t) || (job.region === "london" && /\blondon\b/.test(t)));
      });
      if (own.length) return { value: own, source: "answers.office_attendance.rule (the job's own city)" };
      const acceptable = field.options.filter((o) => !isForeignPlace(o) && !/remote only/i.test(o));
      return acceptable.length ? { value: acceptable, source: "answers.office_attendance.rule (no foreign place)" } : null;
    }
    let val = yesNoValue(field, true);
    if (val === null && field.options && field.options.length) {
      // Omnea: "I am happy to come into the office ..." against "Being in-person Tues-Thurs is not for me": the accepting sentence
      val = field.options.find((o) => /\b(happy|yes|able|willing|comfortable|can)\b/i.test(o) && !/\b(not|no|unable|only open to remote|prefer remote)\b/i.test(o)) || null;
    }
    if (val === null) return null;
    return { value: val, source: "answers.office_attendance.rule" };
  }

  // ---- employment terms and wording-heavy options (Omnea, OQC, causaLens) -----------------------------------------------
  const pickFrom = ["radio", "select", "combobox"].includes(field.type) && field.options && field.options.length;
  // "This role is only available as a permanent (non-contractor) position": he wants permanent employment, never contract/gig work.
  if (pickFrom && /\b(permanent|non-?contract(or)?|only available as)\b[^?]*\b(position|role|basis|employment|contract(or)?)\b/i.test(lower)) {
    const opt = field.options.find((o) => /happy to be employed|employee|permanent|full[- ]?time|\byes\b|accept|comfortable/i.test(o) && !/only open to contract|contract(or)? only|\bno\b/i.test(o));
    if (opt) return { value: opt, source: "standing preference: permanent employment" };
  }
  // "If we think your profile is not senior enough for this role, are you open to more junior roles?": he is searching at junior level.
  if (["radio", "select", "combobox"].includes(field.type) && /(not|isn'?t|aren'?t) (senior|experienced) enough|more junior|junior (role|position|engineer)/i.test(lower) && /\b(open|consider|happy|willing)\b/i.test(lower)) {
    const val = yesNoValue(field, true);
    if (val !== null) return { value: val, source: "standing preference: open to junior roles" };
  }
  // "How many years of paid experience do you have?" with BANDED options: his employment intervals (facts.experience), unioned,
  // placed in the highest band they reach. A skill-specific question ("... experience in ML/DS") is not this one.
  const labelNoParens = lower.replace(/\([^)]*\)/g, " ");
  if (pickFrom && /how many years[^?]*\b(paid|professional|work|employment|commercial)\b[^?]*\bexperience\b(?![^?]*\b(in|with|using|as|of)\s+\w)/i.test(labelNoParens)) {
    const years = paidExperienceYears(facts);
    const band = years === null ? null : bandForYears(field.options, years);
    if (band) return { value: band, source: `facts.experience (paid employment ~${years.toFixed(1)} years, unioned)` };
  }
  // "If you are a dual national or a citizen of another country other than the UK, please name each country."
  if (textLike && answers.nationality_country
      && /dual national|citizen(ship)?[^?]{0,40}(another|other) country|(citizen|national)[^?]{0,60}other than the (uk|united kingdom)|other than the (uk|united kingdom)[^?]{0,60}(citizen|national)/i.test(lower)) {
    return { value: answers.nationality_country, source: "answers.nationality_country" };
  }
  // "Do you live in London or are you happy to relocate?": he lives at the address used for this job and may move within the UK.
  if (/\b(live|based|reside|located)\b[^?]{0,30}\b(in|near|within)\b[^?]{0,30}\b(or|\/)\b[^?]{0,40}\b(relocat|move)/i.test(lower) && toBool(answers.relocation && answers.relocation.willing)) {
    const addr = addressFor(job, answers.addresses);
    if (addr && !addressHasPlaceholder(addr, "city")) {
      if (["radio", "select", "combobox"].includes(field.type)) {
        const val = yesNoValue(field, true);
        if (val !== null) return { value: val, source: "answers.relocation.willing" };
      } else if (textLike) {
        return { value: `I live in ${addr.city}, ${addr.county} and I am happy to relocate.`, source: "answers.addresses + relocation.willing" };
      }
    }
  }


  // "Are you located within 50 miles of one of the following office hubs?": straight-line miles from the address used for this
  // job, read from answers (`addresses.<key>.miles`, see profile/answers.example.yaml). Outside -> the "No, but willing to relocate" option.
  if (["radio", "select", "combobox"].includes(field.type) && /within\s+(\d+)\s+miles?\s+of/i.test(lower) && field.options && field.options.length) {
    const miles = Number(lower.match(/within\s+(\d+)\s+miles?\s+of/i)[1]);
    const near = (answers.addresses && answers.addresses[regionKey] && answers.addresses[regionKey].miles) || {};
    const hubs = Object.keys(near).filter((h) => field.options.some((o) => new RegExp(h, "i").test(o)));
    const inside = hubs.find((h) => near[h] <= miles);
    if (inside) return { value: pickOption(field.options, [new RegExp(inside, "i")]), source: `answers.addresses.${regionKey}.miles (~${near[inside]} miles from ${inside})` };
    const none = pickOption(field.options, [/^no\b/i]);
    if (!none || !hubs.length && !/london|san francisco|new york|seattle|boston|berlin|paris/i.test(field.options.join(" "))) return null;
    return /relocate/i.test(none) && !toBool(answers.relocation && answers.relocation.willing) ? null
      : { value: none, source: `address (${regionKey}) is outside ${miles} miles of every hub offered` };
  }

  // ---- sponsorship --------------------------------------------------
  if (/sponsor|(need|require)s? (an? )?(work )?visa\b/i.test(lower)) {
    if ((field.type === "text" || field.type === "textarea") && /details|explain|please (provide|specify|state)|type of visa/i.test(lower)) {
      return { value: sponsorshipDetailsValue(answers), source: "answers.sponsorship_now/future+visa" };
    }
    return sponsorshipValue(field, answers);
  }

  // ---- notice period --------------------------------------------------
  if (/notice period|how much notice|period of notice/i.test(lower)) {
    return answers.notice_period ? { value: answers.notice_period, source: "answers.notice_period" } : null;
  }

  // ---- salary ------------------------------------------------------
  if (/salary|compensation|pay expectation|desired pay|expected (pay|salary)/i.test(lower)) {
    const band = salaryBand(job, answers.salary);
    if (!band) return null;
    if (field.type === "number") {
      const lowerBound = bandLowerBound(band);
      if (lowerBound === null) return null;
      return { value: String(lowerBound), source: "answers.salary.single_number_rule" };
    }
    return { value: band, source: "answers.salary.rule" };
  }

  // ---- marketing / messaging / talent-pool OPT-INS: declined ----------------------------------------
  // "Do you opt in to WhatsApp messages?", "May we contact you about future roles?": "No" is the safe default --
  // it commits him to nothing -- so a required Yes/No opt-in never holds an application. (A required CHECKBOX
  // opt-in cannot be declined by leaving it empty and stays held.) Interview recording, privacy, terms and
  // demographic consent are different questions and are excluded here.
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && OPTIN_RE.test(lower) && !OPTIN_EXCLUDE_RE.test(lower)) {
    const no = pickOption(field.options, [/^no\b/i, /^i (do not|don'?t) (wish|want|consent|agree)/i, /^(decline|opt[- ]?out|unsubscribe)/i]);
    if (no) return { value: no, source: "standing rule: marketing / messaging / talent-pool opt-ins are declined" };
  }

  // ---- interview-recording opt-ins: declined ---------------------------------------------------------
  // "we offer the option to utilise Metaview, our interview recording platform ... opt-in by clicking 'Yes'" (Helsing),
  // "we use BrightHire to record and transcribe interviews" (Stripe). Consenting to be recorded is never given
  // (standing rule); "No" commits him to nothing and both forms say participation is voluntary and does not affect
  // the candidacy. Config-driven: answers.acknowledgements.decline_interview_recording.
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && answers.acknowledgements && answers.acknowledgements.decline_interview_recording === true
      && /\b(record(ed|ing|s)?|transcri(be|bed|bing|ption)|metaview|brighthire|otter\.ai|fireflies|note-?taker)\b/i.test(lower)
      && /\b(interview|call|conversation|meeting)s?\b/i.test(lower)) {
    const no = pickOption(field.options, [/^no\b/i, /^i (do not|don'?t)\b/i, /^(decline|opt[- ]?out)/i]);
    if (no) return { value: no, source: "answers.acknowledgements.decline_interview_recording" };
  }

  // ---- consent to process the demographic survey answers (moot when every answer was "prefer not to say") --
  // Deliveroo: "I consent to ... processing my responses to the demographic data surveys above" is REQUIRED even
  // when every survey question was declined. Ticked only while answers.acknowledgements
  // .demographic_consent_when_declined is true AND apply.mjs has seen at least one demographic question answered
  // and none left unanswered (ctx.demographicsAllDeclined).
  if (field.type === "checkbox" && field.required && DEMOGRAPHIC_CONSENT_RE.test(lower)
      && answers.acknowledgements && answers.acknowledgements.demographic_consent_when_declined === true
      && ctx.demographicsAllDeclined === true) {
    return { value: true, source: "answers.acknowledgements.demographic_consent_when_declined (every survey answer is 'prefer not to say')" };
  }

  // ---- required application-notice acknowledgements --------------------------
  // Part of submitting at all (read the privacy notice / confirm the information is accurate), and
  // only while answers.acknowledgements.application_notices is true. Never ticked here: marketing,
  // talent pools, third-party sharing, recording, demographic-data consent, interview-conduct
  // promises, contracts -- those stay held for the operator.
  const soleAckOption = ["select", "combobox", "radio"].includes(field.type)
    && field.options && field.options.length === 1 && ACK_RE.test(String(field.options[0]));
  if (answers.acknowledgements && answers.acknowledgements.application_notices === true
      && (field.required || (field.type === "checkbox" && ACK_RE.test(lower) && APPLICATION_SCOPED_RE.test(lower)))
      && (ACK_RE.test(lower) || soleAckOption
          || (field.type === "checkbox" && DATA_CONSENT_RE.test(lower) && APPLICATION_SCOPED_RE.test(lower) && !RETENTION_RE.test(lower)))
      && !ACK_EXCLUDE_RE.test(lower + " " + String((field.options || [])[0] || ""))) {
    if (field.type === "checkbox") return { value: true, source: "answers.acknowledgements.application_notices" };
    if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length) {
      const opt = pickOption(field.options, [/^(yes|i (do )?(acknowledge|agree|accept|consent|understand|confirm|have read))\b/i,
                                             /^(acknowledge|agree|accept|consent|confirmed?)\b/i]);
      if (opt && !/\b(not|n't|decline|disagree|no)\b/i.test(opt)) {
        return { value: opt, source: "answers.acknowledgements.application_notices" };
      }
    }
  }
  // A checkbox-group whose label OR sole option is the acknowledgement (Squarepoint: "Privacy Notice *"
  // with the one option "I confirm that I have read and understood the Privacy Notice").
  if (field.type === "checkbox-group" && field.required && answers.acknowledgements
      && answers.acknowledgements.application_notices === true && field.options && field.options.length === 1
      && (ACK_RE.test(lower) || ACK_RE.test(String(field.options[0]))) && !ACK_EXCLUDE_RE.test(lower + " " + String(field.options[0]))) {
    return { value: [field.options[0]], source: "answers.acknowledgements.application_notices" };
  }

  // ---- interview-conduct promise (his flag) -----------------------------------------------------
  if (answers.acknowledgements && answers.acknowledgements.interview_conduct === true && field.required) {
    const optionTexts = (field.options || []).map(String);
    if ((field.type === "checkbox" || field.type === "checkbox-group")
        && (isInterviewConductPromise(lower) || optionTexts.some(isInterviewConductPromise))) {
      if (field.type === "checkbox") return { value: true, source: "answers.acknowledgements.interview_conduct" };
      if (optionTexts.length === 1) return { value: [optionTexts[0]], source: "answers.acknowledgements.interview_conduct" };
      if (optionTexts.length === 0) return { value: true, source: "answers.acknowledgements.interview_conduct" };
    }
  }
  // A required "anything else you'd like us to know?" box: a neutral line that claims nothing.
  if ((field.type === "textarea" || field.type === "text") && field.required && answers.nothing_further
      && ANYTHING_ELSE_RE.test(lower) && !/cover letter/i.test(lower)) {
    return { value: String(answers.nothing_further), source: "answers.nothing_further" };
  }

  // ---- security clearance HELD (not eligibility / residence history) ----------------------------
  if (/\b(hold|have|possess|currently (hold|have))\b[^?]*\b(security|national security|bpss|sc|dv|nsv)\b[^?]*\bclearance\b|\bclearance\b[^?]*\b(currently )?(held|hold)\b/i.test(lower)
      && !/eligib|lived|resid|years|continuous/i.test(lower) && answers.security_clearance) {
    const none = /^(none|no)$/i.test(String(answers.security_clearance));
    if (field.type === "text" || field.type === "textarea") {
      return { value: none ? "No" : String(answers.security_clearance), source: "answers.security_clearance" };
    }
    const val = yesNoValue(field, !none);
    return val === null ? null : { value: val, source: "answers.security_clearance" };
  }

  // ---- "which office would you prefer" -- the job's own city -----------------------------------------
  // London roles -> the London option; nearby / Sussex roles -> the option whose city appears in the
  // posting's own location text ("Cambridge, UK" -> "Cambridge"). No match -> held, never a guess.
  if (["select", "combobox", "radio", "checkbox-group"].includes(field.type) && field.options && field.options.length
      && /(prefer|which|what)[^?]*\b(office|location)s?\b|\b(office|location)s?\b[^?]*(prefer|preferred)/i.test(lower)
      && !/currently|where do you (live|reside)|region you/i.test(lower) && job
      && ["london", "sussex", "nearby"].includes(job.region)) {
    let opt = job.region === "london" ? pickOption(field.options, [/^london\b/i, /\blondon\b/i]) : null;
    if (!opt) {
      const where = String(job.location || "").toLowerCase();
      opt = field.options.find((o) => {
        const city = String(o || "").toLowerCase().split(/[,(/|-]/)[0].trim();
        return city.length > 2 && new RegExp(`\\b${city.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`).test(where);
      }) || null;
    }
    if (opt) return { value: field.type === "checkbox-group" ? [opt] : opt, source: `job.region (${job.region} role -> its own office)` };
  }

  // ---- "Have you served in the military?" -- a plain Yes/No with NO decline option is answered from
  // answers.military_service (a standing Yes/No on file); a list that offers "prefer not
  // to say" stays with the EEO decline default below.
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && /\bserved in (the )?(military|armed forces|forces)\b|\bhave you (ever )?served in\b/i.test(lower)
      && answers.military_service !== undefined && answers.military_service !== null && !pickDecline(field.options)) {
    const val = yesNoValue(field, answers.military_service);
    if (val !== null) return { value: val, source: "answers.military_service" };
  }

  // ---- U.S. sanctions / export-control declarations --------------------------------------------
  // The options name residents or citizens of Cuba, Iran, North Korea, Syria, the occupied Ukrainian regions and
  // Russia/Belarus. A UK-resident national of any non-sanctioned country is none of those;
  // refuses to answer if the nationality on file ever were one of them.
  if (field.type === "checkbox-group" && field.options && field.options.length && /sanctions|export controls?/i.test(lower)) {
    if (SANCTIONED_RE.test(String(answers.nationality || ""))) return null;
    const none = pickOption(field.options, [/^none of the above/i, /^none of these/i]);
    if (none) return { value: [none], source: "answers.nationality (not a sanctioned jurisdiction) + UK residence" };
  }
  // its follow-up ("If you selected a response other than 'none of the above' ...") has a Not-applicable option
  if (field.type === "checkbox-group" && field.options && field.options.length && /other than .?none of the above.?/i.test(lower)) {
    // two steps: pickOption returns the first OPTION matching any pattern, and "None of these" is listed first
    const na = pickOption(field.options, [/^not applicable/i, /^n\/?a\b/i]) || pickOption(field.options, [/^none of these/i]);
    if (na) return { value: [na], source: "follows the sanctions answer ('none of the above')" };
  }

  // ---- EEO / protected characteristics ------------------------------
  if (EEO_RE.test(lower) || (field.options && field.options.some((o) => EEO_OPTIONS_RE.test(String(o))))) {
    return eeoValue(field);
  }

  // ---- criminal convictions ------------------------------------------
  if (/criminal conviction|unspent conviction/i.test(lower)) {
    const val = yesNoValue(field, answers.criminal_convictions);
    if (val === null) return null;
    return { value: val, source: "answers.criminal_convictions" };
  }

  // ---- background check ------------------------------------------------
  if (/background check/i.test(lower)) {
    const val = yesNoValue(field, answers.background_check);
    if (val === null) return null;
    return { value: val, source: "answers.background_check" };
  }

  // ---- relocation ------------------------------------------------------
  if (/willing to relocate|relocation/i.test(lower)) {
    const val = yesNoValue(field, answers.relocation && answers.relocation.willing);
    if (val === null) return null;
    return { value: val, source: "answers.relocation.willing" };
  }

  // ---- visa expiry (asked separately from status/type) -----------------
  if (/visa expir|visa valid until/i.test(lower)) {
    if (!answers.visa || !answers.visa.expires) return null;
    return { value: answers.visa.expires, source: "answers.visa.expires" };
  }
  // ---- cover letter essay textarea (some ATS use a text box, not upload) --
  // A "why this company/role" or "motivation" question is NOT a cover-letter
  // request even though it's free text -- it's a fact-grounded essay answered
  // via the answer bank (bankLookup above) or left unanswered to queue, never
  // silently overwritten with the cover letter text.
  // "Note to Hiring Manager" / "Message to the Hiring Team" (Point72, SmartRecruiters): a covering note,
  // not a motivation question -- the audited cover letter is exactly what it asks for.
  if (field.type === "textarea"
      && (/cover letter/i.test(lower) || /\b(note|message|letter|comments?)\s+(to|for)\s+(the\s+)?(hiring\s+(manager|team)|recruit\w*)|\bcover note\b/i.test(lower))) {
    if (!ctx.coverLetterText) return null;
    return { value: ctx.coverLetterText, source: "cover.txt" };
  }

  // "How did you hear about us?"-style question with a literal "Job board"
  // option -- always the accurate answer for this pipeline's applications
  // (see HOW_HEARD_JOB_BOARD_RE comment), so it wins before the host-name
  // guess below when that exact option is offered.
  if (["select", "radio", "combobox"].includes(field.type) && HOW_HEARD_JOB_BOARD_RE.test(lower)) {
    const jobBoardOpt = pickOption(field.options, [JOB_BOARD_OPTION_RE]);
    if (jobBoardOpt) {
      return { value: jobBoardOpt, source: "standing answer: found via job board search" };
    }
  }

  // ---- how did you hear about us ---------------------------------------
  if (/how did you (hear|find out|learn)|where did you (hear|find out|learn)/i.test(lower)) {
    if (field.type === "text" || field.type === "textarea") {
      // Free text: say where the pipeline actually found the posting (truthful for every job).
      const where = HOW_HEARD_BY_SOURCE[String((job && job.source) || "").toLowerCase()];
      return where ? { value: where, source: "job.source (where the posting was found)" } : null;
    }
    if (!["select", "radio", "combobox"].includes(field.type)) return null;
    let opt = null;
    try {
      const host = job && job.url ? new URL(job.url).hostname.replace(/^www\./, "").split(".")[0] : null;
      if (host) opt = pickOption(field.options, [new RegExp(host, "i")]);
    } catch {
      opt = null;
    }
    // A posting read straight off the company's own job board: "company website/careers page" is true.
    if (!opt && COMPANY_BOARD_SOURCES.has(String((job && job.source) || "").toLowerCase())) {
      opt = pickOption(field.options, [/company (website|site|careers)|careers? (page|site|website)|corporate website|^website$/i]);
    }
    if (!opt) opt = pickOption(field.options, [/^other$/i]);
    if (!opt) return null;
    return { value: opt, source: "job.url" };
  }

  // ---- certifications ---------------------------------------------------
  if (/certificat/i.test(lower)) {
    const certs = (facts && facts.certifications) || [];
    const m = label.match(/([A-Za-z][A-Za-z0-9.+#-]*)\s+certificat/i);
    const keyword = m ? m[1].trim().toLowerCase() : null;
    const has = keyword ? certs.some((c) => (c.name || "").toLowerCase().includes(keyword)) : certs.length > 0;
    const val = yesNoValue(field, has);
    if (val === null) return null;
    return { value: val, source: "facts.certifications" };
  }

  // ---- Greenhouse-style education fields (school/degree/discipline/end date) --
  if (/^school$|school name|^institution$|^university$|^degree$|degree\/?qualification|discipline|field of study|^major$|end (date\s*)?month|graduation month|end (date\s*)?year|graduation year/i.test(lower)) {
    const edu = facts && Array.isArray(facts.education) && facts.education[0];
    if (!edu) return null;
    if (/^school$|school name|^institution$|^university$/i.test(lower)) {
      return { value: edu.institution, source: "facts.education[0].institution" };
    }
    if (/^degree$|degree\/?qualification/i.test(lower)) {
      const levelWord = /msc|master/i.test(edu.degree) ? "master" : /bsc|bachelor/i.test(edu.degree) ? "bachelor" : null;
      if (["select", "combobox", "radio"].includes(field.type)) {
        // Match the "<level>'s" form specifically (e.g. "Master's
        // Degree") -- a bare "master" substring match would also hit an
        // unrelated option like "Master of Business Administration
        // (M.B.A.)", which sorts earlier in some option lists and would
        // otherwise win first.
        if (!levelWord) return null;
        const opt = pickOption(field.options, [new RegExp(`${levelWord}['\u2019]?s\\b`, "i")]);
        if (!opt) return null;
        return { value: opt, source: "facts.education[0].degree" };
      }
      return { value: edu.degree, source: "facts.education[0].degree" };
    }
    if (/discipline|field of study|^major$/i.test(lower)) {
      if (!["select", "combobox", "radio"].includes(field.type)) return null;
      const decision = disciplineOption(field.options, edu);
      if (!decision) return null;
      return decision;
    }
    const dateMatch = edu.dates && edu.dates.match(/([A-Za-z]+)\s+(\d{4})/);
    if (!dateMatch) return null;
    if (/end (date\s*)?month|graduation month/i.test(lower)) {
      if (["select", "combobox"].includes(field.type)) {
        const opt = pickOption(field.options, [new RegExp(`^${dateMatch[1]}`, "i")]);
        if (!opt) return null;
        return { value: opt, source: "facts.education[0].dates" };
      }
      return { value: dateMatch[1], source: "facts.education[0].dates" };
    }
    if (/end (date\s*)?year|graduation year/i.test(lower)) {
      return { value: dateMatch[2], source: "facts.education[0].dates" };
    }
    return null;
  }

  // ---- current company / job title ---------------------------------------
  const currentExp = () => (facts && Array.isArray(facts.experience)
    ? facts.experience.find((e) => /present/i.test(e.dates || "") && e.employment !== "partnership")
    : null);
  if (textLike && /current(?:\s+or\s+(?:previous|most recent|more recent|latest))?\s+(company|employer|organi[sz]ation)|who is your current/i.test(lower)) {
    const exp = currentExp();
    if (!exp) return null;
    return { value: exp.org, source: "facts.experience" };
  }
  if (textLike && /current(?:\s+or\s+(?:previous|most recent|more recent|latest))?\s+(job\s+)?(title|role|position)\b/i.test(lower)) {
    const exp = currentExp();
    if (!exp || !exp.role) return null;
    return { value: exp.role, source: "facts.experience (current role)" };
  }

  // ---- facts that settle a plain yes/no ---------------------------------------------------------------
  const choiceLike = ["radio", "select", "combobox", "checkbox-group"].includes(field.type);
  // "Are you currently a student?" -> facts.education: every entry is completed (MSc finished December 2025).
  if (choiceLike && !/graduat/i.test(lower)
      && /\b(are you|currently|presently)\b[^?]{0,30}\b(a |an )?(current )?(student|studying|enrolled)\b|\bcurrent(ly)? (a )?student\b/i.test(lower)) {
    const edu = (facts && facts.education) || [];
    if (!edu.length) return null;
    const studying = edu.some((e) => /\b(present|current|ongoing|expected|in progress)\b/i.test(String(e.dates || "")));
    const val = yesNoValue(field, studying);
    return val === null ? null : { value: val, source: "facts.education (every entry completed)" };
  }
  // "Do you have any recruiting timelines / offer deadlines we should be aware of?" -> answers.recruiting_deadlines (he has none).
  if (choiceLike && /recruit(ing|ment)? timelines?|(other|competing) (offers?|processes)|offer deadlines?|deadlines? we should be aware/i.test(lower)) {
    if (typeof answers.recruiting_deadlines !== "boolean") return null;
    const val = yesNoValue(field, answers.recruiting_deadlines);
    return val === null ? null : { value: val, source: "answers.recruiting_deadlines" };
  }
  // "Do you require a visitor visa to enter the UK for your interviews?" -> he lives in the UK with the right to work.
  if (choiceLike && /visitor visa|visa to enter the uk/i.test(lower) && /interview/i.test(lower)) {
    if (!toBool(answers.based_in_uk) || !toBool(answers.right_to_work)) return null;
    const no = (field.options && field.options.length) ? pickOption(field.options, [/^no\b/i]) : "No";
    if (!no) return null;
    return { value: field.type === "checkbox-group" ? [no] : no, source: "answers.based_in_uk + right_to_work (lives in the UK)" };
  }

  // ---- history with THIS company (conflict-of-interest / rehire questions) ---------------------------
  // "Have you ever been employed by Spotify?" / "Do you currently or have you previously worked for
  // Databricks?" -> facts.experience (his employers are a closed list); "have you applied to us before?"
  // -> the ledger (order.job.previously_applied). Only questions that NAME this company (or say
  // "us"/"this company") qualify: "worked with Python" is a skill question, not this one.
  const thisCompany = String((job && job.company) || "").trim();
  const companyWord = thisCompany.split(/[\s,.(&-]+/).filter((w) => w.length > 2)[0] || "";
  const escapedWord = companyWord.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const namesCompany = (escapedWord && new RegExp(`\\b${escapedWord}\\b`, "i").test(label))
    || /\b(us|our (company|group|organi[sz]ation)|this company|the company)\b/i.test(lower);
  if (namesCompany && ["radio", "select", "combobox", "checkbox-group"].includes(field.type)) {
    if (/\b(applied|application)\b[^?]*\b(before|previously|in the past)\b|\b(previously|ever|before)\b[^?]*\bapplied\b/i.test(lower)) {
      if (!job || typeof job.previously_applied !== "boolean") return null;
      const val = yesNoValue(field, job.previously_applied);
      return val === null ? null : { value: val, source: "ledger (applications to this company)" };
    }
    // "Have you interviewed with Jane Street before?": only a ledger with no application to them proves "No".
    if (/\binterview(ed)?\b[^?]*\b(before|previously|in the past)\b|\b(previously|ever)\b[^?]*\binterview(ed)?\b/i.test(lower)) {
      if (!job || job.previously_applied !== false) return null;
      const val = yesNoValue(field, false);
      return val === null ? null : { value: val, source: "ledger (never applied to this company)" };
    }
    if (/\b(worked|work|been employed|employed|employee|contractor)\b[^?]*\b(for|at|by|with)\b|\b(previous(ly)?|former|ever)\b[^?]*\b(employee|employed|worked)\b/i.test(lower)) {
      const mine = !!escapedWord && ((facts && facts.experience) || []).some((e) => new RegExp(`\\b${escapedWord}`, "i").test(e.org || ""));
      if (mine) return null; // he did work there: never answered by rule
      const val = yesNoValue(field, false);
      return val === null ? null : { value: val, source: "facts.experience (no role at this company)" };
    }
  }

  // ---- start date / availability (free text only) --------------------------
  // "When can you start?" is answered by the standing notice period. A year/month sub-field of an
  // education block ("Start date year") is a different question and is never matched here.
  if ((field.type === "text" || field.type === "textarea" || field.type === "date")
      && /when (can|could|would) you (be able to )?(start|join)|earliest (possible )?start|available to start|availability to start|availability to join|able to join us|start date|how soon can you (start|join)|when are you available/i.test(lower)
      && !/\b(year|month|day)\b/i.test(lower)) {
    if (!answers.notice_period) return null;
    if (field.type === "date") {
      const iso = startDateAfterNotice(answers.notice_period);
      return iso ? { value: iso, source: "answers.notice_period (today + notice)" } : null;
    }
    return { value: `I have a ${answers.notice_period} notice period.`, source: "answers.notice_period" };
  }

  // ---- "Please select the region you are currently located in" (Squarepoint: AMER / APAC / EMEA): he lives in the UK ---------
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length
      && /\bregion\b[^?]{0,40}\b(currently )?(located|based|resid)/i.test(lower) && toBool(answers.based_in_uk)) {
    const opt = pickOption(field.options, [/^emea\b/i, /\beurope\b|\bemea\b|\buk\b|united kingdom/i]);
    if (opt) return { value: opt, source: "answers.based_in_uk (the UK is in EMEA / Europe)" };
  }
  // ---- English level (Shift Technology: "Beginner (A1-A2) ... Advanced (C1-C2) / Fluent/Native") -------------------------------
  if (["select", "combobox", "radio"].includes(field.type) && field.options && field.options.length && answers.english_level
      && /\b(level|proficiency|fluency|standard)\b[^?]{0,25}\benglish\b|\benglish\b[^?]{0,25}\b(level|proficiency|fluency|standard)\b|how (well|fluent)[^?]{0,25}\benglish\b/i.test(lower)) {
    const want = String(answers.english_level).toLowerCase();
    const opt = pickOption(field.options, want === "fluent"
      ? [/\bfluent\b|proficient/i, /\badvanced\b|\bc1\b/i]
      : [/\badvanced\b|\bc1\b/i, /\bfluent\b(?!\s*\/\s*native)|proficient|full professional/i]);
    if (opt) return { value: opt, source: "answers.english_level" };
  }
  // ---- hours of coding experience (Apollo Research: "<100 hours / 100-1000 hours / >1000 hours") ----------------------------
  const hoursValue = codingHours(field, answers);
  if (hoursValue) return hoursValue;

  // ---- his stated years, the startup answer, then a fact-grounded Yes (see the helpers above) ----------------
  const statedValue = statedYears(field, answers);
  if (statedValue) return statedValue;
  if (["radio", "select", "combobox"].includes(field.type) && typeof answers.startup_experience === "boolean"
      && /\b(worked|work|employed|experience)\b[^?]{0,40}\bstart-?ups?\b|\bstart-?ups?\b[^?]{0,40}\b(before|previously|experience)\b/i.test(lower)) {
    const val = yesNoValue(field, answers.startup_experience);
    if (val !== null) return { value: val, source: "answers.startup_experience" };
  }
  const groundedYes = factGroundedYes(field, facts);
  if (groundedYes !== null) return { value: groundedYes, source: "facts (every term of the question is evidenced)" };

  // ---- years of experience ------------------------------------------
  if (/years?\s+or more\s+[^.?!]*\bexperience|years?\s+(of\s+)?experience/i.test(lower)) {
    return yearsExperienceValue(field, answers, facts);
  }

  return null;
}

// ------------------------------------------------------- pre-submit gate ---
// Every control on the form is snapshotted immediately before submit (or at
// the end of a dry run) -- not just the ones this pipeline filled -- so an
// ATS autofilling a field from the uploaded CV is captured and checked,
// never silently trusted. See DESIGN.md §4. Pure/offline-testable so the
// gate itself is unit-tested without a browser.

// The same phone number as a widget shows it. UK widgets drop the trunk 0 ("07700 900123" -> "7700 900123") or take it as an
// international number ("+447700900123", even "+07700900123"): some forms block at the phone box on a raw digit compare.
export function samePhone(a, b) {
  const national = (s) => {
    let d = String(s == null ? "" : s).replace(/[^\d+]/g, "").replace(/^\+/, "");
    if (d.startsWith("0044")) d = d.slice(2);
    if (d.startsWith("44") && d.length >= 12) d = d.slice(2);
    return d.replace(/^0+/, "");
  };
  const x = national(a);
  return x !== "" && x === national(b);
}

function normalizeForSubstring(s) {
  return String(s == null ? "" : s).toLowerCase().replace(/\s+/g, " ").trim();
}

// What a select/combobox shows while nothing is chosen ("Select...", "Please select", "-- Choose --"): not an answer, and not an
// ATS autofill either (Spotify/Lever: an untouched optional select held the form as an "unverifiable autofilled value").
const PLACEHOLDER_RE = /^[\s\-–—]*(please\s+)?(select|choose|pick)(\s+an?\s+(option|one|answer))?[\s.…:\-–—]*$/i;

function isEmptySnapshotValue(v) {
  if (v === null || v === undefined || v === "" || v === false) return true;
  if (Array.isArray(v)) return v.every((x) => x === "" || (typeof x === "string" && PLACEHOLDER_RE.test(x)));
  return typeof v === "string" && PLACEHOLDER_RE.test(v);
}

// Flattens every string/number/boolean leaf out of facts.yaml/answers.yaml
// into one lowercase, whitespace-normalised haystack, so an ATS-autofilled
// value can be checked as a substring of "anything we're allowed to have
// said" without re-deriving every possible phrasing/field path.
export function buildFactsAnswersHaystack(facts, answers) {
  const parts = [];
  const walk = (v) => {
    if (v === null || v === undefined) return;
    if (typeof v === "string") { parts.push(v); return; }
    if (typeof v === "number" || typeof v === "boolean") { parts.push(String(v)); return; }
    if (Array.isArray(v)) { v.forEach(walk); return; }
    if (typeof v === "object") { Object.values(v).forEach(walk); return; }
  };
  walk(facts);
  walk(answers);
  return normalizeForSubstring(parts.join(" \u241E "));
}

// Decides one snapshot row's provenance. `filledSourceByLabel` is the
// source string apply.mjs recorded for every field THIS pipeline set
// during this run (keyed by label, matching the label-based re-match
// convention used everywhere else in this file after a DOM remount).
export function classifySnapshotEntry(field, value, { filledSourceByLabel, ctx, haystack }) {
  if (filledSourceByLabel && filledSourceByLabel.has(field.label)) {
    return { filled_by: "pipeline", source: filledSourceByLabel.get(field.label) };
  }
  if (isEmptySnapshotValue(value)) {
    return { filled_by: "untouched", source: null };
  }
  // A site's own UI-language selector (observed 2026-10-02: Reply's careers page, autofilled "English
  // US (English US)") is not a statement about the applicant, and where a form does ask which
  // language, English is true of someone applying with an English-taught MSc. Any OTHER language
  // autofill, or a required non-English one, still falls through to the verification below.
  if (/^(preferred\s+)?(language|locale)$/i.test(String(field.label || "").trim())
      && ["combobox", "select"].includes(field.type) && !field.required
      && /^english\b/i.test(String(Array.isArray(value) ? value[0] : value).replace(/[\u200e\u200f]/g, "").trim())) {
    return { filled_by: "ats_autofill", source: "ui language selector (English; not an applicant claim)" };
  }
  // Non-empty, but not something we set this run. If classifyField produces a
  // value for this field, the form's actual value must EQUAL it to count as
  // verified-ours-equivalent (an ATS that prefilled exactly what we would
  // have written) -- recorded honestly as ats_autofill, sourced from the rule
  // that endorses it. A DIFFERENT value (a wrong autofill) must never be
  // waved through as "pipeline": it falls through to the facts/answers
  // haystack check and issues if unverifiable, so the gate blocks it.
  const decision = classifyField(field, ctx);
  if (decision) {
    const phoneish = field.type === "tel" || /phone|mobile|telephone/i.test(String(field.label || ""));
    const same = phoneish ? samePhone(value, decision.value) : canonicalJson(value) === canonicalJson(decision.value);
    if (same) {
      return { filled_by: "ats_autofill", source: decision.source };
    }
  }
  const valueText = normalizeForSubstring(Array.isArray(value) ? value.join(" ") : value);
  const verified = valueText.length > 0 && haystack.includes(valueText);
  return verified
    ? { filled_by: "ats_autofill", source: null }
    : { filled_by: "ats_autofill", source: null, issue: true };
}

// Canonical JSON stringify (recursively sorts object keys; array element
// order is preserved -- these are answer scalars/lists, never reordered
// collections) so two equivalent-but-differently-ordered JSON values
// deep-equal.
function canonicalJson(v) {
  if (Array.isArray(v)) return "[" + v.map(canonicalJson).join(",") + "]";
  if (v && typeof v === "object") {
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonicalJson(v[k])).join(",") + "}";
  }
  return JSON.stringify(v);
}

// Submit-mode gate: every approved label's value must equal what was
// actually filled THIS run (deep-equal after JSON normalisation), and every
// field filled this run must have been part of what was approved -- a field
// that showed up now but wasn't covered by the approval must never slip
// through silently. Returns the mismatched labels (empty = gate passes).
export function checkApprovedAnswers(approvedAnswers, currentFields) {
  if (!approvedAnswers || !approvedAnswers.length) return { ok: true, mismatched: [] };
  const approvedMap = new Map(approvedAnswers.map((a) => [a.label, a.value]));
  const mismatched = [];
  for (const f of currentFields || []) {
    if (!approvedMap.has(f.label)) {
      mismatched.push(f.label);
      continue;
    }
    if (canonicalJson(approvedMap.get(f.label)) !== canonicalJson(f.value)) {
      mismatched.push(f.label);
    }
  }
  return { ok: mismatched.length === 0, mismatched };
}
