"""Offline smoke tests for the LAN QR access feature (netinfo + hot-corner panel).

Run:  python tests/test_qr_access.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 cp1252，中文输出会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.netinfo import best_lan_address, lan_adapters, lan_ipv4_addresses, lan_url
from app.qrcode_panel import MAX_QR_CODES, build_qr_png, in_hot_corner


def test_lan_addresses() -> None:
    addresses = lan_ipv4_addresses()
    assert isinstance(addresses, list)
    seen: list[str] = []
    for ip in addresses:
        assert isinstance(ip, str) and ip, ip
        assert not ip.startswith("127."), f"loopback leaked: {ip}"
        assert not ip.startswith("169.254."), f"link-local leaked: {ip}"
        assert ip not in seen, f"duplicate: {ip}"
        seen.append(ip)
    best = best_lan_address()
    assert best is None or best == addresses[0]
    assert lan_url(8180, host="192.168.1.5") == "http://192.168.1.5:8180"
    assert lan_url(8180).endswith(":8180")
    print(f"PASS 1: LAN 地址解析 -> {addresses or '（无局域网地址，回退 127.0.0.1）'}")


def test_lan_adapters() -> None:
    adapters = lan_adapters()
    assert adapters == [(ip, "") for ip in lan_ipv4_addresses()] or len(adapters) == len(lan_ipv4_addresses())
    for ip, label in adapters:
        assert ip.count(".") == 3, ip
        low = label.lower()
        for marker in ("vethernet", "vmware", "virtualbox", "wsl", "hyper-v", "loopback"):
            assert marker not in low, f"虚拟网卡未过滤: {label}"
    if adapters:
        assert adapters[0][0] == lan_ipv4_addresses()[0]  # 默认路由网卡排最前
    assert MAX_QR_CODES == 2  # 双网卡场景：面板最多两个二维码
    print(f"PASS 1b: 网卡枚举（过滤虚拟网卡） -> {[(ip, label or '?') for ip, label in adapters]}")


def test_hot_corner() -> None:
    width, zone = 1920, 16
    assert in_hot_corner((width - 1, 0), width, zone)
    assert in_hot_corner((width - 8, 12), width, zone)
    assert in_hot_corner((width, 16), width, zone)
    assert not in_hot_corner((width - 17, 0), width, zone)  # 离右边太远
    assert not in_hot_corner((width - 1, 17), width, zone)  # 离顶部太远
    assert not in_hot_corner((100, 0), width, zone)
    print("PASS 2: 右上角热区判定")


def test_qr_png() -> None:
    png = build_qr_png("http://192.168.1.5:8180")
    if png is None:
        print("SKIP 3: qrcode/Pillow 不可用（pip install qrcode 后可生成二维码）")
        return
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"
    assert len(png) > 200
    build_qr_png("")  # 空内容也不应抛异常
    print("PASS 3: 二维码 PNG 生成")


def test_access_info() -> None:
    """真实调用路径：QrcodePanel._access_info（含 lan_adapters 导入与多二维码结构）。"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception:
        print("SKIP: 无显示环境，跳过 _access_info 测试")
        return
    from app.qrcode_panel import QrcodePanel

    panel = QrcodePanel(root, port=8180)
    entries, extras = panel._access_info()
    assert isinstance(entries, list) and isinstance(extras, list)
    for url, label, png in entries:
        assert url.startswith("http://") and url.endswith(":8180"), url
        assert url.removeprefix("http://").split(":")[0] not in extras, url
    print(f"PASS 4: 面板 _access_info（局域网回退） -> {[(url, label or '?') for url, label, _ in entries]}")

    # SSH 隧道公网地址可用 -> 二维码优先展示公网地址
    public_url = "http://gw.example.com:18180"
    panel_pub = QrcodePanel(root, port=8180, public_url_provider=lambda: public_url)
    entries, extras = panel_pub._access_info()
    assert len(entries) == 1 and entries[0][0] == public_url, entries
    assert "公网" in entries[0][1], entries
    assert extras == [], entries
    print(f"PASS 4b: 公网地址优先 -> {entries[0][0]}（{entries[0][1]}）")

    # 隧道失效（provider 返回 None）-> 自动回退局域网地址
    panel_off = QrcodePanel(root, port=8180, public_url_provider=lambda: None)
    entries, _ = panel_off._access_info()
    assert all(url.endswith(":8180") for url, _, _ in entries), entries
    print(f"PASS 4c: 隧道失效回退局域网 -> {[url for url, _, _ in entries]}")

    # 面板开着时隧道状态变化 -> 周期复查能拿到新地址，就地刷新不重播动画
    state = {"url": None}
    panel_live = QrcodePanel(root, port=8180, public_url_provider=lambda: state["url"])
    first, _ = panel_live._access_info()          # 隧道未上线：局域网
    cached, _ = panel_live._access_info()         # 内容没变：直接命中缓存（周期复查零开销）
    assert cached is first, "内容未变化时应返回缓存的同一份条目"
    state["url"] = "http://gw.example.com:18180"  # 模拟面板开着时隧道上线
    second, _ = panel_live._access_info()
    assert second[0][0] == "http://gw.example.com:18180" and second is not first, second
    panel_live._win = panel_live._build_window()
    panel_live._refresh_ui(second, [])            # 展示中的就地刷新：重建内容并原位调整
    assert panel_live._built_key == repr([("http://gw.example.com:18180", "公网访问 · SSH穿透")])
    assert panel_live._rect is not None           # 按新内容更新了面板位置/尺寸
    panel_live._win.destroy()
    print("PASS 4d: 展示中隧道上线 -> 就地切到公网地址（缓存命中不重画二维码）")

    # 全屏展示：铺满整屏（柔和底色），可退出恢复正常面板（电视/大荧幕远距离扫码）
    panel_fs = QrcodePanel(root, port=8180,
                           public_url_provider=lambda: "http://gw.example.com:18180")
    panel_fs._enter_fullscreen()
    assert panel_fs._fullscreen is True and panel_fs._fs_win is not None
    assert panel_fs._visible is False, "全屏期间小面板应收起"
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    panel_fs._fs_win.update_idletasks()
    geometry = panel_fs._fs_win.geometry()
    assert geometry.startswith(f"{screen_w}x{screen_h}+0+0"), (
        f"全屏窗口应铺满整屏 {screen_w}x{screen_h}+0+0，实际 {geometry}")
    assert str(panel_fs._fs_win.cget("bg")).lower() == "#000000", "全屏背景应为黑色（FS_BG）"
    panel_fs._exit_fullscreen()
    assert panel_fs._fullscreen is False and panel_fs._fs_win is None
    assert panel_fs._visible is True, "退出全屏后面板应恢复显示"
    assert panel_fs._win is not None and panel_fs._win.winfo_exists()
    print(f"PASS 5: 二维码全屏（铺满 {screen_w}x{screen_h}、柔和底色）/还原，热角逻辑恢复")

    # 多网卡轮播：全屏下 ◀/▶（键盘 ←/→ 同效）循环切换各网卡二维码
    panel_multi = QrcodePanel(root, port=8180)
    entries_multi = [
        ("http://192.168.1.5:8180", "WLAN", None),
        ("http://192.168.9.9:8180", "Ethernet 2", None),
        ("http://gw.example.com:18180", "公网访问 · SSH穿透", None),
    ]
    panel_multi._access_info = lambda: (list(entries_multi), [])
    panel_multi._enter_fullscreen()
    assert panel_multi._fullscreen is True and panel_multi._fs_index == 0, panel_multi._fs_index
    assert panel_multi._fs_entries[0][0] == "http://192.168.1.5:8180"
    panel_multi._fs_switch(1)
    assert panel_multi._fs_index == 1, panel_multi._fs_index
    panel_multi._fs_switch(1)
    assert panel_multi._fs_index == 2, panel_multi._fs_index
    panel_multi._fs_switch(1)
    assert panel_multi._fs_index == 0, "末张之后应回绕到第一张"
    panel_multi._fs_switch(-1)
    assert panel_multi._fs_index == 2, "第一张之前应回绕到最后一张"
    panel_multi._exit_fullscreen()
    assert panel_multi._fs_entries == [] and panel_multi._fs_index == 0
    print("PASS 6: 多网卡全屏轮播（◀/▶ 循环切换、越界回绕、退出复位）")

    for p in (panel, panel_pub, panel_off, panel_live, panel_fs, panel_multi):
        p.stop()
    root.destroy()


if __name__ == "__main__":
    test_lan_addresses()
    test_lan_adapters()
    test_access_info()
    test_hot_corner()
    test_qr_png()
    print("\nALL QR ACCESS TESTS PASSED")
