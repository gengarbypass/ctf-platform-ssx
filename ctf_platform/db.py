import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

from .domain import username_value, validate_password


SCHEMA_VERSION = 14
REBUILT_TABLES = (
    "contests", "users", "teams", "memberships", "puzzles", "puzzle_files", "target_flags", "flag_submissions",
    "contest_puzzles", "contest_teams", "breach_flags", "breach_submissions", "breach_environments",
    "round_tasks", "service_states", "patches", "score_events",
)
JUNCTION_TABLES = ("contest_puzzles", "contest_teams")


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
    # Older databases may predate some tables introduced by newer schema versions.
    legacy = [
        table for table in REBUILT_TABLES
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    ]
    for table in legacy:
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
    for statement in schema:
        connection.execute(statement)
    connection.execute(
        """INSERT INTO contests (id, title, summary, mode, start_at, end_at, round_seconds, env_image,
                                 env_container_port, env_ttl_minutes, env_max, cover_id, active, created_at)
           SELECT c.id, c.title, c.summary, c.mode, c.start_at, c.end_at,
                  CASE WHEN c.mode = 'round'
                         OR EXISTS (SELECT 1 FROM round_tasks_legacy WHERE contest_id = c.id)
                         OR EXISTS (SELECT 1 FROM score_events_legacy WHERE contest_id = c.id)
                       THEN c.round_seconds END,
                  c.env_image, c.env_container_port, c.env_ttl_minutes, c.env_max,
                  c.cover_id, c.active, c.created_at
           FROM contests_legacy c"""
    )
    for table in legacy[1:]:
        columns = ", ".join(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
        connection.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_legacy")
    for table in reversed(legacy):
        connection.execute(f"DROP TABLE {table}_legacy")
    # Recreate indexes whose names were still in use by the old tables.
    for statement in schema:
        connection.execute(statement)


def table_columns(connection, table):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is None:
        return None
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def add_column(connection, table, column_def):
    """补齐缺失列；列已存在（如由旧应用版本写入）时跳过。"""
    if table_columns(connection, table) is None:
        return
    name = column_def.split()[0]
    if name not in table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def dangling_junctions(connection):
    """找出外键指向不存在表的关联表（历史 rename/drop 迁移可能遗留悬挂引用）。"""
    broken = []
    for table in JUNCTION_TABLES:
        if table_columns(connection, table) is None:
            continue
        for fk in connection.execute(f"PRAGMA foreign_key_list({table})"):
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (fk["table"],),
            ).fetchone() is None:
                broken.append(table)
                break
    return broken


def recreate_junction(connection, table, schema):
    rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    connection.execute(f"DROP TABLE {table}")
    for statement in schema:
        if f"CREATE TABLE IF NOT EXISTS {table} " in statement:
            connection.execute(statement)
            break
    if rows:
        cols = ", ".join(rows[0].keys())
        placeholders = ", ".join("?" * len(rows[0]))
        connection.executemany(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", [tuple(row) for row in rows])


def prepare_schema(connection):
    broken = dangling_junctions(connection)
    if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION and not broken:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with transaction(connection, write=True):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前应用版本。")
            if version == SCHEMA_VERSION:
                # 仅修复历史迁移遗留的悬挂外键
                schema = tuple(schema_statements())
                for table in broken:
                    recreate_junction(connection, table, schema)
                return
            if 1 <= version <= 6:
                # 赛题题库化：先把既有赛题与比赛的归属搬入关联表（重建后 puzzles 不再带 contest_id）
                cols = table_columns(connection, "puzzles")
                if cols and "contest_id" in cols:
                    connection.execute("""CREATE TABLE IF NOT EXISTS contest_puzzles (
                        contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
                        puzzle_id INTEGER NOT NULL,
                        added_at INTEGER NOT NULL,
                        UNIQUE (contest_id, puzzle_id))""")
                    connection.execute(
                        """INSERT OR IGNORE INTO contest_puzzles (contest_id, puzzle_id, added_at)
                           SELECT contest_id, id, strftime('%s', 'now') FROM puzzles WHERE contest_id IS NOT NULL""")
            if version == 1:
                add_column(connection, "contests", "mode TEXT NOT NULL DEFAULT 'puzzle' CHECK (mode IN ('puzzle', 'round'))")
                connection.execute(
                    """UPDATE contests SET mode = 'round'
                       WHERE NOT EXISTS (SELECT 1 FROM contest_puzzles cp WHERE cp.contest_id = contests.id)
                         AND (EXISTS (SELECT 1 FROM round_tasks WHERE contest_id = contests.id)
                              OR EXISTS (SELECT 1 FROM score_events WHERE contest_id = contests.id))"""
                )
            if 1 <= version <= 4:
                for column in ("env_image TEXT", "env_container_port INTEGER", "env_ttl_minutes INTEGER", "env_max INTEGER"):
                    add_column(connection, "contests", column)
            if 1 <= version <= 5:
                add_column(connection, "puzzles", "env_image TEXT")
                add_column(connection, "puzzles", "env_container_port INTEGER")
            if version == 5:
                add_column(connection, "breach_environments", "puzzle_id INTEGER NOT NULL DEFAULT 0")
            if 1 <= version <= 8:
                # AWDP 靶机：题库类型列与补丁的靶机归属列（重建按列名回拷，旧表需先补齐同名列）
                add_column(connection, "puzzles", "kind TEXT NOT NULL DEFAULT 'puzzle'")
                add_column(connection, "patches", "target_id INTEGER")
            if 1 <= version <= 9:
                # 动态容器端口台账：回填存量端口，之后重启换新端口且永不复用
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS env_ports (
                        port INTEGER PRIMARY KEY,
                        allocated_at INTEGER NOT NULL)""")
                env_cols = table_columns(connection, "breach_environments")
                if env_cols and "host_port" in env_cols:
                    connection.execute(
                        "INSERT OR IGNORE INTO env_ports (port, allocated_at) "
                        "SELECT host_port, strftime('%s', 'now') FROM breach_environments WHERE host_port IS NOT NULL")
            if 1 <= version <= 10:
                # AWDP 靶机的靶点：提交记录增加命中的靶点列（重建按列名回拷，旧表需先补齐同名列）
                add_column(connection, "flag_submissions", "target_flag_id INTEGER")
            if 1 <= version <= 11:
                # AWDP 攻击按被攻破的队伍计次：提交记录增加 Flag 取自哪支队伍靶机的列（旧行为空，沿用每队一次）
                add_column(connection, "flag_submissions", "victim_team_id INTEGER")
            if 1 <= version <= 7:
                # 队伍全局化：同名队伍合并到最早创建的一支，所有引用重定向；
                # 随后把旧"比赛私有队伍"登记为参赛关联（重建后 teams 不再带 contest_id）。
                cols = table_columns(connection, "teams")
                if cols and "contest_id" in cols:
                    keeps, merges = {}, []
                    for row in connection.execute("SELECT id, name FROM teams ORDER BY id"):
                        key = row["name"].strip().casefold()
                        if key in keeps:
                            merges.append((row["id"], keeps[key]))
                        else:
                            keeps[key] = row["id"]
                    for dup, keep in merges:
                        connection.execute(
                            """DELETE FROM memberships WHERE team_id = ? AND (contest_id, user_id) IN
                               (SELECT contest_id, user_id FROM memberships WHERE team_id = ?)""", (dup, keep),
                        )
                        connection.execute("UPDATE memberships SET team_id = ? WHERE team_id = ?", (keep, dup))
                        for table in ("flag_submissions", "patches", "score_events", "breach_environments"):
                            connection.execute(f"UPDATE {table} SET team_id = ? WHERE team_id = ?", (keep, dup))
                        connection.execute(
                            """DELETE FROM service_states WHERE team_id = ? AND task_id IN
                               (SELECT task_id FROM service_states WHERE team_id = ?)""", (dup, keep),
                        )
                        connection.execute("UPDATE service_states SET team_id = ? WHERE team_id = ?", (keep, dup))
                        connection.execute("DELETE FROM teams WHERE id = ?", (dup,))
                    connection.execute("""CREATE TABLE IF NOT EXISTS contest_teams (
                        contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
                        team_id INTEGER NOT NULL,
                        UNIQUE (contest_id, team_id))""")
                    connection.execute(
                        "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) "
                        "SELECT contest_id, id FROM teams WHERE contest_id IS NOT NULL")
            schema = tuple(schema_statements())
            if 1 <= version <= 12:
                rebuild_tables(connection, schema)
            else:
                for statement in schema:
                    connection.execute(statement)
            add_column(connection, "site_settings", "home_background_id INTEGER REFERENCES uploads(id)")
            # rename/drop 可能遗留悬挂外键：如实修复
            for table in dangling_junctions(connection):
                recreate_junction(connection, table, schema)
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
