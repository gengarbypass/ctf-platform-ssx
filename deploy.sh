#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

normalize_port() {
    [[ $1 =~ ^[0-9]{1,5}$ ]] || return 1
    local port=$((10#$1))
    (( port >= 1 && port <= 65535 )) || return 1
    printf '%d\n' "$port"
}

if [[ $# -eq 1 && $1 == "--help" ]]; then
    printf '%s\n' "用法：sudo bash deploy.sh [公网端口]" "首次部署默认端口 8000，后续默认沿用已部署端口。"
    exit 0
fi
(( $# <= 1 )) || fail "用法：sudo bash deploy.sh [公网端口]"
port=""
if (( $# == 1 )); then
    port=$(normalize_port "$1") || fail "端口须为 1–65535 的整数。"
fi

cd -- "$(dirname -- "$0")"
root=$PWD
if command -v flock >/dev/null 2>&1; then
    exec 9>"$root/.deploy.lock"
    flock -n 9 || fail "已有部署正在运行。"
fi

install_docker() {
    (( EUID == 0 )) || fail "安装 Docker 需要管理员权限，请用 sudo bash deploy.sh 运行。"
    [[ -r /etc/os-release ]] || fail "请先安装 Docker Engine、Buildx 和 Compose 插件。"
    . /etc/os-release

    local mirror=https://mirrors.aliyun.com/docker-ce
    local packages=(docker-buildx-plugin docker-compose-plugin)
    if ! command -v docker >/dev/null 2>&1; then
        packages+=(docker-ce docker-ce-cli containerd.io)
    fi

    case "$ID" in
        ubuntu|debian)
            local codename=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
            [[ $codename =~ ^[a-z0-9][a-z0-9-]*$ ]] || fail "无法读取系统的软件源代号。"
            apt-get update
            apt-get install -y ca-certificates curl
            if ! apt-cache show docker-compose-plugin >/dev/null 2>&1; then
                install -d -m 0755 /etc/apt/keyrings
                curl -fsSL --retry 3 "$mirror/linux/$ID/gpg" -o /etc/apt/keyrings/ctf-docker.asc
                chmod 0644 /etc/apt/keyrings/ctf-docker.asc
                cat > /etc/apt/sources.list.d/ctf-docker.sources <<EOF
Types: deb
URIs: $mirror/linux/$ID
Suites: $codename
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/ctf-docker.asc
EOF
                chmod 0644 /etc/apt/sources.list.d/ctf-docker.sources
                apt-get update
            fi
            apt-get install -y "${packages[@]}"
            ;;
        alinux)
            [[ $VERSION_ID == 3 || $VERSION_ID == 3.* ]] || fail "请先为此系统安装 Docker Engine、Buildx 和 Compose 插件。"
            dnf install -y ca-certificates curl dnf-plugins-core
            dnf --enablerepo=alinux3-plus install -y dnf-plugin-releasever-adapter
            if [[ ! -f /etc/yum.repos.d/docker-ce.repo ]]; then
                dnf config-manager --add-repo="$mirror/linux/centos/docker-ce.repo"
            fi
            dnf install -y "${packages[@]}"
            ;;
        *)
            fail "请先为此系统安装 Docker Engine、Buildx 和 Compose 插件。"
            ;;
    esac
}

docker_ready() {
    command -v docker >/dev/null 2>&1 &&
        docker compose version >/dev/null 2>&1 &&
        docker buildx version >/dev/null 2>&1
}

if ! docker_ready; then
    install_docker
fi
docker_ready || fail "Docker Compose 或 Buildx 安装不完整。"
if ! docker info >/dev/null 2>&1; then
    if (( EUID == 0 )) && command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now docker
    fi
    docker info >/dev/null 2>&1 || fail "无法连接 Docker 服务，请确认服务已启动且当前账号有权限。"
fi

env_file="$root/.env"
ecs_file="$root/.env.ecs"
env_tmp=""
ecs_tmp=""
cleanup() {
    if [[ -n $env_tmp ]]; then rm -f -- "$env_tmp"; fi
    if [[ -n $ecs_tmp ]]; then rm -f -- "$ecs_tmp"; fi
}
trap cleanup EXIT

if [[ ! -e $env_file ]]; then
    env_tmp=$(mktemp "$root/.env.init.XXXXXX")
    password=$(od -An -N24 -tx1 /dev/urandom | tr -d '[:space:]')
    [[ $password =~ ^[a-f0-9]{48}$ ]] || fail "无法生成管理员随机密码。"
    awk '!/^CTF_ADMIN_PASSWORD=/' "$root/.env.example" > "$env_tmp"
    printf '\nCTF_ADMIN_PASSWORD=%s\n' "$password" >> "$env_tmp"
    unset password
    mv -- "$env_tmp" "$env_file"
    env_tmp=""
    printf '%s\n' "已生成 .env，首次初始化时使用其中的管理员账号和随机密码。"
fi
[[ -f $env_file ]] || fail ".env 须为普通配置文件。"

compose=(docker compose --env-file "$env_file" -f "$root/compose.yaml")
if [[ -z $port ]]; then
    previous=("${compose[@]}")
    if [[ -f $ecs_file ]]; then previous+=(--env-file "$ecs_file"); fi
    saved_port=$("${previous[@]}" config --environment | tr -d '\r' | awk '/^CTF_PORT=/{print substr($0, 10)}')
    port=$(normalize_port "${saved_port:-8000}") || fail "配置中的 CTF_PORT 无效，请用 deploy.sh 指定正确端口。"
fi

ecs_tmp=$(mktemp "$root/.env.ecs.XXXXXX")
printf 'CTF_BIND_HOST=0.0.0.0\nCTF_PORT=%s\nCTF_COOKIE_SECURE=0\n' "$port" > "$ecs_tmp"
compose+=(--env-file "$ecs_tmp")
export CTF_BIND_HOST=0.0.0.0 CTF_PORT="$port" CTF_COOKIE_SECURE=0

revision=""
if [[ -e "$root/.git" ]] && command -v git >/dev/null 2>&1; then
    revision=$(git -c "safe.directory=$root" -C "$root" rev-parse --verify HEAD 2>/dev/null | tr -d '\r') || revision=""
fi
export CTF_BUILD_REVISION="$revision"

"${compose[@]}" config --quiet
if ! "${compose[@]}" up -d --build; then
    fail "部署未启动。若基础镜像拉取失败，请在 .env 配置可达的 CTF_PYTHON_IMAGE 后重试。"
fi

probe() {
    if command -v curl >/dev/null 2>&1; then
        local response
        response=$(curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$port/healthz" 2>/dev/null) || return 1
        [[ -z $revision ]] || grep -Eq "\"revision\"[[:space:]]*:[[:space:]]*\"$revision\"" <<< "$response"
    else
        "${compose[@]}" exec -T platform python -c \
            'import json, sys, urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3); sys.exit(bool(sys.argv[1]) and json.load(response).get("revision") != sys.argv[1])' \
            "$revision" >/dev/null 2>&1
    fi
}

deadline=$((SECONDS + 60))
until probe; do
    if (( SECONDS >= deadline )); then
        "${compose[@]}" logs --tail 30 platform >&2
        fail "平台健康检查未通过，或服务版本与当前源码提交不一致。"
    fi
    sleep 1
done
published=$("${compose[@]}" port platform 8000 | tr -d '\r')
grep -Fxq -- "0.0.0.0:$port" <<< "$published" || fail "端口 $port 未发布到所有 IPv4 地址。"
mv -- "$ecs_tmp" "$ecs_file"
ecs_tmp=""

valid_ipv4() {
    [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    local octet
    local -a octets
    IFS=. read -r -a octets <<< "$1"
    for octet in "${octets[@]}"; do
        (( 10#$octet <= 255 )) || return 1
    done
}

ecs_public_ip() {
    command -v curl >/dev/null 2>&1 || return 1
    local metadata=http://100.100.100.200/latest
    local token field address
    token=$(curl --noproxy '*' -fsS --connect-timeout 1 --max-time 2 -X PUT \
        -H 'X-aliyun-ecs-metadata-token-ttl-seconds: 60' "$metadata/api/token" 2>/dev/null) || return 1
    for field in eipv4 public-ipv4; do
        address=$(curl --noproxy '*' -fsS --connect-timeout 1 --max-time 2 \
            -H "X-aliyun-ecs-metadata-token: $token" "$metadata/meta-data/$field" 2>/dev/null) || continue
        if valid_ipv4 "$address"; then
            printf '%s\n' "$address"
            return 0
        fi
    done
    return 1
}

public_ip=${CTF_PUBLIC_IP:-}
if ! valid_ipv4 "$public_ip"; then
    public_ip=$(ecs_public_ip) || public_ip="ECS公网IP"
fi
printf '部署完成，公网入口 http://%s:%s\n' "$public_ip" "$port"
if [[ -n $revision ]]; then
    printf '运行版本 %s，可在 /healthz 核对。\n' "$revision"
fi
printf '在 ECS 安全组入方向允许 TCP %s，然后从实例外访问。\n' "$port"
