"""
Tests for normalization utilities: phone numbers, dates, decimals, headers, timestamps.
"""
import pytest
from datetime import timezone, timedelta
from solution.normalizers import (
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    normalize_enum,
    parse_timestamp,
)
from solution.config import IST


class TestHeaderNormalization:
    def test_strip_and_lowercase(self):
        assert normalize_header(" Customer Name ") == "customer name"

    def test_underscores_and_multiple_spaces(self):
        assert normalize_header("Mobile___Number") == "mobile number"
        assert normalize_header("policy__expiry_date") == "policy expiry date"
        assert normalize_header("Insured   Name") == "insured name"

    def test_empty_header(self):
        assert normalize_header("") == ""
        assert normalize_header(None) == ""


class TestPhoneNormalization:
    @pytest.mark.parametrize("raw_phone, expected", [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-98765-43210", "+919876543210"),
        ("(+91) 9876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("6123456789", "+916123456789"),
        ("7123456789", "+917123456789"),
        ("8123456789", "+918123456789"),
    ])
    def test_valid_indian_phones(self, raw_phone, expected):
        assert normalize_phone(raw_phone) == expected

    @pytest.mark.parametrize("bad_phone", [
        "5876543210",            # starts with 5 (not 6,7,8,9)
        "123456789",             # 9 digits
        "98765432101",           # 11 digits not starting with 0
        "022-2123-4567",         # landline
        "+1 415 555 0134",       # US number
        "N/A",                   # alphabetic
        "",                      # empty
        None,                    # None
    ])
    def test_invalid_phones(self, bad_phone):
        assert normalize_phone(bad_phone) is None


class TestDateParsing:
    @pytest.mark.parametrize("raw_date, expected", [
        ("2026-10-14", "2026-10-14"),
        ("14/10/2026", "2026-10-14"),
        ("14-10-2026", "2026-10-14"),
        ("14-Oct-2026", "2026-10-14"),
        ("01-dec-2026", "2026-12-01"),
        ("29-FEB-2024", "2024-02-29"),  # Leap year
    ])
    def test_valid_day_first_dates(self, raw_date, expected):
        assert parse_date(raw_date) == expected

    @pytest.mark.parametrize("invalid_date", [
        "31/02/2026",     # Impossible date
        "2026-13-01",     # Month 13
        "soon",           # Text
        "TBD",            # Text
        "",               # Empty
        None,             # None
    ])
    def test_invalid_dates(self, invalid_date):
        assert parse_date(invalid_date) is None


class TestDecimalParsing:
    @pytest.mark.parametrize("raw_amount, expected", [
        ("27554", 27554.0),
        ("27554.5", 27554.5),
        ("₹27,554", 27554.0),
        ("Rs. 24787.50", 24787.50),
        ("Rs 1000", 1000.0),
        ("INR 18678.3", 18678.3),
        ("0", 0.0),
        ("0.05", 0.05),
    ])
    def test_valid_decimals(self, raw_amount, expected):
        assert parse_decimal(raw_amount) == expected

    @pytest.mark.parametrize("invalid_amount", [
        "-500",            # negative
        "N/A",             # text
        "twelve thousand", # words
        "",                # empty
        None,              # None
    ])
    def test_invalid_decimals(self, invalid_amount):
        assert parse_decimal(invalid_amount) is None


class TestTimestampParsing:
    def test_utc_timestamp(self):
        ts = parse_timestamp("2026-10-01T05:26:10Z")
        assert ts.tzinfo == IST
        assert ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-01 10:56:10"

    def test_offset_timestamp(self):
        ts = parse_timestamp("2026-10-01T10:40:19+05:30")
        assert ts.tzinfo == IST
        assert ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-01 10:40:19"

    def test_naive_timestamp_treated_as_ist(self):
        ts = parse_timestamp("2026-10-01 09:15:55")
        assert ts.tzinfo == IST
        assert ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-01 09:15:55"

