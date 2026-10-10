You write one fact-grounded answer to a job application question. Output
ONLY a JSON object, nothing else — no preamble, no markdown fence.

Question: {{question}}

Job posting (for context on what the question is really asking, and to
restate the role/team's own stated mission where relevant):
{{title}} — {{description}}

Facts you may draw on — cite only what is here, restate it, never add to it
(id, text):
{{facts}}

An adversarial auditor reads every sentence against the facts and the posting.
It has rejected essays for the same few habits again and again, so follow this
STRUCTURE exactly and nothing else:

1. ONE opening sentence about the employer: "Your job description says ..."
   followed by ONE thing the posting states about the role, team or company,
   in the posting's own words. Never join two separate statements of the
   posting into one new phrase (a posting that mentions "security" and
   "reliability" does not mention "secure and reliable guardrails"). Never add
   an adjective or a noun the posting does not use.
2. Two to four sentences that each start "I " and restate exactly ONE cited
   fact in plainer words, keeping the fact's own scale words and numbers and
   adding nothing (no "production", "at scale", "led", "owned", "agent",
   "platform" unless the fact itself says so).
3. Stop. Do NOT write a closing or summary sentence.

NEVER write any of these (each has failed the audit): a sentence that links
the posting to the candidate ("matches", "aligns with", "fits", "relevant to",
"the kind of work", "similar to", "same as", "which is why"); any feeling or
preference ("excited", "interested", "enjoy", "passionate", "keen", "love");
a pattern or generalisation across several facts ("across these", "a common
thread", "the pattern"); the words "facts", "cited", "posting" or "the facts
below" (say "your job description"); a figure, skill or outcome that is not in
the cited fact. If the question asks why he is interested, answer it by the
structure above: what the role involves, then what he has built. Stating
evidence IS the answer; do not claim motivation.

Example of the shape (content is illustrative only, never copy it):
"Your job description says the team builds tools that let customers query
their own data in plain English. I built a database layer that lets customers
query their own database in plain English, restricted to read-only access. I
also routed plain English questions to deterministic metric functions instead
of letting a model compute figures."

British English spelling, no em dashes, 50 to 130 words.

Output:

```json
{"answer": "...", "fact_ids": ["...", "..."]}
```

Previous attempt (if any) and why the auditor rejected it — fix these
exactly, without weakening the rest:
{{errors}}

`fact_ids` must be non-empty and every id must be one listed above. Output
the JSON object now.
