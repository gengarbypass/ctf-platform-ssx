import hmac
import re
import secrets
from datetime import datetime
from functools import wraps

from flask import abort, current_app, g, redirect, request, url_for


CATEGORIES = ("Web", "Crypto", "Reverse", "Pwn", "Misc")
DIFFICULTIES = ("入门", "进阶", "挑战")
SERVICE_STATES = {
    "healthy": "正常",
    "unhealthy": "异常",
    "maintenance": "维护中",
    "unknown": "待检查",
}
PATCH_STATES = {
    "pending": "待评测",
    "testing": "评测中",
    "passed": "通过",
    "rejected": "未通过",
}
FLAG_VERDICTS = {
    "correct": "正确",
    "incorrect": "错误",
    "already_solved": "已解出",
}
SCORE_KINDS = {"attack": "攻防", "defense": "加固", "uptime": "可用性"}


class FormError(ValueError):
    pass


def text_field(key, label, limit, *, optional=False):
    value = request.form.get(key, "").strip()
    if not value and not optional:
        raise FormError(f"请填写{label}。")
    if len(value) > limit:
        raise FormError(f"{label}最多 {limit} 个字符。")
    return value


def integer_field(key, label, minimum, maximum):
    try:
        value = int(request.form.get(key, ""))
    except ValueError as error:
        raise FormError(f"{label}须为整数。") from error
    if not minimum <= value <= maximum:
        raise FormError(f"{label}须在 {minimum} 到 {maximum} 之间。")
    return value


def choice_field(key, label, choices):
    value = request.form.get(key, "")
    if value not in choices:
        raise FormError(f"请选择有效的{label}。")
    return value


def username_value(value):
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
        raise FormError("账号须由 3–40 个字母、数字、点、下划线或连字符组成。")
    return username


def validate_password(value):
    if not 12 <= len(value) <= 128:
        raise FormError("密码须为 12–128 个字符。")


def local_time(timestamp, pattern="%Y-%m-%d %H:%M:%S"):
    if timestamp is None:
        return "—"
    return datetime.fromtimestamp(timestamp, current_app.config["TIME_ZONE"]).strftime(pattern)


def parse_local_time(key, label):
    raw = request.form.get(key, "")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        value = parsed.replace(tzinfo=current_app.config["TIME_ZONE"])
        timestamp = int(value.timestamp())
        if datetime.fromtimestamp(timestamp, value.tzinfo).replace(tzinfo=None) != parsed:
            raise ValueError
        return timestamp
    except (ValueError, OverflowError, OSError) as error:
        raise FormError(f"请填写有效的{label}。") from error


def contest_schedule(contest):
    first = local_time(contest["start_at"], "%Y-%m-%d %H:%M")
    end_format = "%H:%M" if first[:10] == local_time(contest["end_at"], "%Y-%m-%d") else "%Y-%m-%d %H:%M"
    return f"{first} – {local_time(contest['end_at'], end_format)}"


def contest_state(contest, now):
    if contest is None:
        return None
    start = contest["start_at"]
    end = contest["end_at"]
    seconds = contest["round_seconds"]
    rounds = (end - start + seconds - 1) // seconds
    if now < start:
        return {"status": "upcoming", "label": "待开赛", "round": 0, "rounds": rounds, "round_end": start}
    if now >= end:
        return {"status": "finished", "label": "比赛已结束", "round": rounds, "rounds": rounds, "round_end": end}
    round_no = (now - start) // seconds + 1
    return {"status": "live", "label": "比赛进行中", "round": round_no, "rounds": rounds, "round_end": min(end, start + round_no * seconds)}


def flag_digest(salt, value):
    return hmac.digest(current_app.config["FLAG_KEY"], f"{salt}\0{value}".encode("utf-8"), "sha256").hex()


def new_flag(value):
    salt = secrets.token_hex(16)
    return salt, flag_digest(salt, value)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("site.login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def countdown(seconds, show_hours=False):
    seconds = max(int(seconds), 0)
    minutes, second = divmod(seconds, 60)
    if show_hours:
        hours, minute = divmod(minutes, 60)
        return f"{hours:02d}:{minute:02d}:{second:02d}"
    return f"{minutes:02d}:{second:02d}"


def round_segments(state):
    if not state:
        return []
    visible = min(state["rounds"], 24)
    if state["round"] == 0:
        return [""] * visible
    if state["status"] == "finished":
        return ["is-complete"] * visible
    current = (state["round"] - 1) * visible // state["rounds"]
    return ["is-current" if index == current else "is-complete" if index < current else "" for index in range(visible)]
