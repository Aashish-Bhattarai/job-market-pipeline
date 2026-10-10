"""Stage `fit_rules`: pure-code FIT decision from the latest requirements
extraction (RUNBOOK §3 rubric, carried forward verbatim). No model call.

Usage: python -m stages.fit_rules <job_id> [--db path]
"""
from __future__ import annotations

import argparse
import functools
import json
import re
import sys

from stages.common import connect, get_job, set_job_status
from ledger import core
from discover.filter import SUSSEX_TOWNS, UK_CITIES, load_geography

# RUNBOOK §3: pass only if degree requirements are met, professional
# experience required is <= 1 year FTE, no clearance/PhD/non-UK location,
# every required skill/tool/degree/language is fact-mapped. Soft skills
# ("resilience", "creativity"), attitude/availability items and non-required
# ("nice to have") items never block, regardless of kind.
_UK_RE = re.compile(r"\b(uk|u\.k\.|united kingdom|britain|british|london|england|scotland|"
                     r"wales|northern ireland)\b", re.I)
_REMOTE_RE = re.compile(r"\bremote\b", re.I)

_NEEDS_FACT_KINDS = {"skill_tool", "degree", "language", "other"}

# "No visa sponsorship offered", "relocation not available", "must have UK
# work authorisation" etc. — statements about the employer's sponsorship
# stance never block (Graduate visa, no sponsorship needed until 2028).
_VISA_RE = re.compile(r"\b(visa|sponsorship|work (permit|authorisation|authorization)|"
                      r"(authori[sz]ed|eligible|entitled|legally (able|allowed)) to work|"
                      r"right to work|relocation)\b", re.I)


# An `experience_years` item with no number ("Experience conducting large scale A/B experiments") is a claim
# like any tool and needs a fact; a level statement or a maximum is not a claim.
_NOT_A_CLAIM_RE = re.compile(
    r"\b(entry|junior|mid)[- ]?level\b|\bgraduate\b|\bearly[- ]career|"
    r"\bno (prior |previous )?(professional |industry )?experience\b|\bno more than\b|\bat most\b|\bup to\b", re.I)

_ENGLISH_RE = re.compile(r"\benglish\b", re.I)

# The extractor sometimes files job ATTRIBUTES as required `other` items ("100% remote role", "Permanent full-time role",
# "No agencies or recruiters", "Start ASAP"): nothing he must evidence. A real condition that excludes him (contract,
# freelance, fixed-term, equity-only, project basis, part-time...) is never benign.
_BENIGN_OTHER_RE = re.compile(
    r"(100%|fully|completely)\s+remote|remote[- ](first|role|position|working)|\bhybrid\b|\bon-?site\b|\bno (agenc|recruiter)|"
    r"\bpermanent\b|\bfull[- ]?time\b|\bstart (asap|immediately)\b|\bimmediate start\b", re.I)
_NOT_BENIGN_RE = re.compile(
    r"\b(contract|freelanc|fixed[- ]term|temporary|equity[- ]only|unpaid|day rate|project basis|part[- ]time|internship|volunteer)\b", re.I)

# A skill_tool line worded as a RESPONSIBILITY or a personal trait ("Continuously improve existing algorithms", "Ensure
# reliability, monitoring, and observability", "Can grasp advanced engineering concepts") that names no tool. A line that
# names one ("Build event-driven architectures with Apache Kafka", "Design Grafana dashboards") stays a skill.
_DUTY_OPENER_RE = re.compile(
    r"^(continuously|ensure|support|defin(e|ing)|implement(ing)?|own(ing)?|drive|driving|lead(ing)?|collaborat\w+|build(ing)?|"
    r"design(ing)?|develop(ing)?|maintain(ing)?|improv(e|ing)|identify|deliver(ing)?|manag(e|ing)|contribut\w+|partner(ing)?|"
    r"mentor\w*|help(ing)?|creat(e|ing)|translat\w+|communicat\w+|comfortable|can grasp|ability to (work|collaborate|"
    r"communicate|learn|adapt))\b", re.I)
_PLAIN_CAPS = {"ML", "AI", "LLM", "LLMs", "NLP", "API", "APIs", "UK", "EU", "QA", "KPI", "KPIs", "R&D", "ETL", "ELT", "CI", "CD", "SQL"}


def _is_duty_phrased(text: str) -> bool:
    t = (text or "").strip()
    if not _DUTY_OPENER_RE.search(t):
        return False
    for tok in re.findall(r"[A-Za-z0-9+#.&/-]+", t)[1:]:
        if tok in _PLAIN_CAPS:
            continue
        if re.search(r"[0-9+#]", tok) or re.fullmatch(r"[A-Z][A-Za-z0-9]*", tok) or re.fullmatch(r"[A-Z]{2,}", tok):
            return False        # capitalised word / acronym / C++ / .NET: a named tool
    return True


# "An undergraduate, postgraduate or PhD student with practical experience ..." (Jane Street's summer internship). The extractor
# mapped it onto his MSc facts, but the MSc was completed in December 2025: he is not a student. A "recent graduate" / "new grad"
# alternative in the same line keeps such roles open to him.
_STUDENT_ONLY_RE = re.compile(
    r"\b(currently|must be|are|be)\s+(an?\s+)?(enrolled|studying)\b|\bcurrent(ly)?\s+(an?\s+)?student\b|"
    r"\b(undergraduate|postgraduate|masters?|phd|bachelors?)\b[^.;]{0,40}\bstudents?\b|\binternship (programme|program)\b|\bstudents? only\b", re.I)
_GRADUATE_OK_RE = re.compile(r"recent graduate|new[- ]grad|\bgraduated\b|\bgraduates?\b(?!\s+students?)", re.I)


@functools.lru_cache(maxsize=1)
def _studying_now() -> bool:
    try:
        from stages.common import load_bank
        edu = load_bank().education or []
    except Exception:
        return False
    return any(re.search(r"\b(present|current|ongoing|expected|in progress)\b", str(e.get("dates", "")), re.I) for e in edu)


def _is_student_only(text: str) -> bool:
    return bool(_STUDENT_ONLY_RE.search(text or "")) and not _GRADUATE_OK_RE.search(text or "") and not _studying_now()


def _is_benign_attribute(text: str) -> bool:
    t = text or ""
    return bool(_BENIGN_OTHER_RE.search(t)) and (not _NOT_BENIGN_RE.search(t) or bool(re.search(r"permanent (contract|role|position)", t, re.I)))


@functools.lru_cache(maxsize=1)
def _max_required_years() -> float:
    """Most years a required minimum may ask for. The operator's technology stack has been in use
    since their degree began (study, projects, a short contract role, a current role), so "we can mention
    experience as more" -- config.yaml `fit.max_required_years`, 3 today. Falls back to the original 1."""
    try:
        from ledger import llm
        return float(((llm.load_config().get("fit") or {}).get("max_required_years")) or 1)
    except Exception:
        return 1.0


# The same skill under the names job posts use. Only genuine synonyms of what his CV already lists: his RAG work IS
# retrieval over embeddings ("vector search" failed him), Gemini/Anthropic/Ollama IS LLM-API experience, Docker IS
# containerisation, FastAPI IS REST-API work. Product-specific asks (Pinecone, Jenkins, Azure, Terraform...) are NOT here.
_SKILL_SYNONYMS = {
    "rag": ["retrieval-augmented", "retrieval augmented", "retrieval systems", "retrieval system", "vector search",
            "vector database", "vector databases", "vector store", "vector stores", "embeddings", "semantic search"],
    "agents": ["agentic", "multi-agent", "ai agents", "llm agents", "agent frameworks", "tool calling"],
    "lora fine-tuning": ["fine-tuning", "fine tuning", "finetuning", "peft"],
    "transformers": ["transformer models", "transformer architectures", "attention mechanisms"],
    "anthropic": ["llm", "llms", "large language model", "large language models", "llm apis", "prompt engineering",
                  "foundation models", "generative ai", "genai"],
    "docker": ["containerization", "containerisation", "containerized", "containerised", "containers"],
    "kubernetes": ["k8s", "container orchestration"],
    "pytest": ["unit testing", "unit tests", "automated testing", "test automation", "testing frameworks"],
    "ci/cd": ["continuous integration", "continuous delivery", "continuous deployment", "ci pipeline", "ci pipelines"],
    "fastapi": ["rest api", "rest apis", "restful api", "restful apis", "api development", "api design"],
    "postgresql": ["relational database", "relational databases", "sql database", "sql databases"],
    "mongodb": ["nosql"],
    "natural language to sql": ["text-to-sql", "text2sql", "nl2sql"],
    "aws": ["cloud platform", "cloud platforms", "cloud services", "cloud computing"],
    "evaluation harness design": ["model evaluation", "evals", "evaluation frameworks"],
}


@functools.lru_cache(maxsize=1)
def _skill_terms() -> tuple[str, ...]:
    """The skills section of facts.yaml (his CV lists them) as coverage terms.
    Terms shorter than 3 characters are excluded: a bare "R" would false-match
    "R&D" and similar fragments."""
    try:
        from stages.common import load_bank
        bank = load_bank()
    except Exception:
        return ()
    terms = set()
    for items in (bank.skills or {}).values():
        for item in items or []:
            t = str(item).strip()
            if len(t) >= 3:
                terms.add(t)
                terms.update(_SKILL_SYNONYMS.get(t.lower(), []))
    return tuple(sorted(terms, key=len, reverse=True))


def _skill_list_covers(text: str) -> bool:
    low = text.lower()
    return any(re.search(r"(?<![a-z0-9+#])" + re.escape(t.lower()) + r"(?![a-z0-9+#])", low)
               for t in _skill_terms())
_OTHER_LANG_RE = re.compile(r"\b(german|french|spanish|italian|portuguese|dutch|mandarin|cantonese|chinese|japanese|korean|"
                            r"arabic|russian|hindi|polish|swedish|danish|norwegian|finnish|turkish|hebrew)\b", re.I)


@functools.lru_cache(maxsize=1)
def _geography() -> dict:
    return load_geography()


# Regions the UK sits inside, and named places that are NOT the UK. A location requirement blocks only when it names a foreign place
# (or a UK city this geography does not accept); "in office 3 days a week" or "no travel" names no place, so it is not a geographic
# exclusion (causaLens, Xelix, Accurx, Lloyds and Omnea roles failed on exactly that).
_EMEA_RE = re.compile(r"\b(emea|europe|european|eea|eu|gmt|cet|bst)\b", re.I)
_FOREIGN_RE = re.compile(
    r"\b(usa|united states|americas?|american|canada|canadian|mexico|brazil|argentina|germany|france|spain|italy|portugal|poland|"
    r"netherlands|ireland|belgium|austria|switzerland|sweden|norway|denmark|finland|israel|india|singapore|australia|new zealand|japan|"
    r"china|hong kong|korea|philippines|uae|dubai|nigeria|kenya|south africa|apac|latam|asia|north america|new york|san francisco|"
    r"seattle|austin|boston|chicago|los angeles|toronto|vancouver|berlin|munich|paris|amsterdam|dublin|madrid|barcelona|lisbon|warsaw|"
    r"zurich|sydney|melbourne|tokyo|bangalore|bengaluru|mumbai|tel aviv)\b", re.I)
_US_CS_RE = re.compile(r"\b(US|U\.S\.A?\.?)\b")      # case-sensitive: "tell us" is not a country


def _location_blocks(text: str) -> bool:
    """True if a REQUIRED location requirement names somewhere the operator does not accept: outside
    the UK, and not a plain 'remote' posting, a Sussex town, or one of the nearby places in
    config.yaml `geography` (AI/ML roles in nearby cities are fine). Heuristic on the
    extracted requirement text (already a short paraphrase from stages/requirements.py), not full
    geocoding: mentions UK/a UK nation/city, or 'remote' with no named non-UK place, never blocks."""
    if _UK_RE.search(text) or _REMOTE_RE.search(text):
        return False
    low = text.lower()
    if any(town in low for town in SUSSEX_TOWNS):
        return False
    geo = _geography()
    places = list(geo["nearby_places"]) + (UK_CITIES if geo.get("anywhere_in_uk") else [])
    if any(re.search(rf"\b{re.escape(p)}\b", low) for p in places):
        return False
    if _EMEA_RE.search(text):
        return False        # EMEA / Europe / GMT include the UK
    if _FOREIGN_RE.search(text) or _US_CS_RE.search(text):
        return True
    # a UK city this geography does not accept (the nearby/anywhere rungs are off)
    return any(re.search(rf"\b{re.escape(c)}\b", low) for c in UK_CITIES)


def decide_fit(requirements: list[dict]) -> dict:
    """Pure rubric decision over a list of requirement dicts (each with at
    least: text, kind, required, years, fact_ids). Returns
    {"decision": "pass"|"fail", "score": float, "verdicts": [...], "reasons": [...]}."""
    verdicts = []
    blockers: list[str] = []
    total_weight = 0.0
    earned_weight = 0.0

    for req in requirements:
        kind = req.get("kind")
        required = bool(req.get("required"))
        text = req.get("text") or ""
        years = req.get("years")
        fact_ids = req.get("fact_ids") or []
        weight = 2.0 if required else 1.0
        blocks = False
        met = True

        # Visa/sponsorship/relocation statements NEVER block: the operator holds a
        # visa that needs no sponsorship right now (answers.yaml sponsorship_now: no;
        # the rubric treats "visa sponsorship statements never block" as the rule). A posting
        # that doesn't offer sponsorship is still fully applicable today.
        if _VISA_RE.search(text):
            met = True
        elif kind in ("attitude", "soft_skill", "duty"):
            # never a CV claim the auditor would pass unsupported; a duty describes the role, not the candidate
            met = True
        elif required and _is_student_only(text):
            met = False
            blocks = True
        elif kind == "language" and _ENGLISH_RE.search(text) and not _OTHER_LANG_RE.search(text):
            # "Fluent in English" (11 FIT failures on 2026-10-02 -- Jane Street, OpenAI -- had no other
            # blocker): a BSc and an MSc taught in English at UK universities, living in the UK. facts.yaml has
            # no spoken-language entry (and the writer must not gain one), so this is decided here.
            met = True
        elif kind == "clearance":
            met = not required
            blocks = required
        elif kind == "phd":
            met = not required
            blocks = required
        elif kind == "experience_years":
            # "up to N years" is a maximum, not a minimum — an early-career
            # applicant qualifies, so a cap never blocks. The years guardrail
            # (config.yaml fit.max_required_years) only guards "N+ years" minimums.
            over = (isinstance(years, (int, float)) and years > _max_required_years()
                    and not re.search(r"\bup to\b", text, re.I))
            # Live jobs passed FIT carrying "Led GenAI implementations for government agencies", "Peer-reviewed
            # publications at top ML conferences" and "A/B experiments" as required, number-less, unevidenced.
            unevidenced = required and years is None and not fact_ids and not _NOT_A_CLAIM_RE.search(text)
            met = not (required and over) and not unevidenced
            blocks = (required and over) or unevidenced
        elif kind == "location":
            loc_blocks = required and _location_blocks(text)
            met = not loc_blocks
            blocks = loc_blocks
        elif kind in _NEEDS_FACT_KINDS:
            # A required TOOL that his CV's own skills section lists ("Git", "Docker", "CI/CD", ...)
            # is defensible from the CV without a project fact -- the skills list IS CV evidence.
            mapped = bool(fact_ids) or (kind == "skill_tool" and (_skill_list_covers(text) or _is_duty_phrased(text))) \
                or (kind == "other" and _is_benign_attribute(text))
            met = mapped or not required
            blocks = required and not mapped
        else:  # unknown kind: never block, treat as met (defensive default)
            met = True

        if blocks:
            blockers.append(f"{kind}: {text}")

        total_weight += weight
        if met:
            earned_weight += weight

        verdicts.append({"text": text, "kind": kind, "required": required,
                          "met": met, "blocks": blocks})

    score = round(100.0 * earned_weight / total_weight, 1) if total_weight else 0.0
    decision = "fail" if blockers else "pass"
    return {"decision": decision, "score": score, "verdicts": verdicts, "reasons": blockers}


def run(job_id: str, db_path=None, actor: str = "code:fit_rules") -> dict:
    conn = connect(db_path)
    try:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"job_id {job_id!r} not found in jobs table")

        latest = conn.execute(
            "SELECT MAX(extraction) AS ext FROM requirements WHERE job_id=?", (job_id,)
        ).fetchone()
        extraction = latest["ext"] if latest else None
        if extraction is None:
            reason = "no requirements extraction found; run stages.requirements first"
            current = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current and current["status"] in ("fit_fail", "fit_pass"):
                # An earlier (pre-extraction, v1-era) decision stands: re-running the rubric over
                # nothing must never demote it to needs_review (a bulk re-decide did exactly that to 38
                # rows on 2026-10-02 and had to be restored from backup).
                return {"ok": False, "job_id": job_id, "reason": reason, "unchanged": True}
            with core.tx(conn):
                set_job_status(conn, job_id, "needs_review", actor, reason=reason)
            return {"ok": False, "job_id": job_id, "reason": reason}

        rows = conn.execute(
            "SELECT id, text, kind, required, years, fact_ids FROM requirements "
            "WHERE job_id=? AND extraction=? ORDER BY idx", (job_id, extraction)
        ).fetchall()
        requirements = [{"id": r["id"], "text": r["text"], "kind": r["kind"],
                          "required": bool(r["required"]), "years": r["years"],
                          "fact_ids": json.loads(r["fact_ids"])} for r in rows]

        result = decide_fit(requirements)
        status = "fit_pass" if result["decision"] == "pass" else "fit_fail"
        reason = "; ".join(result["reasons"]) if result["reasons"] else \
            f"score {result['score']}"
        fit_json = core.canonical({
            "decision": result["decision"], "score": result["score"],
            "verdicts": result["verdicts"], "requirement_ids": [r["id"] for r in requirements],
            "extraction": extraction,
        })

        with core.tx(conn):
            set_job_status(conn, job_id, status, actor, reason=reason,
                            extra_cols={"fit_score": result["score"], "fit_json": fit_json})
            core.append_event(conn, actor, "fit.decided", {
                "decision": result["decision"], "score": result["score"],
                "verdicts": result["verdicts"], "reasons": result["reasons"],
                "extraction": extraction,
            }, job_id=job_id)

        return {"ok": True, "job_id": job_id, "decision": result["decision"],
                "score": result["score"], "reasons": result["reasons"]}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply the FIT rubric to one job")
    ap.add_argument("job_id")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    try:
        result = run(args.job_id, args.db)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    if result["ok"]:
        print(f"{result['decision'].upper()}: {result['job_id']} score={result['score']} "
              f"reasons={result['reasons']}")
    else:
        print(f"NEEDS_REVIEW: {result['job_id']}: {result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
