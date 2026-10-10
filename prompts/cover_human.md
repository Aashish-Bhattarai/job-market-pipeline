You write one cover letter, in the applicant's own first-person voice, for a company's own application form. Output ONLY a JSON object, nothing else: no preamble, no markdown fence.

Role: {{title}} at {{company}}

The company's own question for this letter:
{{question}}

The job posting (verbatim):
{{description}}

Facts you may draw on. Cite only what is here, restate it, never add to it (id, text):
{{facts}}

Reasons the applicant gave himself (he may be written as saying these, in his own first person):
{{reasons}}

INTEGRITY RULES (non-negotiable; an independent auditor checks every sentence against the facts and the posting):
1. Every sentence about the applicant restates only what a cited fact, or a stated reason, says, in plainer or more compact words. Add no number, date, job title, outcome or scale word (production, at scale, led, owned, shipped). Keep each fact's own qualifiers, for example "a common source of".
2. Say nothing about when things happened ("most recently", "currently", "last year"): the facts carry no dates.
3. Say only what the posting itself says about the role and the company, and attribute each statement to the right part of the posting. No praise of the company beyond the posting. No mention of visa, salary, relocation or Milan.
4. Answer the company's question in its order: why this role, why Bending Spoons, why he is a strong fit for both. The reasons for the role and for Bending Spoons come ONLY from the stated reasons, written plainly as his own. The fit comes from cited facts that answer the posting's responsibilities.
5. Every paragraph except the last cites at least one real fact id; it may also cite the stated reasons as user.reason-work or user.reason-ai. Write 3 or 4 paragraphs, then the close. The close is exactly: My notice period is one month. with "kind": "close" and "fact_ids": [].
6. State a fact and stop. Never write what a fact proves or shows about him: no "that is work I already do", "which shows", "exactly what you ask for", "others missed", no result a fact does not state (a fact that says a system flags extractions does not say it "keeps bad data out"), and no general claim about where he puts his attention. A fact is not evidence of a different thing (the model adapter is not evidence that he works faster).
7. About 300 words before the close, never over 340. British spelling. Plain text only: no bullets, no bold, no dashes used as punctuation.

PARAGRAPH PLAN (follow it exactly; the wording is yours):
{{plan}}

AUDITED BASE LETTER (if it says (none), ignore this section). Every sentence in it has already been verified against the facts. Rewrite it in a natural human voice under the rules below: keep every factual statement and its fact ids, you may reorder, merge, trim and re-voice sentences, but add no claim, outcome, comparison or reason that the base does not already make:
{{base}}

HUMAN WRITING RULES (from the humanize skill; follow every one):
{{style}}

Previous attempt and why it was rejected (fix exactly these, change nothing else that was fine):
{{errors}}

Output shape:

{"paragraphs": [{"text": "...", "fact_ids": ["id", "..."]}, {"text": "...", "fact_ids": ["id"]}, {"text": "My notice period is one month.", "fact_ids": [], "kind": "close"}]}
