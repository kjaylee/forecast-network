-- Nonbillable sandbox only. No receipts, balance or audit here represent real money.
CREATE TABLE service_billing_sandbox_capital (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    available_cents INTEGER NOT NULL CHECK (available_cents >= 0),
    version INTEGER NOT NULL CHECK (version >= 0)
);
INSERT INTO service_billing_sandbox_capital VALUES (1, 0, 0);
CREATE TABLE service_billing_sandbox_invoices (
    id TEXT PRIMARY KEY CHECK (id LIKE 'sandbox:%'),
    owner_hash TEXT NOT NULL CHECK (length(owner_hash) = 64),
    scope_hash TEXT NOT NULL CHECK (length(scope_hash) = 64),
    body TEXT NOT NULL CHECK (json_valid(body)),
    version INTEGER NOT NULL CHECK (version >= 0),
    CHECK (json_extract(body, '$.id') IS id),
    CHECK (json_extract(body, '$.ownerHash') IS owner_hash),
    CHECK (json_extract(body, '$.scopeHash') IS scope_hash),
    CHECK (json_extract(body, '$.mode') IS 'sandbox'),
    CHECK (typeof(json_extract(body, '$.assetsCents')) = 'integer'),
    CHECK (typeof(json_extract(body, '$.principalCents')) = 'integer'),
    CHECK (typeof(json_extract(body, '$.remainingCostCents')) = 'integer'),
    CHECK (json_extract(body, '$.billable') IS 0),
    CHECK (json_extract(body, '$.assetsCents') >= 0),
    CHECK (json_extract(body, '$.principalCents') >= 0),
    CHECK (json_extract(body, '$.remainingCostCents') >= 0),
    CHECK (json_extract(body, '$.assetsCents') >=
        json_extract(body, '$.principalCents') + json_extract(body, '$.remainingCostCents'))
);
CREATE TABLE service_billing_sandbox_receipts (
    reference TEXT PRIMARY KEY CHECK (reference LIKE 'sandbox:%'),
    invoice_id TEXT NOT NULL UNIQUE REFERENCES service_billing_sandbox_invoices(id)
);
CREATE TABLE service_billing_sandbox_audit (
    operation_key TEXT PRIMARY KEY CHECK (operation_key LIKE 'sandbox:%'),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    action TEXT NOT NULL,
    invoice_id TEXT,
    result TEXT NOT NULL CHECK (json_valid(result)),
    created_at INTEGER NOT NULL,
    CHECK (json_extract(result, '$.mode') IS 'sandbox'),
    CHECK (json_extract(result, '$.billable') IS 0)
);
CREATE TRIGGER service_billing_sandbox_audit_no_update BEFORE UPDATE ON service_billing_sandbox_audit
BEGIN SELECT RAISE(ABORT, 'sandbox billing audit is immutable'); END;
CREATE TRIGGER service_billing_sandbox_audit_no_delete BEFORE DELETE ON service_billing_sandbox_audit
BEGIN SELECT RAISE(ABORT, 'sandbox billing audit is immutable'); END;
CREATE TRIGGER service_billing_sandbox_receipt_no_update BEFORE UPDATE ON service_billing_sandbox_receipts
BEGIN SELECT RAISE(ABORT, 'sandbox receipts are immutable'); END;
CREATE TRIGGER service_billing_sandbox_receipt_no_delete BEFORE DELETE ON service_billing_sandbox_receipts
BEGIN SELECT RAISE(ABORT, 'sandbox receipts are immutable'); END;
CREATE TABLE service_billing_sandbox_guards (
    operation_key TEXT PRIMARY KEY,
    valid INTEGER NOT NULL CHECK (valid = 1)
);

CREATE TRIGGER service_billing_sandbox_quote_immutable BEFORE UPDATE ON service_billing_sandbox_invoices
WHEN NEW.id IS NOT OLD.id OR NEW.owner_hash IS NOT OLD.owner_hash OR NEW.scope_hash IS NOT OLD.scope_hash
    OR json_extract(NEW.body, '$.priceCents') IS NOT json_extract(OLD.body, '$.priceCents')
    OR json_extract(NEW.body, '$.costCapCents') IS NOT json_extract(OLD.body, '$.costCapCents')
    OR json_extract(NEW.body, '$.maxAttempts') IS NOT json_extract(OLD.body, '$.maxAttempts')
    OR json_extract(NEW.body, '$.expiresAt') IS NOT json_extract(OLD.body, '$.expiresAt')
    OR json_extract(NEW.body, '$.refundUntil') IS NOT json_extract(OLD.body, '$.refundUntil')
BEGIN SELECT RAISE(ABORT, 'sandbox accepted quote is immutable'); END;
