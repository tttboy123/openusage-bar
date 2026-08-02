"""Data-only SQLite schema contract for content-free routing evidence."""

LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2

EXPECTED_SCHEMA = {
    "routing_meta": (
        ("key", "TEXT", 0, None, 1),
        ("value", "INTEGER", 1, None, 0),
    ),
    "routing_decisions": (
        ("decision_id", "TEXT", 0, None, 1),
        ("created_at", "TEXT", 1, None, 0),
        ("expires_at", "TEXT", 1, None, 0),
        ("policy_id", "TEXT", 1, None, 0),
        ("policy_revision", "INTEGER", 1, None, 0),
        ("data_revision", "INTEGER", 1, None, 0),
        ("runtime_revision", "INTEGER", 0, None, 0),
        ("client_request_ref", "TEXT", 0, None, 0),
        ("session_ref", "TEXT", 0, None, 0),
        ("selected_target_id", "TEXT", 0, None, 0),
        ("selected_score", "INTEGER", 0, None, 0),
        ("candidates_json", "TEXT", 1, None, 0),
    ),
    "routing_attempts": (
        ("attempt_id", "TEXT", 0, None, 1),
        ("decision_id", "TEXT", 1, None, 0),
        ("target_id", "TEXT", 1, None, 0),
        ("ordinal", "INTEGER", 1, None, 0),
        ("started_at", "TEXT", 1, None, 0),
        ("completed_at", "TEXT", 1, None, 0),
        ("outcome", "TEXT", 1, None, 0),
        ("status_class", "TEXT", 0, None, 0),
        ("reason_code", "TEXT", 0, None, 0),
        ("input_tokens", "INTEGER", 0, None, 0),
        ("output_tokens", "INTEGER", 0, None, 0),
        ("cache_read_tokens", "INTEGER", 0, None, 0),
        ("cache_creation_tokens", "INTEGER", 0, None, 0),
        ("reasoning_tokens", "INTEGER", 0, None, 0),
        ("total_tokens", "INTEGER", 0, None, 0),
        ("cost_micros", "INTEGER", 0, None, 0),
        ("cost_currency", "TEXT", 0, None, 0),
    ),
    "routing_shadow_evaluations": (
        ("shadow_id", "TEXT", 0, None, 1),
        ("created_at", "TEXT", 1, None, 0),
        ("policy_id", "TEXT", 1, None, 0),
        ("policy_revision", "INTEGER", 1, None, 0),
        ("data_revision", "INTEGER", 1, None, 0),
        ("runtime_revision", "INTEGER", 0, None, 0),
        ("actual_target_id", "TEXT", 1, None, 0),
        ("recommended_target_id", "TEXT", 0, None, 0),
        ("actual_state", "TEXT", 1, None, 0),
        ("actual_rejection_codes_json", "TEXT", 1, None, 0),
        ("agreement", "INTEGER", 1, None, 0),
        ("recommended_score", "INTEGER", 0, None, 0),
        ("actual_score", "INTEGER", 0, None, 0),
        ("score_advantage", "INTEGER", 0, None, 0),
        ("estimated_cost_delta_micros", "INTEGER", 0, None, 0),
        ("cost_currency", "TEXT", 0, None, 0),
        ("estimated_latency_delta_ms", "INTEGER", 0, None, 0),
    ),
}

EXPECTED_INDEXES = {
    "routing_decisions_created": (
        "routing_decisions",
        0,
        (("created_at", 0), ("decision_id", 0)),
    ),
    "routing_attempts_completed": (
        "routing_attempts",
        0,
        (("completed_at", 0), ("attempt_id", 0)),
    ),
    "routing_attempts_decision": (
        "routing_attempts",
        0,
        (("decision_id", 0), ("ordinal", 0)),
    ),
    "routing_shadows_created": (
        "routing_shadow_evaluations",
        0,
        (("created_at", 0), ("shadow_id", 0)),
    ),
}


LEGACY_EXPECTED_SCHEMA = {
    name: columns
    for name, columns in EXPECTED_SCHEMA.items()
    if name != "routing_shadow_evaluations"
}

LEGACY_EXPECTED_INDEXES = {
    name: definition
    for name, definition in EXPECTED_INDEXES.items()
    if name != "routing_shadows_created"
}
