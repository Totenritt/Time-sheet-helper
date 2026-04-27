CREATE TABLE time_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT,
    start_at TIMESTAMP NOT NULL,
    end_at TIMESTAMP,
    note TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('work', 'not_work', 'idle_unresolved')),
    jira_worklog_id TEXT,
    pushed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX one_active ON time_entries(kind) WHERE end_at IS NULL;
CREATE INDEX time_entries_start_at ON time_entries(start_at);

CREATE TABLE tickets_cache (
    ticket_key TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    assignee_email TEXT NOT NULL,
    last_fetched_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP NOT NULL
);
