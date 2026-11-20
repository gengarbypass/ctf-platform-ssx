import hashlib
import hmac
import secrets
import time

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db, transaction
from .domain import (
    CATEGORIES, CHALLENGE_TABLES, CONTEST_MODES, FormError, contest_schedule, contest_state, env_flag_value,
    flag_digest, login_required, local_time, round_segments, text_field,
)
from .range import RangeError, start_environment, stop_environment
from .queries import (
    add_score, breach_player_rankings, breach_rankings, breach_records, flag_records, patch_player_rankings,
    patch_records, puzzle_player_rankings, puzzle_rankings, round_rankings, round_targets, round_trend,
    score_records, service_matrix,
)
from .storage import read_upload, save_upload, serve_upload


site = Blueprint("site", __name__)
PAGE_SIZE = 30
LOGIN_BACKGROUND = "login-campus.webp"


def page_number():
    return min(max(request.args.get("page", 1, type=int), 1), 1_000_000)


def first_page():
    return url_for("manage.overview" if g.user["role"] == "admin" else "site.home")


def scope_entry(mode):
    if g.contest and g.contest["mode"] != mode:
        g.contest = g.match = g.team = None


def records_kind(contest):
    expected = {"puzzle": "flag", "round": "patch", "breach": "breach"}[contest["mode"]] if contest else "flag"
    kind = request.args.get("kind", expected)
    # AWDP 比赛同时存在补丁与靶机 Flag 两类提交
    allowed = {"patch", "flag"} if contest and contest["mode"] == "round" else {expected} if contest else {"flag", "patch", "breach"}
    if kind not in allowed:
        abort(404)
    return kind


@site.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(first_page())
    presentation = {"title": "登录比赛平台", "login_background": url_for("static", filename=LOGIN_BACKGROUND)}
    if request.method == "GET":
        return render_template("login.html", **presentation)
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    principal = hashlib.sha256(f"{request.remote_addr}\0{username[:40]}".encode()).hexdigest()
    db = get_db()
    threshold = g.now - 600
    attempts = db.execute(
        "SELECT COUNT(*) FROM login_failures WHERE principal = ? AND occurred_at >= ?", (principal, threshold),
    ).fetchone()[0]
    if attempts >= 8:
        return render_template("login.html", **presentation, error="尝试次数过多，请在 10 分钟后重试。", username=username[:40]), 429
    user = db.execute("SELECT * FROM users WHERE username = ? AND enabled = 1", (username,)).fetchone() if len(username) <= 40 else None
    digest = user["password_hash"] if user else current_app.config["DUMMY_PASSWORD_HASH"]
    valid = len(password) <= 128 and check_password_hash(digest, password)
    if not valid or not user:
        with transaction(db, write=True):
            db.execute("DELETE FROM login_failures WHERE occurred_at < ?", (threshold,))
            db.execute("INSERT INTO login_failures VALUES (?, ?)", (principal, g.now))
        return render_template("login.html", **presentation, error="账号或密码错误，请重试。", username=username[:40]), 401
    db.execute("DELETE FROM login_failures WHERE principal = ?", (principal,))
    session.clear()
    session.update(user_id=user["id"], auth_version=user["auth_version"])
    session.permanent = True
    g.user = user
    return redirect(first_page())


@site.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("site.login"))


@site.get("/")
@login_required
def home():
    if g.user["role"] == "admin":
        return redirect(first_page())
    return render_template("home.html", title="比赛首页", entry_mode=None)


@site.get("/puzzle")
@login_required
def puzzle_home():
    scope_entry("puzzle")
    metrics = {}
    if g.contest:
        db = get_db()
        contest_id = g.contest["id"]
        if g.match["status"] != "upcoming":
            metrics["puzzles"] = db.execute(
                """SELECT COUNT(*) FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id
                   WHERE cp.contest_id = ? AND p.published = 1""", (contest_id,),
            ).fetchone()[0]
        if g.team:
            metrics["puzzle"] = next((row for row in puzzle_rankings(db, contest_id) if row["id"] == g.team["id"]), None)
    return render_template("home.html", title="解题赛", entry_mode="puzzle", metrics=metrics)


@site.get("/challenges")
@login_required
def puzzles():
    if g.contest and g.contest["mode"] != "puzzle":
        abort(404)
    category = request.args.get("category", "")
    if category and category not in CATEGORIES:
        abort(404)
    cards = []
    if g.contest and g.match["status"] != "upcoming":
        cards = get_db().execute(
            """SELECT p.id, p.title, p.category, p.difficulty, p.points,
                      COUNT(f.id) AS solves, MAX(CASE WHEN f.team_id = ? THEN 1 ELSE 0 END) AS solved,
                      EXISTS(SELECT 1 FROM puzzle_files a WHERE a.puzzle_id = p.id) AS attached,
                      (p.env_image IS NOT NULL) AS has_env
               FROM contest_puzzles cp
               JOIN puzzles p ON p.id = cp.puzzle_id
               LEFT JOIN flag_submissions f ON f.puzzle_id = p.id AND f.verdict = 'correct' AND f.contest_id = ?
               WHERE cp.contest_id = ? AND p.published = 1 AND (? = '' OR p.category = ?)
               GROUP BY p.id ORDER BY p.category, p.points, p.id""",
            (g.team["id"] if g.team else None, g.contest["id"], g.contest["id"], category, category),
        ).fetchall()
    return render_template("puzzles.html", title="赛题", cards=cards, categories=CATEGORIES, category=category)


def readable_task(mode, task_id):
    if not g.contest or g.contest["mode"] != mode or g.match["status"] == "upcoming":
        abort(404)
    db = get_db()
    if mode == "puzzle":
        task = db.execute(
            """SELECT p.* FROM puzzles p JOIN contest_puzzles cp ON cp.puzzle_id = p.id
               WHERE p.id = ? AND cp.contest_id = ? AND p.published = 1""",
            (task_id, g.contest["id"]),
        ).fetchone()
    else:
        task = db.execute(
            "SELECT * FROM round_tasks WHERE id = ? AND contest_id = ? AND published = 1",
            (task_id, g.contest["id"]),
        ).fetchone()
    if task is None:
        abort(404)
    return task


@site.get("/challenges/<int:puzzle_id>")
@login_required
def puzzle(puzzle_id):
    challenge = readable_task("puzzle", puzzle_id)
    db = get_db()
    files = db.execute(
        """SELECT f.id, f.name, length(f.content) AS size FROM puzzle_files a JOIN uploads f ON f.id = a.file_id
           WHERE a.puzzle_id = ? ORDER BY f.id""", (puzzle_id,),
    ).fetchall()
    solves = db.execute("SELECT COUNT(*) FROM flag_submissions WHERE puzzle_id = ? AND verdict = 'correct'", (puzzle_id,)).fetchone()[0]
    history = flag_records(db, g.contest["id"], team_id=g.team["id"], puzzle_id=puzzle_id, limit=20) if g.team else []
    solved = bool(g.team and db.execute(
        "SELECT 1 FROM flag_submissions WHERE puzzle_id = ? AND team_id = ? AND verdict = 'correct'",
        (puzzle_id, g.team["id"]),
    ).fetchone())
    environment = None
    if challenge["env_image"]:
        sweep_environments(db, g.now)
        if g.team:
            environment = db.execute(
                "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?",
                (g.contest["id"], g.team["id"], puzzle_id),
            ).fetchone()
    notice = session.pop("flag_notice", None)
    if notice and notice["puzzle_id"] != puzzle_id:
        notice = None
    return render_template("puzzle.html", title=challenge["title"], puzzle=challenge, files=files, solves=solves,
                           solved=solved, history=history, notice=notice, environment=environment,
                           environment_host=environment_host() if challenge["env_image"] else "")


def live_context(db, mode):
    now = int(time.time())
    contest = db.execute("SELECT * FROM contests WHERE active = 1").fetchone()
    if not contest or contest_state(contest, now)["status"] != "live":
        abort(403, "当前不在比赛提交时间内。")
    if contest["mode"] != mode:
        abort(404)
    team = db.execute(
        """SELECT t.* FROM memberships m JOIN teams t ON t.id = m.team_id JOIN users u ON u.id = m.user_id
           WHERE m.contest_id = ? AND u.id = ? AND u.enabled = 1 AND u.role = 'player' AND u.auth_version = ?""",
        (contest["id"], g.user["id"], g.user["auth_version"]),
    ).fetchone()
    if team is None:
        abort(403, "请先加入当前比赛的队伍。")
    return contest, team, now


def submission_context(db, mode, task_id):
    contest, team, now = live_context(db, mode)
    table = CHALLENGE_TABLES[mode]
    if mode == "puzzle":
        task = db.execute(
            """SELECT p.* FROM puzzles p JOIN contest_puzzles cp ON cp.puzzle_id = p.id
               WHERE p.id = ? AND cp.contest_id = ? AND p.published = 1""",
            (task_id, contest["id"]),
        ).fetchone()
    else:
        task = db.execute(
            "SELECT * FROM round_tasks WHERE id = ? AND contest_id = ? AND published = 1",
            (task_id, contest["id"]),
        ).fetchone()
    if task is None:
        abort(404)
    return contest, team, task, now


@site.post("/challenges/<int:puzzle_id>/flag")
@login_required
def submit_flag(puzzle_id):
    value = text_field("flag", "Flag", 256)
    db = get_db()
    with transaction(db, write=True):
        contest, team, challenge, now = submission_context(db, "puzzle", puzzle_id)
        attempts = db.execute(
            "SELECT COUNT(*) FROM flag_submissions WHERE contest_id = ? AND team_id = ? AND submitted_at >= ?",
            (contest["id"], team["id"], now - 60),
        ).fetchone()[0]
        if attempts >= 30:
            abort(429)
        prior = db.execute(
            "SELECT 1 FROM flag_submissions WHERE puzzle_id = ? AND team_id = ? AND verdict = 'correct'",
            (puzzle_id, team["id"]),
        ).fetchone()
        digest_match = bool(challenge["flag_digest"]) and hmac.compare_digest(
            challenge["flag_digest"], flag_digest(challenge["flag_salt"], value))
        dynamic_match = False
        if not digest_match and not challenge["flag_digest"] and challenge["env_image"]:
            env = db.execute(
                """SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?
                   AND status = 'running' AND (expires_at IS NULL OR expires_at > ?)""",
                (contest["id"], team["id"], puzzle_id, now),
            ).fetchone()
            dynamic_match = bool(env) and hmac.compare_digest(env_flag_value(env["secret"], env["id"], puzzle_id), value)
        if prior:
            verdict, points = "already_solved", 0
        elif digest_match or dynamic_match:
            verdict, points = "correct", challenge["points"]
        else:
            verdict, points = "incorrect", 0
        db.execute(
            """INSERT INTO flag_submissions (contest_id, puzzle_id, team_id, user_id, verdict, points, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contest["id"], puzzle_id, team["id"], g.user["id"], verdict, points, now),
        )
    session["flag_notice"] = {"puzzle_id": puzzle_id, "verdict": verdict, "points": points}
    return redirect(url_for("site.puzzle", puzzle_id=puzzle_id), code=303)


def environment_host():
    configured = current_app.config["ENV_HOST"]
    if configured:
        return configured
    return request.host.rsplit(":", 1)[0] if request.host else "127.0.0.1"


def allocate_environment_port(db):
    taken = {row[0] for row in db.execute("SELECT host_port FROM breach_environments WHERE host_port IS NOT NULL")}
    port = current_app.config["ENV_PORT_START"]
    while port in taken and port < 65535:
        port += 1
    return port


def sweep_environments(db, now):
    """惰性清理：过期或所属比赛不再进行中的环境；Docker 故障记入 error 不阻塞页面。"""
    rows = db.execute(
        """SELECT e.*, c.active, c.start_at, c.end_at FROM breach_environments e
           JOIN contests c ON c.id = e.contest_id WHERE e.status IN ('starting', 'running')""",
    ).fetchall()
    for row in rows:
        contest_live = row["active"] == 1 and row["start_at"] <= now < row["end_at"]
        expired = row["expires_at"] is not None and row["expires_at"] <= now
        if contest_live and not expired:
            continue
        try:
            stop_environment(row["contest_id"], row["team_id"], container_id=row["container_id"])
            with transaction(db, write=True):
                db.execute(
                    "UPDATE breach_environments SET status = 'stopped', stopped_at = ? WHERE id = ?", (now, row["id"]),
                )
        except RangeError as error:
            with transaction(db, write=True):
                db.execute(
                    "UPDATE breach_environments SET status = 'error', last_error = ? WHERE id = ?", (str(error), row["id"]),
                )


@site.get("/breach")
@login_required
def breach():
    scope_entry("breach")
    data = {}
    if g.contest and g.match["status"] != "upcoming":
        db = get_db()
        contest_id = g.contest["id"]
        flags = db.execute(
            """SELECT f.id, f.layer, f.title, f.description, f.points,
                      COUNT(s.id) AS solves, MAX(CASE WHEN s.team_id = ? THEN 1 ELSE 0 END) AS solved
               FROM breach_flags f LEFT JOIN breach_submissions s ON s.flag_id = f.id AND s.verdict = 'correct'
               WHERE f.contest_id = ? AND f.published = 1
               GROUP BY f.id
               ORDER BY (SELECT MIN(x.id) FROM breach_flags x WHERE x.contest_id = f.contest_id AND x.layer = f.layer), f.id""",
            (g.team["id"] if g.team else None, contest_id),
        ).fetchall()
        layers = []
        for flag in flags:
            if not layers or layers[-1]["name"] != flag["layer"]:
                layers.append({"name": flag["layer"], "flags": []})
            layers[-1]["flags"].append(flag)
        data = {
            "total": len(flags),
            "layers": layers,
            "mine": breach_records(db, contest_id, team_id=g.team["id"], limit=10) if g.team else [],
            "standings": breach_rankings(db, contest_id),
        }
        if g.contest["env_image"]:
            sweep_environments(db, g.now)
            environment = None
            if g.team:
                environment = db.execute(
                    "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = 0",
                    (contest_id, g.team["id"]),
                ).fetchone()
            data["environment"] = environment
            data["environment_host"] = environment_host()
    notice = session.pop("breach_notice", None)
    return render_template("breach.html", title="综合渗透", data=data, notice=notice)


def ensure_environment_row(db, *, contest, team, puzzle_id, now):
    """事务内确保环境记录并置为待启动；已在运行返回 None，否则返回 (env_id, secret, host_port)。"""
    env = db.execute(
        "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?",
        (contest["id"], team["id"], puzzle_id),
    ).fetchone()
    if env and env["status"] == "running":
        return None
    live_count = db.execute(
        "SELECT COUNT(*) FROM breach_environments WHERE contest_id = ? AND status IN ('starting', 'running')",
        (contest["id"],),
    ).fetchone()[0]
    if not env and live_count >= (contest["env_max"] or 20):
        raise FormError("环境并发已达上限，请稍后重试或联系赛事人员。")
    secret = env["secret"] if env else secrets.token_hex(32)
    port = env["host_port"] if env and env["host_port"] else allocate_environment_port(db)
    expires_at = now + (contest["env_ttl_minutes"] or 120) * 60
    if env:
        db.execute(
            """UPDATE breach_environments SET secret = ?, host_port = ?, status = 'starting', last_error = '',
               expires_at = ?, container_id = '', network_id = '' WHERE id = ?""",
            (secret, port, expires_at, env["id"]),
        )
        return env["id"], secret, port
    env_id = db.execute(
        """INSERT INTO breach_environments (contest_id, team_id, puzzle_id, secret, host_port, status, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, 'starting', ?, ?)""",
        (contest["id"], team["id"], puzzle_id, secret, port, now, expires_at),
    ).lastrowid
    return env_id, secret, port


def launch_environment(db, *, contest, team, env_id, puzzle_id, image, container_port, host_port, flag_env, now, done_url):
    error, container_id, network_id = "", "", ""
    try:
        container_id, network_id = start_environment(
            contest=contest, team=team, image=image, container_port=container_port,
            host_port=host_port, flag_env=flag_env, puzzle_id=puzzle_id,
        )
    except RangeError as failure:
        error = str(failure)
    with transaction(db, write=True):
        if error:
            db.execute(
                "UPDATE breach_environments SET status = 'error', last_error = ?, stopped_at = ? WHERE id = ?",
                (error, now, env_id),
            )
        else:
            db.execute(
                "UPDATE breach_environments SET status = 'running', container_id = ?, network_id = ?, started_at = ? WHERE id = ?",
                (container_id, network_id, now, env_id),
            )
    return redirect(done_url, code=303)


@site.post("/breach/env/start")
@login_required
def start_breach_environment():
    db = get_db()
    with transaction(db, write=True):
        contest, team, now = live_context(db, "breach")
        if not contest["env_image"]:
            abort(404)
        ensured = ensure_environment_row(db, contest=contest, team=team, puzzle_id=0, now=now)
        if ensured is None:
            return redirect(url_for("site.breach"), code=303)
        env_id, secret, port = ensured
        flag_ids = [row[0] for row in db.execute(
            "SELECT id FROM breach_flags WHERE contest_id = ? AND published = 1 AND flag_digest = ''", (contest["id"],),
        ).fetchall()]
    flag_env = {f"FLAG_{flag_id}": env_flag_value(secret, env_id, flag_id) for flag_id in flag_ids}
    return launch_environment(db, contest=contest, team=team, env_id=env_id, puzzle_id=0,
                              image=contest["env_image"], container_port=contest["env_container_port"],
                              host_port=port, flag_env=flag_env, now=now, done_url=url_for("site.breach"))


@site.post("/breach/env/stop")
@login_required
def stop_breach_environment():
    db = get_db()
    with transaction(db, write=True):
        contest, team, now = live_context(db, "breach")
        env = db.execute(
            "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = 0",
            (contest["id"], team["id"]),
        ).fetchone()
        if not env:
            abort(404)
        db.execute(
            "UPDATE breach_environments SET status = 'stopped', stopped_at = ? WHERE id = ?", (now, env["id"]),
        )
    try:
        stop_environment(contest["id"], team["id"], 0, container_id=env["container_id"])
    except RangeError:
        pass
    return redirect(url_for("site.breach"), code=303)


@site.post("/challenges/<int:puzzle_id>/env/start")
@login_required
def start_puzzle_environment(puzzle_id):
    db = get_db()
    with transaction(db, write=True):
        contest, team, task, now = submission_context(db, "puzzle", puzzle_id)
        if not task["env_image"]:
            abort(404)
        ensured = ensure_environment_row(db, contest=contest, team=team, puzzle_id=task["id"], now=now)
        if ensured is None:
            return redirect(url_for("site.puzzle", puzzle_id=puzzle_id), code=303)
        env_id, secret, port = ensured
    derived = env_flag_value(secret, env_id, task["id"])
    flag_env = {"FLAG": derived, f"FLAG_{task['id']}": derived}
    return launch_environment(db, contest=contest, team=team, env_id=env_id, puzzle_id=task["id"],
                              image=task["env_image"], container_port=task["env_container_port"],
                              host_port=port, flag_env=flag_env, now=now,
                              done_url=url_for("site.puzzle", puzzle_id=puzzle_id))


@site.post("/challenges/<int:puzzle_id>/env/stop")
@login_required
def stop_puzzle_environment(puzzle_id):
    db = get_db()
    with transaction(db, write=True):
        contest, team, task, now = submission_context(db, "puzzle", puzzle_id)
        env = db.execute(
            "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?",
            (contest["id"], team["id"], task["id"]),
        ).fetchone()
        if not env:
            abort(404)
        db.execute(
            "UPDATE breach_environments SET status = 'stopped', stopped_at = ? WHERE id = ?", (now, env["id"]),
        )
    try:
        stop_environment(contest["id"], team["id"], task["id"], container_id=env["container_id"])
    except RangeError:
        pass
    return redirect(url_for("site.puzzle", puzzle_id=puzzle_id), code=303)


@site.post("/breach/flag")
@login_required
def submit_breach_flag():
    value = text_field("flag", "Flag", 256)
    db = get_db()
    with transaction(db, write=True):
        contest, team, now = live_context(db, "breach")
        sweep_environments(db, now)
        attempts = db.execute(
            "SELECT COUNT(*) FROM breach_submissions WHERE contest_id = ? AND team_id = ? AND submitted_at >= ?",
            (contest["id"], team["id"], now - 60),
        ).fetchone()[0]
        if attempts >= 30:
            abort(429)
        flags = db.execute(
            "SELECT id, title, points, flag_salt, flag_digest FROM breach_flags WHERE contest_id = ? AND published = 1",
            (contest["id"],),
        ).fetchall()
        match = next(
            (row for row in flags
             if row["flag_digest"] and hmac.compare_digest(row["flag_digest"], flag_digest(row["flag_salt"], value))),
            None,
        )
        if match is None and contest["env_image"]:
            environment = db.execute(
                """SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND status = 'running'
                   AND (expires_at IS NULL OR expires_at > ?)""",
                (contest["id"], team["id"], now),
            ).fetchone()
            if environment:
                match = next(
                    (row for row in flags
                     if not row["flag_digest"]
                     and hmac.compare_digest(env_flag_value(environment["secret"], environment["id"], row["id"]), value)),
                    None,
                )
        prior = db.execute(
            "SELECT 1 FROM breach_submissions WHERE flag_id = ? AND team_id = ? AND verdict = 'correct'",
            (match["id"], team["id"]),
        ).fetchone() if match else None
        if match is None:
            verdict, points, title = "incorrect", 0, ""
        elif prior:
            verdict, points, title = "already_solved", 0, match["title"]
        else:
            verdict, points, title = "correct", match["points"], match["title"]
        db.execute(
            """INSERT INTO breach_submissions (contest_id, flag_id, team_id, user_id, verdict, points, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contest["id"], match["id"] if match else None, team["id"], g.user["id"], verdict, points, now),
        )
    session["breach_notice"] = {"verdict": verdict, "points": points, "title": title}
    return redirect(url_for("site.breach"), code=303)


@site.get("/awdp")
@login_required
def awdp():
    scope_entry("round")
    view = request.args.get("view", "live")
    if view not in {"live", "tasks", "teams", "patches"}:
        abort(404)
    data = {}
    if g.contest:
        db = get_db()
        contest_id = g.contest["id"]
        team_id = g.team["id"] if g.team else None
        state = service_matrix(db, contest_id) if g.match["status"] != "upcoming" else None
        data = {
            "matrix": state,
            "tasks": state["tasks"] if state else [],
            "targets": round_targets(db, contest_id, team_id),
            "events": score_records(db, contest_id, team_id=team_id, limit=10),
            "trend": round_trend(db, contest_id, team_id, g.match["round"]),
            "patches": patch_records(db, contest_id, team_id=team_id, limit=10) if team_id else [],
            "standings": round_rankings(db, contest_id),
        }
    return render_template("awdp.html", title="AWDP", view=view, data=data)


@site.get("/awdp/challenges/<int:task_id>")
@login_required
def round_task(task_id):
    task = readable_task("round", task_id)
    return render_template("round_task.html", title=task["title"], task=task, max_upload=current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))


@site.post("/awdp/challenges/<int:task_id>/patch")
@login_required
def submit_patch(task_id):
    if g.contest and g.contest["mode"] != "round":
        abort(404)
    if not g.team or not g.match or g.match["status"] != "live":
        abort(403)
    version = text_field("version", "补丁版本", 40)
    notes = text_field("notes", "加固说明", 2000, optional=True)
    upload = read_upload(request.files.get("patch"), "patch", required=True)
    db = get_db()
    with transaction(db, write=True):
        contest, team, task, now = submission_context(db, "round", task_id)
        file_id = save_upload(db, upload, now)
        patch_id = db.execute(
            """INSERT INTO patches (contest_id, team_id, task_id, user_id, file_id, version, notes, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (contest["id"], team["id"], task["id"], g.user["id"], file_id, version, notes, now),
        ).lastrowid
    flash(f"补丁 P-{patch_id:04d} 已进入待评测队列。", "success")
    return redirect(url_for("site.records", kind="patch"), code=303)


def target_record(db, target_id, contest_id):
    """AWDP 靶机 = 题库中已加入本比赛的 round 类型赛题。"""
    return db.execute(
        """SELECT p.* FROM puzzles p JOIN contest_puzzles cp ON cp.puzzle_id = p.id
           WHERE p.id = ? AND cp.contest_id = ? AND p.kind = 'round' AND p.published = 1""",
        (target_id, contest_id),
    ).fetchone()


def readable_target(target_id):
    if not g.contest or g.contest["mode"] != "round" or g.match["status"] == "upcoming":
        abort(404)
    task = target_record(get_db(), target_id, g.contest["id"])
    if task is None:
        abort(404)
    return task


def target_context(db, target_id):
    contest, team, now = live_context(db, "round")
    task = target_record(db, target_id, contest["id"])
    if task is None:
        abort(404)
    return contest, team, task, now


@site.get("/awdp/targets/<int:target_id>")
@login_required
def round_target(target_id):
    target = readable_target(target_id)
    db = get_db()
    files = db.execute(
        """SELECT f.id, f.name, length(f.content) AS size FROM puzzle_files a JOIN uploads f ON f.id = a.file_id
           WHERE a.puzzle_id = ? ORDER BY f.id""", (target_id,),
    ).fetchall()
    solves = db.execute(
        "SELECT COUNT(*) FROM flag_submissions WHERE puzzle_id = ? AND verdict = 'correct'", (target_id,),
    ).fetchone()[0]
    history = flag_records(db, g.contest["id"], team_id=g.team["id"], puzzle_id=target_id, limit=20) if g.team else []
    solved = bool(g.team and db.execute(
        "SELECT 1 FROM flag_submissions WHERE puzzle_id = ? AND team_id = ? AND verdict = 'correct'",
        (target_id, g.team["id"]),
    ).fetchone())
    environment = rivals = None
    if target["env_image"]:
        sweep_environments(db, g.now)
        if g.team:
            environment = db.execute(
                "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?",
                (g.contest["id"], g.team["id"], target_id),
            ).fetchone()
            # 其他队伍的在线靶机：攻击方据此访问对手容器提取 Flag
            rivals = db.execute(
                """SELECT e.host_port, t.name AS team_name FROM breach_environments e
                   JOIN teams t ON t.id = e.team_id
                   WHERE e.contest_id = ? AND e.puzzle_id = ? AND e.team_id != ?
                   AND e.status = 'running' AND (e.expires_at IS NULL OR e.expires_at > ?)
                   ORDER BY e.host_port""",
                (g.contest["id"], target_id, g.team["id"], g.now),
            ).fetchall()
    notice = session.pop("flag_notice", None)
    if notice and notice["puzzle_id"] != target_id:
        notice = None
    return render_template("target.html", title=target["title"], target=target, files=files, solves=solves,
                           solved=solved, history=history, notice=notice, environment=environment, rivals=rivals,
                           environment_host=environment_host() if target["env_image"] else "",
                           max_upload=current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))


@site.post("/awdp/targets/<int:target_id>/flag")
@login_required
def submit_target_flag(target_id):
    value = text_field("flag", "Flag", 256)
    db = get_db()
    own_flag = False
    with transaction(db, write=True):
        contest, team, target, now = target_context(db, target_id)
        attempts = db.execute(
            "SELECT COUNT(*) FROM flag_submissions WHERE contest_id = ? AND team_id = ? AND submitted_at >= ?",
            (contest["id"], team["id"], now - 60),
        ).fetchone()[0]
        if attempts >= 30:
            abort(429)
        prior = db.execute(
            "SELECT 1 FROM flag_submissions WHERE puzzle_id = ? AND team_id = ? AND verdict = 'correct'",
            (target_id, team["id"]),
        ).fetchone()
        digest_match = bool(target["flag_digest"]) and hmac.compare_digest(
            target["flag_digest"], flag_digest(target["flag_salt"], value))
        # 动态靶机：比对全部队伍在线环境的派生 Flag，命中他队环境才算攻击成功
        dynamic_match = False
        if not digest_match and not target["flag_digest"] and target["env_image"]:
            for env in db.execute(
                """SELECT * FROM breach_environments WHERE contest_id = ? AND puzzle_id = ?
                   AND status = 'running' AND (expires_at IS NULL OR expires_at > ?)""",
                (contest["id"], target_id, now),
            ).fetchall():
                if hmac.compare_digest(env_flag_value(env["secret"], env["id"], target_id), value):
                    dynamic_match = env["team_id"] != team["id"]
                    own_flag = env["team_id"] == team["id"]
                    break
        if prior:
            verdict, points = "already_solved", 0
        elif digest_match or dynamic_match:
            verdict, points = "correct", target["points"]
        else:
            verdict, points = "incorrect", 0
        db.execute(
            """INSERT INTO flag_submissions (contest_id, puzzle_id, team_id, user_id, verdict, points, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contest["id"], target_id, team["id"], g.user["id"], verdict, points, now),
        )
        if verdict == "correct":
            add_score(db, contest_id=contest["id"], team_id=team["id"], kind="attack", delta=target["points"],
                      round_no=max(1, contest_state(contest, now)["round"]),
                      reason=f"攻破靶机「{target['title']}」", actor_id=g.user["id"], now=now)
    notice = {"puzzle_id": target_id, "verdict": verdict, "points": points, "own_flag": own_flag}
    session["flag_notice"] = notice
    return redirect(url_for("site.round_target", target_id=target_id), code=303)


@site.post("/awdp/targets/<int:target_id>/env/start")
@login_required
def start_target_environment(target_id):
    db = get_db()
    with transaction(db, write=True):
        contest, team, target, now = target_context(db, target_id)
        if not target["env_image"]:
            abort(404)
        ensured = ensure_environment_row(db, contest=contest, team=team, puzzle_id=target["id"], now=now)
        if ensured is None:
            return redirect(url_for("site.round_target", target_id=target_id), code=303)
        env_id, secret, port = ensured
    derived = env_flag_value(secret, env_id, target["id"])
    flag_env = {"FLAG": derived, f"FLAG_{target['id']}": derived}
    return launch_environment(db, contest=contest, team=team, env_id=env_id, puzzle_id=target["id"],
                              image=target["env_image"], container_port=target["env_container_port"],
                              host_port=port, flag_env=flag_env, now=now,
                              done_url=url_for("site.round_target", target_id=target_id))


@site.post("/awdp/targets/<int:target_id>/env/stop")
@login_required
def stop_target_environment(target_id):
    db = get_db()
    with transaction(db, write=True):
        contest, team, target, now = target_context(db, target_id)
        env = db.execute(
            "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?",
            (contest["id"], team["id"], target["id"]),
        ).fetchone()
        if not env:
            abort(404)
        db.execute(
            "UPDATE breach_environments SET status = 'stopped', stopped_at = ? WHERE id = ?", (now, env["id"]),
        )
    try:
        stop_environment(contest["id"], team["id"], target["id"], container_id=env["container_id"])
    except RangeError:
        pass
    return redirect(url_for("site.round_target", target_id=target_id), code=303)


@site.post("/awdp/targets/<int:target_id>/fix")
@login_required
def submit_target_fix(target_id):
    if g.contest and g.contest["mode"] != "round":
        abort(404)
    if not g.team or not g.match or g.match["status"] != "live":
        abort(403)
    version = text_field("version", "修复版本", 40)
    notes = text_field("notes", "加固说明", 2000, optional=True)
    upload = read_upload(request.files.get("fix"), "patch", required=True)
    db = get_db()
    with transaction(db, write=True):
        contest, team, target, now = target_context(db, target_id)
        file_id = save_upload(db, upload, now)
        patch_id = db.execute(
            """INSERT INTO patches (contest_id, team_id, target_id, user_id, file_id, version, notes, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (contest["id"], team["id"], target["id"], g.user["id"], file_id, version, notes, now),
        ).lastrowid
    flash(f"修复文档 P-{patch_id:04d} 已进入待评测队列。", "success")
    return redirect(url_for("site.records", kind="patch"), code=303)


@site.get("/rankings")
@login_required
def ranking():
    expected = g.contest["mode"] if g.contest else "puzzle"
    kind = request.args.get("kind", expected)
    if kind not in CONTEST_MODES or (g.contest and kind != expected):
        abort(404)
    standings = []
    if g.contest:
        query = {"puzzle": puzzle_rankings, "round": round_rankings, "breach": breach_rankings}[kind]
        standings = query(get_db(), g.contest["id"])
    return render_template("ranking.html", title="排行榜", standings=standings, kind=kind)


@site.get("/submissions")
@login_required
def records():
    kind = records_kind(g.contest)
    page = page_number()
    rows = []
    if g.team and g.contest:
        query = {"flag": flag_records, "patch": patch_records, "breach": breach_records}[kind]
        rows = query(get_db(), g.contest["id"], team_id=g.team["id"], limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    return render_template("records.html", title="我的提交", kind=kind, rows=rows[:PAGE_SIZE], page=page, more=len(rows) > PAGE_SIZE, admin=False)


@site.get("/images/<int:file_id>")
def image(file_id):
    is_admin = g.user and g.user["role"] == "admin"
    if not is_admin and file_id not in {g.brand["logo_id"], g.contest["cover_id"] if g.contest else None}:
        abort(404)
    row = get_db().execute("SELECT * FROM uploads WHERE id = ? AND kind IN ('logo', 'cover')", (file_id,)).fetchone()
    if not row:
        abort(404)
    return serve_upload(row, inline=True)


@site.get("/downloads/<int:file_id>")
@login_required
def download(file_id):
    db = get_db()
    admin = g.user["role"] == "admin"
    contest_id = g.contest["id"] if g.contest else None
    puzzle_open = bool(g.contest and g.contest["mode"] in ("puzzle", "round") and g.match["status"] != "upcoming")
    patch_team = g.team["id"] if g.team and g.contest["mode"] == "round" else None
    row = db.execute(
        """SELECT f.* FROM uploads f WHERE f.id = ? AND (
             (f.kind = 'attachment' AND (? = 1 OR (? = 1 AND EXISTS (
               SELECT 1 FROM puzzle_files a
               JOIN contest_puzzles cp ON cp.puzzle_id = a.puzzle_id
               JOIN puzzles p ON p.id = cp.puzzle_id
               WHERE a.file_id = f.id AND cp.contest_id = ? AND p.published = 1
             )))) OR (f.kind = 'patch' AND EXISTS (
               SELECT 1 FROM patches p WHERE p.file_id = f.id AND (? = 1 OR (p.contest_id = ? AND p.team_id = ?))
             ))
           )""",
        (file_id, admin, puzzle_open, contest_id, admin, contest_id, patch_team),
    ).fetchone()
    if not row:
        abort(404)
    return serve_upload(row)


def board_snapshot(kind):
    if kind not in CONTEST_MODES:
        abort(404)
    now = int(time.time())
    with transaction(get_db()) as db:
        contest = db.execute("SELECT * FROM contests WHERE active = 1 AND mode = ?", (kind,)).fetchone()
        state = contest_state(contest, now)
        data = {"teams": [], "events": [], "players": [], "matrix": None}
        if contest:
            contest_id = contest["id"]
            if kind == "puzzle":
                data.update(teams=puzzle_rankings(db, contest_id), events=flag_records(db, contest_id, accepted=True, limit=5),
                            players=puzzle_player_rankings(db, contest_id))
            elif kind == "breach":
                data.update(teams=breach_rankings(db, contest_id), events=breach_records(db, contest_id, accepted=True, limit=5),
                            players=breach_player_rankings(db, contest_id))
            else:
                matrix = service_matrix(db, contest_id) if state["status"] != "upcoming" else None
                data.update(teams=round_rankings(db, contest_id), matrix=matrix,
                            events=score_records(db, contest_id, round_no=state["round"], limit=5),
                            players=patch_player_rankings(db, contest_id))
    clock_label, clock_end = "赛事未开启", now
    round_label = ""
    if state and kind == "round":
        current_round, rounds = state["round"], state["rounds"]
        round_label = f"第 {current_round:02d} / {rounds} 回合" if current_round else f"共 {rounds} 回合"
    if contest:
        if state["status"] == "upcoming":
            clock_label, clock_end = "距开赛", contest["start_at"]
        elif state["status"] == "finished":
            clock_label, clock_end = "比赛已结束", contest["end_at"]
        elif kind != "round":
            clock_label, clock_end = "距比赛结束", contest["end_at"]
        else:
            clock_label, clock_end = "本回合剩余", state["round_end"]
    timing = {
        "id": contest["id"] if contest else None,
        "title": contest["title"] if contest else "现场计分板",
        "schedule": contest_schedule(contest) if contest else "",
        "status": state["label"] if state else "暂无比赛",
        "clock_label": clock_label,
        "clock_end": clock_end,
        "now": now,
        "round_label": round_label,
        "segments": round_segments(state),
        "start_label": local_time(contest["start_at"], "%H:%M") if contest else "",
        "end_label": local_time(contest["end_at"], "%H:%M") if contest else "",
    }
    return data, timing


@site.get("/scoreboard/<kind>")
@login_required
def scoreboard(kind):
    board, timing = board_snapshot(kind)
    return render_template("scoreboard.html", title="计分板大屏", kind=kind, board=board, timing=timing,
                           poll_seconds=current_app.config["BOARD_POLL_SECONDS"])


@site.get("/api/scoreboard/<kind>")
@login_required
def scoreboard_data(kind):
    board, timing = board_snapshot(kind)
    return jsonify(timing=timing, content=render_template("board_content.html", kind=kind, board=board, timing=timing))
