import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import connect, get_db, transaction
from .domain import (
    CATEGORIES, CHALLENGE_TABLES, CONTEST_MODES, FormError, attack_pill, contest_schedule, contest_state,
    env_flag_value, flag_digest, flag_matches, login_required, local_time, obtained_pill, round_segments, text_field,
)
from .range import RangeError, start_environment, stop_environment
from .queries import (
    add_score, breach_player_rankings, breach_rankings, breach_records, flag_records, patch_player_rankings,
    patch_records, puzzle_player_rankings, puzzle_rankings, rival_count, round_rankings, round_targets, round_trend,
    score_records, service_matrix, target_awarded, target_awards, target_breaches, target_flags, team_list,
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
            dynamic_match = bool(env) and flag_matches(env_flag_value(env["secret"], env["id"], puzzle_id), value)
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


_autostart_guard = threading.Lock()
AWDP_RETRY_SECONDS = 5


def round_env_targets(db, contest_id):
    """比赛中已发布且配置环境镜像的 AWDP 靶机。"""
    return db.execute(
        """SELECT p.* FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id
           WHERE cp.contest_id = ? AND p.kind = 'round' AND p.published = 1 AND p.env_image IS NOT NULL
           ORDER BY p.id""", (contest_id,),
    ).fetchall()


def sync_round_environments(app, db, contest, state, now):
    """有缺失的实例或新回合待注入时，唤醒 AWDP 靶机维护。"""
    if not contest or contest["mode"] != "round" or state["status"] != "live":
        return
    if db.execute(
        """SELECT 1 FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id
           JOIN contest_teams ct ON ct.contest_id = cp.contest_id
           LEFT JOIN breach_environments e ON e.contest_id = cp.contest_id
                AND e.puzzle_id = cp.puzzle_id AND e.team_id = ct.team_id
           WHERE cp.contest_id = ? AND p.kind = 'round' AND p.published = 1 AND p.env_image IS NOT NULL
             AND (e.id IS NULL OR e.restart_requested = 1 OR e.expires_at IS NOT ?
                  OR e.flag_round IS NOT CASE WHEN p.flag_digest = '' THEN ? ELSE 0 END
                  OR (e.status != 'running' AND (e.status != 'error' OR COALESCE(e.stopped_at, 0) <= ?)))
           LIMIT 1""", (contest["id"], contest["end_at"], state["round"], now - AWDP_RETRY_SECONDS),
    ).fetchone():
        spawn_autostart(app, contest["id"])


def autostart_round_targets(app, contest_id):
    """补齐靶机、应用重启请求并注入本回合 Flag。"""
    # 独立的 SQLite 锁串行化 Gunicorn 进程的 Docker 操作，主库在容器启动期间可正常写入。
    with app.app_context(), closing(sqlite3.connect(app.config["DATABASE"] + ".awdp-lock", timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return
            raise
        with closing(connect(app.config["DATABASE"])) as db:
            with transaction(db, write=True):
                now = int(time.time())
                contest = db.execute(
                    "SELECT * FROM contests WHERE id = ? AND active = 1 AND mode = 'round'", (contest_id,),
                ).fetchone()
                state = contest_state(contest, now)
                if not state or state["status"] != "live":
                    return
                round_no = state["round"]
                teams = team_list(db, contest_id)
                environments = {(row["puzzle_id"], row["team_id"]): row for row in db.execute(
                    "SELECT * FROM breach_environments WHERE contest_id = ?", (contest_id,),
                )}
                pending = []
                for target in round_env_targets(db, contest_id):
                    flag_round = round_no if not target["flag_digest"] else 0
                    flag_ids = [flag["id"] for flag in target_flags(db, target["id"])]
                    for team in teams:
                        env = environments.get((target["id"], team["id"]))
                        if env and env["flag_round"] == flag_round and not env["restart_requested"]:
                            if env["status"] == "running":
                                if env["expires_at"] != contest["end_at"]:
                                    db.execute("UPDATE breach_environments SET expires_at = ? WHERE id = ?",
                                               (contest["end_at"], env["id"]))
                                continue
                            if env["status"] == "error" and (env["stopped_at"] or 0) > now - AWDP_RETRY_SECONDS:
                                continue
                        if env:
                            port = env["host_port"]
                            if not port or env["restart_requested"] or env["status"] == "stopped":
                                port = allocate_environment_port(db)
                            env_id, secret = env["id"], env["secret"]
                            db.execute(
                                """UPDATE breach_environments SET status = 'starting', last_error = '',
                                   flag_round = ?, host_port = ?, expires_at = ?, restart_requested = 0,
                                   container_id = '', network_id = '' WHERE id = ?""",
                                (flag_round, port, contest["end_at"], env_id),
                            )
                        else:
                            port, secret = allocate_environment_port(db), secrets.token_hex(32)
                            env_id = db.execute(
                                """INSERT INTO breach_environments
                                   (contest_id, team_id, puzzle_id, secret, flag_round, host_port, created_at, expires_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (contest_id, team["id"], target["id"], secret, flag_round, port, now, contest["end_at"]),
                            ).lastrowid
                        flag_env = target_flag_env(target["id"], flag_ids, secret, env_id, round_no=flag_round)
                        pending.append((env_id, port, team, target, flag_env))
            if not pending:
                return

            def launch(item):
                _, port, team, target, flag_env = item
                try:
                    with app.app_context():
                        container_id, network_id = start_environment(
                            contest=contest, team=team, image=target["env_image"], host_port=port,
                            container_port=target["env_container_port"], flag_env=flag_env, puzzle_id=target["id"],
                        )
                except RangeError as error:
                    return "", "", str(error)
                return container_id, network_id, ""

            with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
                launches = {pool.submit(launch, item): item for item in pending}
                for future in as_completed(launches):
                    env_id, _, team, target, _ = launches[future]
                    container_id, network_id, error = future.result()
                    finished = int(time.time())
                    with transaction(db, write=True):
                        if error:
                            db.execute(
                                """UPDATE breach_environments SET status = 'error', last_error = ?, stopped_at = ?
                                   WHERE id = ? AND status = 'starting'""", (error, finished, env_id),
                            )
                            continue
                        updated = db.execute(
                            """UPDATE breach_environments SET status = 'running', container_id = ?, network_id = ?, started_at = ?
                               WHERE id = ? AND status = 'starting'""", (container_id, network_id, finished, env_id),
                        ).rowcount
                    if not updated:
                        stop_environment(contest_id, team["id"], target["id"], container_id=container_id)


def spawn_autostart(app, contest_id):
    """后台维护全场靶机，测试时同步执行。"""
    # current_app 是线程绑定的代理，跨线程前解出真实应用对象
    app = app._get_current_object() if hasattr(app, "_get_current_object") else app
    with _autostart_guard:
        state = app.extensions.setdefault("awdp_autostart", {}).setdefault(contest_id, {"running": False, "again": False})
        if state["running"]:
            state["again"] = True  # 已有一轮在执行：结束后带着这次的重启请求再跑一轮
            return
        state["running"] = True

    def run():
        try:
            while True:
                with _autostart_guard:
                    state["again"] = False
                autostart_round_targets(app, contest_id)
                with _autostart_guard:
                    if not state["again"]:
                        state["running"] = False
                        return
        except BaseException:
            with _autostart_guard:
                state.update(running=False, again=False)
            raise

    if app.config.get("TESTING"):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"awdp-autostart-{contest_id}").start()


def restart_target_environments(db, puzzle_id):
    """靶机的 Flag 布局（镜像、容器端口、靶点）变动后重启该靶机：运行中的容器不会自动重新注入 Flag。

    为每场进行中的比赛里该靶机已有的环境换新端口、重建容器，交给后台补齐任务执行；返回涉及的比赛 ID。
    """
    now = int(time.time())
    contest_ids = []
    for row in db.execute(
        """SELECT DISTINCT contest_id FROM breach_environments
           WHERE puzzle_id = ? AND status IN ('starting', 'running') ORDER BY contest_id""", (puzzle_id,),
    ).fetchall():
        contest = db.execute("SELECT * FROM contests WHERE id = ?", (row["contest_id"],)).fetchone()
        if contest and contest_state(contest, now)["status"] == "live":
            contest_ids.append(contest["id"])
    if contest_ids:
        with transaction(db, write=True):
            db.executemany(
                "UPDATE breach_environments SET restart_requested = 1 WHERE contest_id = ? AND puzzle_id = ?",
                [(contest_id, puzzle_id) for contest_id in contest_ids],
            )
    for contest_id in contest_ids:
        spawn_autostart(current_app, contest_id)
    return contest_ids


def sync_active_awdp(app):
    with closing(connect(app.config["DATABASE"])) as db:
        now = int(time.time())
        contest = db.execute("SELECT * FROM contests WHERE active = 1 AND mode = 'round'").fetchone()
        sync_round_environments(app, db, contest, contest_state(contest, now), now)


def start_awdp_scheduler(app):
    if app.testing:
        return
    with _autostart_guard:
        if app.extensions.get("awdp_scheduler"):
            return
        app.extensions["awdp_scheduler"] = True

    def run():
        while True:
            try:
                sync_active_awdp(app)
            except Exception:
                app.logger.exception("AWDP 靶机调度失败")
            time.sleep(1)

    threading.Thread(target=run, name="awdp-scheduler", daemon=True).start()


@site.before_app_request
def ensure_awdp_scheduler():
    start_awdp_scheduler(current_app._get_current_object())


def allocate_environment_port(db):
    """端口只增不发：每次分配全新端口并计入台账，重启换新端口且永不与历史端口重复。"""
    taken = {row[0] for row in db.execute("SELECT host_port FROM breach_environments WHERE host_port IS NOT NULL")}
    highest = db.execute(
        "SELECT COALESCE(MAX(port), ?) FROM env_ports", (current_app.config["ENV_PORT_START"] - 1,),
    ).fetchone()[0]
    port = max(current_app.config["ENV_PORT_START"], highest + 1)
    while port in taken and port < 65535:
        port += 1
    db.execute("INSERT OR IGNORE INTO env_ports (port, allocated_at) VALUES (?, ?)", (port, int(time.time())))
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
            # 赛题/靶机环境的容器与网络名带题号：按记录里的题号定位，才能连专属网络一并移除
            stop_environment(row["contest_id"], row["team_id"], row["puzzle_id"], container_id=row["container_id"])
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
    """为解题或综合靶场预约启动，已在运行时返回 None。"""
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
    if live_count >= (contest["env_max"] or 20) and (env is None or env["status"] != "starting"):
        raise FormError("环境并发已达上限，请稍后重试或联系赛事人员。")
    secret = env["secret"] if env else secrets.token_hex(32)
    # 每次重启都分配全新端口（永不复用历史端口）；secret 不变则 Flag 保持稳定
    port = allocate_environment_port(db)
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
    # 回收在写事务之外进行：回收自身要写库，且不应在持有写锁期间等待 Docker
    sweep_environments(db, int(time.time()))
    with transaction(db, write=True):
        contest, team, now = live_context(db, "breach")
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
                     and flag_matches(env_flag_value(environment["secret"], environment["id"], row["id"]), value)),
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
        sync_round_environments(current_app, db, g.contest, g.match, g.now)
        state = service_matrix(db, contest_id) if g.match["status"] != "upcoming" else None
        data = {
            "matrix": state,
            "tasks": state["tasks"] if state else [],
            "targets": round_targets(db, contest_id, team_id, round_no=g.match["round"]),
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


def target_flag_env(target_id, flag_ids, secret, env_id, *, round_no):
    """AWDP 靶机容器的 Flag 环境变量（按队派生）。

    布置了靶点：FLAG_1…FLAG_N 按靶点顺序各注入一枚独立 Flag，另附与 FLAG_1 同值的 FLAG 以兼容只读 FLAG 的镜像；
    未布置靶点：FLAG 与 FLAG_<靶机ID> 同值（靶机级单枚 Flag）。
    """
    if not flag_ids:
        derived = env_flag_value(secret, env_id, target_id, round_no=round_no)
        return {"FLAG": derived, f"FLAG_{target_id}": derived}
    values = [env_flag_value(secret, env_id, flag_id, kind="target", round_no=round_no) for flag_id in flag_ids]
    return {"FLAG": values[0], **{f"FLAG_{index}": value for index, value in enumerate(values, start=1)}}


@site.get("/awdp/targets/<int:target_id>")
@login_required
def round_target(target_id):
    target = readable_target(target_id)
    db = get_db()
    contest_id = g.contest["id"]
    team_id = g.team["id"] if g.team else None
    files = db.execute(
        """SELECT f.id, f.name, length(f.content) AS size FROM puzzle_files a JOIN uploads f ON f.id = a.file_id
           WHERE a.puzzle_id = ? ORDER BY f.id""", (target_id,),
    ).fetchall()
    flags = [dict(flag) for flag in target_flags(db, target_id)]
    awards = target_awards(db, contest_id, [target_id], round_no=g.match["round"])[target_id]
    breaches = target_breaches(awards, len(flags))
    breached_teams = {victim for victims in breaches.values() for victim in victims if victim is not None}
    team_breaches = breaches.get(team_id, set())
    mine = awards.get(team_id, set())
    # 动态靶机每支队伍各有一个实例：按攻破了几支对手队伍的靶机计进度；静态靶机没有对手实例，只看 Flag 是否拿全
    per_team = bool(target["env_image"] and not target["flag_digest"])
    rival_total = rival_count(db, contest_id, team_id) if per_team else 0
    rival_breached = len(team_breaches - {None})
    for flag in flags:
        victims = {victim for flag_id, victim in mine if flag_id == flag["id"]}
        flag["solves"] = sum(any(flag_id == flag["id"] for flag_id, _ in obtained) for obtained in awards.values())
        flag["state"] = obtained_pill(len(victims), rival_total)
    state = attack_pill(rivals=rival_total, breached=rival_breached, flags=len(mine),
                        need=(len(flags) or 1) * (rival_total or 1))
    history = flag_records(db, contest_id, team_id=team_id, puzzle_id=target_id, limit=20) if team_id else []
    environment = rivals = None
    refreshing = False
    if target["env_image"]:
        sweep_environments(db, g.now)
        if g.team:
            environment = db.execute(
                "SELECT * FROM breach_environments WHERE contest_id = ? AND team_id = ? AND puzzle_id = ?",
                (contest_id, team_id, target_id),
            ).fetchone()
            flag_round = g.match["round"] if per_team else 0
            refreshing = bool(environment and g.match["status"] == "live" and (
                environment["restart_requested"] or environment["flag_round"] != flag_round
            ))
            # 其他队伍的在线靶机：展示本队提交进度及该实例是否已被完整攻破
            rivals = [dict(row) for row in db.execute(
                """SELECT e.team_id, e.host_port, t.name AS team_name FROM breach_environments e
                   JOIN teams t ON t.id = e.team_id
                   WHERE e.contest_id = ? AND e.puzzle_id = ? AND e.team_id != ?
                   AND e.status = 'running' AND e.flag_round = ? AND e.restart_requested = 0
                   AND (e.expires_at IS NULL OR e.expires_at > ?)
                   ORDER BY e.host_port""",
                (contest_id, target_id, team_id, flag_round, g.now),
            ).fetchall()]
            obtained_by_rival = Counter(victim for _, victim in mine)
            for rival in rivals:
                rival["obtained"] = obtained_by_rival[rival["team_id"]]
                rival["breached"] = rival["team_id"] in breached_teams
    notice = session.pop("flag_notice", None)
    if notice:
        if notice["puzzle_id"] != target_id or notice.get("flag_round", 0) not in (0, g.match["round"]):
            notice = None
        elif notice["verdict"] == "correct":
            notice["breached"] = notice.get("victim_id") in team_breaches
    return render_template("target.html", title=target["title"], target=target, files=files, solves=len(breaches),
                           state=state, done=state[0] == "success", history=history, notice=notice,
                           environment=environment, refreshing=refreshing, rivals=rivals, flags=flags, flag_total=len(flags),
                           own_breached=team_id in breached_teams, per_team=per_team,
                           rival_total=rival_total, rival_breached=rival_breached,
                           total_points=sum(flag["points"] for flag in flags) if flags else target["points"],
                           environment_host=environment_host() if target["env_image"] else "",
                           max_upload=current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))


@site.post("/awdp/targets/<int:target_id>/flag")
@login_required
def submit_target_flag(target_id):
    value = text_field("flag", "Flag", 256)
    db = get_db()
    own_flag = False
    title = victim_name = ""
    with transaction(db, write=True):
        contest, team, target, now = target_context(db, target_id)
        round_no = contest_state(contest, now)["round"]
        flag_round = round_no if target["env_image"] and not target["flag_digest"] else 0
        attempts = db.execute(
            "SELECT COUNT(*) FROM flag_submissions WHERE contest_id = ? AND team_id = ? AND submitted_at >= ?",
            (contest["id"], team["id"], now - 60),
        ).fetchone()[0]
        if attempts >= 30:
            abort(429)
        # 只接收已完成本回合注入的动态 Flag。
        rivals = db.execute(
            """SELECT e.*, t.name AS team_name FROM breach_environments e JOIN teams t ON t.id = e.team_id
               WHERE e.contest_id = ? AND e.puzzle_id = ?
               AND e.status = 'running' AND e.flag_round = ? AND e.restart_requested = 0
               AND (e.expires_at IS NULL OR e.expires_at > ?)""",
            (contest["id"], target_id, flag_round, now),
        ).fetchall() if flag_round else []
        flags = target_flags(db, target_id)
        slots = flags or [target]  # 可提交的 Flag：布置了靶点即各靶点各一枚，否则为靶机级 Flag
        # 静态 Flag 各队靶机相同：摘要命中即算，不区分取自哪支队伍
        hit, victim = next((slot for slot in slots if slot["flag_digest"] and hmac.compare_digest(
            slot["flag_digest"], flag_digest(slot["flag_salt"], value))), None), None
        for env in rivals:
            if hit is not None or own_flag:
                break
            for slot in slots:
                if slot["flag_digest"]:
                    continue
                derived = env_flag_value(env["secret"], env["id"], slot["id"],
                                         kind="target" if flags else "flag", round_no=flag_round)
                if flag_matches(derived, value):
                    # 命中本队环境的 Flag 不算攻击成功；命中他队环境即攻破该队靶机上的这个靶点
                    own_flag = env["team_id"] == team["id"]
                    hit, victim = (None, None) if own_flag else (slot, env)
                    break
        matched = hit is not None
        flag_id = hit["id"] if matched and flags else None
        title = hit["title"] if matched and flags else ""
        victim_id = victim["team_id"] if victim else None
        victim_name = victim["team_name"] if victim else ""
        prior = matched and target_awarded(db, contest["id"], target_id, flag_id, team["id"], victim_id, flag_round)
        if prior:
            verdict, points = "already_solved", 0
        elif matched:
            verdict, points = "correct", hit["points"]
        else:
            verdict, points = "incorrect", 0
        db.execute(
            """INSERT INTO flag_submissions (contest_id, puzzle_id, target_flag_id, team_id, victim_team_id, user_id, flag_round,
                                            verdict, points, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contest["id"], target_id, flag_id, team["id"], victim_id, g.user["id"], flag_round, verdict, points, now),
        )
        if verdict == "correct":
            reason = (f"攻破队伍「{victim_name}」" if victim else "攻破") + f"靶机「{target['title']}」"
            if flags:
                reason += f"的靶点「{title}」"
            add_score(db, contest_id=contest["id"], team_id=team["id"], kind="attack", delta=points,
                      round_no=round_no, reason=reason, actor_id=g.user["id"], now=now)
    notice = {"puzzle_id": target_id, "verdict": verdict, "points": points, "own_flag": own_flag, "title": title,
              "victim": victim_name, "victim_id": victim_id, "flag_round": flag_round}
    session["flag_notice"] = notice
    return redirect(url_for("site.round_target", target_id=target_id), code=303)


@site.post("/awdp/targets/<int:target_id>/env/start")
@login_required
def start_target_environment(target_id):
    db = get_db()
    with transaction(db, write=True):
        contest, team, target, _ = target_context(db, target_id)
        if not target["env_image"]:
            abort(404)
        db.execute(
            """UPDATE breach_environments SET restart_requested = 1
               WHERE contest_id = ? AND team_id = ? AND puzzle_id = ? AND status != 'starting'""",
            (contest["id"], team["id"], target_id),
        )
    spawn_autostart(current_app, contest["id"])
    return redirect(url_for("site.round_target", target_id=target_id), code=303)


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
            "UPDATE breach_environments SET status = 'stopped', stopped_at = ?, restart_requested = 0 WHERE id = ?",
            (now, env["id"]),
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
    visible_images = {g.brand["logo_id"], g.brand["home_background_id"], g.contest["cover_id"] if g.contest else None}
    if not is_admin and file_id not in visible_images:
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
