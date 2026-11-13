"""
PII security functions: HMAC-SHA256 blind indexing and phone masking.
"""
import hmac
import hashlib
from typing import Optional
from solution.config import PII_SALT


def hash_phone(canonical_phone: str) -> str:
    """
    Computes a deterministic blind index (HMAC-SHA256) of a canonical phone number.
    Allows exact O(1) matching between leads and call events without storing the
    plain-text phone number in the database.
    """
    if not canonical_phone:
        return ""
    return hmac.new(PII_SALT, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """
    Masks a phone number for display/reporting:
    Spec requirement: '+91******' followed by the last 4 digits (e.g. '+91******5036').
    """
    if not canonical_phone or len(canonical_phone) < 4:
        return "+91******0000"
    last_four = canonical_phone[-4:]
    return f"+91******{last_four}"


def hash_pii_string(val: Optional[str]) -> str:
    """
    Computes a cryptographic HMAC-SHA256 hash for any PII string (like customer name)
    so that sensitive names are not stored in plaintext at rest.
    """
    if not val:
        return ""
    return hmac.new(PII_SALT, val.strip().encode("utf-8"), hashlib.sha256).hexdigest()

