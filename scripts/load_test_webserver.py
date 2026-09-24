#!/usr/bin/env python3
"""Webserver 并发承载能力测试（模拟真实浏览器会话，分级加压）。

webserver 是 ThreadingHTTPServer（每个连接占一个线程），浏览器打开攻略页面时
会同时持有：查看器页面、/api/view、一条 /api/events SSE 长连接（服务端每 15s
心跳保活）。因此本脚本每个"虚拟浏览器"完整模拟这条链路：

  1. GET  /                      拉取查看器页面
  2. GET  /api/view              拉取当前推送内容
  3. GET  /api/events            建立 SSE 长连接并保持到本步结束
  4. 保持期间每 --poll 秒 GET /api/download-status，度量高负载下的响应延迟
  5. 保持时长足够时等待服务端心跳（: ping），验证长连接存活

按 --steps 逐级加压（如 10 -> 30 -> 60 -> ...），每级给出成功率和延迟分位数，
默认"会话错误率 > --max-error-rate 或轮询 p95 > --max-p95"判为失败；
跑完后汇总出最大可承载并发浏览器数。

只依赖标准库（asyncio），无需安装额外包。

用法示例：
    python scripts/load_test_webserver.py --port 22818
    python scripts/load_test_webserver.py --host 192.168.1.10 --steps 50,100,200,500
    python scripts/load_test_webserver.py --steps 100 --duration 60        # 单级压 1 分钟
    python scripts/load_test_webserver.py --steps 200 --broadcast 5        # 同时模拟桌面端推送

注意：
  - 只压本机端点（/、/api/view、/api/events、/api/download-status），不打游民星空
    上游；要测 /proxy 链路请自行评估上游站点的承受度。
  - 若失败集中在"连接被拒/10048/超时"，瓶颈可能在实际跑测试的本机（临时端口
    耗尽等），可加大 --poll、减小步进跨度，或换一台机器跑测试。
  - --broadcast 会调用 POST /api/navigate，会修改 webserver 当前的游戏推送状态。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

# 服务端 SSE 空闲心跳周期是 15s，留裕量判定"心跳丢失"
PING_WINDOW = 22.0


# ------------------------------------------------------------------ HTTP

async def _open(host: str, port: int, timeout: float):
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)


async def http_get(host: str, port: int, path: str, timeout: float = 10.0):
    """GET 请求，返回 (status, body, elapsed_ms)。

    webserver 是 HTTP/1.0（响应后即断开），读到 EOF 即得到完整响应体。
    """
    start = time.perf_counter()
    reader, writer = await _open(host, port, timeout)
    try:
        req = (
            f"GET {path} HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            "User-Agent: walkthrough-loadtest/1.0\r\n"
            "Accept: */*\r\n"
            "\r\n"
        )
        writer.write(req.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout)
        data = await asyncio.wait_for(reader.read(-1), timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if not data:
            raise ConnectionError("empty response")
        head, _, body = data.partition(b"\r\n\r\n")
        parts = head.split(b"\r\n", 1)[0].split(None, 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError(f"bad status line: {parts!r}")
        return int(parts[1]), body, elapsed_ms
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def http_post_json(host: str, port: int, path: str, payload: dict,
                         timeout: float = 10.0):
    """POST JSON，返回 (status, body, elapsed_ms)。供 --broadcast 模拟推送用。"""
    start = time.perf_counter()
    reader, writer = await _open(host, port, timeout)
    try:
        body = json.dumps(payload).encode("utf-8")
        req = (
            f"POST {path} HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode("ascii") + body
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout)
        data = await asyncio.wait_for(reader.read(-1), timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        parts = data.split(b"\r\n", 1)[0].split(None, 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError(f"bad status line: {parts!r}")
        return int(parts[1]), data.partition(b"\r\n\r\n")[2], elapsed_ms
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def sse_connect(host: str, port: int, timeout: float = 10.0):
    """建立 SSE 连接并读到首个事件块（retry: 5000），证明流真正在流动。

    返回 (reader, writer, elapsed_ms)。
    """
    start = time.perf_counter()
    reader, writer = await _open(host, port, timeout)
    try:
        req = (
            "GET /api/events HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            "User-Agent: walkthrough-loadtest/1.0\r\n"
            "Accept: text/event-stream\r\n"
            "\r\n"
        )
        writer.write(req.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout)
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != 200:
            raise ConnectionError(f"sse status: {status_line!r}")
        while True:  # 读掉响应头
            if await asyncio.wait_for(reader.readline(), timeout) in (b"\r\n", b"\n", b""):
                break
        while True:  # 首个事件块（retry: ... + 空行）
            if await asyncio.wait_for(reader.readline(), timeout) in (b"\r\n", b"\n", b""):
                break
        return reader, writer, (time.perf_counter() - start) * 1000.0
    except BaseException:
        with contextlib.suppress(Exception):
            writer.close()
        raise


# ------------------------------------------------------------------ 统计

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    data = sorted(values)
    k = (len(data) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(data) - 1)
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


class Counters:
    """单级压测的共享计数器（事件循环单线程内更新，无需加锁）。"""

    def __init__(self) -> None:
        self.page_lat: list[float] = []    # GET / 延迟
        self.view_lat: list[float] = []    # GET /api/view 延迟
        self.sse_lat: list[float] = []     # SSE 建立耗时
        self.poll_lat: list[float] = []    # 保持期轮询延迟
        self.fail_page = 0
        self.fail_view = 0
        self.fail_sse = 0
        self.sse_dropped = 0
        self.fail_ping = 0
        self.ping_ok = 0
        self.poll_errors = 0
        self.ok_sessions = 0
        self.fail_sessions = 0
        self.errors: list[str] = []

    def add_error(self, msg: str) -> None:
        if len(self.errors) < 5:
            self.errors.append(msg)


@dataclass
class StepResult:
    level: int
    counters: Counters
    wall_s: float
    ping_checked: bool


# ------------------------------------------------------------------ 会话

class _StageFail(Exception):
    """会话某个阶段失败，跳过后续阶段（计数已在原地完成）。"""


async def browser_session(idx: int, total: int, args: argparse.Namespace,
                          c: Counters, ping_checked: bool) -> None:
    # 启动错峰：N 个会话在 --ramp 秒内陆续打开（模拟真实用户先后进入）
    await asyncio.sleep(args.ramp * idx / max(total, 1))
    sse_reader = sse_writer = None
    page_ok = view_ok = sse_ok = ping_ok = False
    poll_ok = poll_attempt = 0

    async def stage(name: str, coro):
        try:
            return await coro
        except Exception as exc:
            setattr(c, f"fail_{name}", getattr(c, f"fail_{name}") + 1)
            c.add_error(f"{name}: {type(exc).__name__}: {exc}")
            raise _StageFail from exc

    try:
        # 1. 查看器页面
        status, body, ms = await stage(
            "page", http_get(args.host, args.port, "/", args.timeout))
        if status != 200 or len(body) < 50:
            raise ValueError(f"HTTP {status}, {len(body)} bytes")
        c.page_lat.append(ms)
        page_ok = True

        # 2. 当前推送内容
        status, body, ms = await stage(
            "view", http_get(args.host, args.port, "/api/view", args.timeout))
        if status != 200:
            raise ValueError(f"HTTP {status}")
        json.loads(body.decode("utf-8", "replace"))  # 必须是合法 JSON
        c.view_lat.append(ms)
        view_ok = True

        # 3. SSE 长连接
        sse_reader, sse_writer, ms = await stage("sse", sse_connect(
            args.host, args.port, args.timeout))
        c.sse_lat.append(ms)
        sse_ok = True

        # 4. 保持：周期轮询 + 等心跳
        hold_start = time.monotonic()
        hold_end = hold_start + args.duration
        next_poll = hold_start + random.uniform(0.2 * args.poll, args.poll)
        ping_due = (hold_start + PING_WINDOW) if ping_checked else None
        while True:
            now = time.monotonic()
            if now >= hold_end:
                break
            wake = min(hold_end, next_poll, ping_due or hold_end)
            try:
                line = await asyncio.wait_for(
                    sse_reader.readline(), timeout=max(wake - now, 0.05))
            except asyncio.TimeoutError:
                line = None
            except Exception as exc:
                c.sse_dropped += 1
                c.add_error(f"sse dropped: {type(exc).__name__}: {exc}")
                sse_ok = False
                break
            if line == b"":  # 服务端关闭了连接
                c.sse_dropped += 1
                c.add_error("sse dropped: EOF")
                sse_ok = False
                break
            now = time.monotonic()
            if ping_due is not None and now >= ping_due:
                c.fail_ping += 1
                c.add_error(f"ping: {PING_WINDOW:.0f}s 内未收到心跳")
                ping_due = None  # 每个会话只报一次
            if line and line.startswith(b": ping") and ping_checked and not ping_ok:
                ping_ok = True
                c.ping_ok += 1
                ping_due = None  # 已收到，不再判定超时
            if now >= next_poll:
                next_poll = now + args.poll
                poll_attempt += 1
                try:
                    st, _b, ms = await http_get(
                        args.host, args.port, "/api/download-status", args.timeout)
                    if st != 200:
                        raise ValueError(f"HTTP {st}")
                    c.poll_lat.append(ms)
                    poll_ok += 1
                except Exception as exc:
                    c.poll_errors += 1
                    c.add_error(f"poll: {type(exc).__name__}: {exc}")
    except _StageFail:
        pass
    finally:
        if sse_writer is not None:
            with contextlib.suppress(Exception):
                sse_writer.close()
        healthy = page_ok and view_ok and sse_ok
        if healthy and ping_checked:
            healthy = ping_ok
        if healthy and poll_attempt:
            healthy = poll_ok * 2 >= poll_attempt  # 一半以上轮询失败也算不健康
        if healthy:
            c.ok_sessions += 1
        else:
            c.fail_sessions += 1


# ------------------------------------------------------------------ 步骤

async def broadcast_loop(args: argparse.Namespace, stop: asyncio.Event) -> None:
    """模拟桌面端定期推送 navigate（所有 SSE 连接都会收到并写回）。"""
    payload = {"game": "__loadtest__", "url": "https://www.gamersky.com/",
               "image_src": "", "title": "loadtest broadcast"}
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await http_post_json(args.host, args.port, "/api/navigate",
                                 payload, args.timeout)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), args.broadcast)


async def report_progress(c: Counters, total: int, stop: asyncio.Event) -> None:
    """保持期间每 2s 在同一行刷新进度。"""
    start = time.monotonic()
    printed = False
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
            break
        except asyncio.TimeoutError:
            pass
        printed = True
        elapsed = time.monotonic() - start
        print(f"\r  [{elapsed:5.0f}s] SSE 已建立 {len(c.sse_lat)}/{total}"
              f"  轮询失败 {c.poll_errors}   ", end="", flush=True)
    if printed:
        print()


async def run_step(args: argparse.Namespace, level: int) -> StepResult:
    ping_checked = not args.broadcast and args.duration >= PING_WINDOW + 3
    c = Counters()
    stop = asyncio.Event()
    start = time.monotonic()
    print(f"\n== 并发 {level} ==")
    tasks = [asyncio.create_task(browser_session(i, level, args, c, ping_checked))
             for i in range(level)]
    background = [asyncio.create_task(report_progress(c, level, stop))]
    if args.broadcast > 0:
        background.append(asyncio.create_task(broadcast_loop(args, stop)))
    await asyncio.gather(*tasks)
    stop.set()
    await asyncio.gather(*background, return_exceptions=True)
    return StepResult(level=level, counters=c, wall_s=time.monotonic() - start,
                      ping_checked=ping_checked)


def print_step(r: StepResult, args: argparse.Namespace) -> bool:
    """打印单级结果，返回是否通过。"""
    c = r.counters
    total = c.ok_sessions + c.fail_sessions
    error_rate = c.fail_sessions / total if total else 1.0
    p95 = percentile(c.poll_lat, 95) if c.poll_lat else float("nan")
    reasons = []
    if error_rate > args.max_error_rate:
        reasons.append(f"错误率 {error_rate:.1%} > {args.max_error_rate:.1%}")
    if c.poll_lat and p95 > args.max_p95:
        reasons.append(f"轮询 p95 {p95:.0f}ms > {args.max_p95:.0f}ms")
    if r.ping_checked and c.fail_ping:
        reasons.append(f"{c.fail_ping} 个连接心跳超时")
    passed = not reasons

    def row(label: str, lat: list[float], fails: int) -> None:
        if not lat and not fails:
            return
        stat = (f"p50 {percentile(lat, 50):7.1f}ms  p95 {percentile(lat, 95):8.1f}ms"
                f"  max {max(lat):8.1f}ms") if lat else "(无成功样本)"
        print(f"  {label:<14}{len(lat):>6} 次  {stat}  失败 {fails}")

    print(f"  会话           {total:>6} 个  健康 {c.ok_sessions} / 失败 {c.fail_sessions}"
          f"  (耗时 {r.wall_s:.0f}s)")
    row("页面 /", c.page_lat, c.fail_page)
    row("view", c.view_lat, c.fail_view)
    row("SSE 建立", c.sse_lat, c.fail_sse)
    if r.ping_checked:
        got = c.ping_ok
        print(f"  {'SSE 心跳':<13}{got:>6}/{got + c.fail_ping}  收到服务端 15s 心跳")
    row("保持期轮询", c.poll_lat, c.poll_errors)
    if c.sse_dropped:
        print(f"  保持期掉线     {c.sse_dropped:>6} 个")
    for err in c.errors:
        print(f"  · {err}")
    verdict = "PASS" if passed else "FAIL (" + "; ".join(reasons) + ")"
    print(f"  判定           {verdict}")
    return passed


async def warmup(args: argparse.Namespace) -> bool:
    """单个会话预检：服务端可达、三个阶段都通，才继续加压。"""
    print(f"目标 http://{args.host}:{args.port} ，先做单会话预检…")
    c = Counters()
    probe = argparse.Namespace(**vars(args))
    probe.ramp, probe.duration = 0.0, 1.0
    await browser_session(0, 1, probe, c, False)
    if c.ok_sessions:
        print(f"  预检 OK：页面 {c.page_lat[0]:.0f}ms / view {c.view_lat[0]:.0f}ms"
              f" / SSE {c.sse_lat[0]:.0f}ms")
        return True
    print("  预检失败，服务端不可用或接口异常：")
    for err in c.errors or ["(无错误信息)"]:
        print(f"  · {err}")
    return False


def summary(results: list[StepResult], args: argparse.Namespace) -> None:
    print("\n" + "=" * 62)
    print("汇总")
    best: StepResult | None = None
    first_fail: StepResult | None = None
    for r in results:
        c = r.counters
        total = c.ok_sessions + c.fail_sessions
        error_rate = c.fail_sessions / total if total else 1.0
        ok = error_rate <= args.max_error_rate and (
            not r.ping_checked or c.fail_ping == 0) and (
            not c.poll_lat or percentile(c.poll_lat, 95) <= args.max_p95)
        mark = "PASS" if ok else "FAIL"
        print(f"  并发 {r.level:>5}  {mark}  (健康 {c.ok_sessions}/{total},"
              f" 轮询 p95 {percentile(c.poll_lat, 95):.0f}ms)")
        if ok:
            best = r
        elif first_fail is None:
            first_fail = r
    print("-" * 62)
    if best is None:
        print("结论：最低档位就未通过，请检查 webserver 配置/防火墙后重试。")
    elif first_fail is None:
        print(f"结论：至少可承载 {best.level} 个并发浏览器"
              f"（已测最高档全过；想摸上限可从 {best.level * 2} 起继续加档）。")
    else:
        print(f"结论：最大可承载约 {best.level} 个并发浏览器"
              f"（{first_fail.level} 档开始失败）。")


# ------------------------------------------------------------------ 入口

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="webserver 并发承载能力测试：模拟 N 个真实浏览器会话分级加压",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1", help="webserver 地址")
    p.add_argument("--port", type=int, default=22818, help="webserver 端口")
    p.add_argument("--steps", default="10,30,60,100,200,400",
                   help="每档并发数，逗号分隔，逐档加压")
    p.add_argument("--duration", type=float, default=25.0,
                   help="每档保持时长(秒)；>=25 时才校验 SSE 心跳")
    p.add_argument("--ramp", type=float, default=3.0, help="每档会话启动错峰时长(秒)")
    p.add_argument("--poll", type=float, default=5.0,
                   help="保持期间每个会话的轮询间隔(秒)")
    p.add_argument("--settle", type=float, default=18.0,
                   help="两档之间等待(秒)：让服务端释放上一批 SSE 线程"
                        "（客户端断开后线程要等下一次心跳才退出）")
    p.add_argument("--timeout", type=float, default=10.0, help="单请求超时(秒)")
    p.add_argument("--max-error-rate", type=float, default=0.02,
                   help="会话失败率超过该值判为 FAIL")
    p.add_argument("--max-p95", type=float, default=2000.0,
                   help="保持期轮询 p95 延迟上限(ms)，超过判为 FAIL")
    p.add_argument("--broadcast", type=float, default=0.0,
                   help=">0 时每 N 秒向所有连接广播一次 navigate"
                        "（注意：会修改 webserver 当前推送状态）")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    steps = sorted({int(s) for s in str(args.steps).split(",") if s.strip()})
    if not await warmup(args):
        return 2
    results: list[StepResult] = []
    for i, level in enumerate(steps):
        r = await run_step(args, level)
        print_step(r, args)
        results.append(r)
        if i < len(steps) - 1 and args.settle > 0:
            print(f"\n  (等待 {args.settle:.0f}s 让服务端释放上一批连接线程…)")
            await asyncio.sleep(args.settle)
    summary(results, args)
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
