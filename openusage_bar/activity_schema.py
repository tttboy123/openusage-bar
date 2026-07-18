"""Canonical SQLite schema contracts for the local activity ledger.

This module intentionally contains data-only declarations. Keeping schema
compatibility rules separate from the repository implementation makes them
auditable without importing the store's mutation logic.
"""

SCHEMA_VERSION = 5
DAILY_ACTIVITY_SOURCE_ID = "openusage.daily"
PUBLIC_CHANGE_TYPES = frozenset(
    {
        "daily_cost",
        "daily_cost_coverage",
        "daily_coverage",
        "daily_usage",
        "ledger_schema",
        "provider_instance",
        "quota",
        "quota_snapshot",
        "source_status",
    }
)

EXPECTED_SCHEMA = {
    "daily_costs": (
        ("day", "TEXT", 1, None, 1),
        ("provider_id", "TEXT", 1, None, 2),
        ("account_ref", "TEXT", 1, "''", 3),
        ("cost_kind", "TEXT", 1, None, 4),
        ("currency", "TEXT", 1, None, 5),
        ("amount", "TEXT", 1, None, 0),
        ("basis", "TEXT", 1, None, 0),
        ("quality", "TEXT", 1, None, 0),
        ("imported_at", "TEXT", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("payload_hash", "TEXT", 1, None, 0),
    ),
    "daily_cost_coverage": (
        ("day", "TEXT", 1, None, 1),
        ("provider_id", "TEXT", 1, None, 2),
        ("account_ref", "TEXT", 1, "''", 3),
        ("imported_at", "TEXT", 1, None, 0),
    ),
    "daily_model_usage": (
        ("day", "TEXT", 1, None, 1),
        ("provider_id", "TEXT", 1, None, 2),
        ("account_ref", "TEXT", 1, "''", 3),
        ("model_id", "TEXT", 1, None, 4),
        ("input_tokens", "INTEGER", 1, None, 0),
        ("output_tokens", "INTEGER", 1, None, 0),
        ("cache_read_tokens", "INTEGER", 1, None, 0),
        ("cache_creation_tokens", "INTEGER", 1, None, 0),
        ("reasoning_tokens", "INTEGER", 0, None, 0),
        ("total_tokens", "INTEGER", 1, None, 0),
        ("cost_amount", "TEXT", 0, None, 0),
        ("cost_currency", "TEXT", 0, None, 0),
        ("cost_basis", "TEXT", 0, None, 0),
        ("quality", "TEXT", 1, None, 0),
        ("imported_at", "TEXT", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("payload_hash", "TEXT", 1, None, 0),
        ("source_id", "TEXT", 1, "'legacy'", 0),
    ),
    "daily_coverage": (
        ("day", "TEXT", 1, None, 1),
        ("provider_id", "TEXT", 1, None, 2),
        ("account_ref", "TEXT", 1, "''", 3),
        ("imported_at", "TEXT", 1, None, 0),
        ("source_id", "TEXT", 1, "'legacy'", 0),
    ),
    "quota_state": (
        ("record_id", "TEXT", 0, None, 1),
        ("observed_at", "TEXT", 1, None, 0),
        ("provider_id", "TEXT", 1, None, 0),
        ("account_ref", "TEXT", 1, "''", 0),
        ("quota_name", "TEXT", 1, None, 0),
        ("unit", "TEXT", 1, None, 0),
        ("used", "TEXT", 0, None, 0),
        ("quota_limit", "TEXT", 0, None, 0),
        ("remaining", "TEXT", 0, None, 0),
        ("remaining_ratio", "REAL", 0, None, 0),
        ("resets_at", "TEXT", 0, None, 0),
        ("period_start", "TEXT", 0, None, 0),
        ("period_end", "TEXT", 0, None, 0),
        ("state", "TEXT", 1, None, 0),
        ("quality", "TEXT", 1, None, 0),
        ("stale", "INTEGER", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("payload_hash", "TEXT", 1, None, 0),
        ("source_id", "TEXT", 1, "'current.quota'", 0),
        ("quota_window", "TEXT", 1, "'subscription'", 0),
        ("applies_to_kind", "TEXT", 1, "'account'", 0),
        ("applies_to_model_ids", "TEXT", 1, "'[]'", 0),
    ),
    "quota_snapshots": (
        ("snapshot_id", "INTEGER", 0, None, 1),
        ("record_id", "TEXT", 1, None, 0),
        ("observed_at", "TEXT", 1, None, 0),
        ("provider_id", "TEXT", 1, None, 0),
        ("account_ref", "TEXT", 1, "''", 0),
        ("quota_name", "TEXT", 1, None, 0),
        ("payload_json", "TEXT", 1, None, 0),
        ("payload_hash", "TEXT", 1, None, 0),
        ("source_id", "TEXT", 1, "'current.quota'", 0),
        ("quota_window", "TEXT", 1, "'subscription'", 0),
        ("applies_to_kind", "TEXT", 1, "'account'", 0),
        ("applies_to_model_ids", "TEXT", 1, "'[]'", 0),
    ),
    "source_status": (
        ("provider_id", "TEXT", 1, None, 1),
        ("source_id", "TEXT", 1, None, 2),
        ("state", "TEXT", 1, None, 0),
        ("last_attempt_at", "TEXT", 1, None, 0),
        ("last_success_at", "TEXT", 0, None, 0),
        ("stale_at", "TEXT", 0, None, 0),
        ("error_code", "TEXT", 0, None, 0),
        ("revision", "INTEGER", 1, "1", 0),
        ("payload_hash", "TEXT", 1, "''", 0),
    ),
    "provider_instances": (
        ("provider_id", "TEXT", 0, None, 1),
        ("family_id", "TEXT", 1, None, 0),
        ("display_name", "TEXT", 1, None, 0),
        ("category", "TEXT", 1, None, 0),
        ("credential_source", "TEXT", 1, None, 0),
        ("source_kind", "TEXT", 1, None, 0),
        ("observed_at", "TEXT", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("payload_hash", "TEXT", 1, None, 0),
    ),
    "change_log": (
        ("change_seq", "INTEGER", 0, None, 1),
        ("record_type", "TEXT", 1, None, 0),
        ("record_id", "TEXT", 1, None, 0),
        ("revision", "INTEGER", 1, None, 0),
        ("operation", "TEXT", 1, None, 0),
        ("changed_at", "TEXT", 1, None, 0),
        ("payload_json", "TEXT", 0, None, 0),
        ("payload_hash", "TEXT", 1, None, 0),
    ),
    "ledger_meta": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
}

LEGACY_SOURCE_SCHEMAS = {
    "daily_model_usage": EXPECTED_SCHEMA["daily_model_usage"][:-1],
    "daily_coverage": EXPECTED_SCHEMA["daily_coverage"][:-1],
    "source_status": EXPECTED_SCHEMA["source_status"][:-2],
    "quota_state": EXPECTED_SCHEMA["quota_state"][:-4],
    "quota_snapshots": EXPECTED_SCHEMA["quota_snapshots"][:-4],
}

EXPECTED_INDEXES = {
    "daily_cost_provider_account_day": (
        "daily_costs",
        0,
        (("provider_id", 0), ("account_ref", 0), ("day", 0)),
    ),
    "quota_snapshot_record_time": (
        "quota_snapshots",
        0,
        (("record_id", 0), ("observed_at", 1)),
    ),
    "quota_snapshot_provider_account_time": (
        "quota_snapshots",
        0,
        (("provider_id", 0), ("account_ref", 0), ("observed_at", 1), ("snapshot_id", 1)),
    ),
    "change_log_record_revision_unique": (
        "change_log",
        1,
        (("record_type", 0), ("record_id", 0), ("revision", 0)),
    ),
}

EXPECTED_AUTOINCREMENT = {
    "quota_snapshots": "snapshot_id",
    "change_log": "change_seq",
}
