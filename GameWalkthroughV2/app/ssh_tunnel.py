"""SSH 反向隧道（内网穿透）：把本机 webserver 映射到云服务器，供外网访问。

手机/平板不在同一局域网时，经云服务器中转也能打开攻略页面。客户端启动时
读取 config/ssh_tunnel.json（enabled=true 且配置完整才生效），在后台拉起
`ssh -N -R <远程端口>:127.0.0.1:<webserver端口>` 反向隧道：

  - 隧道建立成功 -> 扫码面板优先展示公网地址（http://<public_host|host>:<远程端口>）；
  - 云服务器连不上 / ssh 启动失败 / 隧道断开 -> 自动回退局域网地址（原有行为），
    并按 retry_seconds 周期重试，隧道恢复后面板自动切回公网地址。

前置条件（写进 config/ssh_tunnel.json 与 README）：
  - 仅支持密钥认证（BatchMode=yes，子进程永远不会卡在密码输入上）；未显式配
    identity_file 时交给 ~/.ssh/config 的主机条目兜底。
  - 服务器 sshd 需允许远程端口绑定到公网网卡：/etc/ssh/sshd_config 设
    GatewayPorts clientspecified（或 yes）并重启 sshd。否则隧道"建立成功"
    但端口只绑定在服务器 127.0.0.1 上，外网仍访问不到（服务器侧配置问题）。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.winprocess import assign_kill_on_close, close_job_handle

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(slots=True)
class SshTunnelConfig:
    """config/ssh_tunnel.json 的解析结果（字段缺省时用默认值）。"""

    enabled: bool = False
    host: str = ""              # 云服务器地址（IP 或域名）
    port: int = 22              # 服务器 sshd 端口
    user: str = ""              # ssh 登录用户
    identity_file: str = ""     # 私钥路径（相对路径按工程根解析）；空 = 交给 ~/.ssh/config 兜底
    private_key: str = ""       # 私钥内容（令牌式多机部署）：整段贴进配置随 app 分发，
                                # 运行时落地成用户级临时密钥文件交给 ssh -i；identity_file 优先
    remote_port: int = 18180    # 云服务器上监听的端口（外网访问 http://host:remote_port）
    public_host: str = ""       # 展示用地址（如域名）；空 = 用 host
    remote_bind: str = "0.0.0.0"  # 远程绑定地址；0.0.0.0 需服务器 GatewayPorts 支持
    local_host: str = "127.0.0.1"  # 回源地址（本机 webserver）
    keepalive_seconds: int = 30    # ssh ServerAliveInterval
    connect_timeout_seconds: float = 5.0  # 服务器可达性探测/ssh 连接超时
    retry_seconds: float = 30.0    # 失败后的重试间隔
    ssh_executable: str = "ssh"    # ssh 可执行文件（默认走 PATH，Windows 自带 OpenSSH）

    @property
    def valid(self) -> bool:
        return bool(self.enabled and self.host and self.user and self.remote_port > 0)

    def display_host(self) -> str:
        return (self.public_host or self.host).strip()

    def public_url(self) -> str:
        return f"http://{self.display_host()}:{int(self.remote_port)}"


def load_ssh_tunnel_config(path: str | Path | None) -> SshTunnelConfig:
    """读取隧道配置；文件缺失/损坏/字段非法时回退到"未启用"，绝不阻塞客户端启动。"""
    cfg = SshTunnelConfig()
    if path is None:
        return cfg
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg

    def _int(key: str, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float, minimum: float = 0.0) -> float:
        try:
            return max(minimum, float(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _str(key: str) -> str:
        return str(raw.get(key) or "").strip()

    cfg.enabled = bool(raw.get("enabled"))
    cfg.host = _str("host")
    cfg.port = _int("port", 22, 1)
    cfg.user = _str("user")
    identity = _str("identity_file")
    if identity:
        # 支持 ~ / %VAR% 与相对路径（相对路径按工程根解析，部署目录变化也不受影响）
        expanded = os.path.expandvars(os.path.expanduser(identity))
        if not os.path.isabs(expanded):
            expanded = str(_PROJECT_ROOT / expanded)
        cfg.identity_file = expanded
    cfg.private_key = _str("private_key")
    cfg.remote_port = _int("remote_port", 18180, 1)
    cfg.public_host = _str("public_host")
    cfg.remote_bind = _str("remote_bind") or "0.0.0.0"
    cfg.local_host = _str("local_host") or "127.0.0.1"
    cfg.keepalive_seconds = _int("keepalive_seconds", 30, 5)
    cfg.connect_timeout_seconds = _float("connect_timeout_seconds", 5.0, 1.0)
    cfg.retry_seconds = _float("retry_seconds", 30.0, 1.0)
    cfg.ssh_executable = _str("ssh_executable") or "ssh"
    return cfg


class SshTunnelManager:
    """后台维护一条 ssh 反向隧道，并对扫码面板暴露当前公网地址。

    runner 参数仅测试用：注入 (args: list[str]) -> Popen 兼容对象，替代真的
    拉起 ssh 子进程。
    """

    POLL_SECONDS = 1.0  # 隧道建立后的存活轮询间隔

    def __init__(
        self,
        config: SshTunnelConfig,
        *,
        local_port: int,
        log: Callable[[str], None] | None = None,
        runner: Callable[[list[str]], Any] | None = None,
        grace_seconds: float = 3.0,
    ) -> None:
        self._config = config
        self._local_port = int(local_port)
        self._log = log or (lambda m: print(m, file=sys.stderr))
        self._runner = runner
        self._grace_seconds = max(0.0, float(grace_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._proc: Any = None
        self._job_handle: int | None = None
        self._state: dict[str, Any] = {"active": False, "url": "", "detail": ""}

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._config.valid:
            self._log("[tunnel] SSH 隧道未启用或配置不完整（config/ssh_tunnel.json），走局域网")
            return
        self._thread = threading.Thread(target=self._loop, name="ssh-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
        close_job_handle(self._job_handle)  # 正常收尾：子进程已终止，还掉句柄
        self._job_handle = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._set_state(False, detail="已停止")

    def status(self) -> dict[str, Any]:
        """当前状态：{active, url, detail}。active=false 时面板回退局域网地址。"""
        with self._lock:
            return dict(self._state)

    # ------------------------------------------------------------------ 内部
    def _set_state(self, active: bool, url: str = "", detail: str = "") -> None:
        with self._lock:
            self._state = {"active": bool(active), "url": url, "detail": detail}

    def _reachable(self) -> bool:
        """TCP 探测服务器 sshd 端口，连不上就不必白拉一次 ssh。"""
        try:
            with socket.create_connection(
                (self._config.host, int(self._config.port)),
                timeout=self._config.connect_timeout_seconds,
            ):
                return True
        except OSError:
            return False

    def _identity_file(self) -> str:
        """解析出 ssh -i 用的私钥文件路径。

        优先显式 identity_file；否则把配置里内嵌的私钥内容（令牌式多机部署，
        随 app 配置分发）落地成当前用户 TEMP 目录下的临时密钥文件——ssh 命令行
        只认文件，而写到用户级 TEMP 可避开"安装目录共享、Windows 报
        UNPROTECTED PRIVATE KEY FILE"的 ACL 问题。
        """
        if self._config.identity_file:
            return self._config.identity_file
        key_text = self._config.private_key.replace("\r\n", "\n").strip()
        if not key_text:
            return ""
        if not key_text.endswith("\n"):
            key_text += "\n"
        try:
            key_dir = Path(tempfile.gettempdir()) / "game-walkthrough-tunnel"
            key_dir.mkdir(parents=True, exist_ok=True)
            path = key_dir / "tunnel_key"
            path.write_text(key_text, encoding="utf-8")  # 每次启动重写：配置改了即生效
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)  # POSIX 收紧；Windows 用户级 TEMP 目录本身仅本用户可见
            return str(path)
        except OSError as exc:
            self._log(f"[tunnel] 内嵌私钥落地失败: {exc}；回退 ~/.ssh/config 兜底")
            return ""

    def _build_args(self) -> list[str]:
        cfg = self._config
        args = [
            cfg.ssh_executable, "-N", "-T",
            "-o", "BatchMode=yes",                       # 只用密钥认证，绝不卡在密码输入
            "-o", "ExitOnForwardFailure=yes",            # 远程端口被占/无权限 -> 直接退出重试
            "-o", f"ServerAliveInterval={int(cfg.keepalive_seconds)}",
            "-o", "ServerAliveCountMax=3",               # 断网 3 个周期后 ssh 自行退出 -> 触发重连
            "-o", "StrictHostKeyChecking=accept-new",    # 首次连接自动记录主机指纹
            "-o", f"ConnectTimeout={max(1, int(cfg.connect_timeout_seconds))}",
        ]
        identity = self._identity_file()
        if identity:
            args += ["-i", identity]
        args += [
            "-p", str(int(cfg.port)),
            "-R", f"{cfg.remote_bind}:{int(cfg.remote_port)}:{cfg.local_host}:{self._local_port}",
            f"{cfg.user}@{cfg.host}",
        ]
        return args

    def _spawn(self) -> Any:
        args = self._build_args()
        if self._runner is not None:
            return self._runner(args)
        # 客户端常以无控制台方式运行（pythonw / agent 启动）：不挂 CREATE_NO_WINDOW
        # 的话，Windows 会为 ssh.exe 新开一个命令行窗口并常驻桌面
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,  # 认证失败/端口冲突等原因留给状态详情
            creationflags=creationflags,
        )
        # 内核级兜底：ssh 进程放进"父进程退出即全灭"的 Job——客户端被强杀时
        # ssh 不会变成孤儿继续占着云端转发端口（否则重启后一直
        # "remote port forwarding failed"）。
        close_job_handle(self._job_handle)
        self._job_handle = assign_kill_on_close(proc)
        return proc

    @staticmethod
    def _stderr_tail(proc: Any) -> str:
        try:
            return (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[-160:]
        except Exception:
            return ""

    def _loop(self) -> None:
        cfg = self._config
        last_detail = ""

        def announce(detail: str) -> None:
            nonlocal last_detail
            self._set_state(False, detail=detail)
            if detail != last_detail:  # 周期重试的同一原因只记一次日志
                last_detail = detail
                self._log(f"[tunnel] {detail}")

        while not self._stop_event.is_set():
            if not self._reachable():
                announce(f"云服务器 {cfg.host}:{cfg.port} 连不上，走局域网（{cfg.retry_seconds:.0f}s 后重试）")
                if self._stop_event.wait(cfg.retry_seconds):
                    return
                continue

            try:
                proc = self._spawn()
            except FileNotFoundError:
                announce(f"未找到 ssh 命令（{cfg.ssh_executable}），走局域网（需安装 OpenSSH 客户端）")
                return  # ssh 都没有，重试无意义
            except OSError as exc:
                announce(f"ssh 启动失败: {exc}")
                if self._stop_event.wait(cfg.retry_seconds):
                    return
                continue

            with self._lock:
                self._proc = proc

            # 宽限期：认证失败/远程端口被占时 ssh 会立刻退出，不能算建立成功
            deadline = time.monotonic() + self._grace_seconds
            while time.monotonic() < deadline and proc.poll() is None and not self._stop_event.is_set():
                time.sleep(0.2)
            if self._stop_event.is_set():
                with contextlib.suppress(Exception):
                    proc.terminate()
                return
            if proc.poll() is not None:
                reason = self._stderr_tail(proc)
                hint = ""
                if "remote port forwarding failed" in reason:
                    hint = ("；远端端口已被占用（另一实例/残留 ssh 会话正在使用该端口，"
                            "清理占用或换 remote_port 后可自动恢复）")
                announce(
                    f"ssh 退出(code={proc.returncode}){': ' + reason if reason else ''}"
                    f"{hint}，走局域网（{cfg.retry_seconds:.0f}s 后重试）"
                )
                if self._stop_event.wait(cfg.retry_seconds):
                    return
                continue

            url = cfg.public_url()
            self._set_state(True, url=url)
            last_detail = ""  # 断开后的失败原因允许重新记录
            self._log(f"[tunnel] SSH 隧道已建立: {url} -> {cfg.local_host}:{self._local_port}")

            while proc.poll() is None and not self._stop_event.is_set():
                time.sleep(self.POLL_SECONDS)
            if self._stop_event.is_set():
                with contextlib.suppress(Exception):
                    proc.terminate()
                return
            announce("隧道断开，走局域网（稍后自动重连）")
            if self._stop_event.wait(cfg.retry_seconds):
                return
