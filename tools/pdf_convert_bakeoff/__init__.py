"""PDF -> DOCX conversion bake-off harness (DEV/EVAL tool, NOT wired into prod).

Compares conversion engines on real NDA PDFs across three scoring layers:
intrinsic fidelity (LLM judge), downstream review/redline effect (reuses the
shipped pipeline), and operational metrics (latency / success / size).

This package is intentionally self-contained under ``tools/`` and is never
imported by the application server. All external credentials come from ENV ONLY.
"""
