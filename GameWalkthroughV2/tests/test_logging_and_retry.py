"""Offline tests for network retry (app/net_retry.py) and file logging
(app/file_logging.py).

Run:  python tests/test_logging_and_retry.py
"""

from __future__ import annotations

import gzip
import logging
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

from app.net_retry import DEFAULT_ATTEMPTS, DEFAULT_INTERVAL, RetryingSession
from app.file_logging import DailyFileHandler, compress_old_logs, setup_logging


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FlakySession:
    """前 fail_count 次抛异常，之后返回 ok_response；记录调用次数。"""

    def __init__(self, fail_count: int, exc: Exception, ok_response: FakeResponse) -> None:
        self.fail_count = fail_count
        self.exc = exc
        self.ok_response = ok_response
        self.calls = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc
        return self.ok_response

    def close(self) -> None:
        pass


class AlwaysFailSession:
    def __init__(self, exc: Exception, statuses: list[int]) -> None:
        self.exc = exc
        self.statuses = list(statuses)
        self.calls = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls += 1
        if self.statuses:
            return FakeResponse(self.statuses.pop(0))
        raise self.exc

    def close(self) -> None:
        pass


def assert_eq(actual, expected, message: str) -> None:
    assert actual == expected, f"{message}: 期望 {expected!r}, 实际 {actual!r}"


def test_defaults() -> None:
    assert_eq(DEFAULT_ATTEMPTS, 3, "默认重试次数应为 3")
    assert_eq(DEFAULT_INTERVAL, 20.0, "默认重试间隔应为 20 秒")
    print("PASS 1: 默认重试策略为 3 次 / 20 秒")


def test_retry_on_exception() -> None:
    inner = FlakySession(2, requests.ConnectionError("boom"), FakeResponse(200))
    notes: list[str] = []
    session = RetryingSession(inner, interval=0.0, notify=notes.append)
    response = session.get("https://example.com/x")
    assert_eq(response.status_code, 200, "重试后应拿到成功响应")
    assert_eq(inner.calls, 3, "前 2 次失败 + 1 次成功 = 3 次调用")
    assert_eq(len(notes), 2, "每次重试前应有一次提示")
    assert "第 1/3 次" in notes[0] and "自动重试" in notes[0], notes[0]
    print("PASS 2: 网络异常按 3 次/20 秒策略自动重试")


def test_retry_on_5xx() -> None:
    inner = AlwaysFailSession(RuntimeError("unused"), [503, 500, 200])
    session = RetryingSession(inner, interval=0.0)
    response = session.get("https://example.com/x")
    assert_eq(response.status_code, 200, "5xx 后重试应成功")
    assert_eq(inner.calls, 3, "调用次数应为 3")
    print("PASS 3: 服务端 5xx 自动重试")


def test_gives_up_after_attempts() -> None:
    inner = AlwaysFailSession(requests.Timeout("timeout"), [])
    notes: list[str] = []
    session = RetryingSession(inner, interval=0.0, notify=notes.append)
    try:
        session.get("https://example.com/x")
    except requests.Timeout:
        pass
    else:
        raise AssertionError("次数用尽后应抛出最后一次异常")
    assert_eq(inner.calls, DEFAULT_ATTEMPTS + 1, "初次尝试 + 3 次重试 = 4 次调用")
    assert_eq(len(notes), DEFAULT_ATTEMPTS, "应有 3 条重试提示")

    # 5xx 次数用尽：返回最后一次响应，交由调用方 raise_for_status 报错
    inner2 = AlwaysFailSession(requests.Timeout("unused"), [503, 503, 503, 503])
    session2 = RetryingSession(inner2, interval=0.0)
    response = session2.get("https://example.com/x")
    assert_eq(response.status_code, 503, "5xx 用尽后应返回响应本身")
    try:
        response.raise_for_status()
    except requests.HTTPError:
        pass
    else:
        raise AssertionError("raise_for_status 应报错")
    print("PASS 4: 重试次数用尽后按场景抛异常/返回响应")


def test_no_retry_on_4xx() -> None:
    inner = AlwaysFailSession(requests.ConnectionError("unused"), [404, 200])
    session = RetryingSession(inner, interval=0.0)
    response = session.get("https://example.com/x")
    assert_eq(response.status_code, 404, "4xx 应原样返回")
    assert_eq(inner.calls, 1, "4xx 不应重试")
    print("PASS 5: 4xx 不重试")


def test_daily_file_and_cleanup() -> None:
    with tempfile.TemporaryDirectory(prefix="gwa-logs-") as tmp:
        log_dir = Path(tmp)

        # 写入今天的日志：同一天两条记录进同一个文件
        handler = DailyFileHandler(log_dir)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
        logger = logging.getLogger("file-logging-test")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.info("第一条日志")
            logger.info("第二条日志")
        finally:
            logger.removeHandler(handler)

        today = time.strftime("%Y%m%d")
        log_file = log_dir / f"game-assistant-{today}.log"
        assert log_file.exists(), f"今天的日志文件应存在: {log_file}"
        content = log_file.read_text(encoding="utf-8")
        assert content.count("\n") == 2, f"应有两行日志: {content!r}"
        assert "第一条日志" in content and "INFO" in content, content

        # 造 3 个历史文件：待压缩（>30 天 <182 天）、待删除（>182 天）、新鲜
        old_ts = time.mktime(time.strptime("20240115", "%Y%m%d"))
        raw_old = log_dir / "game-assistant-20240115.log"
        raw_old.write_bytes(b"old log line\n")
        gz_expired = log_dir / "game-assistant-20230601.log.gz"
        gz_expired.write_bytes(b"stale gz")
        gz_fresh = log_dir / "game-assistant-20240215.log.gz"
        gz_fresh.write_bytes(b"fresh gz")

        result = compress_old_logs(
            log_dir,
            keep_raw_days=30,
            keep_gz_days=182,
            now=old_ts + 60 * 86400,  # 2024-03 中旬：raw_old 已 60 天
        )
        assert_eq(result["compressed"], 1, "应压缩 1 个超龄原始日志")
        assert_eq(result["deleted"], 1, "应删除 1 个过期压缩包")
        assert not raw_old.exists(), "压缩后原文件应删除"
        gz_new = log_dir / "game-assistant-20240115.log.gz"
        assert gz_new.exists(), "压缩包应生成"
        assert gzip.decompress(gz_new.read_bytes()) == b"old log line\n", "压缩包内容应可还原"
        assert not gz_expired.exists(), "超过半年的压缩包应删除"
        assert gz_fresh.exists(), "半年内的压缩包应保留"
        assert log_file.exists(), "今天的日志不受清理影响"
    print("PASS 6: 按天写日志、超期压缩与清理")


def test_setup_logging_idempotent() -> None:
    with tempfile.TemporaryDirectory(prefix="gwa-logs-setup-") as tmp:
        setup_logging(tmp)
        try:
            root = logging.getLogger()
            count = sum(1 for h in root.handlers if isinstance(h, DailyFileHandler))
            assert_eq(count, 1, "首次 setup 应挂 1 个文件处理器")
            setup_logging(tmp)
            count = sum(1 for h in root.handlers if isinstance(h, DailyFileHandler))
            assert_eq(count, 1, "重复 setup 不应重复挂处理器")
            logging.getLogger("setup-logging-test").info("setup 日志写入")
            day = time.strftime("%Y%m%d")
            log_file = Path(tmp) / f"game-assistant-{day}.log"
            assert log_file.exists() and "setup 日志写入" in log_file.read_text(encoding="utf-8")
        finally:
            root = logging.getLogger()
            for handler in list(root.handlers):
                if isinstance(handler, DailyFileHandler):
                    root.removeHandler(handler)
    print("PASS 7: setup_logging 幂等且可指定目录")


def main() -> int:
    test_defaults()
    test_retry_on_exception()
    test_retry_on_5xx()
    test_gives_up_after_attempts()
    test_no_retry_on_4xx()
    test_daily_file_and_cleanup()
    test_setup_logging_idempotent()
    print("ALL RETRY & LOGGING TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
