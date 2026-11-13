"""
Lead CSV ingestion engine with schema validation, fuzzy header matching,
row rejection, and duplicate merging.
"""
import csv
import json
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import sqlite3

from solution.config import WORKFLOW_SCHEMAS
from solution.normalizers import (
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    normalize_enum,
)
from solution.security import hash_phone, mask_phone, hash_pii_string


class SchemaManager:
    """Loads and caches workflow schemas with normalized header lookups."""
    def __init__(self):
        self.schemas: Dict[str, dict] = {}
        self.header_maps: Dict[str, Dict[str, dict]] = {}
        self.ref_fields: Dict[str, str] = {}
        self._load_schemas()

    def _load_schemas(self):
        for wf, path in WORKFLOW_SCHEMAS.items():
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                schema = json.load(f)
                self.schemas[wf] = schema
                hmap = {}
                for field in schema["fields"]:
                    fname = field["name"]
                    if field.get("reference"):
                        self.ref_fields[wf] = fname
                    # Map normalized field name and all aliases
                    candidates = [fname] + field.get("aliases", [])
                    for cand in candidates:
                        norm = normalize_header(cand)
                        hmap[norm] = field
                self.header_maps[wf] = hmap

    def get_schema(self, wf: str) -> Optional[dict]:
        return self.schemas.get(wf)

    def get_header_map(self, wf: str) -> Dict[str, dict]:
        return self.header_maps.get(wf, {})

    def get_ref_field(self, wf: str) -> str:
        return self.ref_fields.get(wf, "")


# Global schema manager
SCHEMA_MANAGER = SchemaManager()


def validate_lead_row(
    raw_row: Dict[str, str],
    schema: dict,
    header_map: Dict[str, dict]
) -> Tuple[bool, Optional[str], Optional[dict], int]:
    """
    Validates a single non-blank CSV row against the workflow schema in exact schema order.
    Returns:
    - (is_valid: bool, rejection_reason: Optional[str], cleaned_data: Optional[dict], field_warnings: int)
    """
    # 1. Map incoming CSV row keys to schema field names
    mapped_values: Dict[str, str] = {}
    for raw_header, raw_val in raw_row.items():
        if raw_header is None:
            continue
        norm_h = normalize_header(raw_header)
        field_def = header_map.get(norm_h)
        if field_def:
            val = raw_val.strip() if raw_val is not None else ""
            mapped_values[field_def["name"]] = val

    cleaned_data: Dict[str, Any] = {}
    field_warnings = 0

    # 2. Walk fields in EXACT schema order
    for field in schema["fields"]:
        fname = field["name"]
        ftype = field["type"]
        is_required = field.get("required", False)
        raw_val = mapped_values.get(fname, "")

        # Check required field missing
        if is_required and not raw_val:
            return False, f"missing_required:{fname}", None, 0

        # Type validation & normalization
        if ftype == "phone":
            if not raw_val:
                # Handled above if required; if optional and missing, store None
                cleaned_data[fname] = None
            else:
                norm_p = normalize_phone(raw_val)
                if norm_p is None:
                    # Invalid phone
                    return False, "invalid_phone", None, 0
                cleaned_data[fname] = norm_p

        elif ftype == "string":
            if not raw_val:
                cleaned_data[fname] = None
            else:
                cleaned_data[fname] = raw_val

        elif ftype == "date":
            if not raw_val:
                cleaned_data[fname] = None
            else:
                parsed_d = parse_date(raw_val)
                if parsed_d is None:
                    if is_required:
                        return False, f"invalid_required:{fname}", None, 0
                    cleaned_data[fname] = None
                    field_warnings += 1
                else:
                    cleaned_data[fname] = parsed_d

        elif ftype == "decimal":
            if not raw_val:
                cleaned_data[fname] = None
            else:
                parsed_dec = parse_decimal(raw_val)
                if parsed_dec is None:
                    if is_required:
                        return False, f"invalid_required:{fname}", None, 0
                    cleaned_data[fname] = None
                    field_warnings += 1
                else:
                    cleaned_data[fname] = parsed_dec

        elif ftype == "enum":
            if not raw_val:
                cleaned_data[fname] = None
            else:
                parsed_enum = normalize_enum(raw_val, field.get("values", []))
                if parsed_enum is None:
                    if is_required:
                        return False, f"invalid_required:{fname}", None, 0
                    cleaned_data[fname] = None
                    field_warnings += 1
                else:
                    cleaned_data[fname] = parsed_enum

    return True, None, cleaned_data, field_warnings


def ingest_lead_file(
    file_path: Path,
    workflow_id: str,
    batch_id: str,
    drop_name: str,
    conn: sqlite3.Connection,
    stats: dict
):
    """
    Ingests one CSV lead file.
    Tracks rejections, duplicates, warnings, and inserts/updates the leads table.
    """
    schema = SCHEMA_MANAGER.get_schema(workflow_id)
    if not schema:
        raise ValueError(f"Unknown workflow schema for {workflow_id}")
    header_map = SCHEMA_MANAGER.get_header_map(workflow_id)
    ref_field = SCHEMA_MANAGER.get_ref_field(workflow_id)

    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        row_number = 0

        for row in reader:
            # Check for blank rows (all cells empty or whitespace)
            # Spec: "Blank rows (every cell empty or whitespace) are skipped and not counted."
            is_blank = all(not (v and v.strip()) for v in row.values() if v is not None)
            if is_blank:
                continue

            row_number += 1
            stats["lead_rows_total"] += 1

            is_valid, reason, cleaned, warnings = validate_lead_row(row, schema, header_map)

            if not is_valid:
                # Row rejected
                stats["lead_rows_rejected"] += 1
                stats["rejections_by_reason"][reason] = stats["rejections_by_reason"].get(reason, 0) + 1
                conn.execute("""
                    INSERT INTO rejections (workflow_id, batch_id, row_number, reason, drop_name)
                    VALUES (?, ?, ?, ?, ?)
                """, (workflow_id, batch_id, row_number, reason, drop_name))
                continue

            # Row is valid -> count warnings
            stats["field_warnings"] += warnings

            # Lead identity is (workflow_id, canonical phone)
            canonical_phone = cleaned["phone"]
            phone_h = hash_phone(canonical_phone)
            ext_ref = cleaned.get(ref_field, "")
            cust_name = cleaned.get("customer_name", "")
            name_h = hash_pii_string(cust_name)
            masked_p = mask_phone(canonical_phone)

            # Apply defaults (e.g. preferred_language -> en) to fields that are still null
            for f in schema.get("fields", []):
                fname = f["name"]
                if "default" in f and cleaned.get(fname) is None:
                    cleaned[fname] = f["default"]

            # Check if lead already exists
            cur = conn.cursor()
            cur.execute("""
                SELECT attributes_json, batch_id FROM leads
                WHERE workflow_id = ? AND phone_hash = ?
            """, (workflow_id, phone_h))
            existing = cur.fetchone()

            if existing is None:
                # First valid row creates the lead permanently with this batch_id
                conn.execute("""
                    INSERT INTO leads (
                        workflow_id, phone_hash, masked_phone, batch_id, external_ref,
                        name_hash, attributes_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """, (
                    workflow_id, phone_h, masked_p, batch_id, ext_ref,
                    name_h, json.dumps(cleaned)
                ))
            else:
                # Duplicate row -> merge non-null fields (last-write-wins)
                stats["duplicate_lead_rows_merged"] += 1
                existing_attrs = json.loads(existing["attributes_json"])

                for k, v in cleaned.items():
                    if v is not None:
                        existing_attrs[k] = v

                # Ensure defaults apply to any field still null
                for f in schema.get("fields", []):
                    fname = f["name"]
                    if "default" in f and existing_attrs.get(fname) is None:
                        existing_attrs[fname] = f["default"]

                new_ref = existing_attrs.get(ref_field, ext_ref)

                conn.execute("""
                    UPDATE leads
                    SET attributes_json = ?, external_ref = ?, updated_at = datetime('now')
                    WHERE workflow_id = ? AND phone_hash = ?
                """, (json.dumps(existing_attrs), new_ref, workflow_id, phone_h))

