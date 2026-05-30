# nda-automation

A small, separate NDA hard-clause checker.

This project deliberately stays away from triage workflows, ranking layers, Gmail/Drive integrations, corpus history, and generated redlines. It answers one question: does the NDA meet the required hard clauses?

You can paste NDA text directly, upload a plain text file, or upload a `.docx` Word document for review.

## Run locally

Requires Python 3.9 or newer.

```bash
python3 -m nda_automation.server --port 8787
```

Then open:

```text
http://127.0.0.1:8787
```

## Test

```bash
python3 -m unittest discover -s tests
```

## Current checks

- Mutual NDA obligations
- Broad confidential information definition
- Approved governing law
- Term and ordinary confidentiality survival up to five years
- No non-circumvention or substitute-purpose exclusivity
- Complete execution block
