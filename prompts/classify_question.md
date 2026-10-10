You classify one unmapped application-form question. Output ONLY a JSON
object, nothing else — no preamble, no markdown fence.

Form question: {{question}}
Options (if this is a choice field): {{options}}
Job posting (title + description, for judging "fact_answerable"):
{{title}} — {{description}}

Classify into exactly one category:

- `personal`: opinions, values, preferences and plans that no fact can state --
  "what are you looking for in your next role", "where do you see yourself in
  five years", "your biggest strength / weakness", "which of our values
  resonates with you and why", salary beyond the standing band,
  nationality/citizenship/identity/demographics, or anything that needs
  information not in facts.yaml (e.g. "have you used <the company's product>").
- `fact_answerable`: answerable from the facts a candidate's CV would contain
  plus the posting text. This INCLUDES "why do you want to work at <company>" /
  "why this role" / "what interests you about this position": the answer
  restates what the posting itself asks for and the facts that match it, and
  never claims a feeling (a later audit rejects any claim a fact does not
  support). Also "describe a project / your most impactful work", "describe
  your experience with X" where X is evidenced in facts.yaml.

(An earlier version of this prompt listed "why do you want this role" under
`personal` AND "why this role" under `fact_answerable`; the same question was
routed both ways and motivation questions were held for a human for no reason.)

When genuinely unsure, prefer `personal` -- a fact_answerable draft that cannot
pass the audit just waits for the operator, while a wrongly-fact_answerable opinion
could invent a view, which is never acceptable.

Output:

```json
{"category": "personal"|"fact_answerable", "reason": "..."}
```

Output the JSON object now.
