# 蓝域 CTF

蓝白配色的 PC 端 CTF 平台。使用统一账号入口，登录后按账号权限进入比赛首页或赛事工作台。

- 解题支持分类题库、题目详情、多附件下载、Flag 判定弹窗、队伍积分、提交记录和排行榜。
- AWDP 支持赛题、ZIP 补丁与加固说明、评测记录、队伍服务状态、回合进度和计分流水。
- 两块大屏各有独立入口。解题大屏展示领奖台与解出动态，回合大屏展示回合计分、服务状态与得分流水。前三名显示金、银、铜奖杯，大屏支持自动刷新、暂停和浏览器全屏。
- 平台名称、图标、Logo、颜色和圆角可在「品牌与外观」修改。当前比赛的背景图、标题、简介和赛程由「比赛配置」管理。登录页和比赛首页读取当前开启的比赛。

## 比赛入口与筹备

- 解题赛入口为 `/puzzle`，提供题库、Flag 提交、排行榜和解题计分板。
- AWDP 入口为 `/awdp`，提供赛场、补丁提交、队伍服务状态和回合计分板。

两个入口共用账号。通过统一登录进入时，参赛账号会跳转到当前开启的比赛，赛事账号进入工作台。直接访问某个比赛入口时，只有同类型的比赛开启才显示该场比赛。

在「比赛配置」创建比赛时选择解题赛或 AWDP，并设置赛程与封面。AWDP 需要设置每回合时长，解题赛按开赛和结束时间运行。保存后会直接进入题库。已创建的比赛可在比赛列表点「管理赛题」，提前创建、编辑赛题、上传解题附件或设置开放状态。创建参赛账号和队伍后，在队伍管理中分队。准备完成后选择「开启比赛」。同一时间只开启一场，开启另一场会撤下原来的比赛。赛题和附件在比赛开启且到达开赛时间后开放。

比赛、队伍和账号列表均提供删除入口，确认页会列出删除范围。删除比赛会清理该比赛的队伍、赛题、提交、积分和关联文件。删除队伍会解除分队并清理该队伍的提交、补丁包、服务状态和积分。删除账号会解除所有分队，已有提交继续计入原队伍成绩。系统至少保留一个启用的赛事管理账号。

已有数据库会自动升级。缺少比赛模式的旧数据库会按现有题库与积分识别比赛。只有回合赛题或回合积分的历史比赛归为 AWDP，其余归为解题赛。混合类型的历史比赛撤下后可在「编辑比赛」修改模式并查看对应题库与记录。

## 阿里云 ECS 一键部署

使用带公网 IPv4 或弹性公网 IP（EIP）的 Linux ECS。实例能访问 GitHub 和镜像源后，在服务器上运行下面的命令。私有仓库可使用已授权的 SSH 克隆地址。

```bash
git clone https://github.com/gengarbypass/ctf-platform-ssx.git
cd ctf-platform-ssx
sudo bash deploy.sh 8000
```

脚本会构建并启动平台，将所选 TCP 端口发布到 `0.0.0.0`，并检查健康接口。首次运行会创建 `.env`，其中保存初始账号 `arenaops` 和随机密码。已有 `.env` 和数据库继续使用。Ubuntu、Debian 和 Alibaba Cloud Linux 3 缺少 Docker 组件时，脚本会从软件源安装。其他 Linux 系统可预装 Docker Engine、Buildx 和 Compose 插件后运行。[Alibaba Cloud Linux 3 安装 Docker](https://help.aliyun.com/zh/document_detail/2842585.html) · [Docker Compose 插件安装](https://docs.docker.com/compose/install/linux/)

在 **ECS 控制台 → 实例关联的安全组 → 入方向** 添加允许规则，协议选自定义 TCP，目的端口填 `8000`。授权对象填需要访问的公网 IP 段。若要向所有 IPv4 来源公开，填 `0.0.0.0/0`。实例启用系统防火墙时，也按同一访问范围放行所选端口。然后从实例外访问 `http://ECS公网IPv4:8000`。此入口使用 HTTP。[阿里云安全组说明](https://help.aliyun.com/zh/ecs/user-guide/start-using-security-groups)

需要换端口时，运行 `sudo bash deploy.sh 8080`，并同步调整安全组。脚本将部署端口写入 `.env.ecs`。更新时在同一目录拉取新版，再运行 `sudo bash deploy.sh`，沿用上次端口和数据卷。脚本可从 ECS 元数据识别公网 IPv4，用于显示访问地址。

如果实例所在地域拉取基础镜像有困难，在 `.env` 将 `CTF_PYTHON_IMAGE` 设置为可达的官方 Python 基础镜像副本，例如同步到本地域 ACR 的镜像。Python 依赖可用 `CTF_PIP_INDEX_URL` 指定镜像源。修改后重新运行部署脚本。[阿里云镜像拉取说明](https://help.aliyun.com/zh/acr/user-guide/accelerate-the-pulls-of-docker-official-images)

## 手动 Docker 部署

1. 把项目源码放到服务器，在项目根目录运行 `cp .env.example .env`。
2. 编辑 `.env`，为 `CTF_ADMIN_PASSWORD` 设置至少 12 个字符的随机密码。
3. 执行 `docker compose up -d --build`，然后用 `docker compose ps` 查看运行和健康状态。
4. 打开 `http://服务器地址:8000`，使用环境变量中配置的账号和密码登录。

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
| `CTF_PYTHON_IMAGE` | `python:3.13-slim-bookworm` | 构建使用的 Python 基础镜像 |
| `CTF_PIP_INDEX_URL` | `https://pypi.org/simple` | 构建使用的 Python 软件源 |

## 计分与提交

Flag 按题目分值一次性给本队计分。重复判定和未通过判定保留记录。已获得的积分保留原分值。

补丁包作为评测材料保存。赛事人员可在「提交管理」更新评测状态和通过分值，在「队伍与服务」录入服务状态及攻防、加固、可用性的加减分。复核补丁时按分值差额写入流水，重复保存同一分值不会重复计分。排行榜按总分降序，同分按队名排序。

## 数据与维护

SQLite 数据库、比赛配置、账号、积分、提交记录和上传文件都保存在 Compose 命名卷 `data` 的 `/data/arena.sqlite3` 中。采用单应用实例部署，容器内可以运行多个 Web 进程。一键部署的实例用 `sudo bash deploy.sh` 更新，手动部署的实例用 `docker compose up -d --build` 更新。两种方式均使用同一项目的数据卷。

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
