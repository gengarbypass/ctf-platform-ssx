import csv
import io
import re
import sqlite3

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from .db import get_db, transaction
from .domain import (
    CATEGORIES, CHALLENGE_TABLES, CONTEST_MODES, DIFFICULTIES, PATCH_STATES, SCORE_KINDS, SERVICE_STATES, FormError, admin_required,
    choice_field, contest_state, integer_field, new_flag, parse_local_time, text_field, username_value, validate_password,
)
from .queries import add_score, breach_records, flag_records, patch_records, score_records, service_matrix
from .range import RangeError, probe_image, stop_environment
from .storage import crop_cover, read_upload, save_upload
from .views import PAGE_SIZE, page_number, records_kind


def purge_environments(env_rows):
    """尽力停止并移除一批环境容器（删除队伍/比赛后防止孤儿容器）。"""
    for row in env_rows:
        try:
            stop_environment(row["contest_id"], row["team_id"], row["puzzle_id"], container_id=row["container_id"])
        except RangeError:
            pass


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


def csv_response(body, filename):
    """带 BOM 的 CSV 下载，Excel 直接打开不乱码。"""
    from flask import Response
    return Response("\ufeff" + body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


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
        metrics["teams"] = db.execute("SELECT COUNT(*) FROM contest_teams WHERE contest_id = ?", (contest_id,)).fetchone()[0]
        if mode == "puzzle":
            metrics["challenges"] = db.execute(
                """SELECT COUNT(*) FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id
                   WHERE cp.contest_id = ? AND p.published = 1""", (contest_id,),
            ).fetchone()[0]
        else:
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
    env_image = env_container_port = env_ttl_minutes = env_max = None
    if mode in ("breach", "puzzle"):
        if mode == "breach":
            env_image = request.form.get("env_image", "").strip() or None
            if env_image:
                if len(env_image) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]*", env_image):
                    raise FormError("环境镜像名须为合法镜像引用（字母、数字与 . _ : / - 组成）。")
                env_container_port = integer_field("env_container_port", "容器端口", 1, 65535)
        # 环境时长与并发上限留空时，环境启动按默认值（120 分钟 / 20 个）执行
        if request.form.get("env_ttl_minutes", "").strip():
            env_ttl_minutes = integer_field("env_ttl_minutes", "环境时长", 5, 1440)
        if request.form.get("env_max", "").strip():
            env_max = integer_field("env_max", "环境上限", 1, 500)
    if end_at <= start_at:
        raise FormError("结束时间须晚于开赛时间。")
    cover = crop_cover(read_upload(request.files.get("cover"), "cover"), request.form)
    remove_cover = request.form.get("remove_cover") == "1"
    with transaction(db, write=True):
        if contest:
            contest = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
            if not contest:
                abort(404)
            if contest["active"] and mode != contest["mode"]:
                raise FormError("请先撤下比赛，再修改比赛模式。")
            if mode != "round":
                round_seconds = contest["round_seconds"]
            last_round = db.execute("SELECT MAX(round_no) FROM score_events WHERE contest_id = ?", (contest_id,)).fetchone()[0]
            if last_round and (
                round_seconds is None or start_at != contest["start_at"] or round_seconds != contest["round_seconds"]
                or (end_at - start_at + round_seconds - 1) // round_seconds < last_round
            ):
                raise FormError("已有回合积分记录，请保留开赛时间、回合长度和已计分回合。")
        old_cover_id = contest["cover_id"] if contest else None
        cover_id = save_upload(db, cover, g.now) if cover else (None if remove_cover else old_cover_id)
        values = (title, summary, mode, start_at, end_at, round_seconds,
                  env_image, env_container_port, env_ttl_minutes, env_max, cover_id)
        if contest:
            db.execute(
                """UPDATE contests SET title = ?, summary = ?, mode = ?, start_at = ?, end_at = ?, round_seconds = ?,
                   env_image = ?, env_container_port = ?, env_ttl_minutes = ?, env_max = ?, cover_id = ? WHERE id = ?""",
                (*values, contest_id),
            )
        else:
            contest_id = db.execute(
                """INSERT INTO contests (title, summary, mode, start_at, end_at, round_seconds,
                                         env_image, env_container_port, env_ttl_minutes, env_max, cover_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*values, g.now),
            ).lastrowid
            # 新比赛自动沿用最近一场比赛的队伍与分队，避免重复配置
            src = db.execute(
                "SELECT contest_id FROM contest_teams WHERE contest_id != ? ORDER BY contest_id DESC LIMIT 1",
                (contest_id,),
            ).fetchone()
            carried = 0
            if src:
                db.execute(
                    "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) SELECT ?, team_id FROM contest_teams WHERE contest_id = ?",
                    (contest_id, src["contest_id"]),
                )
                db.execute(
                    """INSERT OR IGNORE INTO memberships (contest_id, user_id, team_id)
                       SELECT ?, user_id, team_id FROM memberships WHERE contest_id = ?""",
                    (contest_id, src["contest_id"]),
                )
                carried = db.execute(
                    "SELECT COUNT(DISTINCT team_id) FROM contest_teams WHERE contest_id = ?", (contest_id,),
                ).fetchone()[0]
        if old_cover_id is not None and cover_id != old_cover_id:
            db.execute("DELETE FROM uploads WHERE id = ?", (old_cover_id,))
    session["manage_contest_id"] = contest_id
    if not contest:
        note = f"已沿用此前比赛的 {carried} 支队伍与选手分队，可在「队伍管理」调整。" if carried else ""
        return saved("比赛已创建，可以开始筹备赛题。" + note, "manage.challenges", kind=mode)
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
        impact = "将一并删除本场比赛的队伍、提交、积分和关联文件；题库赛题会保留，供其他比赛复用。"
        if contest["active"]:
            impact += "当前比赛会立即下线。"
        return render_template("manage/delete.html", title="删除比赛", subject=contest["title"],
                               impact=impact, back=url_for("manage.contests"))
    with transaction(db, write=True):
        require_current_admin(db)
        env_rows = db.execute("SELECT contest_id, team_id, puzzle_id, container_id FROM breach_environments WHERE contest_id = ?", (contest_id,)).fetchall()
        for table in ("flag_submissions", "patches", "score_events", "service_states", "breach_submissions", "breach_environments"):
            db.execute(f"DELETE FROM {table} WHERE contest_id = ?", (contest_id,))
        if not db.execute("DELETE FROM contests WHERE id = ?", (contest_id,)).rowcount:
            abort(404)
    purge_environments(env_rows)
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


def _csv_rows(file, required):
    """读取上传的 CSV（UTF-8，兼容 Excel BOM），校验表头，返回数据行。"""
    if file is None or not file.filename:
        raise FormError("请选择要导入的 CSV 文件。")
    raw = file.stream.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")   # 兼容中文 Excel 默认的 ANSI/GBK 导出
        except UnicodeDecodeError as error:
            raise FormError("CSV 文件须为 UTF-8 或 GBK 编码。") from error
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        raise FormError("文件内容为空。")
    header = [cell.strip() for cell in rows[0]]
    if header != required:
        raise FormError(f"表头须为：{'，'.join(required)}（可从模板下载获取）。")
    data = rows[1:]
    if not data:
        raise FormError("模板中没有数据行。")
    return data


@manage.get("/import")
@admin_required
def import_page():
    return render_template("manage/import.html", title="批量导入", has_contest=bool(g.managed))


@manage.get("/import/users-template")
@admin_required
def users_template():
    body = "账号,显示名,密码,角色\nbluefox,蓝狐队长,ChangeMe-2026,player\n"
    return csv_response(body, "users-template.csv")


@manage.get("/import/teams-template")
@admin_required
def teams_template():
    body = "队伍,账号,显示名,密码\n蓝狐突击队,fox01,队员一号,ChangeMe-2026\n蓝狐突击队,fox02,队员二号,ChangeMe-2026\n极光战队,,,\n"
    return csv_response(body, "teams-template.csv")


@manage.post("/import/users")
@admin_required
def import_users():
    db = get_db()
    data = _csv_rows(request.files.get("file"), ["账号", "显示名", "密码", "角色"])
    seen, prepared = set(), []
    for number, row in enumerate(data, start=2):
        if len(row) < 4:
            raise FormError(f"第 {number} 行列数不足（须为 4 列）。")
        username = username_value(row[0])
        if username in seen:
            raise FormError(f"第 {number} 行账号 {username} 在文件中重复。")
        seen.add(username)
        name = row[1].strip()
        if not name or len(name) > 50:
            raise FormError(f"第 {number} 行显示名须为 1–50 个字符。")
        password = row[2].strip()
        validate_password(password)
        role = row[3].strip() or "player"
        if role not in ("player", "admin"):
            raise FormError(f"第 {number} 行角色须为 player 或 admin。")
        prepared.append((username, name, generate_password_hash(password), role))
    with transaction(db, write=True):
        for username, *_ in prepared:
            if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise FormError(f"账号 {username} 已存在，导入未执行（全部回滚）。")
        db.executemany(
            "INSERT INTO users (username, display_name, password_hash, role, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            [(username, name, pwd, role, g.now) for username, name, pwd, role in prepared],
        )
    return saved(f"已导入 {len(prepared)} 个账号。", "manage.import_page")


@manage.post("/import/teams")
@admin_required
def import_teams():
    contest_id = request.form.get("contest_id", type=int)
    db = get_db()
    if not contest_id or not db.execute("SELECT 1 FROM contests WHERE id = ?", (contest_id,)).fetchone():
        raise FormError("请先创建比赛（导入页会自动带上当前编辑的比赛）。")
    data = _csv_rows(request.files.get("file"), ["队伍", "账号", "显示名", "密码"])
    with transaction(db, write=True):
        team_ids = {}
        for row in db.execute("SELECT id, name FROM teams"):
            team_ids[row["name"]] = row["id"]
        users = {row["username"]: row["id"] for row in db.execute("SELECT id, username FROM users")}
        teams_created, users_created, assigned = 0, 0, 0
        for number, row in enumerate(data, start=2):
            if len(row) < 4:
                raise FormError(f"第 {number} 行列数不足（须为 4 列）。")
            team_name = row[0].strip()
            if not team_name or len(team_name) > 50:
                raise FormError(f"第 {number} 行队伍名须为 1–50 个字符。")
            if team_name not in team_ids:
                # 名册大小写不敏感：大小写变体视为同一支队伍，直接复用
                existing = db.execute("SELECT id FROM teams WHERE name = ? COLLATE NOCASE", (team_name,)).fetchone()
                team_ids[team_name] = existing["id"] if existing else db.execute(
                    "INSERT INTO teams (name) VALUES (?)", (team_name,),
                ).lastrowid
                if not existing:
                    teams_created += 1
            db.execute(
                "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) VALUES (?, ?)",
                (contest_id, team_ids[team_name]),
            )
            username = row[1].strip().lower()
            if not username:
                continue
            if username in users:
                user_id = users[username]
            else:
                name = row[2].strip() or username
                password = row[3].strip()
                validate_password(password)
                if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
                    raise FormError(f"第 {number} 行账号格式无效（3–40 位小写字母、数字、点、下划线或连字符）。")
                user_id = db.execute(
                    "INSERT INTO users (username, display_name, password_hash, role, enabled, created_at) VALUES (?, ?, ?, 'player', 1, ?)",
                    (username, name, generate_password_hash(password), g.now),
                ).lastrowid
                users[username] = user_id
                users_created += 1
            db.execute(
                "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) VALUES (?, ?)",
                (contest_id, team_ids[team_name]),
            )
            db.execute(
                """INSERT INTO memberships (contest_id, user_id, team_id) VALUES (?, ?, ?)
                   ON CONFLICT(contest_id, user_id) DO UPDATE SET team_id = excluded.team_id""",
                (contest_id, user_id, team_ids[team_name]),
            )
            assigned += 1
    return saved(f"导入完成：新增队伍 {teams_created} 支、新增账号 {users_created} 个、分队 {assigned} 人。", "manage.import_page")


@manage.route("/accounts/delete-bulk", methods=["POST"])
@admin_required
def accounts_bulk_delete():
    """批量删除账号：第一阶段渲染确认页，第二阶段（stage=execute）执行删除。"""
    ids = sorted({int(v) for v in request.form.getlist("select") if v.isdecimal() and 1 <= int(v) <= 2**63 - 1})
    if not ids:
        return saved("请先勾选要删除的账号。", "manage.accounts")
    if g.user["id"] in ids:
        raise FormError("批量删除不能包含当前登录账号。")
    db = get_db()
    placeholders = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT id, username, display_name, role, enabled FROM users WHERE id IN ({placeholders})", ids,
    ).fetchall()
    if len(rows) != len(ids):
        raise FormError("部分账号不存在，请刷新后重试。")
    if request.form.get("stage") != "execute":
        return render_template("manage/accounts_bulk.html", accounts=rows, ids=ids)
    with transaction(db, write=True):
        require_current_admin(db)
        admin_selected = any(row["role"] == "admin" and row["enabled"] for row in rows)
        remaining = db.execute(
            f"SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1 AND id NOT IN ({placeholders})", ids,
        ).fetchone()[0]
        if admin_selected and remaining == 0:
            raise FormError("至少保留一个启用的赛事管理账号。")
        db.execute(f"DELETE FROM users WHERE id IN ({placeholders})", ids)
    return saved(f"已删除 {len(ids)} 个账号。", "manage.accounts")


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
            """SELECT t.id, t.name,
                      (ct.team_id IS NOT NULL) AS in_contest,
                      (SELECT COUNT(*) FROM memberships m WHERE m.team_id = t.id AND m.contest_id = ?) AS members,
                      (SELECT GROUP_CONCAT(c.title, '、') FROM contest_teams ct2
                         JOIN contests c ON c.id = ct2.contest_id WHERE ct2.team_id = t.id) AS contests
               FROM teams t LEFT JOIN contest_teams ct ON ct.team_id = t.id AND ct.contest_id = ?
               GROUP BY t.id ORDER BY t.name COLLATE NOCASE""", (contest_id, contest_id),
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


@manage.post("/contests/<int:contest_id>/teams/add")
@admin_required
def contest_team_add(contest_id):
    team_id = integer_field("team_id", "队伍", 1, 2**63 - 1)
    db = get_db()
    with transaction(db, write=True):
        if not db.execute("SELECT 1 FROM contests WHERE id = ?", (contest_id,)).fetchone():
            abort(404)
        if not db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
            abort(404)
        db.execute(
            "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) VALUES (?, ?)", (contest_id, team_id),
        )
    return saved("队伍已加入比赛。", "manage.teams")


@manage.post("/contests/<int:contest_id>/teams/remove")
@admin_required
def contest_team_remove(contest_id):
    team_id = integer_field("team_id", "队伍", 1, 2**63 - 1)
    db = get_db()
    with transaction(db, write=True):
        if not db.execute("SELECT 1 FROM contests WHERE id = ?", (contest_id,)).fetchone():
            abort(404)
        db.execute("DELETE FROM contest_teams WHERE contest_id = ? AND team_id = ?", (contest_id, team_id))
        db.execute("DELETE FROM memberships WHERE contest_id = ? AND team_id = ?", (contest_id, team_id))
    return saved("队伍已移出本场比赛（名册保留，可再次加入）。", "manage.teams")


@manage.route("/teams/new", defaults={"team_id": None}, methods=["GET", "POST"])
@manage.route("/teams/<int:team_id>/edit", methods=["GET", "POST"])
@admin_required
def team_edit(team_id):
    contest_id = managed_id()
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone() if team_id is not None else None
    if team_id is not None and not team:
        abort(404)
    if request.method == "GET":
        return render_template("manage/team_form.html", title="编辑队伍" if team else "创建队伍", team=team)
    name = text_field("name", "队名", 50)
    with transaction(db, write=True):
        managed_id()
        if team:
            db.execute("UPDATE teams SET name = ? WHERE id = ?", (name, team_id))
        else:
            # 队伍为全局名册：同名即复用同一支队伍（幂等），并加入当前比赛
            existing = db.execute("SELECT id FROM teams WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            team_id = existing["id"] if existing else db.execute(
                "INSERT INTO teams (name) VALUES (?)", (name,),
            ).lastrowid
        db.execute(
            "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) VALUES (?, ?)", (contest_id, team_id),
        )
    return saved("队伍已保存并加入当前比赛。", "manage.teams")


@manage.route("/teams/<int:team_id>/delete", methods=["GET", "POST"])
@admin_required
def team_delete(team_id):
    db = get_db()
    team = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        abort(404)
    if request.method == "GET":
        return render_template(
            "manage/delete.html", title="删除队伍", subject=team["name"], scoped=True, back=url_for("manage.teams"),
            impact="队伍为全局名册：将从所有比赛中移除并解除成员分队，同时删除其全部提交、补丁、积分、服务状态与环境容器。",
        )
    with transaction(db, write=True):
        require_current_admin(db)
        env_rows = db.execute("SELECT contest_id, team_id, puzzle_id, container_id FROM breach_environments WHERE team_id = ?", (team_id,)).fetchall()
        if not db.execute("DELETE FROM teams WHERE id = ?", (team_id,)).rowcount:
            abort(404)
    purge_environments(env_rows)
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
            if not db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
                abort(404)
            db.execute(
                """INSERT INTO memberships (contest_id, user_id, team_id) VALUES (?, ?, ?)
                   ON CONFLICT(contest_id, user_id) DO UPDATE SET team_id = excluded.team_id""",
                (contest_id, user_id, team_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO contest_teams (contest_id, team_id) VALUES (?, ?)", (contest_id, team_id),
            )
    return saved("参赛账号的队伍已更新。", "manage.teams")


@manage.get("/challenges/<kind>")
@admin_required
def challenges(kind):
    if kind == "puzzle":
        return redirect(url_for("manage.bank"), code=301)
    table = CHALLENGE_TABLES.get(kind)
    if not table:
        abort(404)
    rows = get_db().execute(
        f"SELECT * FROM {table} WHERE contest_id = ? ORDER BY published DESC, id", (managed_id(kind),),
    ).fetchall() if g.managed else []
    return render_template("manage/challenges.html", title="题目管理", rows=rows, kind=kind)


@manage.get("/bank")
@admin_required
def bank():
    kind = request.args.get("kind", "")
    if kind and kind not in ("puzzle", "round"):
        abort(404)
    rows = get_db().execute(
        """SELECT p.*, (p.env_image IS NOT NULL) AS has_env,
                  GROUP_CONCAT(c.title, '、') AS contests
           FROM puzzles p
           LEFT JOIN contest_puzzles cp ON cp.puzzle_id = p.id
           LEFT JOIN contests c ON c.id = cp.contest_id
           GROUP BY p.id ORDER BY p.id DESC""",
    ).fetchall()
    if kind:
        rows = [row for row in rows if row["kind"] == kind]
    return render_template("manage/bank.html", title="题库", rows=rows, kind=kind)


@manage.get("/contests/<int:contest_id>/puzzles")
@admin_required
def contest_puzzles(contest_id):
    db = get_db()
    contest = db.execute("SELECT * FROM contests WHERE id = ?", (contest_id,)).fetchone()
    if not contest or contest["mode"] not in ("puzzle", "round"):
        abort(404)
    kind = contest["mode"]
    unit = "靶机" if kind == "round" else "赛题"
    rows = db.execute(
        """SELECT p.id, p.title, p.category, p.points, p.published, (p.env_image IS NOT NULL) AS has_env
           FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id
           WHERE cp.contest_id = ? AND p.kind = ? ORDER BY p.id""",
        (contest_id, kind),
    ).fetchall()
    available = db.execute(
        """SELECT p.id, p.title, p.category, p.points FROM puzzles p
           WHERE p.kind = ? AND p.id NOT IN (SELECT puzzle_id FROM contest_puzzles WHERE contest_id = ?)
           ORDER BY p.id DESC""",
        (kind, contest_id),
    ).fetchall()
    return render_template("manage/contest_puzzles.html", title=f"比赛{unit}", contest=contest,
                           rows=rows, available=available, unit=unit)


@manage.post("/contests/<int:contest_id>/puzzles/add")
@admin_required
def contest_puzzle_add(contest_id):
    puzzle_id = integer_field("puzzle_id", "赛题", 1, 2**63 - 1)
    db = get_db()
    with transaction(db, write=True):
        contest_row = db.execute("SELECT mode FROM contests WHERE id = ?", (contest_id,)).fetchone()
        if not contest_row:
            abort(404)
        # 只允许同类型：解题赛题入解题赛，AWDP 靶机入 AWDP 比赛
        if not db.execute(
            "SELECT 1 FROM puzzles WHERE id = ? AND kind = ?", (puzzle_id, contest_row["mode"]),
        ).fetchone():
            abort(404)
        db.execute(
            "INSERT OR IGNORE INTO contest_puzzles (contest_id, puzzle_id, added_at) VALUES (?, ?, ?)",
            (contest_id, puzzle_id, g.now),
        )
    return saved("赛题已加入比赛。", "manage.contest_puzzles", contest_id=contest_id)


@manage.post("/contests/<int:contest_id>/puzzles/remove")
@admin_required
def contest_puzzle_remove(contest_id):
    puzzle_id = integer_field("puzzle_id", "赛题", 1, 2**63 - 1)
    db = get_db()
    with transaction(db, write=True):
        if not db.execute("SELECT 1 FROM contests WHERE id = ?", (contest_id,)).fetchone():
            abort(404)
        db.execute("DELETE FROM contest_puzzles WHERE contest_id = ? AND puzzle_id = ?", (contest_id, puzzle_id))
    return saved("赛题已移出比赛（题库保留，历史提交仍计入原成绩）。", "manage.contest_puzzles", contest_id=contest_id)


@manage.post("/bank/probe")
@admin_required
def bank_probe():
    """创建赛题页的「检测连通性」按钮：即时验证镜像可拉取且配置端口可访问，不落库。"""
    data = request.get_json(silent=True) or {}
    image = (data.get("env_image") or "").strip()
    if not image or len(image) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]*", image):
        return jsonify(ok=False, message="请先填写合法的环境镜像名。")
    try:
        port = int(data.get("env_container_port"))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="容器端口须为 1–65535 的整数。")
    if not 1 <= port <= 65535:
        return jsonify(ok=False, message="容器端口须为 1–65535 的整数。")
    ok, reason = probe_image(image, port)
    return jsonify(ok=ok, message="连接成功，镜像可拉取且端口可访问。" if ok else f"检测失败：{reason}")


@manage.route("/bank/new", defaults={"task_id": None}, methods=["GET", "POST"])
@manage.route("/bank/<int:task_id>/edit", methods=["GET", "POST"])
@admin_required
def bank_edit(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM puzzles WHERE id = ?", (task_id,)).fetchone() if task_id is not None else None
    if task_id is not None and not task:
        abort(404)
    # 解题赛题与 AWDP 靶机共用同一套题库表单与校验策略；编辑时沿用题目自身类型
    kind = task["kind"] if task else request.form.get("kind", request.args.get("kind", "puzzle"))
    if kind not in ("puzzle", "round"):
        abort(404)
    unit = "靶机" if kind == "round" else "赛题"
    raw_join = request.form.get("join_contest", request.args.get("contest", "")).strip()
    join_contest = int(raw_join) if raw_join.isdecimal() else None
    if request.method == "GET":
        files = db.execute(
            """SELECT f.id, f.name FROM puzzle_files a JOIN uploads f ON f.id = a.file_id WHERE a.puzzle_id = ?""",
            (task_id,),
        ).fetchall() if task else []
        return render_template("manage/challenge_form.html", title=f"编辑{unit}" if task else f"创建{unit}", task=task,
                               kind=kind, bank=True, files=files, categories=CATEGORIES, difficulties=DIFFICULTIES,
                               join_contest=join_contest, subtitle="题库")
    title = text_field("title", "题目名称", 100)
    description = text_field("description", "题目说明", 16000)
    published = request.form.get("published") == "1"
    category = choice_field("category", "分类", CATEGORIES)
    difficulty = choice_field("difficulty", "难度", DIFFICULTIES)
    points = integer_field("points", "分值", 1, 100_000)
    env_image = request.form.get("env_image", "").strip() or None
    env_container_port = None
    if env_image:
        if len(env_image) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]*", env_image):
            raise FormError("环境镜像名须为合法镜像引用（字母、数字与 . _ : / - 组成）。")
        env_container_port = integer_field("env_container_port", "容器端口", 1, 65535)
    flag = text_field("flag", "Flag", 256, optional=True)
    if not task and not flag and not env_image:
        raise FormError("请填写 Flag；或在填写环境镜像后留空以按队伍动态生成。")
    if not env_image and task and not flag and not task["flag_digest"]:
        raise FormError("该赛题当前为动态环境赛题，请保留环境镜像或改填固定 Flag。")
    salt_and_digest = new_flag(flag) if flag else (("", "") if (env_image and not task) else None)
    uploads = [read_upload(file, "attachment", required=True) for file in request.files.getlist("attachments") if file.filename]
    remove_ids = [int(item) for item in request.form.getlist("remove_file") if item.isdecimal() and len(item) <= 18]
    with transaction(db, write=True):
        if join_contest:
            contest_row = db.execute("SELECT mode FROM contests WHERE id = ?", (join_contest,)).fetchone()
            if not contest_row:
                raise FormError("要加入的比赛不存在。")
            if contest_row["mode"] != kind:
                raise FormError(f"该比赛不是{'AWDP' if kind == 'round' else '解题赛'}比赛，无法加入此{unit}。")
        if task:
            if salt_and_digest:
                db.execute("UPDATE puzzles SET flag_salt = ?, flag_digest = ? WHERE id = ?", (*salt_and_digest, task_id))
            db.execute(
                """UPDATE puzzles SET title = ?, category = ?, difficulty = ?, description = ?, points = ?, published = ?,
                   env_image = ?, env_container_port = ? WHERE id = ?""",
                (title, category, difficulty, description, points, published, env_image, env_container_port, task_id),
            )
        else:
            task_id = db.execute(
                """INSERT INTO puzzles (kind, title, category, difficulty, description, points, flag_salt, flag_digest,
                                        published, env_image, env_container_port)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kind, title, category, difficulty, description, points, *salt_and_digest, published,
                 env_image, env_container_port),
            ).lastrowid
        if join_contest:
            db.execute(
                "INSERT OR IGNORE INTO contest_puzzles (contest_id, puzzle_id, added_at) VALUES (?, ?, ?)",
                (join_contest, task_id, g.now),
            )
        for file_id in remove_ids:
            db.execute("DELETE FROM puzzle_files WHERE puzzle_id = ? AND file_id = ?", (task_id, file_id))
        for upload in uploads:
            db.execute("INSERT INTO puzzle_files VALUES (?, ?)", (task_id, save_upload(db, upload, g.now)))
    if join_contest:
        return saved(f"{unit}已保存并加入比赛。", "manage.contest_puzzles", contest_id=join_contest)
    return saved(f"{unit}已保存。", "manage.bank")


@manage.route("/bank/<int:task_id>/delete", methods=["GET", "POST"])
@admin_required
def bank_delete(task_id):
    db = get_db()
    task = db.execute("SELECT id, title FROM puzzles WHERE id = ?", (task_id,)).fetchone()
    if not task:
        abort(404)
    if request.method == "GET":
        return render_template("manage/delete.html", title="删除赛题", subject=task["title"],
                               impact="将删除该赛题及其附件与全部提交记录，并从所有比赛中移除；相关动态环境容器会一并停止。",
                               back=url_for("manage.bank"))
    with transaction(db, write=True):
        require_current_admin(db)
        env_rows = db.execute(
            "SELECT contest_id, team_id, puzzle_id, container_id FROM breach_environments WHERE puzzle_id = ?",
            (task_id,),
        ).fetchall()
        db.execute("DELETE FROM breach_environments WHERE puzzle_id = ?", (task_id,))
        if not db.execute("DELETE FROM puzzles WHERE id = ?", (task_id,)).rowcount:
            abort(404)
    purge_environments(env_rows)
    return saved("赛题已删除。", "manage.bank")


def breach_flag_rows(*, allow_empty_flags):
    """解析批量创建靶点的多行表单；层级留空时沿用上一行，整行留空的行跳过。

    动态环境比赛（已配置镜像）允许 Flag 留空——各队 Flag 由环境启动时派生注入。
    """
    layers = request.form.getlist("layer")
    titles = request.form.getlist("title")
    point_values = request.form.getlist("points")
    flags = request.form.getlist("flag")
    descriptions = request.form.getlist("description")
    count = max(len(titles), len(flags), len(point_values), len(descriptions))
    rows, carried_layer = [], ""
    for index in range(count):
        title = titles[index].strip() if index < len(titles) else ""
        flag = flags[index].strip() if index < len(flags) else ""
        layer = (layers[index].strip() if index < len(layers) else "") or carried_layer
        description = descriptions[index].strip() if index < len(descriptions) else ""
        points_raw = point_values[index] if index < len(point_values) else ""
        if not title and not flag:
            continue
        label = f"第 {index + 1} 行靶点"
        if not title:
            raise FormError(f"请填写{label}的名称。")
        if len(title) > 100:
            raise FormError(f"{label}名称最多 100 个字符。")
        if not flag and not allow_empty_flags:
            raise FormError(f"请填写{label}的 Flag，或在比赛配置中填写环境镜像启用动态 Flag。")
        if len(flag) > 256:
            raise FormError(f"{label}Flag 最多 256 个字符。")
        if not layer:
            raise FormError(f"请填写{label}的所属层级（留空将沿用上一行）。")
        if len(layer) > 40:
            raise FormError(f"{label}层级最多 40 个字符。")
        if len(description) > 16000:
            raise FormError(f"{label}提示最多 16000 个字符。")
        try:
            points = int(points_raw)
        except (TypeError, ValueError) as error:
            raise FormError(f"{label}分值须为整数。") from error
        if not 1 <= points <= 100_000:
            raise FormError(f"{label}分值须在 1 到 100000 之间。")
        salt, digest = new_flag(flag) if flag else ("", "")
        carried_layer = layer
        rows.append({"title": title, "layer": layer, "points": points, "description": description,
                     "flag_salt": salt, "flag_digest": digest})
    if not rows:
        raise FormError("请至少完整填写一行靶点（名称、分值与 Flag）。")
    return rows


@manage.route("/challenges/<kind>/new", defaults={"task_id": None}, methods=["GET", "POST"])
@manage.route("/challenges/<kind>/<int:task_id>/edit", methods=["GET", "POST"])
@admin_required
def challenge_edit(kind, task_id):
    table = CHALLENGE_TABLES.get(kind)
    if not table or kind == "puzzle":
        abort(404)
    contest_id = managed_id(kind)
    task = scoped_record(table, task_id, contest_id) if task_id is not None else None
    db = get_db()
    if request.method == "GET":
        return render_template("manage/challenge_form.html", title="编辑赛题" if task else "创建赛题", task=task,
                               kind=kind, files=[], categories=CATEGORIES, difficulties=DIFFICULTIES,
                               join_contest=None, subtitle=g.managed["title"])
    title = text_field("title", "题目名称", 100)
    description = text_field("description", "题目说明", 16000, optional=kind == "breach")
    published = request.form.get("published") == "1"
    if kind == "breach":
        if task:
            layer = text_field("layer", "所属层级", 40)
            points = integer_field("points", "分值", 1, 100_000)
            flag = text_field("flag", "Flag", 256, optional=True)
            salt_and_digest = new_flag(flag) if flag else None
            with transaction(db, write=True):
                managed_id(kind)
                if salt_and_digest:
                    db.execute("UPDATE breach_flags SET flag_salt = ?, flag_digest = ? WHERE id = ?", (*salt_and_digest, task_id))
                db.execute(
                    """UPDATE breach_flags SET title = ?, layer = ?, description = ?, points = ?, published = ?
                       WHERE id = ? AND contest_id = ?""", (title, layer, description, points, published, task_id, contest_id),
                )
            return saved("靶点已保存。", "manage.challenges", kind=kind)
        rows = breach_flag_rows(allow_empty_flags=bool(db.execute(
            "SELECT env_image FROM contests WHERE id = ?", (contest_id,),
        ).fetchone()[0]))
        with transaction(db, write=True):
            managed_id(kind)
            for row in rows:
                db.execute(
                    """INSERT INTO breach_flags (contest_id, title, layer, description, points, flag_salt, flag_digest, published)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (contest_id, row["title"], row["layer"], row["description"], row["points"],
                     row["flag_salt"], row["flag_digest"], published),
                )
        return saved(f"已创建 {len(rows)} 个靶点。", "manage.challenges", kind=kind)
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
        if not db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
            abort(404)
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
        if not db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
            abort(404)
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
        query = {"flag": flag_records, "patch": patch_records, "breach": breach_records}[kind]
        records = query(get_db(), g.managed["id"], limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    return render_template("records.html", title="提交管理", kind=kind, rows=records[:PAGE_SIZE], page=page,
                           more=len(records) > PAGE_SIZE, admin=True)


@manage.route("/patches/<int:patch_id>", methods=["GET", "POST"])
@admin_required
def patch_review(patch_id):
    contest_id = managed_id("round")
    db = get_db()
    patch = db.execute(
        """SELECT p.*, t.name AS team_name, COALESCE(r.title, z.title) AS title, f.name AS file_name FROM patches p
           JOIN teams t ON t.id = p.team_id
           LEFT JOIN round_tasks r ON r.id = p.task_id
           LEFT JOIN puzzles z ON z.id = p.target_id
           JOIN uploads f ON f.id = p.file_id
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
