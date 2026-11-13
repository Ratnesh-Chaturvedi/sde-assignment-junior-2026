# AI Usage Disclosure

## Tools used
Antigravity (Google DeepMind)

## What I used them for
- Generating initial boilerplate and scaffolding for the `solution/` package directory.
- Reviewing regular expression patterns for Indian mobile phone prefixes and stripping currency symbols.
- Drafting parameterized unit test cases for `pytest` covering day-first date formats and edge cases.
- Brainstorming database schema options for managing orphan event reconciliation across drops.

## What I wrote or decided myself
- **Core Architecture & Schema:** Decided on a lightweight, zero-dependency SQLite architecture with Write-Ahead Logging (`WAL`) to maximize portability on reviewer machines.
- **Append-Only Ledger Logic:** Designed the query-time resolution mechanism for supervisor overrides (`disposition.override`) to ensure call history remains completely immutable.
- **PII Blind Indexing:** Formulated the HMAC-SHA256 blind indexing approach to allow $O(1)$ phone number joins without persisting plaintext numbers.
- **Validation Order:** Enforced exact schema-order walking for lead row rejection to match the exact contract required by the automated grading diffs.
- **Idempotency Strategy:** Designed the `ingested_drops` and `ingestion_metrics` tables to guarantee that repeated ingestion runs produce zero duplicate records or altered metrics.

## Where the AI was wrong or unhelpful
- **Initial Row Counting Assertion:** In an early test of lead ingestion, the AI suggested asserting `lead_rows_total == leads_count + rejections_count`. This failed because it forgot to account for `duplicate_lead_rows_merged` (leads that were validly merged into existing records). I caught this discrepancy immediately by inspecting the counts: `389 (distinct leads) + 14 (merged duplicates) + 13 (rejections) = 416 (total non-blank rows)`.
- **Date Format Assumption:** Initial regex suggestions for dates assumed standard ISO `YYYY-MM-DD` or US `MM/DD/YYYY` formats. I corrected it to strictly follow Indian day-first conventions (`DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`) and reject impossible dates like `31/02/2026`.

## How I verified AI-generated code
- **Automated Test Suite:** Ran a comprehensive suite of 55 unit tests using `pytest` verifying phone normalization, day-first date parsing, supervisor overrides, and lead bucket statuses.
- **End-to-End Pipeline Verification:** Ingested `drop_1` and `drop_2` on synthetic data, inspected the SQLite database records directly, and diffed the output files (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`) against the specification requirements.
- **Idempotency Testing:** Re-ran `python -m solution ingest data/drop_1` to verify that duplicate ingestion attempts are cleanly detected and skipped with zero state alterations.
