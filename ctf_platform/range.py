"""每队动态靶场环境的 Docker 操作封装。

平台容器直挂 /var/run/docker.sock（compose 已配置 group_add）；开发机可用
CTF_ENV_DOCKER_URL 覆盖（如 Windows Docker Desktop 的 npipe 地址）。
测试通过 app.config["RANGE_DOCKER_FACTORY"] 注入假客户端，避免真实 Docker 依赖。
"""
import os

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


def resource_name(contest_id, team_id, *, network=False):
    name = f"ctfenv-c{contest_id}-t{team_id}"
    return f"{name}-net" if network else name


def stop_environment(contest_id, team_id, *, container_id=""):
    """按容器 ID 与命名兜底停止并移除容器和专属网络，找不到视为已清理。"""
    client = docker_client()
    name = resource_name(contest_id, team_id)
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
        network = client.networks.get(resource_name(contest_id, team_id, network=True))
        network.remove()
    except Exception:
        pass


def start_environment(*, contest, team, host_port, flag_env):
    """为队伍创建专属网络与容器并启动，返回 (container_id, network_id)。

    flag_env 为注入容器的环境变量（各靶点派生的 Flag）。
    """
    client = docker_client()
    name = resource_name(contest["id"], team["id"])
    network_name = resource_name(contest["id"], team["id"], network=True)
    stop_environment(contest["id"], team["id"])
    try:
        network = client.networks.create(network_name, driver="bridge", labels={"ctf.env": name})
        try:
            container = client.containers.run(
                contest["env_image"],
                name=name,
                detach=True,
                network=network_name,
                labels={"ctf.env": name},
                environment=dict(flag_env),
                ports={f"{contest['env_container_port']}/tcp": host_port},
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
