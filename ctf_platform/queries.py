from .domain import SCORE_KINDS


def puzzle_rankings(db, contest_id):
    return db.execute(
        """SELECT t.id, t.name, COUNT(f.id) AS solved, COALESCE(SUM(f.points), 0) AS points
           FROM teams t LEFT JOIN flag_submissions f ON f.contest_id = t.contest_id AND f.team_id = t.id AND f.verdict = 'correct'
           WHERE t.contest_id = ? GROUP BY t.id
           ORDER BY points DESC, t.name COLLATE NOCASE, t.id""",
        (contest_id,),
    ).fetchall()


def round_rankings(db, contest_id):
    return db.execute(
        """SELECT t.id, t.name,
              COALESCE(SUM(CASE WHEN s.kind = 'attack' THEN s.delta ELSE 0 END), 0) AS attack,
              COALESCE(SUM(CASE WHEN s.kind = 'defense' THEN s.delta ELSE 0 END), 0) AS defense,
              COALESCE(SUM(CASE WHEN s.kind = 'uptime' THEN s.delta ELSE 0 END), 0) AS uptime,
              COALESCE(SUM(s.delta), 0) AS points
           FROM teams t LEFT JOIN score_events s ON s.contest_id = t.contest_id AND s.team_id = t.id
           WHERE t.contest_id = ? GROUP BY t.id
           ORDER BY points DESC, t.name COLLATE NOCASE, t.id""",
        (contest_id,),
    ).fetchall()


def team_list(db, contest_id):
    return db.execute("SELECT * FROM teams WHERE contest_id = ? ORDER BY name COLLATE NOCASE, id", (contest_id,)).fetchall()


def service_matrix(db, contest_id, *, public=True):
    tasks = db.execute(
        """SELECT id, title, baseline, published FROM round_tasks WHERE contest_id = ?
           AND (? = 0 OR published = 1) ORDER BY id""", (contest_id, public),
    ).fetchall()
    states = {
        (row["team_id"], row["task_id"]): row["status"]
        for row in db.execute("SELECT team_id, task_id, status FROM service_states WHERE contest_id = ?", (contest_id,))
    }
    teams = team_list(db, contest_id)
    healthy = sum(states.get((team["id"], task["id"])) == "healthy" for team in teams for task in tasks)
    return {"teams": teams, "tasks": tasks, "states": states, "healthy": healthy, "total": len(teams) * len(tasks)}


def flag_records(db, contest_id, *, team_id=None, puzzle_id=None, accepted=False, limit=50, offset=0):
    return db.execute(
        """SELECT f.*, p.title, p.category, t.name AS team_name, u.display_name
           FROM flag_submissions f JOIN puzzles p ON p.id = f.puzzle_id
           JOIN teams t ON t.id = f.team_id JOIN users u ON u.id = f.user_id
           WHERE f.contest_id = ? AND (? IS NULL OR f.team_id = ?) AND (? IS NULL OR f.puzzle_id = ?)
           AND (? = 0 OR f.verdict = 'correct') ORDER BY f.id DESC LIMIT ? OFFSET ?""",
        (contest_id, team_id, team_id, puzzle_id, puzzle_id, accepted, limit, offset),
    ).fetchall()


def patch_records(db, contest_id, *, team_id=None, limit=50, offset=0):
    return db.execute(
        """SELECT p.*, r.title, t.name AS team_name, u.display_name, f.name AS file_name
           FROM patches p JOIN round_tasks r ON r.id = p.task_id JOIN teams t ON t.id = p.team_id
           JOIN users u ON u.id = p.user_id JOIN uploads f ON f.id = p.file_id
           WHERE p.contest_id = ? AND (? IS NULL OR p.team_id = ?) ORDER BY p.id DESC LIMIT ? OFFSET ?""",
        (contest_id, team_id, team_id, limit, offset),
    ).fetchall()


def score_records(db, contest_id, *, team_id=None, round_no=None, limit=50, offset=0):
    return db.execute(
        """SELECT s.*, t.name AS team_name, u.display_name AS actor
           FROM score_events s JOIN teams t ON t.id = s.team_id JOIN users u ON u.id = s.actor_id
           WHERE s.contest_id = ? AND (? IS NULL OR s.team_id = ?) AND (? IS NULL OR s.round_no = ?)
           ORDER BY s.id DESC LIMIT ? OFFSET ?""",
        (contest_id, team_id, team_id, round_no, round_no, limit, offset),
    ).fetchall()


def round_trend(db, contest_id, team_id, current_round):
    if team_id is None or current_round < 1:
        return []
    totals = dict(db.execute(
        """SELECT round_no, SUM(delta) FROM score_events WHERE contest_id = ? AND team_id = ?
           AND round_no <= ? GROUP BY round_no""", (contest_id, team_id, current_round),
    ).fetchall())
    first = max(1, current_round - 7)
    total = sum(value for round_no, value in totals.items() if round_no < first)
    result = []
    for round_no in range(first, current_round + 1):
        change = totals.get(round_no, 0)
        total += change
        result.append({"round": round_no, "total": total, "change": change})
    return result


def add_score(db, *, contest_id, team_id, kind, delta, round_no, reason, actor_id, now, patch_id=None):
    if kind not in SCORE_KINDS or not delta:
        raise ValueError("Invalid score event")
    db.execute(
        """INSERT INTO score_events (contest_id, team_id, kind, delta, round_no, reason, patch_id, occurred_at, actor_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contest_id, team_id, kind, delta, round_no, reason, patch_id, now, actor_id),
    )
