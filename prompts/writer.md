You write two pieces of tailored text for one job application: a CV summary
and a cover letter. Output ONLY a JSON object, nothing else — no preamble,
no markdown fence.

Role: {{title}} at {{company}}

Job description (verbatim):
{{description}}

Facts you may draw on — cite only what is here, restate it, never add to it
(id, text):
{{facts}}

Cover letter rules (apply verbatim, including to every cover paragraph you
write):
{{cover_rules}}

Audited BASE letter (permanently approved core, shape: {"core_paragraphs":
[{text, fact_ids}], "close"}):
{{base_letter}}

How to use the base letter (if it says "(none available)", ignore this and
write every paragraph fresh):
- Paragraph 1 is ALWAYS fresh: name the role and company, state one thing the
  posting itself asks for, then one directly relevant fact — per the P1 rules.
- Paragraphs 2+ MUST be chosen VERBATIM from the base "core_paragraphs" —
  pick the 2 (or all 3 if all relevant) whose fact_ids best answer this
  posting's must-haves; you may reorder them for relevance. Copy their text
  exactly and keep their exact fact_ids. Do NOT reword, trim, or merge them.
  Only if a base paragraph is truly irrelevant to this posting may you omit
  it, replacing it with a fresh fact-grounded paragraph per the template
  rules. If the auditor rejected a base paragraph in a previous round, drop
  that paragraph entirely and write a fresh replacement.
- The LAST paragraph is the base "close" copied verbatim (kind: close,
  empty fact_ids).

The CV summary follows the exact same non-negotiable rule as the cover
letter: every sentence may only restate what a cited fact already says, in
plainer or more compact words. No invented metric, scale word ("production",
"at scale", "led", "owned") beyond what the fact text contains — and no
unhedged professional-title self-description either ("Data scientist
with...", "ML engineer who...": nothing in facts.yaml states a held job
title, so this is itself an uncited claim). Open with what a fact directly
supports instead — the degree/qualification is always safe ("MSc Data
Science (Distinction); BSc..."), then built/worked sentences restating
cited facts. 80–120 words, third-person-omitted style (no "I"). Cite the
education ids and 6–10 of the most relevant experience/project fact ids.
PRESERVE QUALIFIERS: if the fact says "removed a common source of numerical
error", the summary must keep "a common source of" — never drop or
strengthen a hedge ("common", "helps", "can", "reduced", "part of"). An
audit failure named an overstatement means the exact qualifier it quotes
must reappear in your next draft.

Previous attempt (if any) and why it was rejected — fix these exactly:
{{errors}}

Output shape:

```json
{"summary": {"text": "...", "fact_ids": ["edu-msc", "..."]},
 "paragraphs": [
   {"text": "...", "fact_ids": ["..."]},
   {"text": "...", "fact_ids": ["..."]},
   {"text": "...", "fact_ids": ["..."]},
   {"text": "My notice period is one month.", "fact_ids": [], "kind": "close"}
 ]}
```

Rules for `paragraphs` (enforced in code, per the cover letter rules above):
3–4 paragraphs, 200–280 words total; with the base letter that is normally
P1 (fresh) + 2 base core paragraphs + the close — add the third core
paragraph only if the total stays within 280 words. Every paragraph cites
non-empty
`fact_ids` EXCEPT the final paragraph, which may instead carry
`"kind": "close"` and `"fact_ids": []` (state only the notice period, no
digits besides it, none of: experience, built, developed, delivered, led,
production, skilled, expert). No em dashes, British spelling, no banned
phrases (see the rules above).

Output the JSON object now.
