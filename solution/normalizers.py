"""
Pure normalization and parsing functions for messy inputs.
"""
from datetime import datetime, timezone
import re
from typing import Optional
from solution.config import IST


def normalize_header(header: str) -> str:
    """
    Normalizes a CSV header:
    Lowercase, trim, and replace every run of '_' or whitespace with a single space.
    """
    if not header:
        return ""
    cleaned = re.sub(r"[_\s]+", " ", header.lower().strip())
    return cleaned


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """
    Normalizes an Indian phone number:
    - Strips all non-digit characters.
    - If 12 digits starting with '91', drops the '91'.
    - If 11 digits starting with '0', drops the '0'.
    - Valid only if exactly 10 digits and starts with 6, 7, 8, or 9.
    - Returns canonical format: '+91XXXXXXXXXX'.
    - Returns None if invalid or empty.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def parse_date(raw: Optional[str]) -> Optional[str]:
    """
    Parses a date string in one of four day-first formats:
    - YYYY-MM-DD
    - DD/MM/YYYY
    - DD-MM-YYYY
    - DD-Mon-YYYY (e.g. '01-Dec-2026', case-insensitive month)

    Returns canonical ISO string 'YYYY-MM-DD' or None if invalid.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    # Supported format patterns
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def parse_decimal(raw: Optional[str]) -> Optional[float]:
    """
    Parses a decimal / currency string:
    - Optionally prefixed by '₹', 'Rs', 'Rs.', or 'INR' (case-insensitive, optional space).
    - Commas are thousands separators.
    - Must be >= 0.
    - Rounded to 2 decimal places.
    - Returns float or None if invalid.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    # Remove currency prefixes (case-insensitive)
    # Using regex for currency prefixes
    cleaned = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE).strip()
    # Remove commas
    cleaned = cleaned.replace(",", "")

    try:
        val = float(cleaned)
        if val < 0:
            return None
        return round(val, 2)
    except ValueError:
        return None


def normalize_enum(raw: Optional[str], allowed_values: list[str]) -> Optional[str]:
    """
    Compares raw string case-insensitively against allowed values.
    Returns the lower-case value if matched, otherwise None.
    """
    if not raw:
        return None
    s = raw.strip().lower()
    allowed_lower = {v.lower(): v.lower() for v in allowed_values}
    return allowed_lower.get(s)


def parse_timestamp(raw: str) -> datetime:
    """
    Parses an event timestamp into an aware datetime in IST (+05:30).
    Handles:
    - UTC with 'Z' (e.g. '2026-10-01T05:26:10Z') -> converted to IST
    - Explicit offset (e.g. '2026-10-01T10:40:19+05:30') -> converted to IST
    - Naive (e.g. '2026-10-01 09:15:55') -> treated as IST
    """
    s = raw.strip()
    if s.endswith("Z"):
        # Parse UTC and convert to IST
        dt_utc = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(IST)
    if "+" in s or ("-" in s and "T" in s and s.count("-") >= 3):
        # Has timezone offset
        dt = datetime.fromisoformat(s)
        return dt.astimezone(IST)
    # Naive timestamp -> treat as IST
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=IST)

