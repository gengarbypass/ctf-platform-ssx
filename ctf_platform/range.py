"""每队动态靶场环境的 Docker 操作封装。

平台容器直挂 /var/run/docker.sock（compose 已配置 group_add）；开发机可用
CTF_ENV_DOCKER_URL 覆盖（如 Windows Docker Desktop 的 npipe 地址）。
测试通过 app.config["RANGE_DOCKER_FACTORY"] 注入假客户端，避免真实 Docker 依赖。
"""
import os
from ipaddress import IPv4Network

from flask import current_app


class RangeError(RuntimeError):
    pass


def docker_client():
    factory = current_app.config.get("RANGE_DOCKER_FACTORY")
    if factory is not None:
        return factory()
    import docker
    base_url = os.environ.get("CTF_ENV_DOCKER_URL", "unix:///var/run/docker.sock")
    return docker.DockerClient(base_url=base_url)


def resource_name(contest_id, team_id, puzzle_id=0, *, network=False):
    name = f"ctfenv-c{contest_id}-t{team_id}-p{puzzle_id}"
    return f"{name}-net" if network else name


def stop_environment(contest_id, team_id, puzzle_id=0, *, container_id=""):
    """按容器 ID 与命名兜底停止并移除容器和专属网络，找不到视为已清理。"""
    client = docker_client()
    name = resource_name(contest_id, team_id, puzzle_id)
    for identifier in (container_id, name):
        if not identifier:
            continue
        try:
            container = client.containers.get(identifier)
            container.stop(timeout=10)
            container.remove(force=True)
        except Exception:
            pass
    try:
        network = client.networks.get(resource_name(contest_id, team_id, puzzle_id, network=True))
        network.remove()
    except Exception:
        pass


def create_environment_network(client, name, network_name, host_port):
    """从靶机地址池分配一个独立的 /28 子网。"""
    from docker.errors import APIError
    from docker.types import IPAMConfig, IPAMPool

    try:
        pool = IPv4Network(current_app.config["ENV_NETWORK_POOL"])
    except ValueError as error:
        raise RangeError("CTF_ENV_NETWORK_POOL 须为有效的 IPv4 CIDR。") from error
    if pool.prefixlen > 28:
        raise RangeError("CTF_ENV_NETWORK_POOL 至少应包含一个 /28 子网。")

    occupied = []
    for network in client.networks.list():
        for config in (network.attrs.get("IPAM") or {}).get("Config") or []:
            subnet = config.get("Subnet")
            if subnet and ":" not in subnet:
                occupied.append(IPv4Network(subnet, strict=False))

    subnet_size = 16
    subnet_count = pool.num_addresses // subnet_size
    first_address = int(pool.network_address)
    for offset in range(subnet_count):
        index = (host_port + offset) % subnet_count
        subnet = IPv4Network((first_address + index * subnet_size, 28))
        if any(subnet.overlaps(existing) for existing in occupied):
            continue
        ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))])
        try:
            return client.networks.create(network_name, driver="bridge", labels={"ctf.env": name}, ipam=ipam)
        except APIError as error:
            # 其他启动任务可能在读取网络列表之后占用了该子网。
            if "overlap" not in str(error.explanation).lower():
                raise
    raise RangeError(f"靶机地址池 {pool} 没有可用的 /28 子网，请配置空闲的 CTF_ENV_NETWORK_POOL。")


def start_environment(*, contest, team, image, container_port, host_port, flag_env, puzzle_id=0):
    """为队伍创建专属网络与容器并启动，返回 (container_id, network_id)。

    flag_env 为注入容器的环境变量（按队派生的 Flag）；image 解题赛取赛题配置、
    综合渗透取比赛配置。
    """
    name = resource_name(contest["id"], team["id"], puzzle_id)
    network_name = resource_name(contest["id"], team["id"], puzzle_id, network=True)
    try:
        client = docker_client()
        stop_environment(contest["id"], team["id"], puzzle_id)
        network = create_environment_network(client, name, network_name, host_port)
        try:
            container = client.containers.run(
                image,
                name=name,
                detach=True,
                network=network_name,
                labels={"ctf.env": name},
                environment=dict(flag_env),
                ports={f"{container_port}/tcp": host_port},
                mem_limit=current_app.config["ENV_MEM_LIMIT"],
                nano_cpus=int(current_app.config["ENV_CPU_QUOTA"]) * 10000,
                pids_limit=current_app.config["ENV_PIDS_LIMIT"],
            )
            return container.id, network.id
        except Exception:
            try:
                network.remove()
            except Exception:
                pass
            raise
    except RangeError:
        raise
    except Exception as error:
        raise RangeError(f"启动环境容器失败：{error}") from error


def environment_alive(contest_id, team_id, *, container_id=""):
    """探测容器是否仍在运行；容器不存在返回 False，Docker 不可用抛 RangeError。"""
    client = docker_client()
    name = resource_name(contest_id, team_id)
    for identifier in (container_id, name):
        if not identifier:
            continue
        try:
            container = client.containers.get(identifier)
            return container.status == "running"
        except Exception:
            continue
    return False


def probe_image(image, container_port, *, timeout=12):
    """创建一次性容器验证镜像可用、配置端口可连通。返回 (True, "") 或 (False, 原因)。"""
    import secrets as _secrets
    import socket
    import time as _time

    client = docker_client()
    try:
        client.images.get(image)
    except Exception:
        try:
            client.images.pull(image)
        except Exception as error:
            return False, f"镜像不存在且拉取失败：{error}"
    name = f"ctf-probe-{_secrets.token_hex(4)}"
    envs = {"FLAG": "flag{probe-selftest}", "FLAG_1": "flag{probe-selftest}"}
    limits = dict(
        mem_limit=current_app.config["ENV_MEM_LIMIT"],
        nano_cpus=int(current_app.config["ENV_CPU_QUOTA"]) * 10000,
        pids_limit=current_app.config["ENV_PIDS_LIMIT"],
    )
    container = None
    try:
        platform = None
        try:
            platform = next(iter(client.containers.list(filters={"label": "com.docker.compose.service=platform"})), None)
        except Exception:
            platform = None
        if platform is not None:
            net_name = next(iter(platform.attrs.get("NetworkSettings", {}).get("Networks", {})), None)
            if not net_name:
                return False, "未能识别平台容器所在网络，无法探测"
            container = client.containers.run(image, name=name, detach=True, network=net_name, environment=envs, **limits)
            _time.sleep(2.5)
            container.reload()
            ip = container.attrs.get("NetworkSettings", {}).get("Networks", {}).get(net_name, {}).get("IPAddress")
            if not ip:
                return False, "探针容器未获得网络地址，镜像可能立即退出"
            target = (ip, container_port)
        else:
            container = client.containers.run(image, name=name, detach=True,
                                              ports={f"{container_port}/tcp": None}, environment=envs, **limits)
            _time.sleep(2.5)
            container.reload()
            mapping = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get(f"{container_port}/tcp") or []
            host_port = next((item.get("HostPort") for item in mapping if item.get("HostPort")), None)
            if not host_port:
                return False, "探针容器未分配宿主端口，镜像可能立即退出"
            target = ("127.0.0.1", int(host_port))
        deadline = _time.time() + timeout
        last_error = ""
        while _time.time() < deadline:
            try:
                with socket.create_connection(target, timeout=2.5):
                    return True, ""
            except OSError as error:
                last_error = str(error)
            _time.sleep(1)
        return False, f"容器端口 {container_port} 连接失败（{last_error or '超时'}）；请确认服务监听 0.0.0.0 且端口一致"
    except RangeError:
        raise
    except Exception as error:
        return False, str(error)
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
