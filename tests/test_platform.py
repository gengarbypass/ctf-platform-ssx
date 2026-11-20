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
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from ctf_platform import create_app
from ctf_platform.db import connect
from ctf_platform.domain import env_flag_value


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


class FakeDocker:
    """记录容器/网络操作的最小 Docker 客户端替身。"""

    def __init__(self):
        self.state = {"runs": [], "networks": []}
        self.containers = self.Containers(self.state)
        self.networks = self.Networks(self.state)

    class Containers:
        def __init__(self, state):
            self.state = state

        def run(self, image, **kwargs):
            self.state["runs"].append({"image": image, **kwargs})
            return SimpleNamespace(id=f"cid-{len(self.state['runs'])}", status="running")

        def get(self, identifier):
            for index, run in enumerate(self.state["runs"], start=1):
                if identifier in (run.get("name"), f"cid-{index}"):
                    return SimpleNamespace(id=f"cid-{index}", status="running",
                                           stop=lambda timeout=10: None, remove=lambda force=False: None)
            raise LookupError(identifier)

    class Networks:
        def __init__(self, state):
            self.state = state

        def create(self, name, **kwargs):
            self.state["networks"].append(name)
            return SimpleNamespace(id=f"nid-{len(self.state['networks'])}",
                                   remove=lambda: self.state["networks"].remove(name))

        def get(self, name):
            if name in self.state["networks"]:
                return SimpleNamespace(id=name, remove=lambda: self.state["networks"].remove(name))
            raise LookupError(name)


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

    def contest(self, title="蓝域杯 · 测试赛", *, mode="puzzle", active=True, upcoming=False, cover=False, **extra):
        now = datetime.now(self.app.config["TIME_ZONE"])
        start = now + timedelta(hours=1) if upcoming else now - timedelta(minutes=5)
        end = now + timedelta(hours=2)
        data = {"title": title, "mode": mode, "summary": "赛题与回合赛况", "start_at": start.strftime("%Y-%m-%dT%H:%M"),
                "end_at": end.strftime("%Y-%m-%dT%H:%M"), **extra}
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
        data = {"title": "镜像回廊", "category": "Web", "difficulty": "进阶",
                "points": 200, "description": "核对样例中的导航标签。", "flag": "flag{fixture}",
                "published": "1", "join_contest": contest_id}
        if attachment:
            data["attachments"] = (io.BytesIO(b"Navigation sample"), "sample.txt")
        self.post(self.admin, "/manage/bank/new", **data)
        return self.lookup(
            "SELECT cp.puzzle_id FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id "
            "WHERE cp.contest_id = ? AND p.title = '镜像回廊' ORDER BY cp.puzzle_id DESC", (contest_id,),
        )[0][0]

    def round_task(self, contest_id):
        self.post(self.admin, "/manage/challenges/round/new", contest_id=contest_id, title="档案检索",
                  baseline="v1.0", description="记录服务状态与加固说明。", published="1")
        return self.lookup("SELECT id FROM round_tasks WHERE contest_id = ?", (contest_id,))[0][0]

    def awdp_target(self, contest_id, *, title="镜像靶场", points=200, env_image=None, flag=None, attachment=False):
        data = {"title": title, "category": "Web", "difficulty": "进阶", "points": points,
                "description": "AWDP 攻防靶机", "kind": "round", "published": "1", "join_contest": contest_id}
        if env_image:
            data["env_image"] = env_image
            data["env_container_port"] = "80"
        if flag:
            data["flag"] = flag
        if attachment:
            data["attachments"] = (io.BytesIO(b"Target sample"), "target.txt")
        self.post(self.admin, "/manage/bank/new", **data)
        return self.lookup(
            "SELECT cp.puzzle_id FROM contest_puzzles cp JOIN puzzles p ON p.id = cp.puzzle_id "
            "WHERE cp.contest_id = ? AND p.kind = 'round' AND p.title = ? ORDER BY cp.puzzle_id DESC", (contest_id, title),
        )[0][0]

    def breach_flag(self, contest_id, title, layer, flag, *, points=100, description="", published="1"):
        data = {"contest_id": contest_id, "title": title, "layer": layer, "points": points,
                "flag": flag, "description": description, "published": published}
        if published is None:
            del data["published"]
        self.post(self.admin, "/manage/challenges/breach/new", **data)
        return self.lookup("SELECT id FROM breach_flags WHERE contest_id = ?", (contest_id,))[-1][0]

    def test_login_stays_the_same_when_active_contest_changes(self):
        public = self.app.test_client()
        self.assertEqual(public.get("/healthz").json, {"status": "ok"})
        login = public.get("/login").get_data(as_text=True)
        cover = "/static/login-campus.webp"
        with public.get(cover) as image:
            self.assertEqual(image.mimetype, "image/webp")
        self.assertIn(f'src="{cover}"', login)
        self.assertRegex(login, r"<span>筑牢边疆数字防线</span>\s*<span>锻造基层网安尖兵</span>")
        self.assertNotIn('href="/puzzle"', login)
        self.assertNotIn('href="/awdp"', login)
        self.assertEqual(public.get("/puzzle").location, "/login")
        self.assertEqual(public.get("/awdp").location, "/login")
        first = self.contest(cover=True)
        first_image = self.lookup("SELECT cover_id FROM contests WHERE id = ?", (first,))[0][0]
        player, _ = self.player(first, "coverqa", "Cover QA")
        home = player.get("/").get_data(as_text=True)
        self.assertIn("蓝域杯 · 测试赛", home)
        self.assertIn("赛题与回合赛况", home)
        self.assertIn(f'src="/images/{first_image}"', home)
        self.assertEqual(public.get("/login").get_data(as_text=True), login)
        self.contest("蓝域杯 · 总决赛", mode="round", cover=True)
        self.assertEqual(public.get(f"/images/{first_image}").status_code, 404)
        self.assertEqual(self.admin.post(f"/manage/contests/{first}/activate").status_code, 400)
        self.assertIn("蓝域杯 · 总决赛", player.get("/").get_data(as_text=True))
        self.assertEqual(public.get("/login").get_data(as_text=True), login)
        self.assertEqual(public.get("/api/login-presentation").status_code, 404)

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
        self.assertIn("金色奖杯", ranking)
        self.assertIn("1 题", ranking)
        self.assertIn("选手得分排行", ranking)
        self.assertIn('data-sb-player="', ranking)
        self.assertIn("bluefox", ranking)
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
        self.assertIn("选手加固得分", board)
        self.assertIn("复核退回", player.get("/submissions?kind=patch").get_data(as_text=True))
        for page in ("/awdp", "/awdp?view=teams", "/awdp?view=patches", "/awdp?view=tasks", f"/awdp/challenges/{task_id}"):
            self.assertEqual(player.get(page).status_code, 200, page)

    def test_breach_range_layered_flags_and_auto_match_scoring(self):
        contest_id = self.contest("蓝域杯 · 综合渗透赛", mode="breach")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        teammate = self.login("bluefox")
        other, _ = self.player(contest_id, "packetlab", "PacketLab")
        self.breach_flag(contest_id, "F1 备份泄露", "边界突破", "flag{agri_f1}", points=100, description="robots 与备份目录")
        self.breach_flag(contest_id, "F3 上传 Getshell", "边界突破", "flag{agri_f3}", points=150)
        third = self.breach_flag(contest_id, "F5 拖库", "内网渗透", "flag{agri_f5}", points=300)
        self.breach_flag(contest_id, "F8 痕迹取证", "内网渗透", "flag{agri_f8}", points=400, published=None)

        page = player.get("/breach").get_data(as_text=True)
        self.assertIn("边界突破", page)
        self.assertIn("内网渗透", page)
        self.assertIn("F1 备份泄露", page)
        self.assertIn("robots 与备份目录", page)
        self.assertIn("0 / 3", page)
        self.assertNotIn("F8 痕迹取证", page)

        wrong = self.post(player, "/breach/flag", flag="flag{nope}", follow=True, code=200)
        self.assertIn("Flag 错误", wrong.get_data(as_text=True))

        # 正确提交按靶点分值计分，且不要求按环节顺序（自动匹配）
        hit = self.post(player, "/breach/flag", flag="flag{agri_f3}", follow=True, code=200)
        self.assertIn("Flag 正确", hit.get_data(as_text=True))
        self.assertIn("本队获得 150 分", hit.get_data(as_text=True))
        # 草稿靶点的 flag 不可匹配
        hidden = self.post(player, "/breach/flag", flag="flag{agri_f8}", follow=True, code=200)
        self.assertIn("Flag 错误", hidden.get_data(as_text=True))
        # 同队重复提交（含队友）不重复计分
        repeat = self.post(teammate, "/breach/flag", flag="flag{agri_f3}", follow=True, code=200)
        self.assertIn("该 Flag 已提交", repeat.get_data(as_text=True))
        self.post(player, "/breach/flag", flag="flag{agri_f1}", follow=True, code=200)
        # 其他队伍独立计分
        self.post(other, "/breach/flag", flag="flag{agri_f5}", follow=True, code=200)
        awards = self.lookup("SELECT verdict, points FROM breach_submissions WHERE team_id = ? ORDER BY id", (team_id,))
        self.assertEqual([tuple(row) for row in awards],
                         [("incorrect", 0), ("correct", 150), ("incorrect", 0), ("already_solved", 0), ("correct", 100)])
        self.assertIn("2 / 3", player.get("/breach").get_data(as_text=True))

        board = player.get("/api/scoreboard/breach").json
        self.assertEqual(board["timing"]["round_label"], "")
        content = board["content"]
        self.assertIn("突破动态", content)
        self.assertIn("队伍排名", content)
        self.assertIn("选手得分排行", content)
        self.assertIn('data-sb-value="250"', content)
        self.assertIn('data-sb-value="300"', content)
        self.assertNotIn("flag{agri_f1}", content)

        self.assertIn("F3 上传 Getshell", player.get("/submissions").get_data(as_text=True))
        self.assertIn("未匹配靶点", self.admin.get("/manage/submissions").get_data(as_text=True))
        self.assertIn("F5 拖库", self.admin.get("/manage/challenges/breach").get_data(as_text=True))

        # 编辑靶点不重置 flag，仅调整分值与说明
        self.post(self.admin, f"/manage/challenges/breach/{third}/edit", contest_id=contest_id, title="F5 拖库",
                  layer="内网渗透", points=350, description="设备台账与拓扑", published="1")
        still = self.post(other, "/breach/flag", flag="flag{agri_f5}", follow=True, code=200)
        self.assertIn("该 Flag 已提交", still.get_data(as_text=True))

        # 与其他模式互不可见
        self.assertEqual(player.get("/challenges").status_code, 404)
        self.assertEqual(player.get("/rankings?kind=puzzle").status_code, 404)
        self.assertEqual(player.get("/submissions?kind=flag").status_code, 404)
        lobby = player.get("/puzzle").get_data(as_text=True)
        self.assertNotIn("arena-subnav", lobby)
        arena = player.get("/awdp").get_data(as_text=True)
        self.assertNotIn("综合渗透赛", arena)

        # 未开赛的靶场不可提交
        self.contest("蓝域杯 · 渗透预赛", mode="breach", upcoming=True)
        self.post(player, "/breach/flag", code=403, flag="flag{agri_f1}")
        waiting = player.get("/breach").get_data(as_text=True)
        self.assertIn("渗透预赛", waiting)
        self.assertNotIn("F1 备份泄露", waiting)

    def test_breach_bulk_flag_creation(self):
        contest_id = self.contest("蓝域杯 · 批量靶点赛", mode="breach")
        self.post(self.admin, "/manage/challenges/breach/new", contest_id=contest_id,
                  layer=["边界突破", "", "内网横向", ""],
                  title=["F1 备份泄露", "F3 上传 Getshell", "F5 拖库取证", ""],
                  points=["100", "150", "250", "300"],
                  flag=["flag{f1}", "flag{f3}", "flag{f5}", ""],
                  description=["robots 与备份目录", "", "", ""], published="1")
        rows = self.lookup(
            "SELECT layer, title, points, description FROM breach_flags WHERE contest_id = ? ORDER BY id", (contest_id,),
        )
        self.assertEqual([tuple(row) for row in rows], [
            ("边界突破", "F1 备份泄露", 100, "robots 与备份目录"),
            ("边界突破", "F3 上传 Getshell", 150, ""),
            ("内网横向", "F5 拖库取证", 250, ""),
        ])
        form = self.admin.get("/manage/challenges/breach/new").get_data(as_text=True)
        self.assertIn("靶点 1", form)
        self.assertIn("留空沿用上一行", form)
        # 半填行（有名称无 Flag）整体报错且不落库
        self.post(self.admin, "/manage/challenges/breach/new", contest_id=contest_id,
                  layer=["边界突破"], title=["F9 痕迹取证"], points=["400"], flag=[""], code=400)
        # 整行留空视为无效提交
        self.post(self.admin, "/manage/challenges/breach/new", contest_id=contest_id,
                  layer=["", ""], title=["", ""], points=["100", "100"], flag=["", ""], code=400)
        self.assertEqual(len(self.lookup("SELECT id FROM breach_flags WHERE contest_id = ?", (contest_id,))), 3)

    def test_breach_dynamic_environments(self):
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("农联云 · 动态靶场赛", mode="breach", env_image="iotmanage-dmz:1.0",
                                  env_container_port="80", env_ttl_minutes="120", env_max="2")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        other, other_team = self.player(contest_id, "packetlab", "PacketLab")
        third, _ = self.player(contest_id, "nightowl", "NightOwl")
        self.post(self.admin, "/manage/challenges/breach/new", contest_id=contest_id,
                  layer=["边界突破", ""], title=["F1 备份泄露", "F3 上传 Getshell"],
                  points=["100", "150"], flag=["", ""], description=["", ""], published="1")
        self.assertIn("启动环境", player.get("/breach").get_data(as_text=True))

        self.post(player, "/breach/env/start")
        env = self.lookup("SELECT * FROM breach_environments WHERE team_id = ?", (team_id,))[0]
        self.assertEqual((env["status"], env["host_port"], env["last_error"]), ("running", 8400, ""))
        run = fake.state["runs"][0]
        self.assertEqual(run["image"], "iotmanage-dmz:1.0")
        self.assertEqual(run["ports"], {"80/tcp": 8400})
        self.assertEqual(run["network"], "ctfenv-c%d-t%d-p0-net" % (contest_id, team_id))
        self.assertEqual(set(run["environment"]), {"FLAG_1", "FLAG_2"})
        page = player.get("/breach").get_data(as_text=True)
        self.assertIn(":8400</a>", page)

        first_flag = env_flag_value(env["secret"], env["id"], 1)
        self.assertEqual(run["environment"]["FLAG_1"], first_flag)
        result = self.post(player, "/breach/flag", flag=first_flag, follow=True, code=200)
        self.assertIn("本队获得 100 分", result.get_data(as_text=True))
        # 别队的动态 Flag 对本队无效
        other_post = self.post(other, "/breach/env/start")
        self.assertEqual(fake.state["runs"][1]["ports"], {"80/tcp": 8401})
        other_env = self.lookup("SELECT * FROM breach_environments WHERE team_id = ?", (other_team,))[0]
        other_flag = env_flag_value(other_env["secret"], other_env["id"], 1)
        self.assertNotEqual(other_flag, first_flag)
        self.post(player, "/breach/flag", flag=other_flag, follow=True, code=200)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT verdict, points FROM breach_submissions WHERE team_id = ? ORDER BY id", (team_id,))],
            [("correct", 100), ("incorrect", 0)])

        # 并发上限：第三队被拒
        self.post(third, "/breach/env/start", code=400)
        # 停止后重启：secret 与端口不变，Flag 稳定
        self.post(player, "/breach/env/stop")
        self.post(player, "/breach/env/start")
        restarted = self.lookup("SELECT * FROM breach_environments WHERE team_id = ?", (team_id,))[0]
        self.assertEqual((restarted["secret"], restarted["host_port"]), (env["secret"], 8400))
        result = self.post(player, "/breach/flag", flag=env_flag_value(env["secret"], env["id"], 2), follow=True, code=200)
        self.assertIn("本队获得 150 分", result.get_data(as_text=True))
        self.assertIn("<strong>250</strong>", player.get("/breach").get_data(as_text=True))

        # TTL 过期由任意靶场访问惰性清理
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("UPDATE breach_environments SET expires_at = ? WHERE team_id = ?", (1, team_id))
        self.assertIn("重启环境", player.get("/breach").get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT status FROM breach_environments WHERE team_id = ?", (team_id,))[0][0], "stopped")
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM breach_environments WHERE status = 'running'")[0][0], 1)

    def test_puzzle_dynamic_environments(self):
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 动态解题赛", mode="puzzle")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        other, other_team = self.player(contest_id, "packetlab", "PacketLab")
        # 创建赛题：保存不触发检测；「检测连通性」按钮走独立接口
        self.post(self.admin, "/manage/bank/new", join_contest=contest_id,
                  title="容器入口", category="Web", difficulty="入门", points=100,
                  description="读取容器内 FLAG", env_image="nginx:alpine", env_container_port="80", published="1")
        puzzle_id = self.lookup("SELECT id FROM puzzles WHERE env_image IS NOT NULL")[0][0]
        self.puzzle(contest_id)
        static_id = self.lookup(
            "SELECT p.id FROM puzzles p WHERE p.env_image IS NULL "
            "AND p.id IN (SELECT puzzle_id FROM contest_puzzles WHERE contest_id = ?)", (contest_id,),
        )[0][0]
        page = player.get(f"/challenges/{puzzle_id}").get_data(as_text=True)
        self.assertIn("启动环境", page)
        self.assertIn("动态环境", player.get("/challenges?category=Web").get_data(as_text=True))

        self.post(player, f"/challenges/{puzzle_id}/env/start")
        env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ?", (puzzle_id,))[0]
        self.assertEqual((env["status"], env["host_port"]), ("running", 8400))
        run = fake.state["runs"][0]
        self.assertEqual(run["image"], "nginx:alpine")
        self.assertEqual(run["ports"], {"80/tcp": 8400})
        derived = env_flag_value(env["secret"], env["id"], puzzle_id)
        self.assertEqual(run["environment"]["FLAG"], derived)
        self.assertIn(":8400</a>", player.get(f"/challenges/{puzzle_id}").get_data(as_text=True))

        result = self.post(player, f"/challenges/{puzzle_id}/flag", flag=derived, follow=True, code=200)
        self.assertIn("Flag 正确", result.get_data(as_text=True))
        # 其他队伍环境的派生 flag 无效（other 解题前先验证跨队派生无效）
        self.post(other, f"/challenges/{puzzle_id}/env/start")
        other_env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (puzzle_id, other_team))[0]
        self.assertEqual(other_env["host_port"], 8401)
        wrong = self.post(other, f"/challenges/{puzzle_id}/flag",
                          flag=derived, follow=True, code=200)
        self.assertIn("Flag 错误", wrong.get_data(as_text=True))
        # 静态赛题不受影响
        self.post(player, f"/challenges/{static_id}/flag", flag="flag{fixture}", follow=True, code=200)
        self.assertEqual([tuple(r) for r in self.lookup(
            "SELECT verdict FROM flag_submissions WHERE puzzle_id = ? ORDER BY id", (puzzle_id,))],
            [("correct",), ("incorrect",)])
        # 停止后环境卡片回到待启动状态
        self.post(player, f"/challenges/{puzzle_id}/env/stop")
        self.assertEqual(self.lookup(
            "SELECT status FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (puzzle_id, team_id))[0][0],
            "stopped")
        again = player.get(f"/challenges/{puzzle_id}").get_data(as_text=True)
        self.assertIn("重启环境", again)
        self.assertNotIn(":8400</a>", again)
        # 编辑保留动态属性并更新镜像（保存路径不做连通性检测）
        self.post(self.admin, f"/manage/bank/{puzzle_id}/edit",
                  title="容器入口", category="Web", difficulty="入门", points=120,
                  description="同上", env_image="nginx:alpine", env_container_port="8080", published="1")
        row = self.lookup("SELECT points, env_image, env_container_port, flag_digest FROM puzzles WHERE id = ?", (puzzle_id,))[0]
        self.assertEqual((row["points"], row["env_image"], row["env_container_port"], row["flag_digest"]),
                         (120, "nginx:alpine", 8080, ""))
        # 比赛创建表单已瘦化：解题赛不再出现环境配置字段（改为默认值）
        self.assertNotIn("env_ttl_minutes", self.admin.get("/manage/contests/new").get_data(as_text=True))
        # 检测连通性按钮接口：成功与失败两种结果
        with patch("ctf_platform.admin.probe_image", return_value=(True, "")):
            with self.admin.session_transaction() as session:
                token = session["csrf"]
            ok = self.admin.post("/manage/bank/probe", json={"env_image": "nginx:alpine", "env_container_port": 80},
                                 headers={"X-CSRF-Token": token})
            self.assertTrue(ok.json["ok"])
            self.assertIn("连接成功", ok.json["message"])
        with patch("ctf_platform.admin.probe_image", return_value=(False, "端口 80 连接失败")):
            bad = self.admin.post("/manage/bank/probe", json={"env_image": "nginx:alpine", "env_container_port": 80},
                                  headers={"X-CSRF-Token": token})
            self.assertFalse(bad.json["ok"])
            self.assertIn("检测失败", bad.json["message"])
        invalid = self.admin.post("/manage/bank/probe", json={"env_image": "", "env_container_port": 80},
                                  headers={"X-CSRF-Token": token})
        self.assertFalse(invalid.json["ok"])
        # 表单中存在检测按钮与状态区
        self.assertIn("检测连通性", self.admin.get("/manage/bank/new").get_data(as_text=True))

    def test_awdp_target_attack_and_defense(self):
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · AWDP 攻防赛", mode="round")
        player, team_a = self.player(contest_id, "bluefox", "BlueFox")
        other, team_b = self.player(contest_id, "packetlab", "PacketLab")
        task_id = self.round_task(contest_id)
        target_id = self.awdp_target(contest_id, env_image="nginx:alpine", points=200, attachment=True)
        static_id = self.awdp_target(contest_id, title="静态靶机", points=120, flag="flag{static_target}")

        # 题库类型区分与筛选
        self.assertIn("环境镜像", self.admin.get("/manage/bank/new?kind=round").get_data(as_text=True))
        self.assertIn("镜像靶场", self.admin.get("/manage/bank?kind=round").get_data(as_text=True))
        self.assertNotIn("镜像靶场", self.admin.get("/manage/bank?kind=puzzle").get_data(as_text=True))
        self.assertIn("AWDP 靶机", self.admin.get("/manage/challenges/round").get_data(as_text=True))
        targets_page = self.admin.get(f"/manage/contests/{contest_id}/puzzles").get_data(as_text=True)
        self.assertIn("比赛靶机", targets_page)
        self.assertIn("镜像靶场", targets_page)

        # AWDP 靶机面板与详情入口
        arena = player.get("/awdp?view=tasks").get_data(as_text=True)
        self.assertIn("镜像靶场", arena)
        self.assertIn("进入靶机", arena)
        self.assertIn("档案检索", arena)

        # 双方各自启动靶机容器，端口依次分配
        self.post(player, f"/awdp/targets/{target_id}/env/start")
        env_a = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ?", (target_id,))[0]
        self.assertEqual((env_a["status"], env_a["host_port"]), ("running", 8400))
        run = fake.state["runs"][0]
        self.assertEqual((run["image"], run["ports"]), ("nginx:alpine", {"80/tcp": 8400}))
        flag_a = env_flag_value(env_a["secret"], env_a["id"], target_id)
        self.assertEqual(run["environment"]["FLAG"], flag_a)
        self.post(other, f"/awdp/targets/{target_id}/env/start")
        env_b = self.lookup(
            "SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_b))[0]
        self.assertEqual(env_b["host_port"], 8401)
        flag_b = env_flag_value(env_b["secret"], env_b["id"], target_id)

        # 详情页展示双方在线靶机地址与修复文档入口
        detail = player.get(f"/awdp/targets/{target_id}").get_data(as_text=True)
        self.assertIn(":8400</a>", detail)
        self.assertIn(":8401</a>", detail)
        self.assertIn("提交修复文档", detail)

        # B 提交本队 flag → 判错误；无效 flag → 判错误
        own = self.post(other, f"/awdp/targets/{target_id}/flag", flag=flag_b, follow=True, code=200)
        self.assertIn("Flag 错误", own.get_data(as_text=True))
        self.assertIn("本队靶机", own.get_data(as_text=True))
        self.post(other, f"/awdp/targets/{target_id}/flag", flag="flag{nope}", follow=True, code=200)
        # B 攻破 A 队靶机 → 攻击得分
        hit = self.post(other, f"/awdp/targets/{target_id}/flag", flag=flag_a, follow=True, code=200)
        self.assertIn("Flag 正确", hit.get_data(as_text=True))
        self.assertIn("200 攻击积分", hit.get_data(as_text=True))
        # 重复提交（含队友）不重复计分
        repeat = self.post(other, f"/awdp/targets/{target_id}/flag", flag=flag_a, follow=True, code=200)
        self.assertIn("已攻破", repeat.get_data(as_text=True))
        verdicts = [tuple(r) for r in self.lookup(
            "SELECT verdict, points FROM flag_submissions WHERE puzzle_id = ? AND team_id = ? ORDER BY id",
            (target_id, team_b))]
        self.assertEqual(verdicts, [("incorrect", 0), ("incorrect", 0), ("correct", 200), ("already_solved", 0)])
        # 攻击得分进入回合积分流水
        self.assertEqual([tuple(r) for r in self.lookup("SELECT kind, delta, round_no FROM score_events ORDER BY id")],
                         [("attack", 200, 1)])
        # 静态靶机自行解题同样计攻击分
        self.post(other, f"/awdp/targets/{static_id}/flag", flag="flag{static_target}", follow=True, code=200)
        self.assertEqual([tuple(r) for r in self.lookup("SELECT kind, delta, round_no FROM score_events ORDER BY id")],
                         [("attack", 200, 1), ("attack", 120, 1)])

        # 修复文档：ZIP 校验、队伍隔离下载、评审发放加固分
        self.post(other, f"/awdp/targets/{target_id}/fix", version="v1.1",
                  fix=(io.BytesIO(b"not a zip"), "fix.zip"), code=400)
        self.post(other, f"/awdp/targets/{target_id}/fix", version="v1.1", notes="修复说明", fix=(patch_zip(), "fix.zip"))
        fix = self.lookup("SELECT * FROM patches WHERE target_id IS NOT NULL")[0]
        self.assertIsNone(fix["task_id"])
        download = f"/downloads/{fix['file_id']}"
        self.assertEqual(other.get(download).status_code, 200)
        self.assertEqual(player.get(download).status_code, 404)
        self.assertEqual(self.admin.get(download).status_code, 200)
        review = f"/manage/patches/{fix['id']}"
        self.assertIn("镜像靶场", self.admin.get(review).get_data(as_text=True))
        self.post(self.admin, review, contest_id=contest_id, status="passed", award=80, review_notes="加固到位")
        self.assertEqual([tuple(r) for r in self.lookup("SELECT kind, delta, round_no FROM score_events ORDER BY id")],
                         [("attack", 200, 1), ("attack", 120, 1), ("defense", 80, 1)])
        board = other.get("/api/scoreboard/round").json["content"]
        self.assertIn('data-sb-value="400"', board)

        # 记录页双类型切换与靶机附件下载
        flag_records_page = other.get("/submissions?kind=flag").get_data(as_text=True)
        self.assertIn("镜像靶场", flag_records_page)
        patch_records_page = other.get("/submissions").get_data(as_text=True)
        self.assertIn("补丁与修复文档", patch_records_page)
        self.assertIn("镜像靶场", patch_records_page)
        self.assertIn("P-0001", patch_records_page)
        attachment_id = self.lookup("SELECT id FROM uploads WHERE name = 'target.txt'")[0][0]
        self.assertEqual(player.get(f"/downloads/{attachment_id}").status_code, 200)

        # 停止靶机后地址不再展示
        self.post(player, f"/awdp/targets/{target_id}/env/stop")
        self.assertEqual(self.lookup(
            "SELECT status FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_a))[0][0],
            "stopped")
        self.assertNotIn(":8400</a>", player.get(f"/awdp/targets/{target_id}").get_data(as_text=True))

        # 类型不可混入：解题赛题不能加入 AWDP 比赛，靶机不能加入解题赛
        self.post(self.admin, "/manage/bank/new", join_contest=contest_id, title="混入挑战",
                  category="Web", difficulty="入门", points=100, description="x", flag="flag{x}", published="1", code=400)
        other_contest = self.contest("蓝域杯 · 解题对照赛", mode="puzzle")
        self.post(self.admin, "/manage/bank/new", join_contest=other_contest, kind="round", title="反向混入",
                  category="Web", difficulty="入门", points=100, description="x", published="1", code=400)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM contest_puzzles WHERE puzzle_id IN "
                                     "(SELECT id FROM puzzles WHERE kind = 'puzzle') AND contest_id = ?",
                                     (contest_id,))[0][0], 0)
        # 切换到解题赛后，靶机页面不可访问
        self.assertEqual(other.get(f"/awdp/targets/{target_id}").status_code, 404)

    def test_contest_cover_upload_crops_to_selection(self):
        def png_stream():
            buffer = io.BytesIO()
            Image.new("RGB", (400, 200), "#224466").save(buffer, "PNG")
            buffer.seek(0)
            return buffer
        self.post(self.admin, "/manage/contests/new", mode="puzzle", title="截取赛",
                  start_at=(datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                  end_at=(datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"))
        contest_id = self.lookup("SELECT id FROM contests WHERE title = '截取赛'")[0][0]
        # 无裁剪参数：整图保存
        self.post(self.admin, f"/manage/contests/{contest_id}/edit", mode="puzzle", title="截取赛",
                  start_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                  end_at=(datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                  cover=(png_stream(), "wide.png"))
        raw = self.lookup("SELECT content FROM uploads WHERE id = (SELECT cover_id FROM contests WHERE id = ?)",
                          (contest_id,))[0][0]
        self.assertEqual(Image.open(io.BytesIO(raw)).size, (400, 200))
        # 带裁剪参数：保存为截取后的区域（所见即所得）
        self.post(self.admin, f"/manage/contests/{contest_id}/edit", mode="puzzle", title="截取赛",
                  start_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                  end_at=(datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                  cover=(png_stream(), "crop.png"), crop_x="50", crop_y="60", crop_w="280", crop_h="100")
        raw = self.lookup("SELECT content FROM uploads WHERE id = (SELECT cover_id FROM contests WHERE id = ?)",
                          (contest_id,))[0][0]
        self.assertEqual(Image.open(io.BytesIO(raw)).size, (280, 100))
        # 非法裁剪参数被拒绝且不落库
        self.post(self.admin, f"/manage/contests/{contest_id}/edit", mode="puzzle", title="截取赛",
                  start_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                  end_at=(datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                  cover=(png_stream(), "crop.png"), crop_x="-5", crop_y="0", crop_w="280", crop_h="100", code=400)
        self.assertEqual(Image.open(io.BytesIO(
            self.lookup("SELECT content FROM uploads WHERE id = (SELECT cover_id FROM contests WHERE id = ?)",
                        (contest_id,))[0][0])).size, (280, 100))

    def test_bulk_delete_accounts(self):
        contest_id = self.contest()
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        spare, spare_id = self.player(contest_id, "packetlab", "PacketLab")
        puzzle_id = self.puzzle(contest_id)
        self.post(player, f"/challenges/{puzzle_id}/flag", flag="flag{fixture}")
        page = self.admin.get("/manage/accounts").get_data(as_text=True)
        self.assertIn("批量删除", page)
        self.assertIn("data-select-all", page)

        ids = [row[0] for row in self.lookup("SELECT id FROM users WHERE username IN ('bluefox', 'packetlab')")]
        admin_id = self.lookup("SELECT id FROM users WHERE username = 'arenaops'")[0][0]
        # 空选与包含当前登录账号均被拒绝
        self.post(self.admin, "/manage/accounts/delete-bulk", select=[], follow=True, code=200)
        self.post(self.admin, "/manage/accounts/delete-bulk", code=400, select=[str(admin_id)])
        # 确认页展示所选账号
        confirm = self.post(self.admin, "/manage/accounts/delete-bulk",
                            select=[str(v) for v in ids], code=200)
        self.assertIn("批量删除账号", confirm.get_data(as_text=True))
        self.assertIn("bluefox", confirm.get_data(as_text=True))
        # 执行删除
        self.post(self.admin, "/manage/accounts/delete-bulk", select=[str(v) for v in ids], stage="execute")
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM users WHERE username IN ('bluefox', 'packetlab')")[0][0], 0)
        # 提交历史保留且归入原队伍（user_id 置空）
        rows = self.lookup("SELECT verdict, user_id, team_id FROM flag_submissions")
        self.assertEqual([(row["verdict"], row["user_id"], row["team_id"]) for row in rows], [("correct", None, team_id)])
        self.assertIn("已删除账号", self.admin.get("/api/scoreboard/puzzle").json["content"])

    def test_junction_foreign_key_repair(self):
        """复现历史迁移遗留的悬挂外键（关联表指向已删除的 _legacy 表），启动时自动修复。"""
        contest_id = self.contest()
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript(f"""
                BEGIN IMMEDIATE;
                DROP TABLE contest_teams;
                CREATE TABLE contest_teams (
                    contest_id INTEGER NOT NULL REFERENCES contests_missing(id) ON DELETE CASCADE,
                    team_id INTEGER NOT NULL,
                    UNIQUE (contest_id, team_id));
                INSERT INTO contest_teams VALUES ({contest_id}, {team_id});
                COMMIT;""")
        repaired = create_app({**self.settings, "ADMIN_PASSWORD": ""})
        with closing(connect(repaired.config["DATABASE"])) as db:
            parents = [row["table"] for row in db.execute("PRAGMA foreign_key_list(contest_teams)")]
            self.assertEqual(parents, ["contests"])
            self.assertEqual(len(db.execute(
                "SELECT team_id FROM contest_teams WHERE contest_id = ?", (contest_id,)).fetchall()), 1)
        # 修复后关联写入恢复正常（此前会 500）
        admin = self.login("arenaops", repaired)
        self.post(admin, f"/manage/contests/{contest_id}/teams/add", team_id=team_id)
        with closing(connect(repaired.config["DATABASE"])) as db:
            self.assertEqual(len(db.execute(
                "SELECT team_id FROM contest_teams WHERE contest_id = ?", (contest_id,)).fetchall()), 1)

    def test_import_users_and_teams(self):
        contest_id = self.contest("导入验证赛", active=False)
        users_csv = "账号,显示名,密码,角色\nfox01,狐狸一号,ImportPass-01,player\nfox02,狐狸二号,ImportPass-02,player\nchief,指挥官,ImportPass-03,admin\n"
        self.post(self.admin, "/manage/import/users",
                  file=(io.BytesIO(users_csv.encode("utf-8")), "users.csv"))
        self.assertEqual(len(self.lookup("SELECT id FROM users WHERE username LIKE 'fox%'")), 2)
        # 重复导入整体回滚
        self.post(self.admin, "/manage/import/users", code=400,
                  file=(io.BytesIO(users_csv.encode("utf-8")), "users.csv"))
        # GBK 编码兼容（中文 Excel 默认导出）
        gbk_csv = "账号,显示名,密码,角色\ngbkuser,中文编码,ImportPass-07,player\n".encode("gbk")
        self.post(self.admin, "/manage/import/users",
                  file=(io.BytesIO(gbk_csv), "users.csv"))
        self.assertEqual(len(self.lookup("SELECT id FROM users WHERE username = 'gbkuser'")), 1)
        teams_csv = ("队伍,账号,显示名,密码\n"
                     "蓝狐突击队,fox01,狐狸一号,\n"
                     "蓝狐突击队,fox03,新队员,ImportPass-04\n"
                     "极光战队,,,\n")
        self.post(self.admin, "/manage/import/teams", contest_id=contest_id,
                  file=(io.BytesIO(teams_csv.encode("utf-8")), "teams.csv"))
        self.assertEqual(len(self.lookup(
            "SELECT team_id FROM contest_teams WHERE contest_id = ?", (contest_id,))), 2)
        self.assertEqual(len(self.lookup("SELECT id FROM users WHERE username = 'fox03'")), 1)
        memberships = self.lookup("SELECT team_id, user_id FROM memberships WHERE contest_id = ?", (contest_id,))
        self.assertEqual(len(memberships), 2)
        # 题库独立创建（不挂任何比赛）后再加入比赛
        self.post(self.admin, "/manage/bank/new",
                  title="独立题", category="Misc", difficulty="入门", points=50,
                  description="题库题", flag="flag{bank}", published="1")
        bank_id = self.lookup("SELECT id FROM puzzles WHERE title = '独立题'")[0][0]
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM contest_puzzles WHERE puzzle_id = ?", (bank_id,))[0][0], 0)
        self.post(self.admin, f"/manage/contests/{contest_id}/puzzles/add", puzzle_id=bank_id)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM contest_puzzles WHERE puzzle_id = ?", (bank_id,))[0][0], 1)
        rows = self.admin.get(f"/manage/contests/{contest_id}/puzzles").get_data(as_text=True)
        self.assertIn("独立题", rows)
        self.post(self.admin, f"/manage/contests/{contest_id}/puzzles/remove", puzzle_id=bank_id)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM contest_puzzles WHERE puzzle_id = ?", (bank_id,))[0][0], 0)
        self.assertEqual(self.lookup("SELECT id FROM puzzles WHERE id = ?", (bank_id,))[0][0], bank_id)
        # 同名（忽略大小写）队伍复用为同一支，不报错、不重复建队
        teams_csv2 = "队伍,账号,显示名,密码\n极光战队,aur01,极光英文队员,ImportPass-08\n"
        self.post(self.admin, "/manage/import/teams", contest_id=contest_id,
                  file=(io.BytesIO(teams_csv2.encode("utf-8")), "teams.csv"))
        self.assertEqual(len(self.lookup("SELECT id FROM teams WHERE name = '极光战队'")), 1)
        self.assertEqual(len(self.lookup(
            "SELECT user_id FROM memberships WHERE team_id = (SELECT id FROM teams WHERE name = '极光战队') "
            "AND contest_id = ?", (contest_id,))), 1)

        # 新建比赛自动沿用上一场比赛的队伍与分队，无需重新配置
        from datetime import timedelta
        now = datetime.now()
        self.post(self.admin, "/manage/contests/new", mode="puzzle", title="沿用验证赛",
                  start_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                  end_at=(now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"))
        carried_id = self.lookup("SELECT id FROM contests WHERE title = '沿用验证赛'")[0][0]
        self.assertEqual(
            self.lookup("SELECT COUNT(*) FROM contest_teams WHERE contest_id = ?", (carried_id,))[0][0], 2)
        carried = self.lookup(
            """SELECT m.user_id FROM memberships m JOIN users u ON u.id = m.user_id
               WHERE m.contest_id = ? AND u.username = 'fox01'""", (carried_id,))
        self.assertEqual(len(carried), 1)

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
            self.assertNotIn("Asia/Shanghai", page)
            self.assertIn("队伍排名", page)
            for medal in ("金色奖杯", "银色奖杯", "铜色奖杯"):
                self.assertIn(medal, page)
        self.assertIn("选手得分排行", puzzle)
        self.assertIn("解出动态", puzzle)
        self.assertNotIn("服务状态", puzzle)
        self.assertIn("选手加固得分", rounds)
        self.assertIn("本回合剩余", rounds)
        self.assertIn("服务状态", rounds)

    def test_manage_unopened_contests_and_switch_entries(self):
        puzzle_contest = self.contest(active=False)
        puzzle_id = self.puzzle(puzzle_contest)
        file_id = self.lookup("SELECT file_id FROM puzzle_files WHERE puzzle_id = ?", (puzzle_id,))[0][0]
        download = f"/downloads/{file_id}"
        self.assertIn("待开放", self.admin.get(f"/manage/contests/{puzzle_contest}/puzzles").get_data(as_text=True))
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
        puzzle_page = player.get("/puzzle").get_data(as_text=True)
        self.assertIn("arena-subnav", puzzle_page)
        self.assertIn("赛题", puzzle_page)
        self.assertIn("排行榜", puzzle_page)
        self.assertIn("我的提交", puzzle_page)
        self.assertNotIn("题库", puzzle_page)

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
        legacy = self.admin.get("/manage/challenges/puzzle")
        self.assertEqual(legacy.status_code, 301)
        self.assertEqual(legacy.location, "/manage/bank")
        detail = f"/awdp/challenges/{task_id}"
        waiting = player.get("/awdp").get_data(as_text=True)
        self.assertNotIn("测试赛", waiting)
        self.assertNotIn("回合赛", waiting)
        self.assertEqual(player.get(detail).status_code, 404)
        self.assertEqual(player.get(f"/challenges/{puzzle_id}").status_code, 200)

        self.post(self.admin, f"/manage/contests/{round_contest}/activate")
        self.assertEqual(self.lookup("SELECT id FROM contests WHERE active = 1")[0][0], round_contest)
        self.assertIn("回合赛", player.get("/").get_data(as_text=True))
        self.assertIn("arena-subnav", player.get("/awdp").get_data(as_text=True))
        self.assertIn("档案检索新版", player.get(detail).get_data(as_text=True))
        self.assertNotIn("回合赛", player.get("/puzzle").get_data(as_text=True))
        self.assertNotIn("arena-subnav", player.get("/puzzle").get_data(as_text=True))
        self.assertEqual(player.get(f"/challenges/{puzzle_id}").status_code, 404)
        self.assertEqual(player.get(download).status_code, 404)
        self.assertEqual(player.get("/rankings?kind=puzzle").status_code, 404)
        # AWDP 比赛的提交记录同时涵盖补丁与靶机 Flag
        self.assertEqual(player.get("/submissions?kind=flag").status_code, 200)
        self.assertEqual(player.get("/submissions?kind=breach").status_code, 404)
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
        self.assertEqual(self.lookup("PRAGMA user_version")[0][0], 9)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])
        self.assertEqual(len(self.lookup("SELECT id FROM puzzles")), 1)
        self.assertEqual(self.lookup("SELECT contest_id FROM contest_puzzles")[0][0], mixed)
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
        self.assertEqual([row["contest_id"] for row in self.lookup("SELECT contest_id FROM contest_puzzles")], [other_id])
        self.assertEqual(len(self.lookup("SELECT id FROM puzzles")), 2)
        # 队伍为全局名册：删除比赛不影响队伍本身；分队只保留在未删除的比赛中
        self.assertEqual(self.lookup("SELECT id FROM teams WHERE id = ?", (team_id,))[0][0], team_id)
        self.assertEqual([row["contest_id"] for row in self.lookup("SELECT contest_id FROM memberships")], [other_id])
        self.assertEqual(self.lookup("SELECT * FROM flag_submissions"), [])
        self.assertEqual(self.lookup("SELECT id FROM uploads WHERE id = ?", (cover_id,)), [])
        self.assertEqual(self.lookup("SELECT id FROM uploads WHERE id = ?", (file_id,))[0][0], file_id)
        self.assertEqual(len(self.lookup("SELECT id FROM uploads")), 3)
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
