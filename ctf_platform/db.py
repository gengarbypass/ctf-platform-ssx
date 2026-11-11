import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

from .domain import username_value, validate_password


SCHEMA_VERSION = 3
REBUILT_TABLES = (
    "contests", "users", "teams", "memberships", "puzzles", "puzzle_files", "flag_submissions",
    "round_tasks", "service_states", "patches", "score_events",
)


def connect(path):
    connection = sqlite3.connect(path, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@contextmanager
def transaction(connection, *, write=False):
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def schema_statements():
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise RuntimeError("数据库建表脚本包含未结束的语句。")


def rebuild_tables(connection, schema):
    for table in REBUILT_TABLES:
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
    for statement in schema:
        connection.execute(statement)
    connection.execute(
        """INSERT INTO contests (id, title, summary, mode, start_at, end_at, round_seconds, cover_id, active, created_at)
           SELECT c.id, c.title, c.summary, c.mode, c.start_at, c.end_at,
                  CASE WHEN c.mode = 'round'
                         OR EXISTS (SELECT 1 FROM round_tasks_legacy WHERE contest_id = c.id)
                         OR EXISTS (SELECT 1 FROM score_events_legacy WHERE contest_id = c.id)
                       THEN c.round_seconds END,
                  c.cover_id, c.active, c.created_at
           FROM contests_legacy c"""
    )
    for table in REBUILT_TABLES[1:]:
        columns = ", ".join(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
        connection.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_legacy")
    for table in reversed(REBUILT_TABLES):
        connection.execute(f"DROP TABLE {table}_legacy")
    # Recreate indexes whose names were still in use by the old tables.
    for statement in schema:
        connection.execute(statement)


def prepare_schema(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with transaction(connection, write=True):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前应用版本。")
            if version == SCHEMA_VERSION:
                return
            if version == 1:
                connection.execute("ALTER TABLE contests ADD COLUMN mode TEXT NOT NULL DEFAULT 'puzzle' CHECK (mode IN ('puzzle', 'round'))")
                connection.execute(
                    """UPDATE contests SET mode = 'round'
                       WHERE NOT EXISTS (SELECT 1 FROM puzzles WHERE contest_id = contests.id)
                         AND (EXISTS (SELECT 1 FROM round_tasks WHERE contest_id = contests.id)
                              OR EXISTS (SELECT 1 FROM score_events WHERE contest_id = contests.id))"""
                )
            schema = tuple(schema_statements())
            if version:
                rebuild_tables(connection, schema)
            else:
                for statement in schema:
                    connection.execute(statement)
            invalid = connection.execute("PRAGMA foreign_key_check").fetchone()
            if invalid:
                raise RuntimeError(f"数据库升级发现无效关联，请检查 {invalid['table']} 表。")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def initialize(app):
    path = Path(app.config["DATABASE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        prepare_schema(connection)
        with transaction(connection, write=True):
            connection.execute("INSERT OR IGNORE INTO site_settings (id) VALUES (1)")
            for key in ("session_key", "flag_key"):
                connection.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)", (key, secrets.token_hex(32)))
            if connection.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone() is None:
                password = app.config["ADMIN_PASSWORD"]
                if not password:
                    raise RuntimeError("首次启动需要设置 CTF_ADMIN_PASSWORD，至少 12 个字符。")
                try:
                    username = username_value(app.config["ADMIN_USERNAME"])
                    validate_password(password)
                except ValueError as error:
                    raise RuntimeError(f"初始账号配置无效：{error}") from error
                connection.execute(
                    "INSERT INTO users (username, display_name, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                    (username, username, generate_password_hash(password), int(time.time())),
                )
        meta = dict(connection.execute("SELECT key, value FROM app_meta").fetchall())
    app.secret_key = app.config["SECRET_KEY"] or meta["session_key"]
    app.config["FLAG_KEY"] = bytes.fromhex(meta["flag_key"])
