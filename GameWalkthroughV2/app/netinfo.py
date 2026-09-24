"""LAN address helpers shared by the webserver and the desktop client."""

from __future__ import annotations

import re
import socket
import subprocess
import sys

# 常见虚拟网卡关键字：手机扫这些地址是打不开的，二维码不要浪费在它们上
_VIRTUAL_MARKERS = (
    "vethernet", "virtual", "vmware", "virtualbox", "loopback", "wi-fi direct",
    "wsl", "hyper-v", "tailscale", "zerotier", "isatap", "teredo", "bluetooth",
)


def _default_route_ip() -> str | None:
    """默认路由所在网卡的 IP（UDP connect 探测，不实际发包）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("223.5.5.5", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def _hostname_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    result: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in result:
            result.append(ip)
    return result


def _windows_adapters() -> list[tuple[str, str]]:
    """解析 ipconfig 输出 -> [(ip, 网卡名)]。中文系统输出为 GBK 编码。"""
    # 客户端常以无控制台方式运行（pythonw / agent 启动）：不给 ipconfig 挂
    # CREATE_NO_WINDOW 会周期性弹出命令行窗口闪一下
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["ipconfig"], capture_output=True, timeout=5, creationflags=creationflags
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text = proc.stdout.decode("gbk", "replace")
    adapters: list[tuple[str, str]] = []
    name = ""
    for line in text.splitlines():
        stripped = line.strip()
        if line and not line[0].isspace() and stripped.endswith(":"):
            name = stripped[:-1].strip()  # 网卡段落标题，如 “无线局域网适配器 WLAN:”
            continue
        match = re.search(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", line)
        if match and name:
            adapters.append((match.group(1), name))
    return [
        (ip, _short_adapter_name(label))
        for ip, label in adapters
        if not any(marker in label.lower() for marker in _VIRTUAL_MARKERS)
    ]


def _short_adapter_name(label: str) -> str:
    """“无线局域网适配器 WLAN” -> “WLAN”，“Ethernet adapter Ethernet 2” -> “Ethernet 2”。"""
    for keyword in ("适配器", " adapter "):
        if keyword in label:
            label = label.split(keyword)[-1].strip()
    return label.strip()


def lan_adapters() -> list[tuple[str, str]]:
    """Return (ip, label) pairs, best candidate first.

    默认路由所在网卡排最前；Windows 上过滤虚拟网卡（vEthernet/WSL/VMware 等），
    解析失败时回退到主机名解析（不带网卡名）。
    """
    if sys.platform == "win32":
        adapters = _windows_adapters()
    else:
        adapters = []
    if not adapters:
        adapters = [(ip, "") for ip in _hostname_addresses()]

    best = _default_route_ip()
    ordered = ([a for a in adapters if a[0] == best] if best else []) + [
        a for a in adapters if a[0] != best
    ]
    result: list[tuple[str, str]] = []
    for ip, label in ordered:
        if not ip or ip in {r[0] for r in result}:
            continue
        if ip.startswith(("127.", "169.254.")):
            continue
        result.append((ip, label))
    return result


def lan_ipv4_addresses() -> list[str]:
    """Return usable LAN IPv4 addresses, best candidate first. Loopback/link-local skipped."""
    return [ip for ip, _ in lan_adapters()]


def best_lan_address() -> str | None:
    addresses = lan_ipv4_addresses()
    return addresses[0] if addresses else None


def lan_url(port: int, host: str | None = None) -> str:
    """URL for phones/tablets, e.g. http://192.168.1.5:8180.

    Falls back to 127.0.0.1 when no LAN address can be determined (offline box),
    which is still correct for local browsing.
    """
    return f"http://{host or best_lan_address() or '127.0.0.1'}:{port}"
