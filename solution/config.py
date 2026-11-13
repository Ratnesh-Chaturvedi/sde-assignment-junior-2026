"""
Configuration settings and constants for Assignment A.
"""
from datetime import timedelta, timezone
from pathlib import Path
import os

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Timezone: IST (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Default SQLite database path
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "pipeline.db"

# Secret salt for PII blind index (HMAC-SHA256)
PII_SALT = os.getenv("PII_SALT", "au_secret_blind_index_salt_2026").encode("utf-8")

# Path to schemas and disposition catalogue
SCHEMAS_DIR = PROJECT_ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_PATH = SCHEMAS_DIR / "dispositions.csv"

WORKFLOW_SCHEMAS = {
    "motor_renewal": SCHEMAS_DIR / "motor_renewal.json",
    "policy_feedback": SCHEMAS_DIR / "policy_feedback.json",
}

