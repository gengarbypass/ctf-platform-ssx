from .domain import SCORE_KINDS, attack_pill


PLAYER_BOARD_LIMIT = 10


def puzzle_rankings(db, contest_id):
    return db.execute(
        """SELECT t.id, t.name, COUNT(f.id) AS solved, COALESCE(SUM(f.points), 0) AS points
           FROM contest_teams ct JOIN teams t ON t.id = ct.team_id
           LEFT JOIN flag_submissions f ON f.contest_id = ? AND f.team_id = t.id AND f.verdict = 'correct'
           WHERE ct.contest_id = ? GROUP BY t.id
           ORDER BY points DESC, MAX(f.submitted_at) ASC NULLS LAST, MAX(f.id), t.id""",
        (contest_id, contest_id),
    ).fetchall()


def puzzle_player_rankings(db, contest_id):
    return db.execute(
        """SELECT f.user_id, t.id AS team_id, COALESCE(u.display_name, '已删除账号') AS name, t.name AS team_name,
                  COUNT(f.id) AS solved, COALESCE(SUM(f.points), 0) AS points
           FROM flag_submissions f JOIN teams t ON t.id = f.team_id LEFT JOIN users u ON u.id = f.user_id
           WHERE f.contest_id = ? AND f.verdict = 'correct'
           GROUP BY f.user_id, t.id
           ORDER BY points DESC, MAX(f.submitted_at), MAX(f.id)
           LIMIT ?""",
        (contest_id, PLAYER_BOARD_LIMIT),
    ).fetchall()


def round_player_rankings(db, contest_id):
    return db.execute(
        """WITH submissions AS (
               SELECT f.user_id, f.team_id, f.points AS attack, 0 AS defense, f.submitted_at, f.id AS submission_id
               FROM flag_submissions f JOIN puzzles z ON z.id = f.puzzle_id AND z.kind = 'round'
               WHERE f.contest_id = ? AND f.verdict = 'correct'
               UNION ALL
               SELECT p.user_id, p.team_id, 0, p.award, p.submitted_at, p.id
               FROM patches p WHERE p.contest_id = ?
           )
           SELECT s.user_id, t.id AS team_id, COALESCE(u.display_name, '已删除账号') AS name, t.name AS team_name,
                  SUM(s.attack) AS attack, SUM(s.defense) AS defense, SUM(s.attack + s.defense) AS points
           FROM submissions s JOIN teams t ON t.id = s.team_id LEFT JOIN users u ON u.id = s.user_id
           GROUP BY s.user_id, t.id
           ORDER BY points DESC, MAX(s.submitted_at) FILTER (WHERE s.attack + s.defense > 0) ASC NULLS LAST,
                    MAX(s.submission_id) FILTER (WHERE s.attack + s.defense > 0), t.id, s.user_id
           LIMIT ?""",
        (contest_id, contest_id, PLAYER_BOARD_LIMIT),
    ).fetchall()


def breach_rankings(db, contest_id):
    return db.execute(
        """SELECT t.id, t.name, COUNT(b.id) AS solved, COALESCE(SUM(b.points), 0) AS points
           FROM contest_teams ct JOIN teams t ON t.id = ct.team_id
           LEFT JOIN breach_submissions b ON b.contest_id = ? AND b.team_id = t.id AND b.verdict = 'correct'
           WHERE ct.contest_id = ? GROUP BY t.id
           ORDER BY points DESC, MAX(b.submitted_at) ASC NULLS LAST, MAX(b.id), t.id""",
        (contest_id, contest_id),
    ).fetchall()


def breach_player_rankings(db, contest_id):
    return db.execute(
        """SELECT b.user_id, t.id AS team_id, COALESCE(u.display_name, '已删除账号') AS name, t.name AS team_name,
                  COUNT(b.id) AS solved, COALESCE(SUM(b.points), 0) AS points
           FROM breach_submissions b JOIN teams t ON t.id = b.team_id LEFT JOIN users u ON u.id = b.user_id
           WHERE b.contest_id = ? AND b.verdict = 'correct'
           GROUP BY b.user_id, t.id
           ORDER BY points DESC, MAX(b.submitted_at), MAX(b.id)
           LIMIT ?""",
        (contest_id, PLAYER_BOARD_LIMIT),
    ).fetchall()


def breach_records(db, contest_id, *, team_id=None, flag_id=None, accepted=False, limit=50, offset=0):
    return db.execute(
        """SELECT s.*, COALESCE(f.title, '未匹配靶点') AS title, t.name AS team_name,
                  COALESCE(u.display_name, '已删除账号') AS display_name
           FROM breach_submissions s LEFT JOIN breach_flags f ON f.id = s.flag_id
           JOIN teams t ON t.id = s.team_id LEFT JOIN users u ON u.id = s.user_id
           WHERE s.contest_id = ? AND (? IS NULL OR s.team_id = ?) AND (? IS NULL OR s.flag_id = ?)
           AND (? = 0 OR s.verdict = 'correct') ORDER BY s.id DESC LIMIT ? OFFSET ?""",
        (contest_id, team_id, team_id, flag_id, flag_id, accepted, limit, offset),
    ).fetchall()


def round_rankings(db, contest_id):
    return db.execute(
        """SELECT t.id, t.name,
              COALESCE(SUM(CASE WHEN s.kind = 'attack' THEN s.delta ELSE 0 END), 0) AS attack,
              COALESCE(SUM(CASE WHEN s.kind = 'defense' THEN s.delta ELSE 0 END), 0) AS defense,
              COALESCE(SUM(CASE WHEN s.kind = 'uptime' THEN s.delta ELSE 0 END), 0) AS uptime,
              COALESCE(SUM(s.delta), 0) AS points
           FROM contest_teams ct JOIN teams t ON t.id = ct.team_id
           LEFT JOIN score_events s ON s.contest_id = ? AND s.team_id = t.id
           LEFT JOIN patches p ON p.contest_id = s.contest_id AND p.id = s.patch_id
           WHERE ct.contest_id = ? GROUP BY t.id
           ORDER BY points DESC,
                    MAX(CASE WHEN p.id IS NULL THEN s.occurred_at
                             WHEN p.award > 0 THEN p.submitted_at END) ASC NULLS LAST,
                    MAX(s.id) FILTER (WHERE p.id IS NULL OR p.award > 0), t.id""",
        (contest_id, contest_id),
    ).fetchall()


def team_list(db, contest_id):
    return db.execute(
        """SELECT t.* FROM contest_teams ct JOIN teams t ON t.id = ct.team_id
           WHERE ct.contest_id = ? ORDER BY t.name COLLATE NOCASE, t.id""", (contest_id,),
    ).fetchall()


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
        """SELECT f.*, p.title, p.category, tf.title AS flag_title, t.name AS team_name, v.name AS victim_name,
                  COALESCE(u.display_name, '已删除账号') AS display_name
           FROM flag_submissions f JOIN puzzles p ON p.id = f.puzzle_id
           LEFT JOIN target_flags tf ON tf.id = f.target_flag_id
           JOIN teams t ON t.id = f.team_id LEFT JOIN teams v ON v.id = f.victim_team_id
           LEFT JOIN users u ON u.id = f.user_id
           WHERE f.contest_id = ? AND (? IS NULL OR f.team_id = ?) AND (? IS NULL OR f.puzzle_id = ?)
           AND (? = 0 OR f.verdict = 'correct') ORDER BY f.id DESC LIMIT ? OFFSET ?""",
        (contest_id, team_id, team_id, puzzle_id, puzzle_id, accepted, limit, offset),
    ).fetchall()


def patch_records(db, contest_id, *, team_id=None, limit=50, offset=0):
    return db.execute(
        """SELECT p.*, COALESCE(r.title, z.title) AS title, t.name AS team_name,
                  COALESCE(u.display_name, '已删除账号') AS display_name, f.name AS file_name
           FROM patches p
           LEFT JOIN round_tasks r ON r.id = p.task_id
           LEFT JOIN puzzles z ON z.id = p.target_id
           JOIN teams t ON t.id = p.team_id
           LEFT JOIN users u ON u.id = p.user_id JOIN uploads f ON f.id = p.file_id
           WHERE p.contest_id = ? AND (? IS NULL OR p.team_id = ?) ORDER BY p.id DESC LIMIT ? OFFSET ?""",
        (contest_id, team_id, team_id, limit, offset),
    ).fetchall()


def target_flags(db, puzzle_id):
    """AWDP 靶机的靶点（按布置顺序）。"""
    return db.execute("SELECT * FROM target_flags WHERE puzzle_id = ? ORDER BY id", (puzzle_id,)).fetchall()


def rival_count(db, contest_id, team_id):
    """本比赛中除该队以外的队伍数：动态靶机每队各有一个实例，即该队可攻击的对手数。"""
    if team_id is None:
        return 0
    return db.execute(
        """SELECT COUNT(*) FROM contest_teams ct JOIN teams t ON t.id = ct.team_id
           WHERE ct.contest_id = ? AND ct.team_id IS NOT ?""", (contest_id, team_id),
    ).fetchone()[0]


def target_awards(db, contest_id, puzzle_ids, *, round_no):
    """AWDP 靶机的攻击命中明细：{靶机ID: {队伍ID: {(靶点ID 或 None, 被攻破队伍ID 或 None)}}}。

    动态靶机各队实例的 Flag 互不相同，同一队攻破几支队伍的靶机，同一靶点就出现几次；
    静态 Flag 各队靶机相同、无从区分来源，被攻破队伍记为 None。未布置靶点的靶机没有靶点，靶点记为 None；
    靶机后来登记了靶点时，登记前留下的靶机级记录不再计入进度（积分保留在计分流水）。
    动态实例的进度使用当前回合，静态 Flag 使用整场比赛记录。
    """
    awards = {puzzle_id: {} for puzzle_id in puzzle_ids}
    if not puzzle_ids:
        return awards
    placeholders = ",".join("?" * len(puzzle_ids))
    for row in db.execute(
        f"""SELECT f.puzzle_id, f.team_id, f.target_flag_id, f.victim_team_id
            FROM flag_submissions f JOIN puzzles p ON p.id = f.puzzle_id
            WHERE f.contest_id = ? AND f.verdict = 'correct' AND f.puzzle_id IN ({placeholders})
              AND (f.victim_team_id IS NOT NULL) = (p.env_image IS NOT NULL AND p.flag_digest = '')
              AND f.flag_round = CASE WHEN f.victim_team_id IS NOT NULL THEN ? ELSE 0 END
              AND (f.target_flag_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM target_flags tf WHERE tf.puzzle_id = f.puzzle_id))""",
        (contest_id, *puzzle_ids, round_no),
    ):
        awards[row["puzzle_id"]].setdefault(row["team_id"], set()).add((row["target_flag_id"], row["victim_team_id"]))
    return awards


def target_award_status(db, contest_id, puzzle_id, flag_id, team_id, victim_id, flag_round):
    """查询靶点是否已得分，以及有效记录中是否包括当前队伍。"""
    slot = "target_flag_id IS NULL" if flag_id is None else "target_flag_id = :flag_id"
    scope = "victim_team_id = :victim_id" if victim_id is not None else "victim_team_id IS NULL AND team_id = :team_id"
    return db.execute(
        f"""SELECT MAX(team_id = :team_id) AS claimed_by_team FROM flag_submissions
            WHERE contest_id = :contest_id AND puzzle_id = :puzzle_id AND flag_round = :flag_round
              AND verdict = 'correct' AND {slot} AND {scope}
            HAVING COUNT(*) > 0""",
        {"contest_id": contest_id, "puzzle_id": puzzle_id, "flag_round": flag_round,
         "flag_id": flag_id, "team_id": team_id, "victim_id": victim_id},
    ).fetchone()


def target_claims(awards, flag_total):
    """按动态实例汇总已攻破的靶点与已被完整攻破的队伍。"""
    claims = {}
    for obtained in awards.values():
        for flag_id, victim_id in obtained:
            if victim_id is not None:
                claims.setdefault(victim_id, set()).add(flag_id)
    breached = {victim_id for victim_id, slots in claims.items() if len(slots) >= (flag_total or 1)}
    return claims, breached


def target_breaches(awards, flag_total):
    """由一台靶机的命中明细得出各队已完全攻破的对手：{队伍ID: 被攻破的队伍ID 集合}。

    靶机的全部靶点（未布置靶点即靶机级 Flag）都已从同一支对手的靶机获取，才算攻破该对手；
    集合含 None 表示凭静态 Flag 得分（不区分对手）。
    """
    breaches = {}
    for team_id, obtained in awards.items():
        rivals = {}
        for flag_id, victim in obtained:
            rivals.setdefault(victim, set()).add(flag_id)
        breached = {victim for victim, flags in rivals.items() if len(flags) >= (flag_total or 1)}
        if breached:
            breaches[team_id] = breached
    return breaches


def round_targets(db, contest_id, team_id=None, *, round_no):
    """AWDP 比赛已加入并发布的靶机：附攻破队伍数 solves 与本队进度 state（药丸的色调与文案）。

    布置了靶点的靶机按靶点计：flag_total 为靶点总数，points 取各靶点分值之和；
    未布置靶点的靶机沿用靶机级 Flag（flag_total 为 0）。solves 为已完全攻破该靶机的队伍数——
    动态靶机攻破任意一支对手队伍的实例即计入，静态靶机为拿到全部 Flag 的队伍数。
    """
    rows = [dict(row) for row in db.execute(
        """SELECT p.id, p.title, p.points, (p.env_image IS NOT NULL) AS has_env,
                  (p.env_image IS NOT NULL AND p.flag_digest = '') AS per_team,
                  EXISTS(SELECT 1 FROM puzzle_files a WHERE a.puzzle_id = p.id) AS attached,
                  (SELECT COUNT(*) FROM target_flags tf WHERE tf.puzzle_id = p.id) AS flag_total,
                  (SELECT COALESCE(SUM(tf.points), 0) FROM target_flags tf WHERE tf.puzzle_id = p.id) AS flag_points
           FROM contest_puzzles cp
           JOIN puzzles p ON p.id = cp.puzzle_id AND p.kind = 'round'
           WHERE cp.contest_id = ? AND p.published = 1
           ORDER BY p.points DESC, p.id""", (contest_id,),
    ).fetchall()]
    awards = target_awards(db, contest_id, [row["id"] for row in rows], round_no=round_no)
    rivals = rival_count(db, contest_id, team_id)
    for row in rows:
        target_awards_by_team = awards[row["id"]]
        breaches = target_breaches(target_awards_by_team, row["flag_total"])
        _, breached_instances = target_claims(target_awards_by_team, row["flag_total"])
        row["solves"] = len(breaches)
        row["own_breached"] = row["per_team"] and team_id in breached_instances
        if row["flag_total"]:
            row["points"] = row["flag_points"]
        target_rivals = rivals if row["per_team"] else 0
        row["state"] = attack_pill(
            # 动态靶机按攻破的对手队伍数计；静态靶机没有对手实例，按 Flag 是否全部拿到计
            rivals=target_rivals,
            breached=len(breaches.get(team_id, set()) - {None}),
            flags=len(target_awards_by_team.get(team_id, set())), need=(row["flag_total"] or 1) * (target_rivals or 1),
        )
    return rows


def score_records(db, contest_id, *, team_id=None, round_no=None, limit=50, offset=0):
    return db.execute(
        """SELECT s.*, t.name AS team_name, COALESCE(u.display_name, '已删除账号') AS actor
           FROM score_events s JOIN teams t ON t.id = s.team_id LEFT JOIN users u ON u.id = s.actor_id
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
