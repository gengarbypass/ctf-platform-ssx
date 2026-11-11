CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('logo', 'cover', 'attachment', 'patch')),
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content BLOB NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS site_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT '蓝域 CTF',
    icon TEXT NOT NULL DEFAULT 'flag' CHECK (icon IN ('flag', 'shield', 'code')),
    accent TEXT NOT NULL DEFAULT '#2458d8',
    background TEXT NOT NULL DEFAULT '#f2f5fb',
    surface TEXT NOT NULL DEFAULT '#ffffff',
    radius INTEGER NOT NULL DEFAULT 12 CHECK (radius BETWEEN 4 AND 24),
    logo_id INTEGER REFERENCES uploads(id)
);

CREATE TABLE IF NOT EXISTS contests (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'puzzle' CHECK (mode IN ('puzzle', 'round')),
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL CHECK (end_at > start_at),
    round_seconds INTEGER NOT NULL CHECK (round_seconds > 0),
    cover_id INTEGER REFERENCES uploads(id),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_contest ON contests(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('player', 'admin')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_failures (
    principal TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_recent ON login_failures(principal, occurred_at);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id),
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (contest_id, name),
    UNIQUE (contest_id, id)
);

CREATE TABLE IF NOT EXISTS memberships (
    contest_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    team_id INTEGER NOT NULL,
    PRIMARY KEY (contest_id, user_id),
    FOREIGN KEY (contest_id, team_id) REFERENCES teams(contest_id, id)
);

CREATE TABLE IF NOT EXISTS puzzles (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    description TEXT NOT NULL,
    points INTEGER NOT NULL CHECK (points > 0),
    flag_salt TEXT NOT NULL,
    flag_digest TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
    UNIQUE (contest_id, id)
);
CREATE INDEX IF NOT EXISTS puzzle_listing ON puzzles(contest_id, published, category);

CREATE TABLE IF NOT EXISTS puzzle_files (
    puzzle_id INTEGER NOT NULL REFERENCES puzzles(id),
    file_id INTEGER NOT NULL UNIQUE REFERENCES uploads(id),
    PRIMARY KEY (puzzle_id, file_id)
);

CREATE TABLE IF NOT EXISTS flag_submissions (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL,
    puzzle_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('correct', 'incorrect', 'already_solved')),
    points INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    submitted_at INTEGER NOT NULL,
    FOREIGN KEY (contest_id, puzzle_id) REFERENCES puzzles(contest_id, id),
    FOREIGN KEY (contest_id, team_id) REFERENCES teams(contest_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_flag_award ON flag_submissions(puzzle_id, team_id) WHERE verdict = 'correct';
CREATE INDEX IF NOT EXISTS flags_team ON flag_submissions(contest_id, team_id, id DESC);
CREATE INDEX IF NOT EXISTS flags_team_recent ON flag_submissions(contest_id, team_id, submitted_at);
CREATE INDEX IF NOT EXISTS flags_contest ON flag_submissions(contest_id, id DESC);

CREATE TABLE IF NOT EXISTS round_tasks (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id),
    title TEXT NOT NULL,
    baseline TEXT NOT NULL,
    description TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
    UNIQUE (contest_id, id)
);
CREATE INDEX IF NOT EXISTS round_tasks_listing ON round_tasks(contest_id, published);

CREATE TABLE IF NOT EXISTS service_states (
    contest_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unknown', 'healthy', 'unhealthy', 'maintenance')),
    updated_at INTEGER NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (team_id, task_id),
    FOREIGN KEY (contest_id, team_id) REFERENCES teams(contest_id, id),
    FOREIGN KEY (contest_id, task_id) REFERENCES round_tasks(contest_id, id)
);
CREATE INDEX IF NOT EXISTS states_contest ON service_states(contest_id);

CREATE TABLE IF NOT EXISTS patches (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    file_id INTEGER NOT NULL UNIQUE REFERENCES uploads(id),
    version TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'testing', 'passed', 'rejected')),
    review_notes TEXT NOT NULL DEFAULT '',
    award INTEGER NOT NULL DEFAULT 0 CHECK (award >= 0),
    submitted_at INTEGER NOT NULL,
    reviewed_at INTEGER,
    reviewed_by INTEGER REFERENCES users(id),
    FOREIGN KEY (contest_id, team_id) REFERENCES teams(contest_id, id),
    FOREIGN KEY (contest_id, task_id) REFERENCES round_tasks(contest_id, id),
    UNIQUE (contest_id, id)
);
CREATE INDEX IF NOT EXISTS patches_contest ON patches(contest_id, id DESC);
CREATE INDEX IF NOT EXISTS patches_team ON patches(contest_id, team_id, id DESC);

CREATE TABLE IF NOT EXISTS score_events (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('attack', 'defense', 'uptime')),
    delta INTEGER NOT NULL CHECK (delta <> 0),
    round_no INTEGER NOT NULL CHECK (round_no > 0),
    reason TEXT NOT NULL,
    patch_id INTEGER,
    occurred_at INTEGER NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    FOREIGN KEY (contest_id, team_id) REFERENCES teams(contest_id, id),
    FOREIGN KEY (contest_id, patch_id) REFERENCES patches(contest_id, id)
);
CREATE INDEX IF NOT EXISTS scores_contest ON score_events(contest_id, id DESC);
CREATE INDEX IF NOT EXISTS scores_team ON score_events(contest_id, team_id, round_no);
