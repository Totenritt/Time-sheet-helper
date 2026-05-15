ALTER TABLE time_entries ADD COLUMN reconciliation_reason TEXT;
ALTER TABLE time_entries ADD COLUMN pending_reconciliation INTEGER NOT NULL DEFAULT 0;

CREATE INDEX time_entries_pending_reconciliation
    ON time_entries(pending_reconciliation)
    WHERE pending_reconciliation = 1;

PRAGMA user_version = 2;
