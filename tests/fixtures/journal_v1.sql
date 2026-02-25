-- Frozen layout from the first alpha. Keep this independent of the current schema.
CREATE TABLE operations (
    tenant TEXT NOT NULL,
    account TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    effect TEXT NOT NULL,
    approval_ref TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    knowledge TEXT NOT NULL CHECK (knowledge IN ('unknown', 'applied', 'not_applied_final')),
    phase TEXT NOT NULL CHECK (phase IN ('reserved', 'reconciling', 'unresolved', 'settled')),
    reason TEXT NOT NULL,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant, account, operation_id),
    CHECK ((knowledge = 'applied') = (receipt_json IS NOT NULL)),
    CHECK ((knowledge = 'unknown') = (phase != 'settled'))
);

CREATE UNIQUE INDEX unresolved_scope
ON operations (tenant, account, resource, effect) WHERE knowledge = 'unknown';

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    account TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (tenant, account, operation_id)
        REFERENCES operations (tenant, account, operation_id)
);

CREATE INDEX operation_events ON events (tenant, account, operation_id, sequence);

PRAGMA user_version = 1;
