"""文件日志：每天一个日志文件，自动压缩与清理历史日志。

谁在写（所有进程按天共用 <项目根>/logs/ 下同一份日志文件，追加写入）：
  - 客户端（app/client.py）：下载/导入 bootstrap、推送给 webserver 的消息等
  - webserver（app/webserver/server.py）：收到的推送、页面/代理打开、状态广播
  - 下载器 / 导入器（独立 CLI 运行时）：下载与导入过程

文件布局：logs/game-assistant-YYYYMMDD.log。
  - 超过 KEEP_RAW_DAYS（30 天）的原始日志压缩为 .gz 并删除原文件；
  - 超过 KEEP_GZ_DAYS（约半年）的压缩包直接删除。
清理在 setup_logging 和每次跨天时各触发一次；多进程并发触发时以“失败即跳过”
兜底（Windows 上删除/压缩被其它进程占用的文件会失败，留给下一次清理即可）。

设计要点：
  - 每条日志一次性 os.write(O_APPEND)：不长期持有文件句柄，不会阻碍其它进程
    压缩/删除旧文件；多进程交错写入最多打乱行序，不会互相覆盖；
  - 日志写入永不抛异常（emit 全部吞错）：日志不能影响业务流程。
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = _PROJECT_ROOT / "logs"

FILE_PREFIX = "game-assistant"
KEEP_RAW_DAYS = 30    # 原始日志保留天数，超过后压缩为 .gz 并删除原文件
KEEP_GZ_DAYS = 182    # 压缩日志保留天数（约半年），超过后删除

_DATE_RE = re.compile(r"-(\d{8})\.log(\.gz)?$")

_logger = logging.getLogger("file_logging")


class DailyFileHandler(logging.Handler):
    """按天写文件的日志处理器（每条记录写入时打开、写完即关）。"""

    def __init__(self, log_dir: Path = LOG_DIR, prefix: str = FILE_PREFIX) -> None:
        super().__init__()
        self._dir = Path(log_dir)
        self._prefix = prefix
        self._last_maintain_day = ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            day = time.strftime("%Y%m%d")
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{self._prefix}-{day}.log"
            data = (message + "\n").encode("utf-8", "replace")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception:
            self.handleError(record)
            return
        if day != self._last_maintain_day:
            self._last_maintain_day = day
            # 跨天后清理一次历史日志（后台执行，不拖慢写入）
            threading.Thread(target=self.maintain, name="log-maintain", daemon=True).start()

    def maintain(self) -> None:
        compress_old_logs(
            self._dir,
            self._prefix,
            keep_raw_days=KEEP_RAW_DAYS,
            keep_gz_days=KEEP_GZ_DAYS,
        )


def _file_age_seconds(path: Path, now: float) -> float | None:
    """从文件名里的 -YYYYMMDD 解析日志日期；解析不出返回 None。"""
    match = _DATE_RE.search(path.name)
    if match is None:
        return None
    try:
        file_day = time.mktime(time.strptime(match.group(1), "%Y%m%d"))
    except (ValueError, OverflowError):
        return None
    return now - file_day


def compress_old_logs(
    log_dir: Path | str = LOG_DIR,
    prefix: str = FILE_PREFIX,
    *,
    keep_raw_days: float = KEEP_RAW_DAYS,
    keep_gz_days: float = KEEP_GZ_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """压缩超龄原始日志、删除超龄压缩包；返回 (压缩数, 删除数) 便于测试。

    单个文件失败（被占用/权限等）只跳过该文件，不影响其它文件的清理。
    """
    directory = Path(log_dir)
    result = {"compressed": 0, "deleted": 0}
    if not directory.is_dir():
        return result
    current = time.time() if now is None else now

    for path in sorted(directory.glob(f"{prefix}-*.log")):
        age = _file_age_seconds(path, current)
        if age is None or age < keep_raw_days * 86400:
            continue
        gz_path = path.with_name(path.name + ".gz")
        try:
            if not gz_path.exists():
                tmp_path = gz_path.with_name(gz_path.name + ".tmp")
                with open(path, "rb") as src, gzip.open(tmp_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp_path.replace(gz_path)
            path.unlink()
            result["compressed"] += 1
            _logger.info(f"已压缩历史日志: {path.name} -> {gz_path.name}")
        except OSError as exc:
            _logger.warning(f"压缩历史日志失败(跳过): {path.name} 错误={exc}")

    for path in sorted(directory.glob(f"{prefix}-*.log.gz")):
        age = _file_age_seconds(path, current)
        if age is None or age < keep_gz_days * 86400:
            continue
        try:
            path.unlink()
            result["deleted"] += 1
            _logger.info(f"已删除过期压缩日志: {path.name}")
        except OSError as exc:
            _logger.warning(f"删除压缩日志失败(跳过): {path.name} 错误={exc}")
    return result


def setup_logging(
    log_dir: Path | str | None = None,
    *,
    level: int = logging.INFO,
    prefix: str = FILE_PREFIX,
) -> None:
    """初始化文件日志（幂等）：给 root logger 挂按天写的文件处理器。

    只写文件、不接管控制台——各模块原有的 print 输出保持不变，文件日志是
    额外增量。重复调用不会挂出第二个处理器。
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, DailyFileHandler):
            return
    directory = Path(log_dir) if log_dir is not None else LOG_DIR
    handler = DailyFileHandler(directory, prefix)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level)
    # 启动时清一次历史日志（后台执行，不阻塞启动）
    threading.Thread(target=handler.maintain, name="log-maintain", daemon=True).start()
