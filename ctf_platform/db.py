import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

from .domain import username_value, validate_password


SCHEMA_VERSION = 1


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


def initialize(app):
    path = Path(app.config["DATABASE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("数据库版本高于当前应用版本。")
        if version < SCHEMA_VERSION:
            schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            connection.executescript(f"BEGIN IMMEDIATE;\n{schema}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
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
