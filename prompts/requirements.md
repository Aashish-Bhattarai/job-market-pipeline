You extract structured requirements from a job posting. Output ONLY a JSON
list, nothing else — no preamble, no markdown fence.

Job title: {{title}}

Job description (verbatim):
{{description}}

Facts you may cite (id, text, tags — an id is "citable" only if it appears
here):
{{facts}}

Previous attempt (if any) and why it was rejected — fix these exactly:
{{errors}}

For every requirement or preference the posting states, output one object:

```json
{"text": "short paraphrase of the requirement",
 "quote": "verbatim span copied character-for-character from the description above",
 "kind": "skill_tool|experience_years|degree|clearance|phd|location|language|soft_skill|attitude|duty|other",
 "required": true|false,
 "years": <number>|null,
 "fact_ids": ["<id from the facts list above>", "..."]}
```

Non-negotiable rules (code re-checks every one of these; a violation fails
the whole extraction):

1. `quote` MUST be copied verbatim from the description — the exact
   characters, not a paraphrase or summary. Copy-paste it, don't retype it.
2. `kind` MUST be exactly one of: skill_tool, experience_years, degree,
   clearance, phd, location, language, soft_skill, attitude, duty, other.
3. `required` is `true` only for a must-have; `false` for "nice to have" /
   "bonus" / preferred items.
4. `years` is a number only for `kind: experience_years` (the minimum years
   asked for); `null` for every other kind.
5. `fact_ids` may ONLY contain ids that appear in the facts list above.
   Cite every fact that directly evidences this requirement; leave the list
   empty `[]` if nothing in the facts list evidences it (do not invent or
   guess an id).
6. `kind: attitude` is for non-CV items like "strong desire to...",
   "comfortable in a fast-paced environment", "willing to travel" — these
   never need fact_ids.
7. `kind: duty` is for what the posting says about the ROLE rather than about the
   candidate: a responsibility ("you will design and ship...", "partner with
   customers..."), a hiring-process step ("take-home task", "pair-programming
   exercise", "one application per year"), a benefit, or a description of the
   team or company. A duty never needs fact_ids and never counts against the
   candidate. If the posting makes something a CONDITION the candidate must
   already meet (a tool or skill, years, a degree, being a current student, a
   contract / part-time / freelance arrangement, a fixed start window), it is
   NOT a duty: give it its real kind, or `other`.
8. Cover every requirement stated in the posting, must-have and nice-to-have
   alike; don't skip nice-to-haves.

Example (illustrative only, do not copy its content):

```json
[{"text": "2+ years Python", "quote": "2+ years of professional Python experience",
  "kind": "experience_years", "required": true, "years": 2, "fact_ids": []},
 {"text": "MSc or equivalent", "quote": "MSc in a quantitative field", "kind": "degree",
  "required": true, "years": null, "fact_ids": ["edu-msc"]}]
```

Output the JSON list now.
