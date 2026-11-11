# 蓝域 CTF

蓝白配色的 PC 端 CTF 平台。使用统一账号入口，登录后按账号权限进入比赛首页或赛事工作台。

- 解题支持分类题库、题目详情、多附件下载、Flag 判定弹窗、队伍积分、提交记录和排行榜。
- AWDP 支持赛题、ZIP 补丁与加固说明、评测记录、队伍服务状态、回合进度和计分流水。
- 两块大屏各有独立入口。解题大屏展示领奖台与解出动态，回合大屏展示回合计分、服务状态与得分流水。前三名显示金、银、铜奖杯，大屏支持自动刷新、暂停和浏览器全屏。
- 平台名称、图标、Logo、颜色和圆角可在「品牌与外观」修改。当前比赛的背景图、标题、简介和赛程由「比赛配置」管理。登录页和比赛首页读取当前开启的比赛。

## Docker 部署

1. 把项目源码放到服务器，在项目根目录运行 `cp .env.example .env`。
2. 编辑 `.env`，为 `CTF_ADMIN_PASSWORD` 设置至少 12 个字符的随机密码。
3. 执行 `docker compose up -d --build`，然后用 `docker compose ps` 查看运行和健康状态。
4. 打开 `http://服务器地址:8000`，使用环境变量中配置的账号和密码登录。

首次启动后，在「比赛配置」创建赛事并设置起止时间、回合时长和封面。选择「管理内容」可以配置未开启的比赛。创建参赛账号和队伍，在「队伍与服务」完成分队，在「题目管理」创建并发布赛题。准备好后在比赛列表选择「开启比赛」。比赛开始时间决定何时开放提交。

首个账号只在空数据库中创建。后续改密在「账号管理」完成。已创建账号后，`CTF_ADMIN_PASSWORD` 可以从运行配置移除。生产环境通过 HTTPS 反向代理访问时，将 `CTF_BIND_HOST` 设为 `127.0.0.1`、`CTF_COOKIE_SECURE` 设为 `1`，并在代理中允许与应用设置一致的请求体大小。

## 配置清单

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CTF_ADMIN_USERNAME` | `arenaops` | 空数据库的初始管理账号 |
| `CTF_ADMIN_PASSWORD` | 空 | 首次启动时必填 |
| `CTF_SECRET_KEY` | 自动生成 | 可选会话签名密钥，手动设置至少 32 个字符。更换后旧会话失效 |
| `CTF_TIME_ZONE` | `Asia/Shanghai` | IANA 赛程和记录时区 |
| `CTF_BIND_HOST` | `0.0.0.0` | Compose 宿主机绑定地址 |
| `CTF_PORT` | `8000` | Compose 对外端口 |
| `CTF_COOKIE_SECURE` | `0` | HTTPS 部署设置为 `1` |
| `CTF_MAX_UPLOAD_MB` | `16` | 每次上传请求总大小，单位 MiB |
| `CTF_BOARD_POLL_SECONDS` | `4` | 大屏刷新间隔，单位秒 |
| `CTF_WEB_WORKERS` | `2` | Gunicorn 进程数 |
| `CTF_WEB_THREADS` | `4` | 每个 Gunicorn 进程的线程数 |
| `CTF_DATA_DIR` | `data` | 本地运行数据目录。Compose 内为 `/data` |

## 计分与提交

Flag 按题目分值一次性给本队计分。重复判定和未通过判定保留记录。已获得的积分保留原分值。

补丁包作为评测材料保存。赛事人员可在「提交管理」更新评测状态和通过分值，在「队伍与服务」录入服务状态及攻防、加固、可用性的加减分。复核补丁时按分值差额写入流水，重复保存同一分值不会重复计分。排行榜按总分降序，同分按队名排序。

## 数据与维护

SQLite 数据库、比赛配置、账号、积分、提交记录和上传文件都保存在 Compose 命名卷 `data` 的 `/data/arena.sqlite3` 中。采用单应用实例部署，容器内可以运行多个 Web 进程。升级时运行 `docker compose up -d --build`，并保留该数据卷。

运行中可使用 SQLite 的备份接口生成一致的数据库副本：

```sh
docker compose exec -T platform python -c "import sqlite3; src=sqlite3.connect('/data/arena.sqlite3'); dst=sqlite3.connect('/data/arena-backup.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker compose cp platform:/data/arena-backup.sqlite3 ./arena-backup.sqlite3
```

恢复时先停止平台，再把备份文件作为 `arena.sqlite3` 放入空的数据卷并启动。使用现有数据卷时，先备份原数据库及其 WAL/SHM 文件，再清理旧的伴随文件后替换数据库。备份包含账号数据和附件，应限制访问。

## 本地开发与验证

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python -m flask --app ctf_platform run --port 8000
```

本地首次运行前设置 `CTF_ADMIN_PASSWORD`。健康检查为 `GET /healthz`。两个大屏的地址分别是 `/scoreboard/puzzle` 与 `/scoreboard/round`，登录后可从各自的赛场或榜单进入。
