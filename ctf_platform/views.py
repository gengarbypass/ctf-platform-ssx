import hashlib
import hmac
import time

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db, transaction
from .domain import (
    CATEGORIES, CHALLENGE_TABLES, CONTEST_MODES, contest_schedule, contest_state, flag_digest, login_required,
    local_time, round_segments, text_field,
)
from .queries import flag_records, patch_records, puzzle_rankings, round_rankings, round_trend, score_records, service_matrix
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
    expected = "patch" if contest and contest["mode"] == "round" else "flag"
    kind = request.args.get("kind", expected)
    if kind not in {"flag", "patch"} or (contest and kind != expected):
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
                "SELECT COUNT(*) FROM puzzles WHERE contest_id = ? AND published = 1", (contest_id,),
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
                      EXISTS(SELECT 1 FROM puzzle_files a WHERE a.puzzle_id = p.id) AS attached
               FROM puzzles p LEFT JOIN flag_submissions f ON f.puzzle_id = p.id AND f.verdict = 'correct'
               WHERE p.contest_id = ? AND p.published = 1 AND (? = '' OR p.category = ?)
               GROUP BY p.id ORDER BY p.category, p.points, p.id""",
            (g.team["id"] if g.team else None, g.contest["id"], category, category),
        ).fetchall()
    return render_template("puzzles.html", title="解题题库", cards=cards, categories=CATEGORIES, category=category)


def readable_task(mode, task_id):
    if not g.contest or g.contest["mode"] != mode or g.match["status"] == "upcoming":
        abort(404)
    table = CHALLENGE_TABLES[mode]
    task = get_db().execute(
        f"SELECT * FROM {table} WHERE id = ? AND contest_id = ? AND published = 1",
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
    notice = session.pop("flag_notice", None)
    if notice and notice["puzzle_id"] != puzzle_id:
        notice = None
    return render_template("puzzle.html", title=challenge["title"], puzzle=challenge, files=files, solves=solves, solved=solved, history=history, notice=notice)


def submission_context(db, mode, task_id):
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
    table = CHALLENGE_TABLES[mode]
    task = db.execute(
        f"SELECT * FROM {table} WHERE id = ? AND contest_id = ? AND published = 1",
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
        if prior:
            verdict, points = "already_solved", 0
        elif hmac.compare_digest(challenge["flag_digest"], flag_digest(challenge["flag_salt"], value)):
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


@site.get("/rankings")
@login_required
def ranking():
    expected = g.contest["mode"] if g.contest else "puzzle"
    kind = request.args.get("kind", expected)
    if kind not in CONTEST_MODES or (g.contest and kind != expected):
        abort(404)
    standings = []
    if g.contest:
        query = puzzle_rankings if kind == "puzzle" else round_rankings
        standings = query(get_db(), g.contest["id"])
    return render_template("ranking.html", title="排行榜", standings=standings, kind=kind)


@site.get("/submissions")
@login_required
def records():
    kind = records_kind(g.contest)
    page = page_number()
    rows = []
    if g.team and g.contest:
        query = flag_records if kind == "flag" else patch_records
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
    puzzle_open = bool(g.contest and g.contest["mode"] == "puzzle" and g.match["status"] != "upcoming")
    patch_team = g.team["id"] if g.team and g.contest["mode"] == "round" else None
    row = db.execute(
        """SELECT f.* FROM uploads f WHERE f.id = ? AND (
             (f.kind = 'attachment' AND EXISTS (
               SELECT 1 FROM puzzle_files a JOIN puzzles p ON p.id = a.puzzle_id
               WHERE a.file_id = f.id AND (? = 1 OR (p.contest_id = ? AND p.published = 1 AND ? = 1))
             )) OR (f.kind = 'patch' AND EXISTS (
               SELECT 1 FROM patches p WHERE p.file_id = f.id AND (? = 1 OR (p.contest_id = ? AND p.team_id = ?))
             ))
           )""",
        (file_id, admin, contest_id, puzzle_open, admin, contest_id, patch_team),
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
        data = {"teams": [], "events": [], "matrix": None}
        if contest:
            contest_id = contest["id"]
            if kind == "puzzle":
                data.update(teams=puzzle_rankings(db, contest_id), events=flag_records(db, contest_id, accepted=True, limit=5))
            else:
                matrix = service_matrix(db, contest_id) if state["status"] != "upcoming" else None
                data.update(teams=round_rankings(db, contest_id), matrix=matrix,
                            events=score_records(db, contest_id, round_no=state["round"], limit=5))
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
        elif kind == "puzzle":
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
