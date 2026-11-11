import hmac
import os
import secrets
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, g, jsonify, render_template, request, session
from werkzeug.security import generate_password_hash

from .db import close_db, get_db, initialize
from .domain import (
    CONTEST_MODES, FLAG_VERDICTS, PATCH_STATES, SCORE_KINDS, SERVICE_STATES, FormError,
    contest_schedule, contest_state, countdown, local_time, round_segments,
)


def _setting_int(name, default, minimum, maximum):
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} 须在 {minimum} 到 {maximum} 之间。")
    return value


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def create_app(overrides=None):
    app = Flask(__name__)
    upload_mb = _setting_int("CTF_MAX_UPLOAD_MB", 16, 1, 128)
    app.config.from_mapping(
        DATA_DIR=os.environ.get("CTF_DATA_DIR", "data"),
        ADMIN_USERNAME=os.environ.get("CTF_ADMIN_USERNAME", "arenaops"),
        ADMIN_PASSWORD=os.environ.get("CTF_ADMIN_PASSWORD", ""),
        SECRET_KEY=os.environ.get("CTF_SECRET_KEY") or None,
        TIME_ZONE=os.environ.get("CTF_TIME_ZONE", "Asia/Shanghai"),
        MAX_CONTENT_LENGTH=upload_mb * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=500_000,
        MAX_FORM_PARTS=60,
        BOARD_POLL_SECONDS=_setting_int("CTF_BOARD_POLL_SECONDS", 4, 2, 60),
        SESSION_COOKIE_NAME="lanyu_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CTF_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if overrides:
        app.config.update(overrides)
    if app.config["SECRET_KEY"] and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("CTF_SECRET_KEY 至少需要 32 个字符。")
    app.config["TIME_ZONE"] = ZoneInfo(app.config["TIME_ZONE"])
    app.config["DATABASE"] = str(Path(app.config["DATA_DIR"]).resolve() / "arena.sqlite3")
    initialize(app)
    app.config["ADMIN_PASSWORD"] = ""
    app.config["DUMMY_PASSWORD_HASH"] = generate_password_hash(secrets.token_urlsafe(24))
    app.teardown_appcontext(close_db)

    @app.before_request
    def load_context():
        if request.endpoint in {"static", "healthz"}:
            return
        db = get_db()
        g.now = int(time.time())
        g.brand = db.execute("SELECT * FROM site_settings WHERE id = 1").fetchone()
        g.contest = db.execute("SELECT * FROM contests WHERE active = 1").fetchone()
        g.match = contest_state(g.contest, g.now)
        g.user = None
        g.team = None
        g.managed = None
        if "user_id" in session:
            user = db.execute("SELECT * FROM users WHERE id = ? AND enabled = 1", (session["user_id"],)).fetchone()
            if user and user["auth_version"] == session.get("auth_version"):
                g.user = user
            else:
                session.clear()
        if g.user and g.user["role"] == "admin":
            selection = session.get("manage_contest_id")
            if selection is not None:
                g.managed = db.execute("SELECT * FROM contests WHERE id = ?", (selection,)).fetchone()
            g.managed = g.managed or g.contest or db.execute("SELECT * FROM contests ORDER BY id DESC LIMIT 1").fetchone()
        elif g.user and g.contest:
            g.team = db.execute(
                """SELECT t.* FROM memberships m JOIN teams t ON t.id = m.team_id
                   WHERE m.contest_id = ? AND m.user_id = ?""",
                (g.contest["id"], g.user["id"]),
            ).fetchone()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(expected.encode(), provided.encode()):
                abort(400, "表单已过期，请刷新页面后重试。")

    @app.context_processor
    def common_template_values():
        return {
            "csrf_token": csrf_token,
            "local_time": local_time,
            "contest_schedule": contest_schedule,
            "contest_state": contest_state,
            "round_segments": round_segments,
            "countdown": countdown,
            "contest_modes": CONTEST_MODES,
            "patch_states": PATCH_STATES,
            "service_states": SERVICE_STATES,
            "flag_verdicts": FLAG_VERDICTS,
            "score_kinds": SCORE_KINDS,
            "timezone": app.config["TIME_ZONE"].key,
            "signed": lambda value: f"{value:+,}",
        }

    @app.after_request
    def response_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob:; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz():
        try:
            ready = get_db().execute("SELECT 1 FROM site_settings WHERE id = 1").fetchone()
        except sqlite3.Error:
            ready = False
        return jsonify(status="ok" if ready else "unavailable"), 200 if ready else 503

    @app.errorhandler(FormError)
    def form_error(error):
        return render_template("error.html", code=400, message=str(error)), 400

    def client_error(error):
        defaults = {403: "当前账号无权访问此页面。", 404: "页面或记录不存在。", 413: "上传文件超出平台限制。", 429: "请求过于频繁，请稍后重试。"}
        message = error.description if error.description != type(error).description else defaults.get(error.code, "请求无效，请检查后重试。")
        return render_template("error.html", code=error.code, message=message), error.code

    for status in (400, 403, 404, 413, 429):
        app.register_error_handler(status, client_error)

    from .admin import manage
    from .views import site

    app.register_blueprint(site)
    app.register_blueprint(manage)
    return app
