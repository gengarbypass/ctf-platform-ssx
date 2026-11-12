import io
import re
import secrets
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from threading import Barrier

from PIL import Image

from ctf_platform import create_app
from ctf_platform.db import connect


def png():
    data = io.BytesIO()
    Image.new("RGB", (16, 9), "#2458d8").save(data, "PNG")
    data.seek(0)
    return data


def patch_zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("notes.txt", "Test submission material.")
    data.seek(0)
    return data


class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.password = secrets.token_urlsafe(18)
        self.settings = {
            "TESTING": True, "DATA_DIR": self.directory.name, "ADMIN_USERNAME": "arenaops",
            "ADMIN_PASSWORD": self.password, "SECRET_KEY": None, "SESSION_COOKIE_SECURE": False, "BUILD_REVISION": "",
        }
        self.app = create_app(self.settings)
        self.admin = self.login("arenaops")

    def lookup(self, sql, args=()):
        with closing(connect(self.app.config["DATABASE"])) as db:
            return db.execute(sql, args).fetchall()

    def login(self, username, app=None):
        client = (app or self.app).test_client()
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        result = self.post(client, "/login", username=username, password=self.password, follow=True, code=200)
        self.assertIn("赛事总览" if username == "arenaops" else "比赛首页", result.get_data(as_text=True))
        return client

    def post(self, client, path, code=303, follow=False, **data):
        with client.session_transaction() as session:
            data["csrf_token"] = session["csrf"]
        response = client.post(path, data=data, follow_redirects=follow)
        self.assertEqual(response.status_code, code, (path, response.get_data(as_text=True)[:350]))
        return response

    def contest(self, title="蓝域杯 · 测试赛", *, mode="puzzle", active=True, upcoming=False, cover=False):
        now = datetime.now(self.app.config["TIME_ZONE"])
        start = now + timedelta(hours=1) if upcoming else now - timedelta(minutes=5)
        end = now + timedelta(hours=2)
        data = {"title": title, "mode": mode, "summary": "赛题与回合赛况", "start_at": start.strftime("%Y-%m-%dT%H:%M"),
                "end_at": end.strftime("%Y-%m-%dT%H:%M")}
        if mode == "round":
            data["round_minutes"] = "15"
        if cover:
            data["cover"] = (png(), "cover.png")
        created = self.post(self.admin, "/manage/contests/new", **data)
        self.assertEqual(created.location, f"/manage/challenges/{mode}")
        contest_id = self.lookup("SELECT id FROM contests WHERE title = ?", (title,))[0][0]
        if active:
            self.post(self.admin, f"/manage/contests/{contest_id}/activate")
        return contest_id

    def player(self, contest_id, username, team_name):
        self.post(self.admin, "/manage/teams/new", contest_id=contest_id, name=team_name)
        self.post(self.admin, "/manage/accounts/new", username=username, display_name=username, password=self.password, role="player", enabled="1")
        user_id = self.lookup("SELECT id FROM users WHERE username = ?", (username,))[0][0]
        team_id = self.lookup("SELECT id FROM teams WHERE name = ?", (team_name,))[0][0]
        self.post(self.admin, "/manage/teams/membership", contest_id=contest_id, team_id=team_id, user_id=user_id)
        return self.login(username), team_id

    def puzzle(self, contest_id, *, attachment=True):
        data = {"contest_id": contest_id, "title": "镜像回廊", "category": "Web", "difficulty": "进阶",
                "points": 200, "description": "核对样例中的导航标签。", "flag": "flag{fixture}", "published": "1"}
        if attachment:
            data["attachments"] = (io.BytesIO(b"Navigation sample"), "sample.txt")
        self.post(self.admin, "/manage/challenges/puzzle/new", **data)
        return self.lookup("SELECT id FROM puzzles WHERE contest_id = ?", (contest_id,))[0][0]

    def round_task(self, contest_id):
        self.post(self.admin, "/manage/challenges/round/new", contest_id=contest_id, title="档案检索",
                  baseline="v1.0", description="记录服务状态与加固说明。", published="1")
        return self.lookup("SELECT id FROM round_tasks WHERE contest_id = ?", (contest_id,))[0][0]

    def test_login_uses_one_cover_and_updates_contest_text(self):
        public = self.app.test_client()
        self.assertEqual(public.get("/healthz").json, {"status": "ok"})
        login = public.get("/login").get_data(as_text=True)
        cover = public.get("/api/login-presentation").json["cover"]
        with public.get(cover) as image:
            self.assertEqual(image.mimetype, "image/webp")
        self.assertIn(f'src="{cover}"', login)
        self.assertIn("筑牢边疆数字防线、<br>锻造基层网安尖兵", login)
        self.assertNotIn('href="/puzzle"', login)
        self.assertNotIn('href="/awdp"', login)
        self.assertEqual(public.get("/puzzle").location, "/login")
        self.assertEqual(public.get("/awdp").location, "/login")
        first = self.contest(cover=True)
        before = public.get("/api/login-presentation").json
        self.assertIn("测试赛", before["title"])
        self.assertEqual(before["cover"], cover)
        first_image = self.lookup("SELECT cover_id FROM contests WHERE id = ?", (first,))[0][0]
        player, _ = self.player(first, "coverqa", "Cover QA")
        self.assertIn(f'src="/images/{first_image}"', player.get("/").get_data(as_text=True))
        self.assertNotIn(f'src="/images/{first_image}"', public.get("/login").get_data(as_text=True))
        self.contest("蓝域杯 · 总决赛", mode="round", cover=True)
        after = public.get("/api/login-presentation").json
        self.assertIn("总决赛", after["title"])
        self.assertEqual(after["cover"], cover)
        self.assertEqual(public.get(f"/images/{first_image}").status_code, 404)
        self.assertEqual(self.admin.post(f"/manage/contests/{first}/activate").status_code, 400)
        self.assertEqual(public.get("/api/login-presentation").json["title"], after["title"])

    def test_health_reports_the_deployed_commit(self):
        self.app.config["BUILD_REVISION"] = "a" * 40
        response = self.app.test_client().get("/healthz")
        self.assertEqual(response.json, {"status": "ok", "revision": "a" * 40})

    def test_flag_judgement_download_and_concurrent_one_time_award(self):
        contest_id = self.contest()
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        teammate = self.login("bluefox")
        puzzle_id = self.puzzle(contest_id)
        detail = f"/challenges/{puzzle_id}"
        self.assertIn("镜像回廊", player.get("/challenges?category=Web").get_data(as_text=True))
        self.assertNotIn("镜像回廊", player.get("/challenges?category=Crypto").get_data(as_text=True))
        source = player.get(detail).get_data(as_text=True)
        link = re.search(r'href="(/downloads/\d+)"', source).group(1)
        downloaded = player.get(link)
        self.assertEqual(downloaded.data, b"Navigation sample")
        self.assertIn("attachment", downloaded.headers["Content-Disposition"])
        self.assertEqual(player.get("/manage/").status_code, 403)
        self.assertEqual(player.post(f"{detail}/flag", data={"flag": "flag{fixture}"}).status_code, 400)
        wrong = self.post(player, f"{detail}/flag", flag="flag{wrong}", follow=True, code=200)
        self.assertIn("Flag 错误", wrong.get_data(as_text=True))
        barrier = Barrier(2)

        def solve(client):
            barrier.wait(timeout=10)
            return self.post(client, f"{detail}/flag", flag="flag{fixture}", follow=True, code=200).get_data(as_text=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(solve, (player, teammate)))
        self.assertEqual(sum("Flag 正确" in result for result in results), 1)
        self.assertEqual(sum("本题已解出" in result for result in results), 1)
        awards = self.lookup("SELECT verdict, points FROM flag_submissions WHERE team_id = ? ORDER BY id", (team_id,))
        self.assertEqual([tuple(row) for row in awards], [("incorrect", 0), ("correct", 200), ("already_solved", 0)])
        scoreboard = player.get("/api/scoreboard/puzzle").json
        self.assertEqual(scoreboard["timing"]["round_label"], "")
        self.assertEqual(scoreboard["timing"]["segments"], [])
        ranking = scoreboard["content"]
        self.assertIn('data-sb-value="200"', ranking)
        self.assertIn("已解 1 题", ranking)
        self.assertNotIn("flag{fixture}", ranking)

    def test_patch_review_score_reversal_status_and_file_access(self):
        contest_id = self.contest(mode="round")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        other, _ = self.player(contest_id, "packetlab", "PacketLab")
        task_id = self.round_task(contest_id)
        path = f"/awdp/challenges/{task_id}/patch"
        self.post(player, path, version="v1.1", notes="说明", patch=(io.BytesIO(b"not a zip"), "sample.zip"), code=400)
        self.post(player, path, version="v1.1", notes="说明", patch=(patch_zip(), "sample.zip"))
        patch = self.lookup("SELECT * FROM patches")[0]
        download = f"/downloads/{patch['file_id']}"
        self.assertEqual(player.get(download).status_code, 200)
        self.assertEqual(other.get(download).status_code, 404)
        self.assertEqual(self.admin.get(download).status_code, 200)
        review = f"/manage/patches/{patch['id']}"
        self.assertEqual(self.admin.get(review).status_code, 200)
        for _ in range(2):
            self.post(self.admin, review, contest_id=contest_id, status="passed", award=120, review_notes="通过")
        scores = self.lookup("SELECT delta FROM score_events")
        self.assertEqual([row["delta"] for row in scores], [120])
        self.post(self.admin, review, contest_id=contest_id, status="rejected", award=120, review_notes="复核退回")
        self.post(self.admin, "/manage/services/status", contest_id=contest_id, team_id=team_id, task_id=task_id, status="healthy")
        self.post(self.admin, "/manage/scores", contest_id=contest_id, team_id=team_id, kind="uptime", delta=35, round_no=1, reason="本回合服务正常")
        self.assertEqual([row["delta"] for row in self.lookup("SELECT delta FROM score_events ORDER BY id")], [120, -120, 35])
        board = player.get("/api/scoreboard/round").json["content"]
        self.assertIn('data-sb-value="35"', board)
        self.assertIn('data-sb-health="healthy"', board)
        self.assertIn("复核退回", player.get("/submissions?kind=patch").get_data(as_text=True))
        for page in ("/awdp", "/awdp?view=teams", "/awdp?view=patches", "/awdp?view=tasks", f"/awdp/challenges/{task_id}"):
            self.assertEqual(player.get(page).status_code, 200, page)

    def test_scheduled_access_and_persistence(self):
        contest_id = self.contest(upcoming=True)
        player, _ = self.player(contest_id, "bluefox", "BlueFox")
        puzzle_id = self.puzzle(contest_id)
        file_id = self.lookup("SELECT file_id FROM puzzle_files")[0][0]
        self.assertEqual(player.get(f"/challenges/{puzzle_id}").status_code, 404)
        self.assertEqual(player.get(f"/downloads/{file_id}").status_code, 404)
        self.post(player, f"/challenges/{puzzle_id}/flag", code=403, flag="flag{fixture}")
        self.post(self.admin, "/manage/brand", name="测试品牌", icon="shield", accent="#2458d8", background="#eff4fc",
                  surface="#ffffff", radius=16, logo=(png(), "logo.png"))
        new_app = create_app({**self.settings, "ADMIN_PASSWORD": ""})
        admin = self.login("arenaops", new_app)
        self.assertIn("测试品牌", admin.get("/manage/brand").get_data(as_text=True))
        self.assertEqual(admin.get(f"/downloads/{file_id}").data, b"Navigation sample")
        with new_app.app_context():
            self.assertEqual(new_app.config["FLAG_KEY"], self.app.config["FLAG_KEY"])

    def test_scoreboards_have_independent_blue_white_layouts(self):
        contest_id = self.contest()
        for team in ("BlueFox", "NullByte", "PacketLab"):
            self.post(self.admin, "/manage/teams/new", contest_id=contest_id, name=team)
        puzzle = self.admin.get("/scoreboard/puzzle").get_data(as_text=True)
        self.assertIsNone(self.admin.get("/api/scoreboard/round").json["timing"]["id"])
        contest_id = self.contest("蓝域杯 · 回合赛", mode="round")
        for team in ("BlueFox", "NullByte", "PacketLab"):
            self.post(self.admin, "/manage/teams/new", contest_id=contest_id, name=team)
        rounds = self.admin.get("/scoreboard/round").get_data(as_text=True)
        self.assertIsNone(self.admin.get("/api/scoreboard/puzzle").json["timing"]["id"])
        for page in (puzzle, rounds):
            self.assertNotIn("解题模式", page)
            self.assertNotIn("AWDP", page)
            self.assertNotIn("<button", page)
            self.assertNotIn("<a ", page)
            for medal in ("金色奖杯", "银色奖杯", "铜色奖杯"):
                self.assertIn(medal, page)
        self.assertIn("解出动态", puzzle)
        self.assertNotIn("服务状态", puzzle)
        self.assertIn("本回合剩余", rounds)
        self.assertIn("服务状态", rounds)

    def test_manage_unopened_contests_and_switch_entries(self):
        puzzle_contest = self.contest(active=False)
        puzzle_id = self.puzzle(puzzle_contest)
        file_id = self.lookup("SELECT file_id FROM puzzle_files WHERE puzzle_id = ?", (puzzle_id,))[0][0]
        download = f"/downloads/{file_id}"
        self.assertIn("待开放", self.admin.get("/manage/challenges/puzzle").get_data(as_text=True))
        self.assertEqual(self.admin.get(download).data, b"Navigation sample")
        player, _ = self.player(puzzle_contest, "bluefox", "BlueFox")
        self.assertEqual(player.get(download).status_code, 404)
        self.assertEqual(player.get(f"/challenges/{puzzle_id}").status_code, 404)
        self.post(self.admin, f"/manage/contests/{puzzle_contest}/activate")
        lobby = player.get("/")
        self.assertEqual(lobby.status_code, 200)
        self.assertIn('href="/puzzle"', lobby.get_data(as_text=True))
        self.assertIn('href="/awdp"', lobby.get_data(as_text=True))
        self.assertEqual(player.get(download).status_code, 200)

        round_contest = self.contest("蓝域杯 · 回合赛", mode="round", active=False)
        self.post(self.admin, f"/manage/contests/{puzzle_contest}/select")
        result = self.post(self.admin, f"/manage/contests/{round_contest}/select", section="challenges")
        self.assertEqual(result.location, "/manage/challenges/round")
        task_id = self.round_task(round_contest)
        self.post(self.admin, f"/manage/challenges/round/{task_id}/edit", contest_id=round_contest,
                  title="档案检索新版", baseline="v1.1", description="更新服务说明。", published="1")
        task_list = self.admin.get(result.location).get_data(as_text=True)
        self.assertIn("未开启 · 筹备中", task_list)
        self.assertIn("档案检索新版", task_list)
        self.assertIn("待开放", task_list)
        self.assertEqual(self.admin.get("/manage/challenges/puzzle").status_code, 404)
        detail = f"/awdp/challenges/{task_id}"
        waiting = player.get("/awdp").get_data(as_text=True)
        self.assertNotIn("测试赛", waiting)
        self.assertNotIn("回合赛", waiting)
        self.assertEqual(player.get(detail).status_code, 404)
        self.assertEqual(player.get(f"/challenges/{puzzle_id}").status_code, 200)

        self.post(self.admin, f"/manage/contests/{round_contest}/activate")
        self.assertEqual(self.lookup("SELECT id FROM contests WHERE active = 1")[0][0], round_contest)
        self.assertIn("回合赛", player.get("/").get_data(as_text=True))
        self.assertIn("档案检索新版", player.get(detail).get_data(as_text=True))
        self.assertNotIn("回合赛", player.get("/puzzle").get_data(as_text=True))
        self.assertEqual(player.get(f"/challenges/{puzzle_id}").status_code, 404)
        self.assertEqual(player.get(download).status_code, 404)
        self.assertEqual(player.get("/rankings?kind=puzzle").status_code, 404)
        self.assertEqual(player.get("/submissions?kind=flag").status_code, 404)
        client = self.app.test_client()
        client.get("/login")
        response = self.post(client, "/login", code=302, username="bluefox", password=self.password)
        self.assertEqual(response.location, "/")

    def test_upgrade_legacy_contests_preserves_challenges(self):
        puzzle_only = self.contest("仅解题", active=False)
        mixed = self.contest(active=False)
        self.puzzle(mixed)
        rounds = self.contest("蓝域杯 · 回合赛", mode="round")
        self.round_task(rounds)
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute(
                "INSERT INTO round_tasks (contest_id, title, baseline, description) VALUES (?, ?, ?, ?)",
                (mixed, "旧回合赛题", "v1.0", "存档说明"),
            )
            db.execute("UPDATE contests SET round_seconds = 1800 WHERE id = ?", (mixed,))
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript(
                """BEGIN IMMEDIATE;
                   CREATE TABLE contests_v1 (
                       id INTEGER PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
                       start_at INTEGER NOT NULL, end_at INTEGER NOT NULL CHECK (end_at > start_at),
                       round_seconds INTEGER NOT NULL CHECK (round_seconds > 0), cover_id INTEGER REFERENCES uploads(id),
                       active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)), created_at INTEGER NOT NULL
                   );
                   INSERT INTO contests_v1
                       SELECT id, title, summary, start_at, end_at, COALESCE(round_seconds, 900), cover_id, active, created_at
                       FROM contests;
                   DROP TABLE contests;
                   ALTER TABLE contests_v1 RENAME TO contests;
                   CREATE UNIQUE INDEX one_active_contest ON contests(active) WHERE active = 1;
                   PRAGMA user_version = 1;
                   COMMIT;"""
            )

        upgraded = create_app({**self.settings, "ADMIN_PASSWORD": ""})
        admin = self.login("arenaops", upgraded)
        self.assertEqual([tuple(row) for row in self.lookup("SELECT id, mode, active, round_seconds FROM contests ORDER BY id")],
                         [(puzzle_only, "puzzle", 0, None), (mixed, "puzzle", 0, 1800), (rounds, "round", 1, 900)])
        self.assertEqual(self.lookup("PRAGMA user_version")[0][0], 3)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])
        self.assertEqual(len(self.lookup("SELECT id FROM puzzles")), 1)
        self.assertEqual(len(self.lookup("SELECT id FROM round_tasks")), 2)
        contest = self.lookup("SELECT * FROM contests WHERE id = ?", (mixed,))[0]
        timezone = upgraded.config["TIME_ZONE"]
        self.post(admin, f"/manage/contests/{mixed}/edit", title=contest["title"], mode="round", round_minutes=30,
                  start_at=datetime.fromtimestamp(contest["start_at"], timezone).strftime("%Y-%m-%dT%H:%M"),
                  end_at=datetime.fromtimestamp(contest["end_at"], timezone).strftime("%Y-%m-%dT%H:%M"))
        self.assertIn("旧回合赛题", admin.get("/manage/challenges/round").get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT id FROM contests WHERE active = 1")[0][0], rounds)

    def test_contest_mode_controls_round_duration(self):
        form = self.admin.get("/manage/contests/new").get_data(as_text=True)
        for mode in ("puzzle", "round"):
            self.assertIn(f'<input type="radio" name="mode" value="{mode}" required>', form)
        self.assertIn("<div data-round-duration hidden>", form)
        self.assertRegex(form, r'name="round_minutes"[^>]+\bdisabled\b')
        now = datetime.now(self.app.config["TIME_ZONE"])
        data = {"title": "模式检查", "start_at": now.strftime("%Y-%m-%dT%H:%M"),
                "end_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")}
        self.post(self.admin, "/manage/contests/new", code=400, **data)
        self.post(self.admin, "/manage/contests/new", code=400, mode="round", **data)
        self.post(self.admin, "/manage/contests/new", mode="puzzle", round_minutes="30", **data)
        self.assertIsNone(self.lookup("SELECT round_seconds FROM contests")[0][0])

    def test_delete_contest_removes_contents_and_uploads_in_its_scope(self):
        contest_id = self.contest(cover=True)
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        puzzle_id = self.puzzle(contest_id)
        self.post(player, f"/challenges/{puzzle_id}/flag", flag="flag{fixture}")
        cover_id = self.lookup("SELECT cover_id FROM contests")[0][0]
        file_id = self.lookup("SELECT file_id FROM puzzle_files")[0][0]
        other_id = self.contest("筹备赛", active=False, cover=True)
        self.puzzle(other_id)
        route = f"/manage/contests/{contest_id}/delete"

        self.assertIn("当前比赛会立即下线", self.admin.get(route).get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT active FROM contests WHERE id = ?", (contest_id,))[0][0], 1)
        self.post(player, route, code=403)
        self.assertEqual(self.admin.post(route).status_code, 400)
        self.post(self.admin, route)
        self.assertEqual(self.admin.get(route).status_code, 404)
        self.assertEqual([row["id"] for row in self.lookup("SELECT id FROM contests")], [other_id])
        self.assertEqual([row["contest_id"] for row in self.lookup("SELECT contest_id FROM puzzles")], [other_id])
        self.assertEqual(self.lookup("SELECT * FROM teams WHERE id = ?", (team_id,)), [])
        self.assertEqual(self.lookup("SELECT * FROM memberships"), [])
        self.assertEqual(self.lookup("SELECT * FROM flag_submissions"), [])
        self.assertEqual(self.lookup("SELECT id FROM uploads WHERE id IN (?, ?)", (cover_id, file_id)), [])
        self.assertEqual(len(self.lookup("SELECT id FROM uploads")), 2)
        self.assertEqual(len(self.lookup("SELECT id FROM users")), 2)
        self.assertIsNone(player.get("/api/scoreboard/puzzle").json["timing"]["id"])
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])

    def test_delete_team_cleans_up_patches_and_points(self):
        contest_id = self.contest(mode="round")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        other, other_id = self.player(contest_id, "packetlab", "PacketLab")
        task_id = self.round_task(contest_id)
        for client in (player, other):
            self.post(client, f"/awdp/challenges/{task_id}/patch", version="v1.1", patch=(patch_zip(), "sample.zip"))
        patch = self.lookup("SELECT id, file_id FROM patches WHERE team_id = ?", (team_id,))[0]
        self.post(self.admin, f"/manage/patches/{patch['id']}", contest_id=contest_id, status="passed", award=120)
        self.post(self.admin, "/manage/services/status", contest_id=contest_id, team_id=team_id, task_id=task_id, status="healthy")
        route = f"/manage/teams/{team_id}/delete"
        self.assertIn("解除成员分队", self.admin.get(route).get_data(as_text=True))
        self.post(self.admin, route, contest_id=contest_id + 1, code=400)
        self.post(self.admin, route, contest_id=contest_id)

        self.assertEqual([row["id"] for row in self.lookup("SELECT id FROM teams")], [other_id])
        self.assertEqual([row["team_id"] for row in self.lookup("SELECT team_id FROM memberships")], [other_id])
        self.assertEqual([row["team_id"] for row in self.lookup("SELECT team_id FROM patches")], [other_id])
        self.assertEqual(self.lookup("SELECT id FROM uploads WHERE id = ?", (patch["file_id"],)), [])
        self.assertEqual(self.lookup("SELECT * FROM service_states"), [])
        self.assertEqual(self.lookup("SELECT * FROM score_events"), [])
        self.assertEqual(player.get("/awdp").status_code, 200)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])

    def test_delete_account_preserves_team_history_and_invalidates_session(self):
        contest_id = self.contest()
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        puzzle_id = self.puzzle(contest_id)
        self.post(player, f"/challenges/{puzzle_id}/flag", flag="flag{fixture}")
        user_id = self.lookup("SELECT id FROM users WHERE username = 'bluefox'")[0][0]
        route = f"/manage/accounts/{user_id}/delete"
        self.assertIn("解除所有分队", self.admin.get(route).get_data(as_text=True))
        self.post(self.admin, route)
        self.assertEqual(self.lookup("SELECT id FROM users WHERE id = ?", (user_id,)), [])
        self.assertEqual(self.lookup("SELECT * FROM memberships"), [])
        self.assertEqual(tuple(self.lookup("SELECT team_id, user_id, points FROM flag_submissions")[0]), (team_id, None, 200))
        self.assertIn("已删除账号", self.admin.get("/manage/submissions").get_data(as_text=True))
        self.assertIn('data-sb-value="200"', self.admin.get("/api/scoreboard/puzzle").json["content"])
        self.post(self.admin, "/manage/accounts/new", username="bluefox", display_name="New Fox",
                  password=self.password, role="player", enabled="1")
        self.assertGreater(self.lookup("SELECT id FROM users WHERE username = 'bluefox'")[0][0], user_id)
        self.assertEqual(player.get("/puzzle").location, "/login")

        admin_id = self.lookup("SELECT id FROM users WHERE role = 'admin'")[0][0]
        admin_route = f"/manage/accounts/{admin_id}/delete"
        self.assertIn("需保留一个启用的管理员", self.admin.get("/manage/accounts").get_data(as_text=True))
        self.post(self.admin, admin_route, code=400)
        self.post(self.admin, "/manage/accounts/new", username="newops", display_name="New Ops",
                  password=self.password, role="admin", enabled="1")
        self.assertEqual(self.post(self.admin, admin_route).location, "/login")
        self.assertEqual([row["username"] for row in self.lookup("SELECT username FROM users WHERE role = 'admin'")], ["newops"])
        self.assertEqual(self.admin.get("/manage/").location, "/login")
