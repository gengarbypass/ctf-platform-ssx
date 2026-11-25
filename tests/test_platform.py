import io
import re
import secrets
import sqlite3
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

    def awdp_target(self, contest_id, *, title="镜像靶场", points=200, env_image=None, flag=None, attachment=False, flags=None):
        data = {"title": title, "category": "Web", "difficulty": "进阶", "points": points,
                "description": "AWDP 攻防靶机", "kind": "round", "published": "1", "join_contest": contest_id}
        if env_image:
            data["env_image"] = env_image
            data["env_container_port"] = "80"
        if flag:
            data["flag"] = flag
        if flags:  # 靶点行：(名称, 分值, Flag)；动态靶机的 Flag 留空
            data.update(flag_id=[""] * len(flags), flag_title=[name for name, _, _ in flags],
                        flag_points=[str(score) for _, score, _ in flags], flag_value=[value for _, _, value in flags],
                        flag_description=[""] * len(flags))
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
        # 停止后重启：secret（Flag）不变，端口更换且台账保证永不复用历史端口
        self.post(player, "/breach/env/stop")
        self.post(player, "/breach/env/start")
        restarted = self.lookup("SELECT * FROM breach_environments WHERE team_id = ?", (team_id,))[0]
        self.assertEqual(restarted["secret"], env["secret"])
        self.assertEqual(restarted["host_port"], 8402)
        self.assertEqual([row[0] for row in self.lookup("SELECT port FROM env_ports ORDER BY port")],
                         [8400, 8401, 8402])
        self.assertIn(":8402</a>", player.get("/breach").get_data(as_text=True))
        result = self.post(player, "/breach/flag", flag=env_flag_value(env["secret"], env["id"], 2), follow=True, code=200)
        self.assertIn("本队获得 150 分", result.get_data(as_text=True))
        self.assertIn("<strong>250</strong>", player.get("/breach").get_data(as_text=True))

        # TTL 过期由任意靶场访问惰性清理
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("UPDATE breach_environments SET expires_at = ? WHERE team_id = ?", (1, team_id))
        self.assertIn("重启环境", player.get("/breach").get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT status FROM breach_environments WHERE team_id = ?", (team_id,))[0][0], "stopped")
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM breach_environments WHERE status = 'running'")[0][0], 1)

    def test_breach_flag_submission_sweeps_pending_environments(self):
        """提交 Flag 时遇到过期或所属比赛已下线的待回收环境：先回收再判定，不因事务嵌套报错。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        first = self.contest("蓝域杯 · 渗透首轮", mode="breach", env_image="iotmanage-dmz:1.0", env_container_port="80")
        self.breach_flag(first, "F1 备份泄露", "边界突破", "", points=100)
        player, team_id = self.player(first, "bluefox", "BlueFox")
        self.post(player, "/breach/env/start")
        # 环境到期后直接提交（未经页面触发回收）：过期环境的 Flag 不再匹配，提交照常得到判定
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("UPDATE breach_environments SET expires_at = 1 WHERE team_id = ?", (team_id,))
        result = self.post(player, "/breach/flag", flag="flag{late}", follow=True, code=200)
        self.assertIn("Flag 错误", result.get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT status FROM breach_environments WHERE team_id = ?", (team_id,))[0][0], "stopped")
        self.assertNotIn(f"ctfenv-c{first}-t{team_id}-net", fake.state["networks"])
        # 比赛切换后，上一场尚未回收的环境同样在提交时被回收，不影响新比赛的判定
        self.post(player, "/breach/env/start")
        second = self.contest("蓝域杯 · 渗透次轮", mode="breach", env_image="iotmanage-dmz:1.0", env_container_port="80")
        self.breach_flag(second, "F1 备份泄露", "边界突破", "flag{second_round}", points=100)
        result = self.post(player, "/breach/flag", flag="flag{second_round}", follow=True, code=200)
        self.assertIn("Flag 正确", result.get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT status FROM breach_environments WHERE contest_id = ?", (first,))[0][0], "stopped")

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
        # 时长到期的环境由访问题目页惰性回收：各队容器与各自的专属网络一并移除
        self.post(player, f"/challenges/{puzzle_id}/env/start")
        networks = {f"ctfenv-c{contest_id}-t{team}-p{puzzle_id}-net" for team in (team_id, other_team)}
        self.assertTrue(networks <= set(fake.state["networks"]))
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("UPDATE breach_environments SET expires_at = 1 WHERE puzzle_id = ?", (puzzle_id,))
        self.assertIn("重启环境", player.get(f"/challenges/{puzzle_id}").get_data(as_text=True))
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT DISTINCT status FROM breach_environments WHERE puzzle_id = ?", (puzzle_id,))], [("stopped",)])
        self.assertEqual(set(fake.state["networks"]) & networks, set())

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

        # 全场靶机由平台自动开启：访问 AWDP 页面即补齐所有队伍的靶机环境
        env_a = self.lookup(
            "SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_a))[0]
        self.assertEqual((env_a["status"], env_a["host_port"]), ("running", 8400))
        run = fake.state["runs"][0]
        self.assertEqual((run["image"], run["ports"]), ("nginx:alpine", {"80/tcp": 8400}))
        flag_a = env_flag_value(env_a["secret"], env_a["id"], target_id)
        self.assertEqual(run["environment"]["FLAG"], flag_a)
        env_b = self.lookup(
            "SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_b))[0]
        self.assertEqual((env_b["status"], env_b["host_port"]), ("running", 8401))
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

    def test_awdp_targets_autostart_for_all_teams(self):
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 全场靶机开启", mode="round", active=False)
        target_id = self.awdp_target(contest_id, env_image="nginx:alpine", points=200)
        self.awdp_target(contest_id, title="静态靶机", points=120, flag="flag{static_only}")
        player, team_a = self.player(contest_id, "bluefox", "BlueFox")
        other, team_b = self.player(contest_id, "packetlab", "PacketLab")

        # 激活比赛：全场靶机自动为所有队伍开启，运行至比赛结束
        self.post(self.admin, f"/manage/contests/{contest_id}/activate")
        end_at = self.lookup("SELECT end_at FROM contests WHERE id = ?", (contest_id,))[0][0]
        envs = self.lookup("SELECT team_id, status, host_port, expires_at FROM breach_environments ORDER BY host_port")
        self.assertEqual([tuple(row) for row in envs], [
            (team_a, "running", 8400, end_at), (team_b, "running", 8401, end_at)])
        self.assertEqual(len(fake.state["runs"]), 2)
        flag_values = {run["environment"]["FLAG"] for run in fake.state["runs"]}
        self.assertEqual(len(flag_values), 2)  # 各队派生的 Flag 互不相同
        # 无镜像靶机不创建环境
        self.assertEqual(self.lookup(
            "SELECT COUNT(*) FROM breach_environments e JOIN puzzles p ON p.id = e.puzzle_id "
            "WHERE p.env_image IS NULL")[0][0], 0)

        # 比赛中新增靶机：加入比赛后随即自动补启全场环境
        target2 = self.awdp_target(contest_id, title="追加靶机", env_image="nginx:alpine", points=150)
        added = self.lookup(
            "SELECT team_id, host_port FROM breach_environments WHERE puzzle_id = ? ORDER BY host_port", (target2,))
        self.assertEqual([row["host_port"] for row in added], [8402, 8403])
        self.assertEqual(sorted(row["team_id"] for row in added), sorted([team_a, team_b]))

        # 攻击判定不受自动开启影响：B 提交 A 队派生 Flag 得攻击分
        env_a = self.lookup(
            "SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_a))[0]
        hit = self.post(other, f"/awdp/targets/{target_id}/flag",
                        flag=env_flag_value(env_a["secret"], env_a["id"], target_id), follow=True, code=200)
        self.assertIn("Flag 正确", hit.get_data(as_text=True))
        self.assertEqual([tuple(r) for r in self.lookup("SELECT kind, delta, round_no FROM score_events")],
                         [("attack", 200, 1)])
        # 详情页不再提供停止按钮（靶机由平台统一管理）
        detail = other.get(f"/awdp/targets/{target_id}").get_data(as_text=True)
        self.assertIn("重启靶机", detail)
        self.assertNotIn("停止靶机", detail)

        # 比赛结束后访问靶机页惰性回收全场靶机：各队容器与各自的专属网络一并移除
        networks = {f"ctfenv-c{contest_id}-t{team}-p{target}-net"
                    for team in (team_a, team_b) for target in (target_id, target2)}
        self.assertTrue(networks <= set(fake.state["networks"]))
        with closing(connect(self.app.config["DATABASE"])) as db:
            start_at = db.execute("SELECT start_at FROM contests WHERE id = ?", (contest_id,)).fetchone()[0]
            db.execute("UPDATE contests SET end_at = ? WHERE id = ?", (start_at + 60, contest_id))
        other.get(f"/awdp/targets/{target_id}")
        self.assertEqual([tuple(row) for row in self.lookup("SELECT DISTINCT status FROM breach_environments")],
                         [("stopped",)])
        self.assertEqual(set(fake.state["networks"]) & networks, set())

    def test_awdp_target_flags_inject_score_and_display_per_flag(self):
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 靶点独立 Flag 赛", mode="round", active=False)
        target_id = self.awdp_target(contest_id, env_image="nginx:alpine", points=300, flags=[
            ("SQL 注入拖库", 100, ""), ("拿下管理后台", 100, ""), ("协议取证还原", 200, "")])
        player, team_a = self.player(contest_id, "bluefox", "BlueFox")
        other, team_b = self.player(contest_id, "packetlab", "PacketLab")
        flags = self.lookup("SELECT id, title, points, flag_digest FROM target_flags WHERE puzzle_id = ? ORDER BY id", (target_id,))
        self.assertEqual([(row["title"], row["points"], row["flag_digest"]) for row in flags],
                         [("SQL 注入拖库", 100, ""), ("拿下管理后台", 100, ""), ("协议取证还原", 200, "")])
        # 题库与比赛靶机页显示靶点数
        self.assertIn("3 个靶点", self.admin.get("/manage/bank?kind=round").get_data(as_text=True))
        self.assertIn("3 个靶点", self.admin.get(f"/manage/contests/{contest_id}/puzzles").get_data(as_text=True))

        # 全场自动开启：每个靶点一枚独立 Flag，按顺序注入 FLAG_1..FLAG_3；FLAG 与 FLAG_1 同值，不再注入靶机级 Flag
        self.post(self.admin, f"/manage/contests/{contest_id}/activate")
        values = {}
        for team in (team_a, team_b):
            env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team))[0]
            run = next(run for run in fake.state["runs"] if run["name"] == f"ctfenv-c{contest_id}-t{team}-p{target_id}")
            values[team] = [env_flag_value(env["secret"], env["id"], flag["id"], kind="target") for flag in flags]
            self.assertEqual(run["environment"], {"FLAG": values[team][0], "FLAG_1": values[team][0],
                                                  "FLAG_2": values[team][1], "FLAG_3": values[team][2]})
        self.assertEqual(len({value for team in values for value in values[team]}), 6)  # 各队各靶点的 Flag 互不相同
        # 停止后手动重启：同样按靶点注入
        self.post(player, f"/awdp/targets/{target_id}/env/stop")
        self.post(player, f"/awdp/targets/{target_id}/env/start")
        self.assertEqual(fake.state["runs"][-1]["environment"], {"FLAG": values[team_a][0], "FLAG_1": values[team_a][0],
                                                                 "FLAG_2": values[team_a][1], "FLAG_3": values[team_a][2]})

        page = f"/awdp/targets/{target_id}"
        arena = other.get("/awdp?view=tasks").get_data(as_text=True)
        self.assertIn("3 个靶点", arena)
        self.assertIn("未攻破", arena)
        # B 队提交 A 队各靶点的 Flag：逐个命中靶点并按其分值计攻击分；两支队时对手只有 A 一支
        first = self.post(other, f"{page}/flag", flag=values[team_a][0], follow=True, code=200).get_data(as_text=True)
        self.assertIn("靶点「SQL 注入拖库」", first)
        self.assertIn("取自队伍「BlueFox」的靶机", first)
        self.assertIn("100 攻击积分", first)
        self.assertIn("已获取 1/3 枚", first)
        for index in (1, 2):
            self.post(other, f"{page}/flag", flag=values[team_a][index], follow=True, code=200)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT target_flag_id, victim_team_id, verdict, points FROM flag_submissions WHERE team_id = ? ORDER BY id", (team_b,))],
            [(flags[0]["id"], team_a, "correct", 100), (flags[1]["id"], team_a, "correct", 100),
             (flags[2]["id"], team_a, "correct", 200)])
        self.assertEqual([tuple(row) for row in self.lookup("SELECT kind, delta, reason FROM score_events ORDER BY id")], [
            ("attack", 100, "攻破队伍「BlueFox」靶机「镜像靶场」的靶点「SQL 注入拖库」"),
            ("attack", 100, "攻破队伍「BlueFox」靶机「镜像靶场」的靶点「拿下管理后台」"),
            ("attack", 200, "攻破队伍「BlueFox」靶机「镜像靶场」的靶点「协议取证还原」"),
        ])
        detail = other.get(page).get_data(as_text=True)
        self.assertIn("本队已攻破全部 1 支对手队伍的这台靶机，全部 3 个靶点均已获取", detail)
        self.assertIn("全场已有 1 支队伍攻破过对手的这台靶机", detail)
        self.assertNotIn('name="flag"', detail)
        self.assertIn("已攻破 1/1 队", other.get("/awdp?view=tasks").get_data(as_text=True))
        records = other.get("/submissions?kind=flag").get_data(as_text=True)
        self.assertIn("靶点：SQL 注入拖库", records)
        self.assertIn("攻破队伍：BlueFox", records)
        # 重复提交同一靶点：判已攻破，不重复计分
        repeat = self.post(other, f"{page}/flag", flag=values[team_a][0], follow=True, code=200).get_data(as_text=True)
        self.assertIn("该靶点已被本队攻破", repeat)
        self.assertIn("已经攻破「BlueFox」的这台靶机", repeat)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM score_events")[0][0], 3)
        # 提交本队靶点的 Flag：判错误且不计分
        self.assertIn("本队靶机", self.post(other, f"{page}/flag", flag=values[team_b][2], follow=True, code=200).get_data(as_text=True))
        # 数据库层兜底：同一比赛内同一队伍同一对手同一靶点的第二条正确记录被唯一索引拒绝
        with closing(connect(self.app.config["DATABASE"])) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO flag_submissions (contest_id, puzzle_id, target_flag_id, team_id, victim_team_id,
                                                    verdict, points, submitted_at)
                       VALUES (?, ?, ?, ?, ?, 'correct', 0, 0)""", (contest_id, target_id, flags[0]["id"], team_b, team_a))

    def test_awdp_target_attack_counts_every_rival_team(self):
        """每支队伍都可以提交其他队伍每台靶机的全部 Flag：同一台靶机攻破几支对手队伍就计几次。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 全对手攻防赛", mode="round")
        player, team_a = self.player(contest_id, "bluefox", "BlueFox")
        other, team_b = self.player(contest_id, "packetlab", "PacketLab")
        third, team_c = self.player(contest_id, "nightowl", "NightOwl")
        plain_id = self.awdp_target(contest_id, title="单点靶机", env_image="nginx:alpine", points=300)
        multi_id = self.awdp_target(contest_id, title="多点靶机", env_image="nginx:alpine", points=300, flags=[
            ("SQL 注入拖库", 100, ""), ("拿下管理后台", 200, "")])

        def derived(target_id, team_id, flag_id=None):
            env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_id))[0]
            return env_flag_value(env["secret"], env["id"], flag_id or target_id, kind="target" if flag_id else "flag")

        # 靶机级单枚 Flag：攻破 A 队靶机后进度为 1/2，提交表单保留
        page = f"/awdp/targets/{plain_id}"
        self.assertIn("本队已攻破 0/2 支对手队伍", other.get(page).get_data(as_text=True))
        first = self.post(other, f"{page}/flag", flag=derived(plain_id, team_a), follow=True, code=200).get_data(as_text=True)
        self.assertIn("300 攻击积分", first)
        self.assertIn("取自队伍「BlueFox」的靶机", first)
        self.assertIn("已攻破 1/2 队", first)
        self.assertIn('name="flag"', first)
        repeat = self.post(other, f"{page}/flag", flag=derived(plain_id, team_a), follow=True, code=200).get_data(as_text=True)
        self.assertIn("该靶机已被本队攻破", repeat)
        # C 队靶机上的 Flag 是另一枚：照常计分，全部对手攻破后才收起表单；队友同时提交也只计一次
        teammate = self.login("packetlab")
        barrier = Barrier(2)

        def race(client):
            barrier.wait(timeout=10)
            return self.post(client, f"{page}/flag", flag=derived(plain_id, team_c), follow=True, code=200).get_data(as_text=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(race, (other, teammate)))
        self.assertEqual(sum("300 攻击积分" in result for result in results), 1)
        self.assertEqual(sum("该靶机已被本队攻破" in result for result in results), 1)
        second = next(result for result in results if "300 攻击积分" in result)
        self.assertIn("已攻破 2/2 队", second)
        self.assertIn("本队已攻破全部 2 支对手队伍的这台靶机", second)
        self.assertNotIn('name="flag"', second)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT target_flag_id, victim_team_id, verdict, points FROM flag_submissions WHERE puzzle_id = ? AND team_id = ? ORDER BY id",
            (plain_id, team_b))],
            [(None, team_a, "correct", 300), (None, team_a, "already_solved", 0),
             (None, team_c, "correct", 300), (None, team_c, "already_solved", 0)])

        # 多靶点靶机：拿全 A 队靶机上的两枚 Flag 才算攻破 A 这一支队伍，C 队靶机上的每枚 Flag 另行计分
        page = f"/awdp/targets/{multi_id}"
        sql_id, admin_id = (row["id"] for row in self.lookup(
            "SELECT id FROM target_flags WHERE puzzle_id = ? ORDER BY id", (multi_id,)))
        self.post(other, f"{page}/flag", flag=derived(multi_id, team_a, sql_id))
        middle = self.post(other, f"{page}/flag", flag=derived(multi_id, team_a, admin_id), follow=True, code=200).get_data(as_text=True)
        self.assertIn("已攻破 1/2 队", middle)
        self.assertIn('name="flag"', middle)
        rival_hit = self.post(other, f"{page}/flag", flag=derived(multi_id, team_c, sql_id), follow=True, code=200).get_data(as_text=True)
        self.assertIn("取自队伍「NightOwl」的靶机", rival_hit)
        self.assertIn("已攻破 1/2 队", rival_hit)
        final = self.post(other, f"{page}/flag", flag=derived(multi_id, team_c, admin_id), follow=True, code=200).get_data(as_text=True)
        self.assertIn("200 攻击积分", final)
        self.assertIn("已攻破 2/2 队", final)
        self.assertEqual([tuple(row) for row in self.lookup("SELECT kind, delta, reason FROM score_events ORDER BY id")], [
            ("attack", 300, "攻破队伍「BlueFox」靶机「单点靶机」"),
            ("attack", 300, "攻破队伍「NightOwl」靶机「单点靶机」"),
            ("attack", 100, "攻破队伍「BlueFox」靶机「多点靶机」的靶点「SQL 注入拖库」"),
            ("attack", 200, "攻破队伍「BlueFox」靶机「多点靶机」的靶点「拿下管理后台」"),
            ("attack", 100, "攻破队伍「NightOwl」靶机「多点靶机」的靶点「SQL 注入拖库」"),
            ("attack", 200, "攻破队伍「NightOwl」靶机「多点靶机」的靶点「拿下管理后台」"),
        ])
        arena = other.get("/awdp?view=tasks").get_data(as_text=True)
        self.assertEqual(arena.count("已攻破 2/2 队"), 2)
        self.assertIn("攻破队伍：NightOwl", other.get("/submissions?kind=flag").get_data(as_text=True))
        # 尚未出手的队伍看到自己的 0/2 与全场攻破队伍数
        untouched = third.get(page).get_data(as_text=True)
        self.assertIn("本队已攻破 0/2 支对手队伍", untouched)
        self.assertIn("全场已有 1 支队伍攻破过对手的这台靶机", untouched)
        self.assertIn("<dt>攻破队伍</dt><dd>1 队</dd>", untouched)
        self.assertIn('data-sb-value="1200"', other.get("/api/scoreboard/round").json["content"])

        # 数据库层兜底：同一对手同一靶点只允许一条正确记录，不同对手各自一条
        insert = """INSERT INTO flag_submissions (contest_id, puzzle_id, target_flag_id, team_id, victim_team_id,
                                                  verdict, points, submitted_at)
                    VALUES (?, ?, ?, ?, ?, 'correct', 0, 0)"""
        with closing(connect(self.app.config["DATABASE"])) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(insert, (contest_id, multi_id, sql_id, team_b, team_a))
            db.execute(insert, (contest_id, multi_id, sql_id, team_c, team_a))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(insert, (contest_id, multi_id, sql_id, team_c, team_a))

        # 删除被攻破的队伍：针对它的攻击记录一并删除，攻击方已获积分保留
        self.post(self.admin, f"/manage/teams/{team_c}/delete")
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM contest_teams WHERE team_id = ?", (team_c,))[0][0], 0)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT team_id, victim_team_id, target_flag_id, verdict FROM flag_submissions ORDER BY id")],
            [(team_b, team_a, None, "correct"), (team_b, team_a, None, "already_solved"),
             (team_b, team_a, sql_id, "correct"), (team_b, team_a, admin_id, "correct")])
        self.assertEqual(self.lookup("SELECT SUM(delta) FROM score_events WHERE team_id = ?", (team_b,))[0][0], 1200)
        self.assertIn("已攻破 1/1 队", other.get(f"/awdp/targets/{plain_id}").get_data(as_text=True))
        # 比赛中删掉全部靶点：这两个靶点的攻击记录随之删除（已获积分保留），靶机回到靶机级 Flag
        self.post(self.admin, f"/manage/bank/{multi_id}/edit", title="多点靶机", category="Web", difficulty="进阶",
                  description="AWDP 攻防靶机", kind="round", published="1", points=300, env_image="nginx:alpine",
                  env_container_port="80", flag_id=["", ""], flag_title=["", ""], flag_points=["100", "100"],
                  flag_value=["", ""], flag_description=["", ""])
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT victim_team_id, target_flag_id FROM flag_submissions ORDER BY id")], [(team_a, None), (team_a, None)])
        self.assertEqual(self.lookup("SELECT SUM(delta) FROM score_events WHERE team_id = ?", (team_b,))[0][0], 1200)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])

    def test_awdp_machine_level_flags_are_counted_per_target(self):
        """靶机级 Flag 的靶机：同一支队伍用不同靶机攻破同一对手，各台靶机各计一次，互不串台。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 多靶机攻防赛", mode="round")
        player, team_a = self.player(contest_id, "bluefox", "BlueFox")
        other, team_b = self.player(contest_id, "packetlab", "PacketLab")
        first = self.awdp_target(contest_id, title="取证靶机", env_image="nginx:alpine", points=100)
        second = self.awdp_target(contest_id, title="渗透靶机", env_image="nginx:alpine", points=100)

        def derived(target_id, team_id):
            env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_id))[0]
            return env_flag_value(env["secret"], env["id"], target_id)

        # 先在取证靶机上攻破 A 队
        result = self.post(other, f"/awdp/targets/{first}/flag", flag=derived(first, team_a), follow=True, code=200)
        self.assertIn("100 攻击积分", result.get_data(as_text=True))
        # 再用渗透靶机攻破同一支 A 队：照常计分（不能被取证靶机的记录误判为已攻破）
        result = self.post(other, f"/awdp/targets/{second}/flag", flag=derived(second, team_a), follow=True, code=200)
        self.assertIn("100 攻击积分", result.get_data(as_text=True))
        repeat = self.post(other, f"/awdp/targets/{second}/flag", flag=derived(second, team_a), follow=True, code=200)
        self.assertIn("该靶机已被本队攻破", repeat.get_data(as_text=True))
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT puzzle_id, victim_team_id, verdict, points FROM flag_submissions WHERE team_id = ? ORDER BY id", (team_b,))],
            [(first, team_a, "correct", 100), (second, team_a, "correct", 100), (second, team_a, "already_solved", 0)])
        self.assertEqual(self.lookup("SELECT SUM(delta) FROM score_events WHERE team_id = ?", (team_b,))[0][0], 200)
        for target_id in (first, second):
            self.assertIn("已攻破 1/1 队", other.get(f"/awdp/targets/{target_id}").get_data(as_text=True))

    def test_awdp_flag_point_changes_restart_running_targets(self):
        """开赛后登记靶点：保存即为全部队伍重启该靶机，每个靶点各注入一枚 Flag；只改分值、名称不重启。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 赛中登记靶点", mode="round")
        player, team_a = self.player(contest_id, "bluefox", "BlueFox")
        other, team_b = self.player(contest_id, "packetlab", "PacketLab")
        target_id = self.awdp_target(contest_id, env_image="nginx:alpine", points=100)
        form = {"title": "镜像靶场", "category": "Web", "difficulty": "进阶", "description": "AWDP 攻防靶机",
                "kind": "round", "published": "1", "env_image": "nginx:alpine", "env_container_port": "80"}

        def latest(team):
            return [run for run in fake.state["runs"] if run["name"] == f"ctfenv-c{contest_id}-t{team}-p{target_id}"][-1]

        def environment(team):
            return self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team))[0]

        self.assertEqual(len(fake.state["runs"]), 2)  # 开赛时每队一台
        # 登记靶点之前，B 队已凭靶机级 Flag 攻破过 A 队（100 分）
        before = environment(team_a)
        result = self.post(other, f"/awdp/targets/{target_id}/flag",
                           flag=env_flag_value(before["secret"], before["id"], target_id), follow=True, code=200)
        self.assertIn("已攻破 1/1 队", result.get_data(as_text=True))
        # 赛中登记三个靶点（每靶点 100 分）：保存后两队的靶机都已重建，按靶点顺序各注入一枚 Flag
        saved = self.post(self.admin, f"/manage/bank/{target_id}/edit", follow=True, code=200, **form, points=300,
                          flag_id=["", "", ""], flag_title=["WebShell 落地", "备份归档", "日志取证"],
                          flag_points=["100", "100", "100"], flag_value=["", "", ""], flag_description=["", "", ""])
        self.assertIn("正在为全部队伍重启该靶机", saved.get_data(as_text=True))
        self.assertEqual(len(fake.state["runs"]), 4)
        flags = self.lookup("SELECT id FROM target_flags WHERE puzzle_id = ? ORDER BY id", (target_id,))
        values = {}
        for team in (team_a, team_b):
            env, run = environment(team), latest(team)
            self.assertEqual(run["ports"], {"80/tcp": env["host_port"]})
            self.assertEqual(list(run["environment"]), ["FLAG", "FLAG_1", "FLAG_2", "FLAG_3"])
            values[team] = [env_flag_value(env["secret"], env["id"], flag["id"], kind="target") for flag in flags]
            self.assertEqual(run["environment"]["FLAG_3"], values[team][2])
        page = f"/awdp/targets/{target_id}"
        # 旧的靶机级记录不计入靶点进度：三个靶点都还没拿到
        self.assertIn("未攻破", other.get(page).get_data(as_text=True))
        # 重启前的单枚靶机级 Flag 不再有效；其他队伍提交这台靶机的三个靶点 Flag 各得 100 分
        rival_env = environment(team_a)
        stale = self.post(other, f"{page}/flag", flag=env_flag_value(rival_env["secret"], rival_env["id"], target_id),
                          follow=True, code=200)
        self.assertIn("Flag 错误", stale.get_data(as_text=True))
        for value in values[team_a][:2]:
            result = self.post(other, f"{page}/flag", flag=value, follow=True, code=200)
            self.assertIn("100 攻击积分", result.get_data(as_text=True))
        # 两个靶点加一条旧记录不等于攻破：还差一个靶点
        self.assertIn("已获取 2/3 枚", other.get(page).get_data(as_text=True))
        result = self.post(other, f"{page}/flag", flag=values[team_a][2], follow=True, code=200)
        self.assertIn("100 攻击积分", result.get_data(as_text=True))
        self.assertEqual(self.lookup("SELECT SUM(delta) FROM score_events WHERE team_id = ?", (team_b,))[0][0], 400)
        self.assertIn("已攻破 1/1 队", other.get(page).get_data(as_text=True))

        # 只改靶点的分值和名称：Flag 布局没变，靶机不重启
        self.post(self.admin, f"/manage/bank/{target_id}/edit", **form, points=300,
                  flag_id=[str(flag["id"]) for flag in flags], flag_title=["WebShell 落地·改", "备份归档", "日志取证"],
                  flag_points=["150", "100", "50"], flag_value=["", "", ""], flag_description=["", "", ""])
        self.assertEqual(len(fake.state["runs"]), 4)
        # 删掉中间的靶点：靶机重启、Flag 重新注入；该靶点的攻击记录随之删除，已获积分保留
        self.post(self.admin, f"/manage/bank/{target_id}/edit", **form, points=200,
                  flag_id=[str(flags[0]["id"]), str(flags[2]["id"])], flag_title=["WebShell 落地·改", "日志取证"],
                  flag_points=["150", "50"], flag_value=["", ""], flag_description=["", ""])
        self.assertEqual(len(fake.state["runs"]), 6)
        self.assertEqual(list(latest(team_a)["environment"]), ["FLAG", "FLAG_1", "FLAG_2"])
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT target_flag_id, verdict FROM flag_submissions WHERE team_id = ? AND verdict = 'correct' ORDER BY id", (team_b,))],
            [(None, "correct"), (flags[0]["id"], "correct"), (flags[2]["id"], "correct")])
        self.assertEqual(self.lookup("SELECT SUM(delta) FROM score_events WHERE team_id = ?", (team_b,))[0][0], 400)

    def test_awdp_target_flag_validation_and_editing(self):
        contest_id = self.contest("蓝域杯 · 靶点校验赛", mode="round")
        player, _ = self.player(contest_id, "bluefox", "BlueFox")
        other, _ = self.player(contest_id, "packetlab", "PacketLab")
        base = {"title": "靶点靶机", "category": "Web", "difficulty": "进阶", "description": "靶点校验", "kind": "round",
                "published": "1", "join_contest": contest_id}
        line = {"flag_id": [""], "flag_title": ["靶点甲"], "flag_points": ["100"], "flag_value": [""], "flag_description": [""]}
        # 动态靶机的靶点 Flag 须留空，由平台按队伍生成
        self.post(self.admin, "/manage/bank/new", **base, points=300, env_image="nginx:alpine", env_container_port="80",
                  **{**line, "flag_value": ["flag{a}"]}, code=400)
        # 静态靶机的靶点必须填写 Flag
        self.post(self.admin, "/manage/bank/new", **base, points=300, **line, code=400)
        # 布置靶点后靶机级 Flag 不再生效，须留空
        self.post(self.admin, "/manage/bank/new", **base, points=300, flag="flag{top}",
                  **{**line, "flag_value": ["flag{a}"]}, code=400)
        # 只有 Flag 没有名称的半填行
        self.post(self.admin, "/manage/bank/new", **base, points=300,
                  **{**line, "flag_title": [""], "flag_value": ["flag{x}"]}, code=400)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM target_flags")[0][0], 0)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM puzzles WHERE title = '靶点靶机'")[0][0], 0)
        # 靶点整行留空：不布置靶点，沿用靶机级 Flag
        self.post(self.admin, "/manage/bank/new", **{**base, "title": "无靶点靶机"}, points=120, flag="flag{plain}",
                  **{**line, "flag_title": [""]})
        plain_id = self.lookup("SELECT id FROM puzzles WHERE title = '无靶点靶机'")[0][0]
        self.post(player, f"/awdp/targets/{plain_id}/flag", flag="flag{plain}", follow=True, code=200)
        self.assertEqual([tuple(row) for row in self.lookup("SELECT puzzle_id, target_flag_id, verdict, points FROM flag_submissions")],
                         [(plain_id, None, "correct", 120)])

        # 静态靶机布置两个静态靶点
        self.post(self.admin, "/manage/bank/new", **base, points=300,
                  flag_id=["", ""], flag_title=["靶点甲", "靶点乙"], flag_points=["100", "200"],
                  flag_value=["flag{a}", "flag{b}"], flag_description=["提示甲", ""])
        target_id = self.lookup("SELECT id FROM puzzles WHERE title = '靶点靶机'")[0][0]
        saved = self.lookup(
            "SELECT id, title, points, description, flag_digest FROM target_flags WHERE puzzle_id = ? ORDER BY id", (target_id,))
        self.assertEqual([(row["title"], row["points"], row["description"]) for row in saved],
                         [("靶点甲", 100, "提示甲"), ("靶点乙", 200, "")])
        self.assertTrue(all(row["flag_digest"] for row in saved))
        # 编辑：改名、改分值且 Flag 留空保留原值；未提交的行被删除
        form = {key: base[key] for key in ("title", "category", "difficulty", "description", "kind", "published")}
        self.post(self.admin, f"/manage/bank/{target_id}/edit", **form, points=300, flag_id=[str(saved[0]["id"])],
                  flag_title=["靶点甲·改"], flag_points=["150"], flag_value=[""], flag_description=["提示甲"])
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT id, title, points, flag_digest FROM target_flags WHERE puzzle_id = ?", (target_id,))],
            [(saved[0]["id"], "靶点甲·改", 150, saved[0]["flag_digest"])])
        detail = player.get(f"/awdp/targets/{target_id}").get_data(as_text=True)
        self.assertIn("靶点甲·改", detail)
        self.assertNotIn("靶点乙", detail)
        self.post(player, f"/awdp/targets/{target_id}/flag", flag="flag{a}", follow=True, code=200)
        self.post(player, f"/awdp/targets/{target_id}/flag", flag="flag{b}", follow=True, code=200)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT target_flag_id, verdict, points FROM flag_submissions WHERE puzzle_id = ? ORDER BY id", (target_id,))],
            [(saved[0]["id"], "correct", 150), (None, "incorrect", 0)])
        # 静态靶机去掉全部靶点时必须给出靶机级 Flag；给出后回到靶机级单枚 Flag
        # （已删除靶点的攻击记录随之删除，已获积分保留在计分流水）
        blank = {**line, "flag_title": [""]}
        self.post(self.admin, f"/manage/bank/{target_id}/edit", **form, points=300, **blank, code=400)
        self.post(self.admin, f"/manage/bank/{target_id}/edit", **form, points=300, flag="flag{top}", **blank)
        self.assertEqual(self.lookup("SELECT COUNT(*) FROM target_flags WHERE puzzle_id = ?", (target_id,))[0][0], 0)
        self.post(other, f"/awdp/targets/{target_id}/flag", flag="flag{top}", follow=True, code=200)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT target_flag_id, verdict, points FROM flag_submissions WHERE puzzle_id = ? ORDER BY id", (target_id,))],
            [(None, "incorrect", 0), (None, "correct", 300)])
        self.assertEqual([row[0] for row in self.lookup("SELECT delta FROM score_events ORDER BY id")], [120, 150, 300])

    def test_dynamic_flag_with_non_ascii_characters_is_judged_not_crashing(self):
        """三种模式的动态环境提交含非 ASCII 字符的 Flag：判错并留痕，不报服务器错误。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        exotic = "旗{测试-ünïcödé}"
        puzzle_contest = self.contest("蓝域杯 · 解题动态赛")
        player, _ = self.player(puzzle_contest, "puzzlefox", "PuzzleFox")
        self.post(self.admin, "/manage/bank/new", join_contest=puzzle_contest, title="容器入口", category="Web",
                  difficulty="入门", points=100, description="读取容器内 FLAG", env_image="nginx:alpine",
                  env_container_port="80", published="1")
        puzzle_id = self.lookup("SELECT id FROM puzzles WHERE title = '容器入口'")[0][0]
        self.post(player, f"/challenges/{puzzle_id}/env/start")
        result = self.post(player, f"/challenges/{puzzle_id}/flag", flag=exotic, follow=True, code=200)
        self.assertIn("Flag 错误", result.get_data(as_text=True))
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT verdict FROM flag_submissions WHERE puzzle_id = ?", (puzzle_id,))], [("incorrect",)])

        breach_contest = self.contest("蓝域杯 · 渗透动态赛", mode="breach", env_image="iotmanage-dmz:1.0",
                                      env_container_port="80")
        intruder, _ = self.player(breach_contest, "breachfox", "BreachFox")
        self.post(self.admin, "/manage/challenges/breach/new", contest_id=breach_contest, layer=["边界突破"],
                  title=["F1 备份泄露"], points=["100"], flag=[""], description=[""], published="1")
        self.post(intruder, "/breach/env/start")
        result = self.post(intruder, "/breach/flag", flag=exotic, follow=True, code=200)
        self.assertIn("Flag 错误", result.get_data(as_text=True))
        self.assertEqual([tuple(row) for row in self.lookup("SELECT verdict FROM breach_submissions")], [("incorrect",)])

        round_contest = self.contest("蓝域杯 · 靶机动态赛", mode="round")
        attacker, _ = self.player(round_contest, "roundfox", "RoundFox")
        target_id = self.awdp_target(round_contest, env_image="nginx:alpine", points=200)
        result = self.post(attacker, f"/awdp/targets/{target_id}/flag", flag=exotic, follow=True, code=200)
        self.assertIn("Flag 错误", result.get_data(as_text=True))
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT verdict FROM flag_submissions WHERE puzzle_id = ?", (target_id,))], [("incorrect",)])

    def test_upgrade_from_v10_adds_target_flags_without_losing_submissions(self):
        contest_id = self.contest()
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        puzzle_id = self.puzzle(contest_id)
        self.post(player, f"/challenges/{puzzle_id}/flag", flag="flag{fixture}", follow=True, code=200)
        # 把库还原成 v10 的结构：没有 target_flags 表，flag_submissions 没有 target_flag_id 列
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript(
                """BEGIN IMMEDIATE;
                   DROP INDEX one_target_flag_award;
                   CREATE TABLE flag_submissions_v10 (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, contest_id INTEGER NOT NULL, puzzle_id INTEGER NOT NULL,
                       team_id INTEGER NOT NULL, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                       verdict TEXT NOT NULL CHECK (verdict IN ('correct', 'incorrect', 'already_solved')),
                       points INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0), submitted_at INTEGER NOT NULL,
                       FOREIGN KEY (puzzle_id) REFERENCES puzzles(id) ON DELETE CASCADE,
                       FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE);
                   INSERT INTO flag_submissions_v10
                       SELECT id, contest_id, puzzle_id, team_id, user_id, verdict, points, submitted_at FROM flag_submissions;
                   DROP TABLE flag_submissions;
                   ALTER TABLE flag_submissions_v10 RENAME TO flag_submissions;
                   DROP TABLE target_flags;
                   PRAGMA user_version = 10;
                   COMMIT;"""
            )
            db.execute("PRAGMA foreign_keys = ON")
        upgraded = create_app({**self.settings, "ADMIN_PASSWORD": ""})
        admin = self.login("arenaops", upgraded)
        self.assertEqual(self.lookup("PRAGMA user_version")[0][0], 13)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])
        self.assertIn("target_flag_id", [row["name"] for row in self.lookup("PRAGMA table_info(flag_submissions)")])
        self.assertEqual([row["name"] for row in self.lookup(
            "SELECT name FROM sqlite_master WHERE name IN ('target_flags', 'one_target_flag_award') ORDER BY name")],
            ["one_target_flag_award", "target_flags"])
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT puzzle_id, team_id, verdict, points, target_flag_id FROM flag_submissions")],
            [(puzzle_id, team_id, "correct", 200, None)])
        # 升级后题库表单可布置靶点，原有提交记录照常显示
        self.assertIn("添加靶点", admin.get("/manage/bank/new?kind=round").get_data(as_text=True))
        self.assertIn("镜像回廊", self.login("bluefox", upgraded).get("/submissions").get_data(as_text=True))

    def test_upgrade_from_v11_counts_attacks_per_rival_team(self):
        """v11 库（攻击不记录取自哪支队伍）升级：记录保留，唯一索引改为按被攻破的队伍计次。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 升级验证赛", mode="round")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        other, other_id = self.player(contest_id, "packetlab", "PacketLab")
        third, third_id = self.player(contest_id, "nightowl", "NightOwl")
        target_id = self.awdp_target(contest_id, env_image="nginx:alpine", points=200)
        env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team_id))[0]
        self.post(other, f"/awdp/targets/{target_id}/flag", flag=env_flag_value(env["secret"], env["id"], target_id))
        # 把库还原成 v11 的结构：提交记录没有 victim_team_id 列，唯一索引不区分被攻破的队伍
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript(
                """BEGIN IMMEDIATE;
                   CREATE TABLE flag_submissions_v11 (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, contest_id INTEGER NOT NULL, puzzle_id INTEGER NOT NULL,
                       target_flag_id INTEGER REFERENCES target_flags(id) ON DELETE SET NULL, team_id INTEGER NOT NULL,
                       user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                       verdict TEXT NOT NULL CHECK (verdict IN ('correct', 'incorrect', 'already_solved')),
                       points INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0), submitted_at INTEGER NOT NULL,
                       FOREIGN KEY (puzzle_id) REFERENCES puzzles(id) ON DELETE CASCADE,
                       FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE);
                   INSERT INTO flag_submissions_v11
                       SELECT id, contest_id, puzzle_id, target_flag_id, team_id, user_id, verdict, points, submitted_at
                       FROM flag_submissions;
                   DROP TABLE flag_submissions;
                   ALTER TABLE flag_submissions_v11 RENAME TO flag_submissions;
                   CREATE UNIQUE INDEX one_target_flag_award
                       ON flag_submissions(contest_id, target_flag_id, team_id)
                       WHERE verdict = 'correct' AND target_flag_id IS NOT NULL;
                   PRAGMA user_version = 11;
                   COMMIT;"""
            )
            db.execute("PRAGMA foreign_keys = ON")
        upgraded = create_app({**self.settings, "ADMIN_PASSWORD": ""})
        self.login("arenaops", upgraded)
        self.assertEqual(self.lookup("PRAGMA user_version")[0][0], 13)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])
        self.assertIn("victim_team_id", [row["name"] for row in self.lookup("PRAGMA table_info(flag_submissions)")])
        self.assertEqual([row["name"] for row in self.lookup(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'flag_submissions' AND type = 'index' ORDER BY name")],
            ["flags_contest", "flags_team", "flags_team_recent", "one_target_flag_award", "one_target_machine_award"])
        self.assertIn("COALESCE(victim_team_id, 0)", self.lookup(
            "SELECT sql FROM sqlite_master WHERE name = 'one_target_flag_award'")[0][0])
        self.assertEqual([tuple(row) for row in self.lookup("SELECT team_id, victim_team_id, verdict, points FROM flag_submissions")],
                         [(other_id, None, "correct", 200)])

        # 升级后的攻击按被攻破的队伍计次：A 队攻破 B、C 两支队伍的靶机各得一次分
        def derived(team):
            row = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, team))[0]
            return env_flag_value(row["secret"], row["id"], target_id)

        page = f"/awdp/targets/{target_id}"
        self.assertIn("已攻破 1/2 队", self.post(player, f"{page}/flag", flag=derived(other_id), follow=True, code=200).get_data(as_text=True))
        final = self.post(player, f"{page}/flag", flag=derived(third_id), follow=True, code=200).get_data(as_text=True)
        self.assertIn("已攻破 2/2 队", final)
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT team_id, victim_team_id, verdict, points FROM flag_submissions ORDER BY id")],
            [(other_id, None, "correct", 200), (team_id, other_id, "correct", 200), (team_id, third_id, "correct", 200)])

    def test_upgrade_from_v12_guards_machine_level_awards(self):
        """v12 库升级：补上靶机级攻击计次的唯一索引，已有记录保留。"""
        fake = FakeDocker()
        self.app.config["RANGE_DOCKER_FACTORY"] = lambda: fake
        contest_id = self.contest("蓝域杯 · 升级计次赛", mode="round")
        player, team_id = self.player(contest_id, "bluefox", "BlueFox")
        other, other_id = self.player(contest_id, "packetlab", "PacketLab")
        target_id = self.awdp_target(contest_id, env_image="nginx:alpine", points=100)
        env = self.lookup("SELECT * FROM breach_environments WHERE puzzle_id = ? AND team_id = ?", (target_id, other_id))[0]
        self.post(player, f"/awdp/targets/{target_id}/flag", flag=env_flag_value(env["secret"], env["id"], target_id))
        # 把库还原成 v12 的结构：没有靶机级计次的唯一索引
        with closing(connect(self.app.config["DATABASE"])) as db:
            db.execute("DROP INDEX one_target_machine_award")
            db.execute("PRAGMA user_version = 12")
        self.login("arenaops", create_app({**self.settings, "ADMIN_PASSWORD": ""}))
        self.assertEqual(self.lookup("PRAGMA user_version")[0][0], 13)
        self.assertEqual(self.lookup("PRAGMA foreign_key_check"), [])
        self.assertIn("one_target_machine_award", [row["name"] for row in self.lookup(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'flag_submissions' AND type = 'index'")])
        self.assertEqual([tuple(row) for row in self.lookup(
            "SELECT puzzle_id, team_id, victim_team_id, target_flag_id, verdict, points FROM flag_submissions")],
            [(target_id, team_id, other_id, None, "correct", 100)])
        # 同一支队伍对同一对手同一台靶机的第二条靶机级正确记录被唯一索引拒绝
        with closing(connect(self.app.config["DATABASE"])) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO flag_submissions (contest_id, puzzle_id, target_flag_id, team_id, victim_team_id,
                                                    verdict, points, submitted_at)
                       VALUES (?, ?, NULL, ?, ?, 'correct', 0, 0)""", (contest_id, target_id, team_id, other_id))

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
        self.assertEqual(self.lookup("PRAGMA user_version")[0][0], 13)
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
