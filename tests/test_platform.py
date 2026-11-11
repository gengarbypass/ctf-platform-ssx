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
            "ADMIN_PASSWORD": self.password, "SECRET_KEY": None, "SESSION_COOKIE_SECURE": False,
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

    def contest(self, title="蓝域杯 · 测试赛", *, upcoming=False, cover=False):
        now = datetime.now(self.app.config["TIME_ZONE"])
        start = now + timedelta(hours=1) if upcoming else now - timedelta(minutes=5)
        end = now + timedelta(hours=2)
        data = {"title": title, "summary": "赛题与回合赛况", "start_at": start.strftime("%Y-%m-%dT%H:%M"),
                "end_at": end.strftime("%Y-%m-%dT%H:%M"), "round_minutes": "15"}
        if cover:
            data["cover"] = (png(), "cover.png")
        self.post(self.admin, "/manage/contests/new", **data)
        contest_id = self.lookup("SELECT id FROM contests WHERE title = ?", (title,))[0][0]
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

    def test_login_presentation_follows_active_contest(self):
        public = self.app.test_client()
        self.assertEqual(public.get("/healthz").json, {"status": "ok"})
        self.assertEqual(public.get("/api/login-presentation").json["cover"], "")
        first = self.contest(cover=True)
        before = public.get("/api/login-presentation").json
        self.assertIn("测试赛", before["title"])
        self.assertEqual(public.get(before["cover"]).mimetype, "image/png")
        self.contest("蓝域杯 · 总决赛", cover=True)
        after = public.get("/api/login-presentation").json
        self.assertIn("总决赛", after["title"])
        self.assertNotEqual(before["cover"], after["cover"])
        self.assertEqual(public.get(before["cover"]).status_code, 404)
        self.assertEqual(self.admin.post(f"/manage/contests/{first}/activate").status_code, 400)
        self.assertEqual(public.get("/api/login-presentation").json["title"], after["title"])

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
        ranking = player.get("/api/scoreboard/puzzle").json["content"]
        self.assertIn('data-sb-value="200"', ranking)
        self.assertIn("已解 1 题", ranking)
        self.assertNotIn("flag{fixture}", ranking)

    def test_patch_review_score_reversal_status_and_file_access(self):
        contest_id = self.contest()
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
        rounds = self.admin.get("/scoreboard/round").get_data(as_text=True)
        for page in (puzzle, rounds):
            self.assertNotIn("解题模式", page)
            self.assertNotIn("AWDP", page)
            for medal in ("金色奖杯", "银色奖杯", "铜色奖杯"):
                self.assertIn(medal, page)
        self.assertIn("解出动态", puzzle)
        self.assertNotIn("服务状态", puzzle)
        self.assertIn("本回合剩余", rounds)
        self.assertIn("服务状态", rounds)
