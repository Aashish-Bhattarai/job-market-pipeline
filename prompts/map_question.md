You map one application form question to an existing answer-bank entry, if
one truly answers it. You never write a new answer — only pick an existing
id, or say none fits. Output ONLY a JSON object, nothing else — no preamble,
no markdown fence.

Form question: {{question}}
Options (if this is a choice field, the answer must equal one of these
exactly, or omit it): {{options}}

Existing answer-bank candidates for this job/company (id, question, answer):
{{candidates}}

Pick the `id` of the candidate whose `question` is asking the same thing as
the form question and whose `answer` would correctly answer it as-is (for a
choice field, only if `answer` equals one of the options above exactly). If
none of the candidates genuinely answer this question, return `bank_id: null`
— never force a near-match.

Output:

```json
{"bank_id": <id>|null, "reason": "..."}
```

Output the JSON object now.
