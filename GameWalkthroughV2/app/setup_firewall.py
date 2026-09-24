"""添加/删除 Windows 防火墙入站规则，放行攻略页 webserver 端口。

双网卡/多网段场景下，Windows 防火墙可能只放行了某一类网络（专用/公用），
导致另一网段的设备（如内网 10.x）无法访问 22818 端口。以管理员身份运行本
脚本添加一条不区分网络类型的入站放行规则即可：

    python scripts/setup_firewall.py            # 放行默认端口 22818
    python scripts/setup_firewall.py --port 9000
    python scripts/setup_firewall.py --remove   # 删除规则

仅 Windows 需要；其它平台直接跳过。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys

RULE_NAME = "GameWalkthrough Web Server"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_netsh(args: list[str]) -> int:
    result = subprocess.run(["netsh", "advfirewall", "firewall"] + args, check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="放行/移除攻略页 webserver 的防火墙入站规则")
    parser.add_argument("--port", type=int, default=22818, help="webserver 端口，默认 22818")
    parser.add_argument("--remove", action="store_true", help="删除放行规则")
    args = parser.parse_args()

    if os.name != "nt":
        print("当前不是 Windows，无需配置防火墙。")
        return 0
    if not is_admin():
        print("需要管理员权限：请右键“以管理员身份运行”终端后重新执行。")
        return 1

    if args.remove:
        code = run_netsh(["delete", "rule", f"name={RULE_NAME}"])
        print("规则已删除。" if code == 0 else f"删除失败（netsh 退出码 {code}），可能规则不存在。")
        return code

    code = run_netsh([
        "add", "rule", f"name={RULE_NAME}",
        "dir=in", "action=allow", "protocol=TCP", f"localport={args.port}",
    ])
    if code == 0:
        print(f"已放行 TCP {args.port} 端口入站（不区分网络类型），手机/平板现在应可访问。")
        return 0
    print(f"添加失败（netsh 退出码 {code}）。")
    return code


if __name__ == "__main__":
    sys.exit(main())
