import re
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from .db import get_db, transaction
from .domain import (
    CATEGORIES, CHALLENGE_TABLES, CONTEST_MODES, DIFFICULTIES, PATCH_STATES, SCORE_KINDS, SERVICE_STATES, FormError, admin_required,
    choice_field, contest_state, integer_field, new_flag, parse_local_time, text_field, username_value, validate_password,
)
from .queries import add_score, flag_records, patch_records, score_records, service_matrix
from .storage import read_upload, save_upload
from .views import PAGE_SIZE, page_number, records_kind


manage = Blueprint("manage", __name__, url_prefix="/manage")


def managed_id(mode=None):
    if not g.managed:
        raise FormError("请先创建比赛。")
    contest_id = g.managed["id"]
    if request.method == "POST" and request.form.get("contest_id", type=int) != contest_id:
        raise FormError("当前编辑的比赛已切换，请重新打开表单。")
    contest = get_db().execute("SELECT mode FROM contests WHERE id = ?", (contest_id,)).fetchone()
    if not contest or (mode and contest["mode"] != mode):
        abort(404)
    return contest_id


def scoped_record(table, record_id, contest_id):
    row = get_db().execute(f"SELECT * FROM {table} WHERE id = ? AND contest_id = ?", (record_id, contest_id)).fetchone()
    if not row:
        abort(404)
    return row


def saved(message, endpoint, **values):
    flash(message, "success")
    return redirect(url_for(endpoint, **values), code=303)


def require_current_admin(db):
    if not db.execute(
        "SELECT 1 FROM users WHERE id = ? AND auth_version = ? AND role = 'admin' AND enabled = 1",
        (g.user["id"], g.user["auth_version"]),
    ).fetchone():
        abort(403)


def keep_enabled_admin(db, account):
    if account["role"] == "admin" and account["enabled"] and not db.execute(
        "SELECT 1 FROM users WHERE id <> ? AND role = 'admin' AND enabled = 1 LIMIT 1", (account["id"],),
    ).fetchone():
        raise FormError("至少保留一个启用的赛事管理账号。")


@manage.get("/")
@admin_required
def overview():
    metrics = {}
    patches = []
    if g.managed:
        db = get_db()
        contest_id = g.managed["id"]
        mode = g.managed["mode"]
        table = CHALLENGE_TABLES[mode]
        metrics["teams"] = db.execute("SELECT COUNT(*) FROM teams WHERE contest_id = ?", (contest_id,)).fetchone()[0]
        metrics["challenges"] = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE contest_id = ? AND published = 1", (contest_id,),
        ).fetchone()[0]
        if mode == "round":
            metrics["pending"] = db.execute(
                "SELECT COUNT(*) FROM patches WHERE contest_id = ? AND status IN ('pending', 'testing')", (contest_id,),
            ).fetchone()[0]
            patches = patch_records(db, contest_id, limit=5)
    return render_template("manage/overview.html", title="赛事总览", metrics=metrics, patches=patches)


@manage.get("/contests")
@admin_required
def contests():
    rows = get_db().execute("SELECT * FROM contests ORDER BY active DESC, start_at DESC, id DESC").fetchall()
    return render_template("manage/contests.html", title="比赛配置", rows=rows)


@manage.route("/contests/new", defaults={"contest_id": None}, methods=["GET", "POST"])
@manage.route("/contests/<int:contest_id>/edit", methods=["GET", "POST"])
@admin_required
def contest_edit(contest_id):
    db = get_db()
    contest = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone() if contest_id is not None else None
    if contest_id is not None and contest is None:
        abort(404)
    if request.method == "GET":
        return render_template("manage/contest_form.html", title="编辑比赛" if contest else "创建比赛", contest=contest)
    mode = choice_field("mode", "比赛模式", CONTEST_MODES)
    title = text_field("title", "比赛标题", 80)
    summary = text_field("summary", "比赛简介", 1000, optional=True)
    start_at = parse_local_time("start_at", "开赛时间")
    end_at = parse_local_time("end_at", "结束时间")
    round_seconds = integer_field("round_minutes", "回合分钟数", 1, 1440) * 60 if mode == "round" else None
    if end_at <= start_at:
        raise FormError("结束时间须晚于开赛时间。")
    cover = read_upload(request.files.get("cover"), "cover")
    remove_cover = request.form.get("remove_cover") == "1"
    with transaction(db, write=True):
        if contest:
            contest = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
            if not contest:
                abort(404)
            if contest["active"] and mode != contest["mode"]:
                raise FormError("请先撤下比赛，再修改比赛模式。")
            if mode == "puzzle":
                round_seconds = contest["round_seconds"]
            last_round = db.execute("SELECT MAX(round_no) FROM score_events WHERE contest_id = ?", (contest_id,)).fetchone()[0]
            if last_round and (
                round_seconds is None or start_at != contest["start_at"] or round_seconds != contest["round_seconds"]
                or (end_at - start_at + round_seconds - 1) // round_seconds < last_round
            ):
                raise FormError("已有回合积分记录，请保留开赛时间、回合长度和已计分回合。")
        old_cover_id = contest["cover_id"] if contest else None
        cover_id = save_upload(db, cover, g.now) if cover else (None if remove_cover else old_cover_id)
        values = (title, summary, mode, start_at, end_at, round_seconds, cover_id)
        if contest:
            db.execute(
                "UPDATE contests SET title = ?, summary = ?, mode = ?, start_at = ?, end_at = ?, round_seconds = ?, cover_id = ? WHERE id = ?",
                (*values, contest_id),
            )
        else:
            contest_id = db.execute(
                """INSERT INTO contests (title, summary, mode, start_at, end_at, round_seconds, cover_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (*values, g.now),
            ).lastrowid
        if old_cover_id is not None and cover_id != old_cover_id:
            db.execute("DELETE FROM uploads WHERE id = ?", (old_cover_id,))
    session["manage_contest_id"] = contest_id
    if not contest:
        return saved("比赛已创建，可以开始筹备赛题。", "manage.challenges", kind=mode)
    return saved("比赛配置已保存。", "manage.contests")


@manage.post("/contests/<int:contest_id>/activate")
@admin_required
def activate_contest(contest_id):
    db = get_db()
    with transaction(db, write=True):
        if not db.execute("SELECT 1 FROM contests WHERE id = ?", (contest_id,)).fetchone():
            abort(404)
        db.execute("UPDATE contests SET active = 0 WHERE active = 1")
        db.execute("UPDATE contests SET active = 1 WHERE id = ?", (contest_id,))
    session["manage_contest_id"] = contest_id
    return saved("当前比赛已切换。", "manage.contests")


@manage.post("/contests/<int:contest_id>/deactivate")
@admin_required
def deactivate_contest(contest_id):
    if not get_db().execute("UPDATE contests SET active = 0 WHERE id = ?", (contest_id,)).rowcount:
        abort(404)
    return saved("该比赛已从前台撤下。", "manage.contests")


@manage.route("/contests/<int:contest_id>/delete", methods=["GET", "POST"])
@admin_required
def contest_delete(contest_id):
    db = get_db()
    if request.method == "GET":
        contest = db.execute("SELECT title, active FROM contests WHERE id = ?", (contest_id,)).fetchone()
        if not contest:
            abort(404)
        impact = "将一并删除本场比赛的队伍、赛题、提交、积分和关联文件。"
        if contest["active"]:
            impact += "当前比赛会立即下线。"
        return render_template("manage/delete.html", title="删除比赛", subject=contest["title"],
                               impact=impact, back=url_for("manage.contests"))
    with transaction(db, write=True):
        require_current_admin(db)
        if not db.execute("DELETE FROM contests WHERE id = ?", (contest_id,)).rowcount:
            abort(404)
    if session.get("manage_contest_id") == contest_id:
        session.pop("manage_contest_id")
    return saved("比赛已删除。", "manage.contests")


@manage.post("/contests/<int:contest_id>/select")
@admin_required
def select_contest(contest_id):
    contest = get_db().execute("SELECT mode FROM contests WHERE id = ?", (contest_id,)).fetchone()
    if not contest:
        abort(404)
    session["manage_contest_id"] = contest_id
    if request.form.get("section") == "challenges":
        return redirect(url_for("manage.challenges", kind=contest["mode"]), code=303)
    return saved("已切换编辑的比赛。", "manage.overview")


@manage.get("/accounts")
@admin_required
def accounts():
    rows = get_db().execute(
        """SELECT u.id, u.username, u.display_name, u.role, u.enabled, t.name AS team_name
           FROM users u LEFT JOIN memberships m ON m.user_id = u.id AND m.contest_id = ?
           LEFT JOIN teams t ON t.id = m.team_id ORDER BY u.id""", (g.managed["id"] if g.managed else None,),
    ).fetchall()
    enabled_admins = sum(1 for row in rows if row["role"] == "admin" and row["enabled"])
    return render_template("manage/accounts.html", title="账号管理", rows=rows, enabled_admins=enabled_admins)


@manage.route("/accounts/new", defaults={"user_id": None}, methods=["GET", "POST"])
@manage.route("/accounts/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def account_edit(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id is not None else None
    if user_id is not None and not user:
        abort(404)
    if request.method == "GET":
        return render_template("manage/account_form.html", title="编辑账号" if user else "创建账号", account=user)
    username = username_value(request.form.get("username", ""))
    name = text_field("display_name", "显示名称", 50)
    role = choice_field("role", "账号权限", ("admin", "player"))
    enabled = request.form.get("enabled") == "1"
    password = request.form.get("password", "")
    password_hash = None
    if password or user is None:
        validate_password(password)
        password_hash = generate_password_hash(password)
    if user_id == g.user["id"] and (not enabled or role != "admin"):
        raise FormError("当前登录账号须保持启用和赛事管理权限。")
    try:
        with transaction(db, write=True):
            require_current_admin(db)
            if user:
                current = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if not current:
                    abort(404)
                if not enabled or role != "admin":
                    keep_enabled_admin(db, current)
                invalidate = bool(password_hash or role != current["role"] or enabled != bool(current["enabled"]))
                version = current["auth_version"] + int(invalidate)
                db.execute(
                    """UPDATE users SET username = ?, display_name = ?, password_hash = ?, role = ?, enabled = ?, auth_version = ?
                       WHERE id = ?""",
                    (username, name, password_hash or current["password_hash"], role, enabled, version, user_id),
                )
            else:
                db.execute(
                    """INSERT INTO users (username, display_name, password_hash, role, enabled, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""", (username, name, password_hash, role, enabled, g.now),
                )
    except sqlite3.IntegrityError as error:
        raise FormError("该登录账号已存在。") from error
    if user_id == g.user["id"]:
        session["auth_version"] = version
    return saved("账号已保存。", "manage.accounts")


@manage.route("/accounts/<int:user_id>/delete", methods=["GET", "POST"])
@admin_required
def account_delete(user_id):
    db = get_db()
    deleting = request.method == "POST"
    own_account = user_id == g.user["id"]
    with transaction(db, write=deleting):
        if deleting:
            require_current_admin(db)
        account = db.execute("SELECT id, username, role, enabled FROM users WHERE id = ?", (user_id,)).fetchone()
        if not account:
            abort(404)
        keep_enabled_admin(db, account)
        if deleting:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if not deleting:
        impact = "将删除登录账号并解除所有分队。已有提交会继续计入原队伍成绩。"
        if own_account:
            impact += "删除后会退出当前登录。"
        return render_template("manage/delete.html", title="删除账号", subject=account["username"],
                               impact=impact, back=url_for("manage.accounts"))
    if own_account:
        session.clear()
        return redirect(url_for("site.login"), code=303)
    return saved("账号已删除。", "manage.accounts")


@manage.get("/teams")
@admin_required
def teams():
    rows = []
    players = []
    matrix = None
    events = []
    if g.managed:
        db = get_db()
        contest_id = g.managed["id"]
        rows = db.execute(
            """SELECT t.*, COUNT(m.user_id) AS members FROM teams t LEFT JOIN memberships m ON m.team_id = t.id
               WHERE t.contest_id = ? GROUP BY t.id ORDER BY t.name COLLATE NOCASE""", (contest_id,),
        ).fetchall()
        players = db.execute(
            """SELECT u.id, u.username, u.display_name, m.team_id, t.name AS team_name
               FROM users u LEFT JOIN memberships m ON m.user_id = u.id AND m.contest_id = ?
               LEFT JOIN teams t ON t.id = m.team_id WHERE u.role = 'player' ORDER BY u.display_name, u.id""", (contest_id,),
        ).fetchall()
        if g.managed["mode"] == "round":
            matrix = service_matrix(db, contest_id, public=False)
            events = score_records(db, contest_id, limit=20)
    title = "队伍与服务" if matrix is not None else "队伍管理"
    return render_template("manage/teams.html", title=title, rows=rows, players=players, matrix=matrix, events=events)


@manage.route("/teams/new", defaults={"team_id": None}, methods=["GET", "POST"])
@manage.route("/teams/<int:team_id>/edit", methods=["GET", "POST"])
@admin_required
def team_edit(team_id):
    contest_id = managed_id()
    team = scoped_record("teams", team_id, contest_id) if team_id is not None else None
    if request.method == "GET":
        return render_template("manage/team_form.html", title="编辑队伍" if team else "创建队伍", team=team)
    name = text_field("name", "队名", 50)
    db = get_db()
    try:
        with transaction(db, write=True):
            managed_id()
            if team:
                updated = db.execute(
                    "UPDATE teams SET name = ? WHERE id = ? AND contest_id = ?", (name, team_id, contest_id),
                )
                if not updated.rowcount:
                    abort(404)
            else:
                db.execute("INSERT INTO teams (contest_id, name) VALUES (?, ?)", (contest_id, name))
    except sqlite3.IntegrityError as error:
        raise FormError("当前比赛已有同名队伍。") from error
    return saved("队伍已保存。", "manage.teams")


@manage.route("/teams/<int:team_id>/delete", methods=["GET", "POST"])
@admin_required
def team_delete(team_id):
    contest_id = managed_id()
    db = get_db()
    if request.method == "GET":
        team = scoped_record("teams", team_id, contest_id)
        return render_template(
            "manage/delete.html", title="删除队伍", subject=team["name"], scoped=True, back=url_for("manage.teams"),
            impact="将删除此队伍的提交、积分和服务状态，并解除成员分队。",
        )
    with transaction(db, write=True):
        require_current_admin(db)
        if not db.execute("DELETE FROM teams WHERE id = ? AND contest_id = ?", (team_id, contest_id)).rowcount:
            abort(404)
    return saved("队伍已删除。", "manage.teams")


@manage.post("/teams/membership")
@admin_required
def membership():
    contest_id = managed_id()
    user_id = integer_field("user_id", "参赛账号", 1, 2**63 - 1)
    team_id = request.form.get("team_id", type=int)
    if request.form.get("team_id") not in (None, "") and (team_id is None or not 1 <= team_id < 2**63):
        raise FormError("请选择有效队伍。")
    db = get_db()
    with transaction(db, write=True):
        managed_id()
        if not db.execute("SELECT 1 FROM users WHERE id = ? AND role = 'player'", (user_id,)).fetchone():
            abort(404)
        if team_id is None:
            db.execute("DELETE FROM memberships WHERE contest_id = ? AND user_id = ?", (contest_id, user_id))
        else:
            scoped_record("teams", team_id, contest_id)
            db.execute(
                """INSERT INTO memberships (contest_id, user_id, team_id) VALUES (?, ?, ?)
                   ON CONFLICT(contest_id, user_id) DO UPDATE SET team_id = excluded.team_id""", (contest_id, user_id, team_id),
            )
    return saved("参赛账号的队伍已更新。", "manage.teams")


@manage.get("/challenges/<kind>")
@admin_required
def challenges(kind):
    table = CHALLENGE_TABLES.get(kind)
    if not table:
        abort(404)
    rows = get_db().execute(
        f"SELECT * FROM {table} WHERE contest_id = ? ORDER BY published DESC, id", (managed_id(kind),),
    ).fetchall() if g.managed else []
    return render_template("manage/challenges.html", title="题目管理", rows=rows, kind=kind)


@manage.route("/challenges/<kind>/new", defaults={"task_id": None}, methods=["GET", "POST"])
@manage.route("/challenges/<kind>/<int:task_id>/edit", methods=["GET", "POST"])
@admin_required
def challenge_edit(kind, task_id):
    table = CHALLENGE_TABLES.get(kind)
    if not table:
        abort(404)
    contest_id = managed_id(kind)
    task = scoped_record(table, task_id, contest_id) if task_id is not None else None
    db = get_db()
    if request.method == "GET":
        files = db.execute(
            """SELECT f.id, f.name FROM puzzle_files a JOIN uploads f ON f.id = a.file_id WHERE a.puzzle_id = ?""",
            (task_id,),
        ).fetchall() if task and kind == "puzzle" else []
        return render_template("manage/challenge_form.html", title="编辑赛题" if task else "创建赛题", task=task,
                               kind=kind, files=files, categories=CATEGORIES, difficulties=DIFFICULTIES)
    title = text_field("title", "题目名称", 100)
    description = text_field("description", "题目说明", 16000)
    published = request.form.get("published") == "1"
    if kind == "puzzle":
        category = choice_field("category", "分类", CATEGORIES)
        difficulty = choice_field("difficulty", "难度", DIFFICULTIES)
        points = integer_field("points", "分值", 1, 100_000)
        flag = text_field("flag", "Flag", 256, optional=task is not None)
        salt_and_digest = new_flag(flag) if flag else None
        uploads = [read_upload(file, "attachment", required=True) for file in request.files.getlist("attachments") if file.filename]
        remove_ids = [int(item) for item in request.form.getlist("remove_file") if item.isdecimal() and len(item) <= 18]
        with transaction(db, write=True):
            managed_id(kind)
            if task:
                if salt_and_digest:
                    db.execute("UPDATE puzzles SET flag_salt = ?, flag_digest = ? WHERE id = ?", (*salt_and_digest, task_id))
                db.execute(
                    """UPDATE puzzles SET title = ?, category = ?, difficulty = ?, description = ?, points = ?, published = ?
                       WHERE id = ? AND contest_id = ?""", (title, category, difficulty, description, points, published, task_id, contest_id),
                )
            else:
                task_id = db.execute(
                    """INSERT INTO puzzles (contest_id, title, category, difficulty, description, points, flag_salt, flag_digest, published)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (contest_id, title, category, difficulty, description, points, *salt_and_digest, published),
                ).lastrowid
            for file_id in remove_ids:
                db.execute("DELETE FROM puzzle_files WHERE puzzle_id = ? AND file_id = ?", (task_id, file_id))
            for upload in uploads:
                db.execute("INSERT INTO puzzle_files VALUES (?, ?)", (task_id, save_upload(db, upload, g.now)))
    else:
        baseline = text_field("baseline", "基线版本", 80)
        with transaction(db, write=True):
            managed_id(kind)
            if task:
                db.execute(
                    "UPDATE round_tasks SET title = ?, baseline = ?, description = ?, published = ? WHERE id = ? AND contest_id = ?",
                    (title, baseline, description, published, task_id, contest_id),
                )
            else:
                db.execute(
                    "INSERT INTO round_tasks (contest_id, title, baseline, description, published) VALUES (?, ?, ?, ?, ?)",
                    (contest_id, title, baseline, description, published),
                )
    return saved("赛题已保存。", "manage.challenges", kind=kind)


@manage.post("/services/status")
@admin_required
def update_status():
    contest_id = managed_id()
    team_id = integer_field("team_id", "队伍", 1, 2**63 - 1)
    task_id = integer_field("task_id", "服务", 1, 2**63 - 1)
    status = choice_field("status", "服务状态", SERVICE_STATES)
    db = get_db()
    with transaction(db, write=True):
        require_current_admin(db)
        managed_id("round")
        scoped_record("teams", team_id, contest_id)
        scoped_record("round_tasks", task_id, contest_id)
        db.execute(
            """INSERT INTO service_states VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(team_id, task_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at,
                 updated_by = excluded.updated_by""", (contest_id, team_id, task_id, status, g.now, g.user["id"]),
        )
    return saved("服务状态已更新。", "manage.teams")


@manage.post("/scores")
@admin_required
def record_score():
    contest_id = managed_id()
    team_id = integer_field("team_id", "队伍", 1, 2**63 - 1)
    kind = choice_field("kind", "计分项目", SCORE_KINDS)
    delta = integer_field("delta", "分数变化", -100_000, 100_000)
    reason = text_field("reason", "计分说明", 500)
    db = get_db()
    with transaction(db, write=True):
        require_current_admin(db)
        managed_id("round")
        contest = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
        state = contest_state(contest, g.now)
        if state["round"] < 1:
            raise FormError("开赛后即可录入回合积分。")
        if delta == 0:
            raise FormError("分数变化不能为 0。")
        round_no = integer_field("round_no", "回合", 1, state["round"])
        scoped_record("teams", team_id, contest_id)
        add_score(db, contest_id=contest_id, team_id=team_id, kind=kind, delta=delta, round_no=round_no, reason=reason,
                  actor_id=g.user["id"], now=g.now)
    return saved("得分变化已写入计分流水。", "manage.teams")


@manage.get("/submissions")
@admin_required
def submissions():
    kind = records_kind(g.managed)
    page = page_number()
    records = []
    if g.managed:
        query = flag_records if kind == "flag" else patch_records
        records = query(get_db(), g.managed["id"], limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    return render_template("records.html", title="提交管理", kind=kind, rows=records[:PAGE_SIZE], page=page,
                           more=len(records) > PAGE_SIZE, admin=True)


@manage.route("/patches/<int:patch_id>", methods=["GET", "POST"])
@admin_required
def patch_review(patch_id):
    contest_id = managed_id("round")
    db = get_db()
    patch = db.execute(
        """SELECT p.*, t.name AS team_name, r.title, f.name AS file_name FROM patches p
           JOIN teams t ON t.id = p.team_id JOIN round_tasks r ON r.id = p.task_id JOIN uploads f ON f.id = p.file_id
           WHERE p.contest_id = ? AND p.id = ?""", (contest_id, patch_id),
    ).fetchone()
    if not patch:
        abort(404)
    if request.method == "GET":
        return render_template("manage/patch_review.html", title="补丁提交详情", patch=patch)
    status = choice_field("status", "评测状态", PATCH_STATES)
    notes = text_field("review_notes", "评测说明", 2000, optional=True)
    award = integer_field("award", "加固积分", 0, 100_000) if status == "passed" else 0
    with transaction(db, write=True):
        require_current_admin(db)
        managed_id("round")
        current = scoped_record("patches", patch_id, contest_id)
        delta = award - current["award"]
        db.execute(
            """UPDATE patches SET status = ?, review_notes = ?, award = ?, reviewed_at = ?, reviewed_by = ?
               WHERE id = ? AND contest_id = ?""", (status, notes, award, g.now, g.user["id"], patch_id, contest_id),
        )
        if delta:
            contest = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
            round_no = max(1, contest_state(contest, g.now)["round"])
            add_score(db, contest_id=contest_id, team_id=current["team_id"], kind="defense", delta=delta, round_no=round_no,
                      reason=f"补丁 P-{patch_id:04d} 评测计分", actor_id=g.user["id"], now=g.now, patch_id=patch_id)
    return saved("评测结果已保存。", "manage.patch_review", patch_id=patch_id)


@manage.route("/brand", methods=["GET", "POST"])
@admin_required
def brand():
    if request.method == "GET":
        return render_template("manage/brand.html", title="品牌与外观")
    name = text_field("name", "平台名称", 48)
    icon = choice_field("icon", "图标", ("flag", "shield", "code"))
    colors = [request.form.get(field, "").lower() for field in ("accent", "background", "surface")]
    if not all(re.fullmatch(r"#[0-9a-f]{6}", color) for color in colors):
        raise FormError("颜色须使用六位十六进制。")
    radius = integer_field("radius", "卡片圆角", 4, 24)
    logo = read_upload(request.files.get("logo"), "logo")
    remove_logo = request.form.get("remove_logo") == "1"
    db = get_db()
    with transaction(db, write=True):
        old_id = db.execute("SELECT logo_id FROM site_settings WHERE id = 1").fetchone()[0]
        logo_id = save_upload(db, logo, g.now) if logo else (None if remove_logo else old_id)
        db.execute(
            "UPDATE site_settings SET name = ?, icon = ?, accent = ?, background = ?, surface = ?, radius = ?, logo_id = ? WHERE id = 1",
            (name, icon, *colors, radius, logo_id),
        )
        if old_id is not None and old_id != logo_id:
            db.execute("DELETE FROM uploads WHERE id = ?", (old_id,))
    return saved("平台外观已应用。", "manage.brand")
