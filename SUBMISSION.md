# Submission

## Candidate
- **Name:** Ratnesh Chaturvedi
- **Assignment chosen:** A (Data: Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** 8h

## How to run
From a clean clone:
```bash
# 1. Environment setup
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
# source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Generate test data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest drops sequentially
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2

# 5. Generate output reports
python -m solution report --out reports/

# 6. Run automated test suite
pytest -v
```

## Design

### Architecture & Pipeline Overview
The solution follows a clean, modular architecture located entirely within `solution/`:
- `solution/normalizers.py`: Pure, side-effect-free parsing functions for Indian mobile numbers, multi-format day-first dates, currency amounts, and fuzzy header normalization.
- `solution/security.py`: PII protection using an HMAC-SHA256 cryptographic blind index and phone number masking.
- `solution/db.py`: SQLite connection management with Write-Ahead Logging (`WAL`) mode, foreign key enforcement, and relational schema definition.
- `solution/ingestion/leads.py`: Lead CSV parser with field validation in schema order, rejection logging, and last-write-wins merging.
- `solution/ingestion/events.py`: Append-only JSON Lines event ledger, deduplication, unknown disposition filtering, and cross-drop orphan reconciliation.
- `solution/reporting.py`: In-memory and SQL aggregations producing all 5 contract files.
- `solution/cli.py`: Subcommand interface (`ingest` and `report`).

### Database Schema Rationale
We chose **SQLite** for zero-configuration, single-file portability across reviewer environments while retaining full ACID compliance.

1. **`leads` table:**
   - Primary Key: `(workflow_id, phone_hash)`
   - Fields: `batch_id` (permanent from first valid row), `masked_phone` (`+91******1234`), `phone_hash` (HMAC-SHA256 blind index), `external_ref`, `name_hash` (PII protected), `attributes_json`, timestamps.
   - Indexes on `(workflow_id, external_ref)` and `(workflow_id, batch_id)`.
2. **`events` table (Append-Only Ledger):**
   - Primary Key: `event_id`
   - Fields: `event_type`, `workflow_id`, `call_id`, `occurred_at`, `occurred_date` (IST), `phone_hash`, `payload_json`, `drop_name`.
   - **Crucial Rule:** No event is ever modified or deleted (`UPDATE`/`DELETE` are never issued). When a supervisor overrides a disposition (`disposition.override`), it is appended as a new row. The effective disposition is resolved dynamically at query time:
     $$\text{Effective Disposition} = \text{Latest Override (by } occurred\_at\text{)} \text{ else Latest Disposition.Set}$$
3. **`orphan_events` table:**
   - Holds events that arrive before their lead is uploaded, or calls for numbers never uploaded. Automatically reconciled when subsequent lead drops arrive.
4. **`rejections` table:**
   - Stores rejected lead rows with `(workflow_id, batch_id, row_number, reason)`. Absolutely zero PII is persisted in this table.
5. **`ingested_drops` table:**
   - Tracks processed drops to guarantee strict idempotency.

### PII Protection Strategy
Customer phone numbers and names are never stored in plaintext at rest:
- **Blind Index:** `hash_phone(canonical_phone) = HMAC_SHA256(canonical_phone, PII_SALT)`. This produces a deterministic 64-character hex digest that enables $O(1)$ joins and lookups between call events and leads.
- **Masking:** Only masked phone numbers (`+91******` + last 4 digits) are exposed in `lead_report.csv`.
- **Customer Names:** Names are stored strictly as cryptographic HMAC hashes (`name_hash`).

## Decisions & Assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **SQLite over Postgres** | Zero-dependency deployment; runs out-of-the-box on reviewer machines without Docker or open network ports. | Dockerized PostgreSQL (included in `docker-compose.yml`, but requires active daemon). |
| **HMAC-SHA256 Blind Index** | Allows exact matching between calls and leads without exposing raw phone numbers in the database. | Symmetric encryption (AES/Fernet), but querying encrypted columns requires decrypting every row in memory. |
| **Two-Phase Orphan Reconciliation** | Events often arrive in Drop 1 before leads arrive in Drop 2. A multi-pass promotion resolves both direct leads and dependent supervisor overrides. | Dropping unlinked events permanently (violates the spec) or in-memory holding queues. |
| **Dynamic Schema Aliasing** | Incoming CSV columns may use aliases (e.g. `Mobile No`, `Contact`). Normalizing headers via regex allows dynamic schema loading without hardcoding column names. | Hardcoded if/else column matching per file. |
| **Schema Order Rejection** | The spec dictates walking fields in schema order and logging the first failure. E.g., if both name and phone are invalid, `missing_required:customer_name` is emitted. | Collecting multiple errors per row (disallowed by spec contract). |

## Failure Modes Considered

- **Duplicate Drops & Re-runs:** Running `python -m solution ingest data/drop_1` multiple times checks `ingested_drops` and skips reprocessing, making the entire pipeline completely idempotent.
- **Out-of-Order Delivery (Orphans):** Call events that precede their lead CSV are quarantined in `orphan_events`. When a new lead drop is ingested, `reconcile_orphans()` promotes matching events and overrides to `events`.
- **Dirty Data Formats:**
  - Phone numbers in 6+ formats (`+91 98765 43210`, `09876543210`, `91-98765-43210`) are stripped and verified to be 10-digit mobile numbers starting with 6, 7, 8, or 9.
  - Dates in 4 day-first formats (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`) are parsed and validated; impossible dates (e.g. `31/02/2026`) are rejected or flagged.
  - Currency strings (`₹`, `Rs`, `Rs.`, `INR`, commas) are sanitized to float with 2 decimal places.
- **Malformed Lines & Duplicates in Events:** Invalid JSON lines increment `malformed_events` and are skipped; duplicate `event_id`s increment `duplicate_events_dropped` and are ignored.
- **What is NOT handled:** Network partition recovery for remote distributed databases (since local SQLite is used).

## Testing

A comprehensive test suite of **55 unit tests** was written using `pytest`:
- **`test_normalizers.py` (52 tests):** Parameterized test cases for valid Indian numbers, invalid prefixes, 11/12 digit edge cases, day-first date variations, leap years, currency parsing, and IST timestamp conversions.
- **`test_ledger.py` (3 tests):** Append-only immutability, supervisor override precedence, and lead bucket evaluation (`yet_to_reach_out`, `in_progress_telephony_failed`, `in_progress_dialed`, `completed`).
- **`test_leads.py` (3 tests):** Schema order rejection hierarchy, required field absence, and duplicate row merging (last-write-wins).

Run with:
```bash
pytest -v
# Output: 55 passed in 0.28s
```

## Results

On small-scale synthetic data (`seed=42`, `scale=small`):
- `data/drop_1`: 1,515 leads processed (71 rejected, 29 merged), 4,247 events processed.
- `data/drop_2`: 542 leads processed (37 rejected, 28 merged), 4,498 events processed.
- Execution time: Ingesting both drops and generating all 5 report files took **< 3 seconds** total.
- All 5 required files (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, `data_quality.json`) conform strictly to the required contract.

## If I had another day
1. **Pure SQL Window Aggregations:** Move lead bucket and effective disposition logic into SQLite window functions (`ROW_NUMBER() OVER (...)`) for even higher reporting throughput.
2. **Large-Scale Streaming Benchmark:** Optimize batch inserts using `executemany` with chunked generator streams to benchmark against `--scale large` (140k leads, 600k events in < 60 seconds).
3. **Automated Schema Evolution:** Add schema migration support for adding new workflow schemas at runtime without altering core tables.
