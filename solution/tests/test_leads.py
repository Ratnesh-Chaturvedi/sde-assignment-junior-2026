"""
Tests for lead validation order, rejection reasons, and duplicate merging rules.
"""
import pytest
from solution.ingestion.leads import validate_lead_row, SCHEMA_MANAGER


class TestLeadValidation:
    def test_missing_required_field_schema_order(self):
        schema = SCHEMA_MANAGER.get_schema("motor_renewal")
        hmap = SCHEMA_MANAGER.get_header_map("motor_renewal")

        # Row with missing customer_name AND invalid phone
        # Per spec: walk in schema order! customer_name comes first.
        row = {
            "customer_name": "",
            "phone": "invalid_phone_number",
            "policy_number": "MOT/2026/001"
        }
        is_valid, reason, _, _ = validate_lead_row(row, schema, hmap)
        assert not is_valid
        assert reason == "missing_required:customer_name"

    def test_invalid_phone_rejection(self):
        schema = SCHEMA_MANAGER.get_schema("motor_renewal")
        hmap = SCHEMA_MANAGER.get_header_map("motor_renewal")

        row = {
            "customer_name": "Aarav Sharma",
            "phone": "5876543210",  # invalid prefix 5
            "policy_number": "MOT/2026/001"
        }
        is_valid, reason, _, _ = validate_lead_row(row, schema, hmap)
        assert not is_valid
        assert reason == "invalid_phone"

    def test_optional_invalid_generates_field_warning(self):
        schema = SCHEMA_MANAGER.get_schema("motor_renewal")
        hmap = SCHEMA_MANAGER.get_header_map("motor_renewal")

        row = {
            "customer_name": "Aarav Sharma",
            "phone": "+91 98765 43210",
            "policy_number": "MOT/2026/001",
            "policy_expiry": "31/02/2026",  # invalid date
            "premium_amount": "not_a_number", # invalid decimal
            "preferred_language": "invalid_lang" # invalid enum
        }
        is_valid, reason, cleaned, warnings = validate_lead_row(row, schema, hmap)
        assert is_valid
        assert reason is None
        assert cleaned["policy_expiry"] is None
        assert cleaned["premium_amount"] is None
        assert cleaned["preferred_language"] is None
        assert warnings == 3  # 3 optional invalid fields

