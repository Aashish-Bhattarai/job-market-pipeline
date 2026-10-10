You draft the permanently audited BASE cover letter core for the candidate:
two or three fact-grounded body paragraphs that work unchanged for
ANY UK machine learning / AI / data science role. Output ONLY a JSON object,
nothing else — no preamble, no markdown fence.

Facts you may use (id, text) — the ONLY source of truth; restate, never add:
{{facts}}

Rules (apply verbatim, from the cover template):
{{rules}}

What to produce:
- "core_paragraphs": exactly 2–3 paragraphs, 50–75 words each. Each one is
  built entirely from ONE OR TWO related facts from the list, restated in
  plain confident first person ("At a previous role I built a document-
  intelligence pipeline that routed scanned PDFs through an LLM extraction
  step..."). No company- or role-specific
  framing — these paragraphs must read naturally for any employer in the
  field. Cite the exact fact_id(s) used for each paragraph. Prefer covering
  different strengths across paragraphs (applied ML platform work; safe LLM
  engineering; document/vision systems) rather than three variants of one.
- "close": exactly {"text": "My notice period is one month.", "kind":
  "close", "fact_ids": []}.
- Every sentence must be defensible solely from its cited fact text: no added
  metrics, no scale words the fact lacks ("production", "at scale", "led"),
  no job titles, no analogies. British spelling. No em dashes. Vary sentence
  length. Banned words include: passionate, thrilled, leverage, cutting-edge,
  dynamic, utilize, facilitate, empower, seamlessly, delve,
  "not only... but also".

Output shape:

```json
{"core_paragraphs": [{"text": "...", "fact_ids": ["greenwich-ra.validation"]}],
 "close": {"text": "My notice period is one month.", "kind": "close", "fact_ids": []}}
```

Output the JSON object now.
