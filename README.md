# 蓝域 CTF

蓝白配色的 PC 端 CTF 平台。通过 `/login` 统一登录，按账号权限进入比赛首页或赛事工作台。

- 解题支持分类赛题、题目详情、多附件下载、Flag 判定弹窗、队伍积分、提交记录和排行榜。赛题可选配置环境镜像开启动态容器：队伍在题目页启动本题专属容器（独立端口、按队生成 Flag 注入），比赛配置中的环境时长与并发上限同样生效。
- AWDP 支持赛题、ZIP 补丁与加固说明、评测记录、队伍服务状态、回合进度和计分流水。可在「题库」创建 **AWDP 靶机**，配置分类、分值、Flag、环境镜像与连通性检测。开赛后平台自动为各队启动所有已发布靶机，中途新增靶机或队伍也会自动补启。每个靶点有独立的 Flag 与分值。**动态 Flag 每回合自动更新**，平台沿用实例的原地址重建容器并注入本回合 Flag。容器内的临时改动随重建清空。上一回合的 Flag 在切换时失效。每支队伍对同一实例的同一靶点每回合计分一次，累计积分保留。本回合集齐某个实例的全部 Flag 后，该实例显示「被攻破」。选手可提交本队靶机的修复文档（ZIP），经评审后获得加固积分。
- 综合渗透支持把整场比赛作为一个综合靶场：靶点按层级分组、每个靶点单独设置分值，选手在靶场内逐层突破，提交任意 Flag 由平台自动匹配靶点并计入该靶点分值，每枚 Flag 每队只计一次分。可选开启动态环境：每支队伍在靶场页点击「启动环境」，平台用同一镜像为该队创建独立容器、分配专属端口（8400 起），并为该队动态生成各靶点 Flag 注入容器；环境支持停止、重启（每次重启分配全新端口，历史端口不复用，Flag 保持不变）、超时自动回收和并发上限。
- 三块大屏各有独立入口，采用统一布局：左侧约 2/3 为队伍排名，前三名带金、银、铜奖杯与行首色条；右侧约 1/3 上半为选手得分排行、下半为解出/突破/回合得分动态。AWDP 大屏在底部整行追加服务状态矩阵。大屏自动刷新并展示分数和排名变化。
- 登录页使用塔里木大学网络安全学院的背景图，分两行显示「筑牢边疆数字防线」「锻造基层网安尖兵」。选手登录后可在比赛首页查看当前比赛的标题、简介和赛程。管理员可在「品牌与外观」设置平台名称、图标、Logo、颜色、圆角和首页背景图。

在「品牌与外观」选择 PNG、JPG 或 WebP 图片，可实时预览首页背景。点击「应用平台外观」后，选手登录后的比赛首页优先显示这张图。勾选「移除首页背景图」并保存后，首页恢复使用当前比赛封面。各赛场页继续使用对应的比赛封面。

## 比赛入口与筹备

- 解题赛入口为 `/puzzle`，提供赛题、Flag 提交、排行榜和解题计分板。
- AWDP 入口为 `/awdp`，提供赛场、补丁提交、队伍服务状态和回合计分板。
- 综合渗透入口为 `/breach`，提供综合靶场靶点总览、Flag 提交、排行榜和渗透计分板。

参赛账号登录后从左侧导航选择「解题赛」「AWDP」或「综合渗透」，赛事账号登录后进入工作台。只有同类型的比赛开启时，对应入口才显示该场比赛。

在「比赛配置」创建比赛时选择解题赛、AWDP 或综合渗透，仅需填写名称、简介、赛程与封面；AWDP 额外设置每回合时长。账号与队伍可在「批量导入」页按 CSV 模板一次性导入（队伍导入会自动完成分队）。**队伍为全局名册，跨比赛复用**：新建比赛会自动沿用上一场比赛的队伍与选手分队（可在「队伍管理」按比赛加入/移出或调整分队），无需重复配置。

赛题在「题库」独立维护，可先出题后建赛：创建赛题时填写环境镜像即可开启动态容器，平台会**启动一次性容器自动检测镜像与端口连通性**（检测失败将拒绝保存并给出原因）。题库同时支持创建 **AWDP 靶机**（与解题赛题同一套表单与校验策略），仅解题赛题可加入解题赛、仅 AWDP 靶机可加入 AWDP 比赛。比赛在筹备阶段与开赛以后都可以通过比赛赛题页「从题库添加」或「创建新赛题」把赛题加入比赛，也可随时移出（题库保留、历史成绩仍计）。综合渗透比赛在编辑页配置整场靶场镜像，靶点批量创建；解题赛每道赛题单独配置镜像；AWDP 靶机每题单独配置镜像，并可在同一表单布置靶点。账号管理支持勾选**批量删除**（当前登录账号除外，需二次确认）。

动态环境下每支队伍每个赛题限一个专属容器（点击「启动环境」自动分配独立端口，8400 起），Flag 按队伍生成注入：解题赛赛题以环境变量 `FLAG` 注入；AWDP 靶机未布置靶点时注入 `FLAG`（另附同值的 `FLAG_<靶机ID>`），布置了靶点时按靶点顺序注入 `FLAG_1`…`FLAG_N`（另附与 `FLAG_1` 同值的 `FLAG`，兼容只读 `FLAG` 的镜像）；综合渗透靶点以 `FLAG_<靶点ID>` 注入。镜像需按约定读取，见 `docs/赛题构建模板.md`；环境按时长自动回收并受并发上限约束。未配镜像则维持静态模式（手工设置 Flag）。

比赛、队伍和账号列表的「操作」列分别提供「删除比赛」「删除队伍」「删除账号」，确认页会列出删除范围。删除比赛会清理该比赛的队伍、赛题、提交、积分和关联文件。在「队伍名册」可勾选多支队伍后点击「批量删除」，在确认页核对队名。确认后从全局名册和所有比赛中移除所选队伍，解除分队并清理其提交、补丁包、服务状态、积分和动态环境。删除账号会解除所有分队，已有提交继续计入原队伍成绩。最后一个启用的赛事管理账号会显示禁用的删除按钮。

已有数据库会自动升级。缺少比赛模式的旧数据库会按现有赛题与积分识别比赛。只有回合赛题或回合积分的历史比赛归为 AWDP，其余归为解题赛。混合类型的历史比赛撤下后可在「编辑比赛」修改模式并查看对应赛题与记录。

## 阿里云 ECS 一键部署

使用带公网 IPv4 或弹性公网 IP（EIP）的 Linux ECS。实例能访问 GitHub 和镜像源后，在服务器上运行下面的命令。私有仓库可使用已授权的 SSH 克隆地址。

```bash
git clone https://github.com/gengarbypass/ctf-platform-ssx.git
cd ctf-platform-ssx
sudo bash deploy.sh
```

脚本会构建并启动平台，将所选 TCP 端口发布到 `0.0.0.0`，并检查健康接口。首次运行会创建 `.env`，其中保存初始账号 `arenaops` 和随机密码。已有 `.env` 和数据库继续使用。Ubuntu、Debian 和 Alibaba Cloud Linux 3 缺少 Docker 组件时，脚本会从软件源安装。其他 Linux 系统可预装 Docker Engine、Buildx 和 Compose 插件后运行。[Alibaba Cloud Linux 3 安装 Docker](https://help.aliyun.com/zh/document_detail/2842585.html) · [Docker Compose 插件安装](https://docs.docker.com/compose/install/linux/)

在 **ECS 控制台 → 实例关联的安全组 → 入方向** 添加允许规则，协议选自定义 TCP，目的端口填 `80`。授权对象填需要访问的公网 IP 段。若要向所有 IPv4 来源公开，填 `0.0.0.0/0`。实例启用系统防火墙时，也按同一访问范围放行所选端口。然后从实例外访问 `http://ECS公网IPv4/login`。[阿里云安全组说明](https://help.aliyun.com/zh/ecs/user-guide/start-using-security-groups)

使用综合渗透动态环境时，平台容器会通过宿主 `/var/run/docker.sock` 创建各队环境容器（deploy.sh 自动探测 docker 组 GID 写入 `.env.ecs`），还需在安全组放行环境端口段（默认 `8400` 起，按队伍数放行如 `8400-8500`）。

现有实例如需切换到 `http://ECS公网IPv4/login`，在原项目目录拉取新版，运行 `sudo bash deploy.sh 80`，并放行 TCP `80`。如需使用其他空闲端口，例如 `8080`，运行 `sudo bash deploy.sh 8080` 并访问 `http://ECS公网IPv4:8080/login`。脚本将部署端口写入 `.env.ecs`。同一目录更新时，`sudo bash deploy.sh` 沿用上次端口和数据卷。

从 Git 仓库运行部署脚本时，脚本会核对并打印正在运行的提交。打开平台公网地址的 `/healthz`，其中 `revision` 应与服务器项目目录的 `git rev-parse HEAD` 一致。

如果实例所在地域拉取基础镜像有困难，在 `.env` 将 `CTF_PYTHON_IMAGE` 设置为可达的官方 Python 基础镜像副本，例如同步到本地域 ACR 的镜像。Python 依赖可用 `CTF_PIP_INDEX_URL` 指定镜像源。修改后重新运行部署脚本。[阿里云镜像拉取说明](https://help.aliyun.com/zh/acr/user-guide/accelerate-the-pulls-of-docker-official-images)

## 手动 Docker 部署

1. 把项目源码放到服务器，在项目根目录运行 `cp .env.example .env`。
2. 编辑 `.env`，为 `CTF_ADMIN_PASSWORD` 设置至少 12 个字符的随机密码。
3. 执行 `docker compose up -d --build`，然后用 `docker compose ps` 查看运行和健康状态。
4. 打开 `http://服务器地址/login`，使用环境变量中配置的账号和密码登录。

首个账号只在空数据库中创建。后续改密在「账号管理」完成。已创建账号后，`CTF_ADMIN_PASSWORD` 可以从运行配置移除。生产环境通过 HTTPS 反向代理访问时，将 `CTF_BIND_HOST` 设为 `127.0.0.1`、`CTF_COOKIE_SECURE` 设为 `1`，并在代理中允许与应用设置一致的请求体大小。

## 配置清单

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CTF_ADMIN_USERNAME` | `arenaops` | 空数据库的初始管理账号 |
| `CTF_ADMIN_PASSWORD` | 空 | 首次启动时必填 |
| `CTF_SECRET_KEY` | 自动生成 | 可选会话签名密钥，手动设置至少 32 个字符。更换后旧会话失效 |
| `CTF_TIME_ZONE` | `Asia/Shanghai` | IANA 赛程和记录时区 |
| `CTF_BIND_HOST` | `0.0.0.0` | Compose 宿主机绑定地址 |
| `CTF_PORT` | `80` | Compose 对外端口 |
| `CTF_COOKIE_SECURE` | `0` | HTTPS 部署设置为 `1` |
| `CTF_MAX_UPLOAD_MB` | `16` | 每次上传请求总大小，单位 MiB |
| `CTF_BOARD_POLL_SECONDS` | `4` | 大屏刷新间隔，单位秒 |
| `CTF_WEB_WORKERS` | `2` | Gunicorn 进程数 |
| `CTF_WEB_THREADS` | `4` | 每个 Gunicorn 进程的线程数 |
| `CTF_ENV_HOST` | 空 | 动态环境对选手展示的地址主机名；留空时按访问请求的主机推导 |
| `CTF_ENV_PORT_START` | `8400` | 动态环境端口池起点，安全组需放行后续范围 |
| `CTF_ENV_NETWORK_POOL` | `10.240.0.0/16` | 动态靶机的 IPv4 地址池，每台分配独立的 `/28`。应避开宿主机、VPC 和其他网络使用的网段 |
| `CTF_ENV_MEM_LIMIT` | `768m` | 每个动态环境容器的内存上限 |
| `CTF_ENV_CPU_QUOTA` | `150000` | 每个动态环境容器的 CPU 配额（100000 为 1 核） |
| `CTF_ENV_PIDS_LIMIT` | `320` | 每个动态环境容器的进程数上限 |
| `CTF_DOCKER_GID` | `999` | 宿主 docker 组 GID，deploy.sh 自动探测写入 `.env.ecs` |
| `CTF_DATA_DIR` | `data` | 本地运行数据目录。Compose 内为 `/data` |
| `CTF_PYTHON_IMAGE` | `python:3.13-slim-bookworm` | 构建使用的 Python 基础镜像 |
| `CTF_PIP_INDEX_URL` | `https://pypi.org/simple` | 构建使用的 Python 软件源 |

## 计分与提交

Flag 按题目分值一次性给本队计分。重复判定和未通过判定保留记录。已获得的积分保留原分值。综合渗透同理：选手提交任意 Flag，平台自动匹配本场开放的靶点，命中即按该靶点分值计分，每枚 Flag 每队只计一次，排行榜按积分降序、同分按已获 Flag 数。

补丁包作为评测材料保存。赛事人员可在「提交管理」更新评测状态和通过分值，在「队伍与服务」录入服务状态。复核补丁时按分值差额写入流水，重复保存同一分值不会重复计分。

AWDP 开赛后，赛事人员可在「队伍与服务」点击参赛队伍旁的「加减分」，选择计分项目和回合，填写调整分值及说明。正数加分，负数扣分。保存后更新排行榜，并将分值、说明和操作人记入计分流水。排行榜按总分降序，同分按队名排序。

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

本地首次运行前设置 `CTF_ADMIN_PASSWORD`。健康检查为 `GET /healthz`。三块大屏的地址分别是 `/scoreboard/puzzle`、`/scoreboard/round` 与 `/scoreboard/breach`，登录后可从各自的赛场或榜单进入。
