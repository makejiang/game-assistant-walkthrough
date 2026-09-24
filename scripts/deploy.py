#!/usr/bin/env python3
"""
Game Assistant Tool Server — 一键部署脚本
===========================================
参考: IntelAIGamingAssistantLibrary/demo/dowload_models/download_modelscope_models.py

功能：服务包获取 → 解压安装 → AI 模型下载 → 启动服务 → 健康检查
监听地址：127.0.0.1:22919（新版默认；不通时兼容探测旧版 9190）

用法：
    python deploy.py                              # 默认：自动下载，失败回退手动
    python deploy.py --mode auto                  # 仅自动下载
    python deploy.py --mode manual                # 仅手动选择
    python deploy.py --package ./file.7z          # 跳过下载，直接使用本地包
    python deploy.py --start-only                 # 仅启动已安装的服务
    python deploy.py --skip-models                # 跳过模型下载
    python deploy.py --lang zh                    # Splitter 模型语言 (zh/en)
    python deploy.py --install-dir D:\\MyDir      # 自定义安装目录
    python deploy.py --force                      # 强制重新安装

依赖：
    pip install requests py7zr modelscope
"""

import os
import sys
import re
import json
import time
import logging
import argparse
import socket
import subprocess
import threading
import queue
import traceback
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote, quote
from dataclasses import dataclass, field, replace as dc_replace

# ============================================================================
# Section 1: Constants
# ============================================================================

SERVICE_URL = (
    "https://modelscope.cn/api/v1/models/OpenVINO/GameAssist/"
    "repo?Revision=master&FilePath=GameAssistant.zip"
)
# ModelScope 仓康新布局（服务端 7z 直链，不再套 zip、无版本子目录）。
# 旧布局 bin/vX.Y.Z/windows/... 由 SERVICE_7Z_LEGACY_RE 识别，不写死版本号。
REPO_ID = "OpenVINO/GameAssist"
SERVICE_7Z_NEW = "bin/windows/GameAssistantToolServer.7z"
INNER_7Z_NAME = "GameAssistantToolServer.7z"
SERVICE_7Z_LEGACY_RE = re.compile(
    r"^bin/v(\d+(?:\.\d+)*)/windows/GameAssistantToolServer\.7z$")
# repo files API：返回每文件 Path/Size/Sha256/Type。
# 注意 SDK 的 HubApi.get_model_files 只回 Path/Size 不含 Sha256，版本比对必须打这个原始接口。
# （env GA_REPO_API 可覆盖，供本地假服务器测试）
REPO_API = os.environ.get(
    "GA_REPO_API",
    f"https://modelscope.cn/api/v1/models/{REPO_ID}/repo/files")
# files API 单次返回超过该条数会静默截断，指纹计算需对截断做保守处理
REPO_FILES_TRUNCATION_LIMIT = 3000
# 模型清单脚本（跟随服务端一起更新；ast 静态解析，不执行代码）。
# 下载地址经 _manifest_url() 在调用时由 REPO_API 推导（env 覆盖可整体指向测试服务器）。
MANIFEST_SCRIPT = "demo/dowload_models/download_modelscope_models.py"
# GitHub releases 兜底渠道：版本最及时（ModelScope 延时几天）但国内速度不稳，
# 仅在 ModelScope 路径不可用时使用，且使用前必须经用户确认（弹窗明示速度慢）。
GITHUB_RELEASES_API = (
    "https://api.github.com/repos/GameTechDev/"
    "IntelAIGamingAssistantLibrary/releases/latest")
# 服务包/清单脚本的下载缓存目录（install_dir 下）
UPDATE_CACHE_DIRNAME = "_update_cache"
# 覆盖安装服务端时保留的用户数据目录：已存在则原样保留，绝不用包内骨架
# 覆盖（存档数据库、已装模型都在里面）；包里没有该目录的正常移入
PRESERVE_USER_DIRS = ("saves", "models")
# 置为 0 时整体跳过更新检测（离线/排障开关）
UPDATE_CHECK_ENV = "GA_UPDATE_CHECK"
# 需要用户确认的弹窗倒计时秒数（超时自动选默认项）
CONFIRM_COUNTDOWN_SECONDS = 10
# 9190 与部分 Windows 服务冲突，新版服务端默认 22919；旧版已发布出去仍在用
# 9190 —— 探测/显示经 service_ports()/resolve_service_port() 兼容两者。
SERVICE_PORT = 22919
SERVICE_PORT_LEGACY = 9190
SERVICE_HOST = "127.0.0.1"
DEFAULT_INSTALL_DIR = Path(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
) / "GameAssistant"
# 用户在"未找到服务端"弹窗里选择过的已解压服务端目录。记在默认数据目录下、
# 独立于实际安装位置——下次启动无论配置的 install_dir 指向哪里都能找到这份记录
_CHOSEN_SERVER_DIR_FILE = DEFAULT_INSTALL_DIR / "chosen_server_dir.txt"


def _load_chosen_server_dir() -> Path | None:
    """上次用户选择的服务端目录；已失效（不存在/无服务端 exe）→ None。"""
    try:
        text = _CHOSEN_SERVER_DIR_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    chosen = Path(text)
    if find_server_exe(chosen) is None:
        return None   # 目录被删/服务端被删 → 视为不可用
    return chosen


def _force_rmtree(path: Path) -> None:
    """尽力删除目录树：只读文件先清属性再删（Windows 缓存文件常见只读位），
    被占用删不掉的就放过——由调用方决定容忍策略。"""

    import shutil
    import stat

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass   # 真被锁住就算了——缓存可放弃

    shutil.rmtree(path, onerror=_onerror)


def _replace_dir_fresh(item: Path, target: Path,
                       log: logging.Logger) -> None:
    """整树替换 target 为 item（用于 _internal 等运行时负载目录）。

    不能用合并：旧版残留的 dll 会让新版本链接错误而崩溃。做法是先把旧目录
    整体重命名挪走——rename 是原子操作，任何文件被占用都会立即整体失败，
    此时还没有改动任何文件，更新中止后旧版本完好可用；挪走后新目录移入，
    旧目录再尽力删除（已不在加载路径上，删不掉也无害，下次更新再清）。
    """
    import shutil
    aside = target.with_name(f"{target.name}.old_{time.time_ns()}")
    try:
        os.rename(target, aside)
    except OSError as e:
        raise RuntimeError(
            f"无法移走旧目录 {target}（文件被占用，服务端可能尚未完全停止）："
            f"为避免新旧运行时混装导致崩溃，本次更新已中止——当前版本仍可"
            f"正常使用，可稍后重试更新: {e}") from e
    shutil.move(str(item), str(target))
    _force_rmtree(aside)   # 尽力清理；删不掉也无害（下次更新再清）


def _merge_tree(src: Path, dst: Path, log: logging.Logger) -> None:
    """把 src 的内容合并进 dst——覆盖安装的"覆盖"语义：

    - 同名文件：覆盖（先删旧再移入）
    - dst 独有的文件/目录：原样保留——用户数据（saves 存档、已装模型）不清空
    - 名为 caches 的子树：尽力整树删除（旧缓存对新版无用；只读/被占用
      的残留放过，不影响更新）
    - 名为 _internal 的子树（PyInstaller 运行时负载）：整树替换——旧版
      残留 dll 会让新版本链接错误崩溃，不能合并
    - PRESERVE_USER_DIRS 中的目录若 dst 已存在：保留 dst，包内骨架不进入
    """

    import shutil

    # 上次更新可能留下的旧 _internal 备份：句柄早已释放，此时清理通常能成功
    for stale in dst.glob("_internal.old_*"):
        _force_rmtree(stale)

    # _internal 必须最先处理：整树替换失败（被占用）时中止更新，
    # 此时其他文件尚未被改动，旧版本保持完整可用
    internal = next((i for i in src.iterdir()
                     if i.is_dir() and i.name.lower() == "_internal"), None)
    if internal is not None:
        target = dst / "_internal"
        if target.exists():
            _replace_dir_fresh(internal, target, log)
        else:
            shutil.move(str(internal), str(target))

    for item in src.iterdir():
        if item.is_dir() and item.name.lower() == "_internal":
            continue   # 第一遍已整树替换
        target = dst / item.name
        if item.is_dir():
            if item.name.lower() == "caches":
                _force_rmtree(target)
                if target.exists():
                    log.warning(
                        f"  缓存目录 {target} 未能完全删除（可能被占用/只读），"
                        "已跳过（不影响本次更新，下次更新再清理）")
                continue
            if not target.exists():
                shutil.move(str(item), str(target))
            elif item.name.lower() in PRESERVE_USER_DIRS:
                log.info(f"  保留用户数据目录: {target}")
                # 包内骨架不进入用户数据目录
            else:
                _merge_tree(item, target, log)
        else:
            try:
                if target.exists():
                    target.unlink()
            except OSError as e:
                raise RuntimeError(
                    f"无法覆盖旧文件 {target}（可能被占用或被沙盒安全策略阻止）: {e}"
                ) from e
            shutil.move(str(item), str(target))


def _resolve_data_dir(install_dir: Path) -> Path:
    """日志/状态目录：install_dir 可写则用它，否则回落默认（与 service_manager 对齐）。"""
    if not install_dir.is_dir():
        return DEFAULT_INSTALL_DIR  # 不存在就回落，不凭一个拼错的路径现造目录
    try:
        probe = install_dir / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        return install_dir
    except OSError:
        return DEFAULT_INSTALL_DIR


def find_server_exe(directory: Path) -> Path | None:
    """在目录里找服务端可执行入口（优先根目录 exe/bat/cmd，再递归找 exe）。"""
    for pat in ("*.exe", "*.bat", "*.cmd"):
        hits = list(directory.glob(pat))
        if hits:
            return hits[0]
    for exe in directory.rglob("*.exe"):
        return exe
    return None


def _resolve_startup_dir(install_dir: Path) -> Path:
    """启动用目录解析，优先级：记忆目录 → 预装目录 → 默认目录。

    命中即把日志/状态文件一并落在该目录——首条日志就与实际安装目录一致
    （此前先落默认目录、进入流程后再切，日志里先后出现两个目录，让人误读）。
    连默认目录都建不出来时向上抛（无路可退）。
    """
    remembered = _load_chosen_server_dir()
    if remembered is not None:
        try:
            probe = remembered / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
            print(f"提示: 使用上次选择的服务端目录: {remembered}")
            return remembered
        except OSError:
            print(f"提示: 上次选择的服务端目录不可写: {remembered}，改用其他目录")

    if install_dir.is_dir():
        writable = False
        try:
            probe = install_dir / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
            writable = True
        except OSError:
            writable = False
        if writable and find_server_exe(install_dir) is not None:
            return install_dir   # 预装目录里已有解压好的服务端：预装目录生效

    print(f"提示: 安装目录 {install_dir} 里没有解压好的服务端（或不可写），"
          f"服务将安装/使用默认目录: {DEFAULT_INSTALL_DIR}")
    DEFAULT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_INSTALL_DIR


def _filename_from_url(url: str) -> str:
    """从下载 URL 推断文件名。

    - ModelScope：文件名在 query 的 FilePath 参数里（如 FilePath=GameAssistant.zip）。
      新布局直链 FilePath=bin%2Fwindows%2FGameAssistantToolServer.7z 是带目录的
      相对路径，只取最后一段——否则会把 bin/windows/ 当相对路径拼进下载目录。
    - GitHub 等：文件名是路径最后一段（如 .../GameAssistantToolServer.7z）
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "FilePath" in qs:
        name = Path(unquote(qs["FilePath"][0])).name
        return name or "service_package"
    name = unquote(Path(parsed.path).name)
    return name or "service_package"


def _acquire_single_instance_lock(install_dir: Path):
    """获取单实例文件锁，防止两个 deploy.py 同时部署同一个 install_dir。

    锁放在 install_dir 下（调用前 install_dir 须已由 setup_logger 创建）。
    返回锁文件句柄（进程存活期间保持打开）；若已有实例在运行则返回 None。
    进程退出（含被 kill）时句柄自动关闭、锁自动释放，无需手动清理。
    """
    lock_file = install_dir / ".deploy.lock"
    f = open(lock_file, "a+")
    f.seek(0)  # 锁定文件开头 1 字节，保证所有实例锁同一位置
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None

# ═══════════════════════════════════════════════════════════════════════════
# 模型配置 — 来源:
#   https://github.com/GameTechDev/IntelAIGamingAssistantLibrary/blob/main/
#   demo/dowload_models/download_modelscope_models.py
#
# COMMON_MODELS: 8 类通用模型
# SPLITTER_MODELS: 分词模型（按语言区分，默认 zh）
# ═══════════════════════════════════════════════════════════════════════════
_COMMON = {
    "LLM":       ("DeviLeo/Qwen3-4B-int4-ov",
                   ["models", "llm"]),
    "Embedding": ("DeviLeo/bge-m3-int4-sym-ov",
                   ["models", "emb"]),
    "Rerank":    ("DeviLeo/bge-reranker-v2-m3-int4-sym-ov",
                   ["models", "rerank"]),
    "MMR":       ("DeviLeo/gme-Qwen2-VL-2B-Instruct-int4-sym-ov",
                   ["models", "mmr", "gme"]),
    "ASR":       ("DeviLeo/FunASR-ov",
                   ["models", "asr"]),
    "OCR":       ("DeviLeo/PaddleOCR-ov",
                   ["models", "ocr"]),
    "Action":    ("DeviLeo/BossActionRecognition",
                   ["models", "bar"]),
    "VLM":       ("DeviLeo/Qwen3.5-4B-int4-ov",
                   ["models", "vlm"]),
}

_SPLITTERS = {
    "zh": ("DeviLeo/zh_core_web_sm-3.8.0", ["models", "splitter"]),
    "en": ("DeviLeo/en_core_web_sm-3.8.0", ["models", "splitter"]),
}

# 重试参数
MAX_RETRIES = 6
RETRY_DELAY = 5          # 秒

# 下载参数
CHUNK_SIZE = 1024 * 1024  # 1 MB

# 健康检查参数
HEALTH_CHECK_TIMEOUT = 300  # 最多等 300 秒
HEALTH_CHECK_INTERVAL = 5    # 每 5 秒检查一次

# 状态文件名
STATE_FILE = ".deploy_state.json"


def build_models_config(lang: str = "zh") -> dict:
    """组装完整的模型配置字典。"""
    if lang not in _SPLITTERS:
        raise ValueError(f"不支持的语言: {lang}，可选: {list(_SPLITTERS.keys())}")

    models = {}
    for name, (model_id, dir_parts) in _COMMON.items():
        models[name] = {"model_id": model_id, "dir": Path(*dir_parts)}

    splitter_id, splitter_dirs = _SPLITTERS[lang]
    models["Splitter"] = {"model_id": splitter_id, "dir": Path(*splitter_dirs)}

    return models


def parse_models(raw: str) -> list[str]:
    """解析 --models 参数（逗号分隔模型名列表），校验模型名合法性。"""
    names = [s.strip() for s in raw.split(",") if s.strip()]
    valid = list(build_models_config("zh").keys())  # LLM/Embedding/Rerank/MMR/ASR/OCR/Action/VLM/Splitter
    invalid = [n for n in names if n not in valid]
    if invalid:
        print(f"\n  无效模型名: {', '.join(invalid)}")
        print(f"  可选模型: {', '.join(valid)}\n")
        sys.exit(1)
    return names


# ============================================================================
# Section 2: Logger
# ============================================================================

def setup_logger(log_dir: Path) -> logging.Logger:
    """双输出 Logger：控制台（简洁） + 文件（完整含时间戳）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "log" / "deploy" / "deploy.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("deploy")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # 控制台 — INFO+
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("  %(message)s"))
    logger.addHandler(console)

    # 文件 — DEBUG+，含时间
    file_handler = logging.FileHandler(str(log_file), mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(file_handler)

    # 将 ModelScope SDK 的日志也写入同一文件
    ms_logger = logging.getLogger("modelscope")
    ms_logger.setLevel(logging.DEBUG)
    ms_logger.addHandler(file_handler)

    logger.info(f"日志文件: {log_file}")
    return logger


# ── stderr → logger 拦截（捕获 tqdm 进度条） ────────────────

class _StderrToLogger:
    """将 sys.stderr 输出转发到 logger（用于捕获 tqdm 进度条）。

    tqdm 写 stderr 的规律：每次更新以 \\r 开头原地刷新，下载完成以 \\n 结尾。
    这里把 \\r 与 \\n 都当作帧边界，每个完结的帧序列只保留最后一帧——否则
    多模型并行下载时（_download_models 用 ThreadPoolExecutor 各起一个 tqdm）
    无锁的缓冲拼接会把不同模型的进度条帧交错成乱码刷进日志/进度窗口。

    两道清洗，保证日志/进度窗口里没有"显示不了的字符"：
      - 剥离 ANSI 转义序列（ModelScope 的多行动态条会写 ``ESC[A`` 光标上移，
        ESC 不可显示，落盘后就是一串 "[A[A" 乱码）；
      - 丢弃单文件 tqdm 条帧（``NN%|...| ... [...it/s]``）——每文件从 0 涨到
        100 的原始条对人是噪声，聚合进度由 emit_model_progress 按秒级节流
        写入日志；非进度类的 stderr 行（警告等）照常透传。
    """

    MIN_FRAME_INTERVAL = 1.0  # 刷新中的进度帧入日志的最小间隔（秒）

    _ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\x1b")
    # tqdm 条帧特征：NN%|图形| 已传/总量 [00:00<00:00, N it/s]
    _TQDM_BAR_RE = re.compile(r"\d+%\|[^|]*\|\s*\S+/\S+\s*\[[^\]]*\]\s*$")

    def __init__(self, logger: logging.Logger, original_stderr):
        self._log = logger
        self._orig = original_stderr
        self._lock = threading.Lock()
        self._buf = ""
        self._last_frame_log = 0.0

    def _clean(self, frame: str) -> str:
        """剥离 ANSI 转义；单文件 tqdm 条帧返回空串（不入日志）。"""
        frame = self._ANSI_RE.sub("", frame).strip()
        if not frame:
            return ""
        if self._TQDM_BAR_RE.search(frame):
            return ""  # 单文件条帧：由聚合进度日志代替
        return frame

    def write(self, s: str):
        self._orig.write(s)
        self._orig.flush()
        with self._lock:
            self._buf += s
            frames = re.split(r"[\r\n]+", self._buf)
            self._buf = frames.pop()  # 最后一段可能是未写完的帧，留待下次拼接
            done = [c for c in (self._clean(f) for f in frames) if c]
            if not done:
                return
            if s.endswith("\n"):
                # \n 结尾的是稳定行（tqdm 终帧 / 正常输出），逐行必记
                self._last_frame_log = time.monotonic()
                for frame in done:
                    self._log.debug(frame)
            elif time.monotonic() - self._last_frame_log >= self.MIN_FRAME_INTERVAL:
                # \r 原地刷新帧只留最新一帧并按秒节流，避免进度刷屏
                self._last_frame_log = time.monotonic()
                self._log.debug(done[-1])

    def flush(self):
        self._orig.flush()
        # 不把未完结的帧写入日志：下一帧到达时自然接上（见 write）


class _ModelsByteProgress:
    """聚合各模型下载的真实字节进度（配合 ModelScope progress_callbacks）。

    ModelScope 1.x 回调协议：每个文件实例化一次 ``cls(filename, file_size)``，
    随后多次 ``update(增量字节)``（大文件分片下载时多线程并发调用）、结束
    ``end()``。这里按模型聚合字节增量，让进度条按真实下载字节推进：
    多模型之间按各自仓库大小加权，而不是"每完成一个模型跳一格"。

    重试续传时 ModelScope 会为同一文件再次实例化回调并重报已有字节，
    因此按"模型/文件"逐文件累计，同名文件的新实例把自己的累计清零
    （@retry 串行重试：旧实例已死，旧数据作废）。on_change 在每次字节
    更新后触发（节流由使用方负责），聚合在锁内、on_change 在锁外调用。
    phase_frac 的加权：仓库大小已知的模型按字节加权；大小未知/为 0 的
    模型给最小权重（它们的真实体积本就接近 0），避免触发"全部等权"
    把大模型的进度稀释成跳格。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._files: dict[str, int] = {}  # "模型/文件" -> 最新实例已累计字节
        self._sizes: dict[str, int] = {}  # "模型/文件" -> 文件大小
        self.on_change = None             # callable() | None

    def callback_cls(self, name: str):
        """生成指定模型的回调类（ModelScope 每个文件实例化一次）。"""
        agg = self

        class _ModelFileCallback:
            def __init__(self, filename: str, file_size: int):
                self._key = f"{name}/{filename}"
                with agg._lock:
                    agg._files[self._key] = 0  # 重试的新实例从 0 重新累计
                    agg._sizes[self._key] = max(int(file_size), 0)
                agg._touch()

            def update(self, size: int) -> None:
                if size <= 0:
                    return
                with agg._lock:
                    agg._files[self._key] = agg._files.get(self._key, 0) + size
                agg._touch()

            def end(self) -> None:
                pass

        return _ModelFileCallback

    def _touch(self):
        notify = self.on_change
        if notify:
            try:
                notify()
            except Exception:
                pass  # 进度事件失败不能影响下载本身

    def _model_bytes(self, name: str) -> tuple[int, int]:
        """单模型 (已累计字节, 回调见到的文件大小合计)。"""
        prefix = name + "/"
        done = discovered = 0
        with self._lock:
            for key, val in self._files.items():
                if key.startswith(prefix):
                    done += val
                    discovered += self._sizes.get(key, 0)
        return done, discovered

    def model_frac(self, name: str, total_hint: int = 0) -> tuple[float, int]:
        """单模型 (0..1 进度, 总字节)。total_hint 是 API 查到的仓库大小，
        查不到时用回调已见到的文件大小合计兜底。"""
        done, discovered = self._model_bytes(name)
        total = total_hint or discovered
        if total > 0:
            return min(done / total, 1.0), total
        return 0.0, 0

    def phase_frac(self, names, totals: dict,
                   done: set | None = None) -> float:
        """模型阶段整体进度 0..1：按仓库字节大小加权（大模型的进度条份额
        与它的体积一致）；大小未知/为 0 的模型给最小权重 1 字节——既不
        稀释大模型，完成后也照常记满格。"""
        if not names:
            return 0.0
        done = done or set()
        num = den = 0.0
        for n in names:
            frac = 1.0 if n in done else self.model_frac(n, totals.get(n, 0))[0]
            weight = totals.get(n, 0) or 1
            num += weight * frac
            den += weight
        return num / den if den else 0.0

    def byte_summary(self, names, totals: dict,
                     done: set | None = None) -> tuple[int, int]:
        """(已下载字节, 总字节)，按仓库大小钳制，用于展示 MB/GB 数。"""
        done = done or set()
        done_all = total_all = 0
        for n in names:
            got, discovered = self._model_bytes(n)
            total = totals.get(n, 0) or discovered
            total_all += total
            if n in done:
                done_all += total
            else:
                done_all += min(got, total) if total > 0 else got
        return done_all, total_all


# ============================================================================
# Section 3: State Manager (JSON + Windows 文件锁)
# ============================================================================

class DeployState:
    """部署状态持久化。使用 msvcrt 文件锁防止并发写冲突。"""

    def __init__(self, install_dir: Path):
        self._path = install_dir / STATE_FILE
        self._defaults = {
            "service_installed": False,
            "models_downloaded": {},
            "service_running": False,
            "last_check": None,
            # ── 版本/更新记录（_load 是 dict.update 合并，旧状态文件缺键自动补默认） ──
            "service_sha256": None,        # 已装服务端对应的远端版本标记（见 _probe_service_remote）
            "service_layout": None,        # new | legacy（诊断用）
            "service_source": None,        # 下载来源渠道（诊断用）
            "service_version_label": None,  # 如 v1.1.1（弹窗展示用）
            "models_script_sha256": None,  # 远端清单脚本 Sha256（模型配置变化信号）
            "models_manifest": {},         # 上次成功解析的清单（解析失败时降级复用）
            "models_fingerprint": {},      # name -> 仓库内容指纹（更新候选判定）
            "skipped_update": None,        # 用户拒绝过的版本快照（远端再变才再问）
            "update_commit_inflight": None,  # 提交中的快照（Stage B 中途失败的自愈标记）
            "update_checked_at": None,
        }
        self._data = dict(self._defaults)
        self._load()

    # ── 文件锁 ──

    @staticmethod
    def _lock(f):
        if sys.platform != "win32":
            return
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        except Exception:
            pass

    # ── 持久化 ──

    def _load(self):
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._lock(f)
                self._data.update(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            logging.getLogger("deploy").warning(f"状态文件读取失败，使用默认值: {e}")

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data["last_check"] = datetime.now().isoformat()
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        tmp.replace(self._path)  # 原子替换

    # ── 查询 ──

    @property
    def service_installed(self) -> bool:
        return bool(self._data.get("service_installed"))

    def is_model_downloaded(self, name: str) -> bool:
        return bool(self._data.get("models_downloaded", {}).get(name))

    @property
    def service_running(self) -> bool:
        return bool(self._data.get("service_running"))

    # ── 标记 ──

    def mark_service_installed(self):
        self._data["service_installed"] = True
        self._save()

    def mark_model_downloaded(self, name: str):
        self._data.setdefault("models_downloaded", {})[name] = True
        self._save()

    def mark_service_running(self, running: bool = True):
        self._data["service_running"] = running
        self._save()

    # ── 版本/更新记录 ──

    @property
    def service_sha256(self) -> str | None:
        """已安装服务端的版本标记（无记录为 None）。"""
        return self._data.get("service_sha256")

    def mark_service_version(self, sha256: str, layout: str = "",
                             source: str = "", version_label: str = ""):
        """记录已安装服务端的版本标记（与 _probe_service_remote 同源可比）。"""
        self._data["service_sha256"] = sha256
        self._data["service_layout"] = layout
        self._data["service_source"] = source
        self._data["service_version_label"] = version_label
        self._save()

    def mark_models_fingerprint(self, fingerprint: dict):
        self._data["models_fingerprint"] = fingerprint
        self._save()

    def mark_manifest(self, manifest: dict, script_sha256: str):
        """缓存远端清单解析结果（解析失败时降级复用）与脚本哈希。"""
        self._data["models_manifest"] = manifest
        self._data["models_script_sha256"] = script_sha256
        self._save()

    def mark_skipped(self, snapshot: dict):
        """记录用户拒绝的版本快照：远端与快照一致期间不再询问。"""
        snapshot = dict(snapshot)
        snapshot["skipped_at"] = datetime.now().isoformat()
        self._data["skipped_update"] = snapshot
        self._save()

    def clear_skipped(self):
        self._data["skipped_update"] = None
        self._save()

    def is_skipped(self, snapshot: dict) -> bool:
        skipped = self._data.get("skipped_update")
        if not skipped:
            return False
        return ({k: v for k, v in skipped.items() if k != "skipped_at"}
                == snapshot)

    def set_inflight(self, snapshot: dict):
        """标记更新提交进行中（中途失败时下次启动可识别并重做）。"""
        self._data["update_commit_inflight"] = snapshot
        self._save()

    def clear_inflight(self):
        self._data["update_commit_inflight"] = None
        self._save()

    def reset(self):
        self._data = dict(self._defaults)
        if self._path.exists():
            self._path.unlink()


# ============================================================================
# Section 4: Utilities
# ============================================================================

def check_dependencies():
    """启动时检查第三方依赖。"""
    missing = []
    for lib in ["requests", "py7zr", "modelscope"]:
        try:
            __import__(lib)
        except ImportError:
            missing.append(lib)
    if missing:
        print(f"\n{'='*60}")
        print(f"  缺少依赖: {', '.join(missing)}")
        print(f"  请运行: pip install {' '.join(missing)}")
        print(f"{'='*60}\n")
        sys.exit(1)


def retry(func):
    """装饰器：失败自动重试 MAX_RETRIES 次，间隔 RETRY_DELAY 秒。"""
    def wrapper(*args, **kwargs):
        log = logging.getLogger("deploy")
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    log.warning(f"[重试 {attempt}/{MAX_RETRIES}] {e}")
                    log.info(f"   {RETRY_DELAY}s 后重试...")
                    time.sleep(RETRY_DELAY)
        log.error(f"[重试耗尽] 连续 {MAX_RETRIES} 次失败")
        raise last_err
    return wrapper


def is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 连接探测端口。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def service_ports() -> tuple[int, int]:
    """服务端可能监听的端口：新版默认在前，旧版（已发布部署）在后。"""
    return (SERVICE_PORT, SERVICE_PORT_LEGACY)


def resolve_service_port() -> int:
    """服务端实际监听端口：按 service_ports() 顺序找第一个开着的；
    都没开返回新默认（让后续请求自然报连接错误）。"""
    for port in service_ports():
        if is_port_open(SERVICE_HOST, port):
            return port
    return SERVICE_PORT


# ============================================================================
# Section 4b: 远端版本探测辅助（服务端/模型更新检测）
# ============================================================================

def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算文件 SHA256。"""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _repo_base() -> str:
    """仓库 API 的基地址（跟随 REPO_API，env 覆盖后 API 与下载同指一处）。"""
    if REPO_API.endswith("/repo/files"):
        return REPO_API[: -len("/repo/files")]
    return f"https://modelscope.cn/api/v1/models/{REPO_ID}"


def _repo_file_url(path: str) -> str:
    """仓库内文件 path 的 ModelScope 下载直链。"""
    return (f"{_repo_base()}/repo?Revision=master"
            f"&FilePath={quote(path, safe='')}")


def _manifest_url() -> str:
    """远端模型清单脚本的下载直链。"""
    return _repo_file_url(MANIFEST_SCRIPT)


def list_repo_files(repo_id: str, root: str = "", recursive: bool = False,
                    timeout: float = 15.0) -> list:
    """查询 ModelScope 仓库文件清单（含 Path/Size/Sha256/Type）。

    网络/HTTP/解析任何失败一律返回 []（不抛）：更新检测不许打断启动流程。
    """
    import requests
    api = (REPO_API if repo_id == REPO_ID else
           f"https://modelscope.cn/api/v1/models/{repo_id}/repo/files")
    try:
        resp = requests.get(
            api,
            params={"Revision": "master", "Root": root,
                    "Recursive": str(bool(recursive)).lower()},
            timeout=timeout,
        )
        resp.raise_for_status()
        files = resp.json().get("Data", [])  # Data 可能是 {"Files":[...]} 或裸 list
        if isinstance(files, dict):
            files = files.get("Files") or []
        return [f for f in files if isinstance(f, dict)]
    except Exception:
        return []


def repo_entry(entries: list, path: str):
    """从清单里取 Type 非 tree 且 Path 精确匹配的条目；没有返回 None。"""
    for e in entries:
        if (e.get("Path") or e.get("path")) == path \
                and (e.get("Type") or e.get("type") or "blob") != "tree":
            return e
    return None


# files API 条目里压缩包时间的候选字段。实测（OpenVINO/GameAssist 真实响应）
# 时间字段是 `CommittedDate`——unix 秒时间戳，即该压缩包的提交/上传时间；
# 条目完整字段为 CommitMessage/CommittedDate/CommitterName/InCheck/IsLFS/
# Mode/Name/Path/Revision/Sha256/Size/Type，没有 LastModified 系字段。
# 其余候选保留作 API 变体兜底。
_REMOTE_TIME_FIELDS = ("CommittedDate", "committed_date",
                       "LastModified", "LastModifiedDate", "LastModifiedTime",
                       "last_modified", "GmtModified", "gmt_modified",
                       "LastUpdatedDate", "UpdatedAt", "updated_at",
                       "UpdateTime", "update_time")


def _parse_remote_time(value) -> float | None:
    """解析远端时间值为 epoch 秒：RFC3339/ISO8601、HTTP 日期、unix 秒或毫秒。"""
    if isinstance(value, (int, float)) and value > 0:
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.replace(".", "").isdigit():
        ts = float(s)
        return ts / 1000.0 if ts > 1e12 else ts
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)   # HTTP 日期（HEAD Last-Modified 用）
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _entry_remote_mtime(entry: dict) -> float | None:
    """从 files API 条目里解析压缩包时间（epoch 秒）；没有可靠字段返回 None。"""
    for key in _REMOTE_TIME_FIELDS:
        ts = _parse_remote_time(entry.get(key))
        if ts is not None:
            return ts
    logging.getLogger("deploy").debug(
        f"远端条目无可识别的时间字段，实际字段: {sorted(entry.keys())}")
    return None


def fingerprint_files(entries: list) -> str:
    """仓库内容指纹：对排序后的 "Path:Sha256" 清单做 sha256。

    条目缺 Sha256 时退化用 Path+Size（旧 API 也能感知内容量变化）；
    条目数达到 files API 截断阈值时混入截断标记——截断清单的指纹可能
    误报更新（宁可误报不可漏报）。
    """
    import hashlib
    lines = sorted(
        f"{e.get('Path') or e.get('path')}:"
        f"{e.get('Sha256') or e.get('sha256') or e.get('Size') or e.get('size') or 0}"
        for e in entries
        if (e.get("Type") or e.get("type") or "blob") != "tree")
    total = sum(int(e.get("Size") or e.get("size") or 0) for e in entries)
    h = hashlib.sha256()
    h.update("\n".join(lines).encode("utf-8", "replace"))
    h.update(f"|files={len(lines)}|bytes={total}".encode())
    if len(entries) >= REPO_FILES_TRUNCATION_LIMIT:
        h.update(b"|truncated")
    return h.hexdigest()


def _find_inner_7z(names: list) -> str:
    """zip 内层服务包路径：新布局精确命中 → 旧布局取版本号最大的 → 任意层级同名兜底。

    旧布局（bin/vX.Y.Z/windows/...）按 int 元组比较版本，保证 v1.10 > v1.9。
    """
    if SERVICE_7Z_NEW in names:
        return SERVICE_7Z_NEW
    legacy = []
    for n in names:
        m = SERVICE_7Z_LEGACY_RE.match(n)
        if m:
            legacy.append((tuple(int(x) for x in m.group(1).split(".")), n))
    if legacy:
        return max(legacy)[1]
    loose = [n for n in names
             if n == INNER_7Z_NAME or n.endswith("/" + INNER_7Z_NAME)]
    if len(loose) == 1:
        return loose[0]
    raise RuntimeError(
        f"压缩包内未找到 {INNER_7Z_NAME}（实际条目样例: {names[:20]}）")


def parse_model_manifest(source: str, lang: str = "zh"):
    """ast 静态解析 download_modelscope_models.py 的模型清单（不执行代码）。

    只认一种形态：模块级 NAME = {"名字": ("仓库id", ["models","llm"]), ...}，
    变量名含 COMMON/SPLIT 归类（都对不上时接受条目数 ≥4 且形态匹配的 dict）。
    返回 {名字: [仓库id, 目录段...]}（splitter 按 lang 合并为 "Splitter" 键）；
    解析失败/结构不认识 → None（调用方降级到上次缓存或内置清单）。
    """
    import ast
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    def _parse_dict(node):
        if not isinstance(node, ast.Dict):
            return None
        out = {}
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None
            if not isinstance(v, (ast.Tuple, ast.List)) or len(v.elts) != 2:
                return None
            rid, dirs = v.elts
            if not (isinstance(rid, ast.Constant) and isinstance(rid.value, str)
                    and "/" in rid.value):
                return None
            if not (isinstance(dirs, (ast.List, ast.Tuple)) and dirs.elts
                    and all(isinstance(d, ast.Constant)
                            and isinstance(d.value, str) for d in dirs.elts)):
                return None
            out[k.value] = [rid.value] + [d.value for d in dirs.elts]
        return out or None

    common = split = None
    fallback = None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        parsed = _parse_dict(node.value)
        if not parsed:
            continue
        upper = (getattr(target, "id", "") or "").upper()
        if "COMMON" in upper:
            common = parsed
        elif "SPLIT" in upper:
            split = parsed
        elif fallback is None and len(parsed) >= 4:
            fallback = parsed

    if common is None and fallback is not None:
        common = fallback
    if not common:
        return None

    manifest = {k: list(v) for k, v in common.items()}
    if split and split.get(lang):
        manifest["Splitter"] = list(split[lang])
    return manifest


def github_latest_asset(timeout: float = 15.0):
    """查询 GitHub 最新 release 的服务端 7z 资产（兜底下载渠道）。

    返回 {url, tag, sha256, size}；sha256 取 GitHub 官方 digest（可能为 None，
    此时下载后仅做压缩包校验）。任何失败返回 None——GitHub 仅是兜底渠道，
    API 不可达就直接放弃，绝不阻塞主流程。
    """
    import requests
    try:
        resp = requests.get(
            GITHUB_RELEASES_API, timeout=timeout,
            headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
        for asset in data.get("assets", []):
            name = (asset.get("name") or "").lower()
            if "gameassistanttoolserver" in name and name.endswith(".7z"):
                digest = asset.get("digest") or ""
                sha = (digest.split(":", 1)[1]
                       if digest.startswith("sha256:") else (digest or None))
                return {
                    "url": asset.get("browser_download_url"),
                    "tag": data.get("tag_name") or "",
                    "sha256": sha,
                    "size": int(asset.get("size") or 0),
                    "mtime": _parse_remote_time(
                        data.get("published_at") or data.get("updated_at")),
                }
    except Exception:
        pass
    return None


# ============================================================================
# Section 5: Service Package Downloader
# ============================================================================

class PackageDownloader:
    """服务包下载：断点续传 + 重试 + 大小校验。"""

    def __init__(self, log: logging.Logger):
        self.log = log

    def _remote_size(self, url: str) -> int | None:
        """HEAD 请求获取 Content-Length。"""
        import requests
        try:
            resp = requests.head(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
        except Exception as e:
            self.log.warning(f"无法获取远程文件大小: {e}")
            return None

    @retry
    def download(self, url: str, dest: Path, progress_cb=None, verify_cb=None,
                 sha256: str | None = None) -> Path:
        """
        下载文件到 dest，支持断点续传。
        下载先落到 dest + ".part" 临时文件，完成大小校验后原子改名到 dest，
        保证 dest 要么是完整产物、要么不存在，不会被半成品污染。
        verify_cb(dest) 若提供，则在改名后做内容校验（如 zip CRC）；返回 False
        视为下载产物损坏，删除 dest 并抛异常触发 @retry 重新下载。
        sha256 若提供（远端官方哈希），改名后做哈希比对，不符同样删除重下；
        已存在的本地文件跳过下载前也会先比哈希，防止「大小对、内容坏」被复用。
        progress_cb(downloaded_bytes, total_bytes, speed_bytes_per_sec)
        """
        import requests

        dest.parent.mkdir(parents=True, exist_ok=True)
        self.log.info(f"下载: {url}")
        self.log.info(f"目标: {dest}")

        remote_size = self._remote_size(url)
        part = dest.with_name(dest.name + ".part")

        # 目标文件已存在且大小匹配 → 跳过；但若提供 verify_cb，先做内容校验，
        # 防止「大小对、内容坏」的历史坏文件被跳过卡死。
        if dest.exists():
            local_size = dest.stat().st_size
            if remote_size and local_size == remote_size:
                if verify_cb is not None:
                    try:
                        ok = verify_cb(dest)
                    except Exception:
                        ok = False
                    if ok and sha256 and sha256_file(dest) != sha256:
                        self.log.warning("本地文件哈希与远端记录不一致，删除重新下载")
                        ok = False
                    if ok:
                        self.log.info("文件已完整，跳过下载")
                        if progress_cb:
                            progress_cb(remote_size, remote_size, 0)
                        return dest
                    self.log.warning("本地文件大小匹配但内容校验失败，删除重新下载")
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    # 不 return，继续走下面的下载流程
                else:
                    self.log.info("文件已完整，跳过下载")
                    if progress_cb:
                        progress_cb(remote_size, remote_size, 0)
                    return dest

        # 下载/续传统一落到 part，半成品不占用正式文件名
        downloaded = 0
        mode = "wb"
        if part.exists():
            local_size = part.stat().st_size
            if remote_size and local_size < remote_size:
                self.log.info(f"断点续传: 已有 {local_size / (1024*1024):.1f} MB")
                downloaded = local_size
                mode = "ab"
            else:
                self.log.info("本地临时文件不完整/异常，重新下载")
                part.unlink()

        headers = {}
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"

        start_time = time.time()
        last_tick = time.time()
        total_dl = downloaded

        with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
            cr = resp.headers.get("Content-Range", "")

            # 处理断点续传响应。标准实现返回 206；个别 CDN/网关（如 ModelScope）
            # 返回 200 但同样按 Range 给出部分内容（带 Content-Range 头）。
            # 只有确认服务器真的接受了 Range 才继续 "ab" 追加；否则服务器忽略
            # Range 返回完整文件，仍以 "ab" 追加会拼出一个坏文件，须改回 "wb"。
            if downloaded > 0:
                resume_ok = resp.status_code == 206
                if resp.status_code == 200 and cr:
                    # 非标准 200 + Content-Range：校验起始字节是否等于已下载量
                    try:
                        resume_ok = int(cr.split(" ")[1].split("-")[0]) == downloaded
                    except (IndexError, ValueError):
                        resume_ok = False
                if not resume_ok:
                    self.log.warning(f"服务器不支持续传 (HTTP {resp.status_code})，重新下载")
                    downloaded = 0
                    total_dl = 0
                    mode = "wb"

            # 从响应中推断总大小
            expected = remote_size
            if cr and "/" in cr:
                expected = int(cr.split("/")[-1])
            if not expected:
                # HEAD 失败时的兜底：GET 响应自带的 Content-Length。
                # 续传被接受时它只是剩余字节数，要加回已下载的部分；
                # 拿不到总大小就只能让百分比钉在已下载量上（罕见：分块传输）。
                try:
                    cl = int(resp.headers.get("Content-Length", "") or 0)
                except ValueError:
                    cl = 0
                if cl > 0:
                    expected = cl + (downloaded if downloaded > 0 and resume_ok
                                     else 0)

            # 下载开始前先发一次进度，让 GUI 立刻从「准备中」切换到下载进度，
            # 避免慢网络下首个 chunk 迟迟不来导致的长静默
            if progress_cb:
                progress_cb(total_dl, expected or total_dl, 0)

            with open(part, mode) as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total_dl += len(chunk)
                    now = time.time()
                    if progress_cb and (now - last_tick) > 0.3:
                        elapsed = now - start_time
                        speed = total_dl / elapsed if elapsed > 0 else 0
                        progress_cb(total_dl, expected or total_dl, speed)
                        last_tick = now

        # 末次回调
        elapsed = time.time() - start_time
        speed = total_dl / elapsed if elapsed > 0 else 0
        if progress_cb:
            progress_cb(total_dl, expected or total_dl, speed)

        # 校验
        actual = part.stat().st_size
        if expected and actual != expected:
            raise IOError(f"文件大小校验失败: 期望 {expected}B, 实际 {actual}B")
        if actual == 0:
            raise IOError("下载文件为空")

        # 原子改名到正式文件名
        os.replace(part, dest)

        # 远端哈希校验：不匹配视为下载损坏，删除坏文件并抛异常，交由 @retry 重新下载
        if sha256 and sha256_file(dest) != sha256:
            try:
                dest.unlink()
            except OSError:
                pass
            raise IOError("下载内容 SHA256 与远端不一致，将重新下载")

        # 内容校验（如 zip CRC）：失败则删除坏文件并抛异常，交由 @retry 重新下载
        if verify_cb is not None:
            self.log.info("校验下载文件完整性...")
            try:
                ok = verify_cb(dest)
            except Exception:
                ok = False
            if not ok:
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise IOError("下载内容校验失败，已删除坏文件，将重新下载")

        self.log.info(f"下载完成 ({actual / (1024*1024):.1f} MB)")
        return dest


# ============================================================================
# Section 6: Installer
# ============================================================================

def _fmt_dur(seconds: float) -> str:
    """秒数 → "MM:SS" / "H:MM:SS"（用于进度显示）。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Installer:
    """7z 解压 + 安装验证。"""

    def __init__(self, log: logging.Logger):
        self.log = log
        # 解压 zip 包时对内层服务包 7z 增量计算的 SHA256（直链 7z 不经此路径，保持 None）
        self.last_inner_sha256: str | None = None

    def verify_archive(self, path: Path) -> bool:
        """校验压缩包完整性（zip → testzip；7z → 列目录）。"""
        if path.suffix.lower() == ".zip":
            import zipfile
            try:
                with zipfile.ZipFile(path, "r") as zf:
                    bad = zf.testzip()
                if bad is not None:
                    self.log.error(f"zip 校验失败（损坏文件）: {bad}")
                    return False
                return True
            except (zipfile.BadZipFile, OSError) as e:
                self.log.error(f"zip 校验失败: {e}")
                return False

        import py7zr
        try:
            with py7zr.SevenZipFile(path, "r") as sz:
                sz.getnames()
            return True
        except Exception as e:
            self.log.error(f"7z 校验失败: {e}")
            return False

    def extract(self, archive: Path, dest: Path, progress_cb=None):
        """解压压缩包到 dest。

        - 7z：将压缩包顶层内容直接搬到 dest（与旧逻辑一致）。
        - zip：只是套壳，内层是 GameAssistantToolServer.7z；取出内层 7z
          后复用 7z 解压流程，最终目录结构与直接解压 7z 一致。

        progress_cb(stage, pct, detail)：解压过程中周期回调，
        pct 为 0~100 的整体进度，detail 携带文件数/耗时等实时信息。
        """
        import shutil

        dest.mkdir(parents=True, exist_ok=True)
        self.last_inner_sha256 = None
        self.log.info(f"解压 → {dest}")

        # 上次运行可能留下未能删除的临时目录（沙盒禁止脚本删除文件），先尽力清掉。
        # 目录名由 tempfile 随机生成，正常环境不会误伤同名目录。
        for stale in (*dest.parent.glob("_extract_*"), *dest.parent.glob("_unwrap_*")):
            shutil.rmtree(stale, ignore_errors=True)

        if progress_cb:
            progress_cb("解压服务包...", 0, "准备中")

        if archive.suffix.lower() == ".zip":
            self._extract_zip(archive, dest, progress_cb)
        else:
            self._extract_7z(archive, dest, progress_cb)

        if progress_cb:
            progress_cb("解压服务包...", 100, "完成")

        self.log.info("解压完成")

    @staticmethod
    def _entry_size(entry) -> int:
        """单个条目的解压后大小（py7zr 不同版本可能返回 int 或 list）。"""
        size = getattr(entry, "uncompressed", 0)
        if isinstance(size, (list, tuple)):
            return int(sum(size))
        return int(size)

    @staticmethod
    def _scan_extract(tmp_path: Path, order):
        """扫描临时目录并与打包顺序逐项比对。

        返回 (已解压字节数, 已完成文件数, 当前正在解压的文件相对路径)。
        7z 按打包顺序落盘：列表中第一个尚未写够大小的文件即当前文件。
        """
        actual: dict[str, int] = {}
        for root, _dirs, files in os.walk(tmp_path):
            for name in files:
                p = Path(root) / name
                try:
                    rel = p.relative_to(tmp_path).as_posix()
                    actual[rel] = p.stat().st_size
                except OSError:
                    pass  # 文件刚创建/正被写入的瞬时状态，下轮再统计

        done_bytes = 0
        done_files = 0
        current = ""
        for entry in order:
            size = Installer._entry_size(entry)
            have = actual.get(entry.filename)
            if have is None:
                current = entry.filename
                break
            done_bytes += min(have, size)
            if have >= size:
                done_files += 1
            else:
                current = entry.filename
                break
        else:
            if order:
                current = order[-1].filename
        return done_bytes, done_files, current

    def _extract_7z(self, archive: Path, dest: Path,
                    progress_cb=None, lo: float = 0.0, hi: float = 100.0):
        """解压 7z 到临时目录（带实时进度），完成后把内容搬到 dest。

        不用 py7zr 自带的 progress_callback：其签名跨版本不兼容
        （0.x 与 1.x 不同），且大文件时触发稀疏。改为把 extractall
        放到工作线程，本线程每 0.5s 扫描一次临时目录，统计出已解压
        字节/文件数/当前文件，折算百分比与剩余时间。lo/hi 把进度映射
        到整体区间上（外层 zip 取内层占 0~10%，7z 解压占 10~100%）。
        """
        import shutil
        import tempfile
        import py7zr

        with py7zr.SevenZipFile(archive, "r") as sz:
            order = [f for f in sz.files if not f.is_directory]
            total_files = len(order)
            total_bytes = sum(self._entry_size(f) for f in order)
            self.log.info(
                f"  共 {total_files} 个文件, "
                f"{total_bytes / (1024 * 1024):.0f} MB (解压后)"
            )

            # mkdtemp + finally 手动清理（而非 TemporaryDirectory）：Agent 沙盒
            # 可能禁止脚本删除文件，收尾清理失败不该让已完成的解压/安装中途
            # 崩溃——保留临时目录并告警即可，下次运行会先尽力清掉旧目录。
            tmp = tempfile.mkdtemp(prefix="_extract_", dir=str(dest.parent))
            try:
                tmp_path = Path(tmp)

                error: list[Exception] = []

                def _worker():
                    try:
                        sz.extractall(path=tmp)
                    except Exception as e:
                        error.append(e)  # 保存起来，join 后在调用线程重抛

                worker = threading.Thread(
                    target=_worker, name="7z-extract", daemon=True)
                start = time.monotonic()
                worker.start()

                reported = -100.0
                milestone = 0  # 每 10% 记一条日志（跳过 0%：此时 ETA 还算不出来）
                while True:
                    done_bytes, done_files, current = self._scan_extract(
                        tmp_path, order)

                    frac = done_bytes / total_bytes if total_bytes else 1.0
                    pct = lo + (hi - lo) * frac
                    elapsed = time.monotonic() - start
                    eta = elapsed * (1 - frac) / frac if frac > 0.001 else 0.0

                    if progress_cb and pct - reported >= 0.2:
                        reported = pct
                        cur = current if len(current) <= 48 else "…" + current[-47:]
                        progress_cb(
                            "解压服务包...", pct,
                            (f"{done_files}/{total_files} 个文件 "
                             f"{done_bytes / (1024*1024):.0f}/"
                             f"{total_bytes / (1024*1024):.0f} MB"
                             f" | 已用 {_fmt_dur(elapsed)}"
                             f" | 剩余 ~{_fmt_dur(eta)} | {cur}"),
                        )

                    if int(pct / 10) > milestone:
                        milestone = int(pct / 10)
                        self.log.info(
                            f"  解压 {pct:.0f}%: "
                            f"{done_files}/{total_files} 个文件, "
                            f"已用 {_fmt_dur(elapsed)}, "
                            f"剩余 ~{_fmt_dur(eta)}, "
                            f"当前: {current}"
                        )

                    if not worker.is_alive():
                        break
                    worker.join(0.5)

                if error:
                    raise error[0]

                if progress_cb:
                    progress_cb("解压服务包...", hi, "安装文件...")

                # ── 将临时目录的内容合并进 dest（目录级覆盖合并）──
                # 真实服务端包是包着一层 GameAssistantToolServer/ 的：整目录
                # rmtree 会把用户数据（saves/models/caches）一起清空，所以
                # 目录级走 _merge_tree 逐项覆盖——同名文件覆盖、dst 多出的
                # （用户数据）保留、caches 子树尽力删除。
                for item in tmp_path.iterdir():
                    target = dest / item.name
                    if target.exists():
                        if item.is_dir() and item.name.lower() in PRESERVE_USER_DIRS:
                            self.log.info(f"  保留用户数据目录: {item.name}/")
                            continue
                        if item.is_dir() and item.name.lower() == "_internal":
                            # 运行时负载：整树替换，不能合并（旧 dll 残留会
                            # 让新版本链接错误崩溃）
                            _replace_dir_fresh(item, target, self.log)
                            continue
                        if item.is_dir():
                            _merge_tree(item, target, self.log)
                            continue
                        try:
                            target.unlink()
                        except OSError as e:
                            raise RuntimeError(
                                f"无法覆盖旧文件 {target}（可能被占用或被沙盒安全策略阻止）: {e}"
                            ) from e
                    shutil.move(str(item), str(target))

                self.log.info(f"  已安装 {total_files} 个文件 → {dest}")
            finally:
                # 删除被拒（沙盒策略）时保留目录，不当作失败
                shutil.rmtree(tmp, ignore_errors=True)
                if Path(tmp).exists():
                    self.log.warning(
                        f"  临时目录未能删除（可能被 Agent 沙盒安全策略阻止），已保留: {tmp}"
                    )

    def _extract_zip(self, archive: Path, dest: Path, progress_cb=None):
        """解压外层 zip：取出内层 GameAssistantToolServer.7z 后复用 7z 解压流程。

        内层路径不写死版本号（新布局 bin/windows/、旧布局 bin/vX.Y.Z/windows/
        均可，见 _find_inner_7z）。取出内层按已写字节数实时回报 0~10% 进度；
        随后的 7z 解压占 10~100%；同时增量计算内层 7z 的 SHA256 存到
        last_inner_sha256（首装/本地包场景记录版本标记用）。
        """
        import zipfile
        import hashlib
        import shutil
        import tempfile

        with zipfile.ZipFile(archive, "r") as zf:
            inner_path = _find_inner_7z(zf.namelist())

            # 内层 7z 较大（~380MB），先落盘再交给原 7z 解压逻辑
            # （mkdtemp + finally：清理被沙盒拒绝时保留目录，见 _extract_7z 同款注释）
            tmp = tempfile.mkdtemp(prefix="_unwrap_", dir=str(dest.parent))
            try:
                inner = Path(tmp) / Path(inner_path).name
                total = zf.getinfo(inner_path).file_size
                copied = 0
                inner_hash = hashlib.sha256()
                start = time.monotonic()
                last_emit = 0.0
                with zf.open(inner_path) as src, open(inner, "wb") as dst:
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                        inner_hash.update(chunk)
                        copied += len(chunk)

                        now = time.monotonic()
                        if progress_cb is None or now - last_emit < 0.3:
                            continue
                        last_emit = now
                        frac = copied / max(total, 1)
                        eta = (now - start) * (1 - frac) / frac if frac > 0 else 0.0
                        progress_cb(
                            "解压服务包...", 10.0 * frac,
                            (f"取出内层 {Path(inner_path).name} "
                             f"{copied / (1024*1024):.0f}/"
                             f"{total / (1024*1024):.0f} MB"
                             f" | 已用 {_fmt_dur(now - start)}"
                             f" | 剩余 ~{_fmt_dur(eta)}"),
                        )
                self.last_inner_sha256 = inner_hash.hexdigest()
                self.log.info(f"  已取出内层服务包: {inner_path}")
                self._extract_7z(inner, dest, progress_cb, lo=10.0, hi=100.0)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
                if Path(tmp).exists():
                    self.log.warning(
                        f"  临时目录未能删除（可能被 Agent 沙盒安全策略阻止），已保留: {tmp}"
                    )

    def find_executable(self, install_dir: Path) -> Path | None:
        """在安装目录查找可执行入口。"""
        return find_server_exe(install_dir)

    def verify(self, install_dir: Path) -> bool:
        exe = self.find_executable(install_dir)
        if exe:
            self.log.info(f"可执行文件: {exe.name}")
            return True
        self.log.warning(f"未找到可执行文件: {install_dir}")
        return False


# ============================================================================
# Section 7: Service Manager
# ============================================================================

class ServiceManager:
    """启动 / 停止 / 健康检查。"""

    def __init__(self, install_dir: Path, log: logging.Logger):
        self.install_dir = install_dir
        self.log = log
        self._proc = None

    def start(self) -> subprocess.Popen | None:
        installer = Installer(self.log)
        exe = installer.find_executable(self.install_dir)
        if not exe:
            self.log.error("找不到可执行文件")
            return None

        self.log.info(f"启动: {exe}")
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self._proc = subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            self.log.info(f"  PID: {self._proc.pid}")
            return self._proc
        except Exception as e:
            self.log.error(f"启动失败: {e}")
            return None

    def wait_healthy(self, host=SERVICE_HOST, ports=service_ports(),
                     timeout=HEALTH_CHECK_TIMEOUT, interval=HEALTH_CHECK_INTERVAL,
                     on_wait=None) -> bool:
        """轮询端口直到就绪或超时（端口探测每 interval 秒一次）。

        on_wait(elapsed, timeout)：每秒回调一次——进度窗口以 1 秒频率刷新
        "已等待 X 秒"，否则最长 300s 的静止画面会被误认为卡死。
        等待过程不写日志（探活本身有首尾两条，过程行只会刷屏）。
        """
        self.log.info(f"等待 {host} 端口 {'/'.join(map(str, ports))} 就绪 (最多 {timeout}s)...")
        start = time.time()
        next_check = 0.0
        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                break
            if elapsed >= next_check:
                next_check = elapsed + interval
                for port in ports:
                    if is_port_open(host, port, timeout=3.0):
                        self.log.info(f"[OK] 就绪 ({elapsed:.1f}s, 端口 {port})")
                        return True
            if on_wait:
                try:
                    on_wait(int(elapsed), timeout)
                except Exception:
                    pass  # 进度刷新失败不能影响健康检查本身
            time.sleep(1.0)
        self.log.error(f"超时 ({timeout}s)")
        return False

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self.log.info("停止服务...")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self.log.info("已停止")


# ============================================================================
# Section 8: Tkinter Dialogs
# ============================================================================

# ── 8a: Font helpers ────────────────────────────────────────

def _pick_font(families: tuple, size: int, root) -> tuple:
    import tkinter.font as tkfont
    available = set(tkfont.families(root))
    for f in families:
        if f in available:
            return f, size
    return families[-1], size

_MONO_FONTS = ("Cascadia Code", "Consolas", "Courier New", "SimSun")
_UI_FONTS   = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimSun")


# ── 8b: Progress Dialog ─────────────────────────────────────

class ProgressDialog:
    """部署进度窗口——参考 show_progress_window.py 风格：
    大窗口、状态标签、进度条、实时日志区、底部提示。"""

    def __init__(self, root: "tk.Tk", log_path: str = "",
                 title: str = "游戏助手服务部署"):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.log_path = log_path

        ui_font   = _pick_font(_UI_FONTS, 10, root)
        mono_font = _pick_font(_MONO_FONTS, 10, root)

        root.title(title)
        root.geometry("860x560")
        root.minsize(640, 400)
        root.configure(bg="#fafafa")
        root.protocol("WM_DELETE_WINDOW", self._on_user_close)

        self._on_close_cb = None   # 外部注入的关闭回调

        # ── 状态标签 ──
        self._status_var = tk.StringVar(value="准备中...")
        status = tk.Label(
            root, textvariable=self._status_var,
            font=ui_font, anchor="w", justify="left",
            wraplength=820, bg="#fafafa", fg="#333333",
        )
        status.place(x=12, y=12, width=820, height=44)

        # ── 进度条 ──
        self._bar = ttk.Progressbar(root, mode="indeterminate", length=820)
        self._bar.place(x=12, y=62, width=820, height=20)
        self._bar.start(30)

        # ── 日志文本框 ──
        log_frame = tk.Frame(root, bg="#fafafa")
        log_frame.place(x=12, y=94, width=820, height=368)

        self._log_box = tk.Text(
            log_frame, font=mono_font, wrap="word",
            state="disabled", relief="sunken", borderwidth=1,
            bg="#ffffff", fg="#333333",
        )
        v_scroll = tk.Scrollbar(
            log_frame, orient="vertical", command=self._log_box.yview,
        )
        self._log_box.configure(yscrollcommand=v_scroll.set)
        v_scroll.pack(side="right", fill="y")
        self._log_box.pack(side="left", fill="both", expand=True)

        self._center(860, 560)
        self._close_at: float | None = None   # 计划关闭时间戳

    def _center(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ── 更新 API ────────────────────────────────────────────

    def update(self, *, stage: str = None, progress: float = None):
        """主线程调用：更新状态文本 & 进度条。"""
        if stage is not None:
            self._status_var.set(stage)
        if progress is not None:
            if self._bar["mode"] != "determinate":
                # __init__ 里 start(30) 注册的自动步进定时器与 mode 无关，
                # 不 stop() 的话它会一直每 30ms step(+1) 并在 100% 处回绕，
                # 进度条就被反复从真实值推高、过百归零再重爬，看起来像
                # "不断从 0 跳到当前下载进度"。切 determinate 前必须停掉。
                self._bar.stop()
                self._bar.configure(mode="determinate")
            self._bar["value"] = progress

    def update_indeterminate(self, stage: str = None):
        """切换到不确定进度（旋转条）。"""
        if stage is not None:
            self._status_var.set(stage)
        if self._bar["mode"] != "indeterminate":
            self._bar.configure(mode="indeterminate")
            self._bar.start(30)

    def refresh_log(self):
        """从日志文件读取尾部并更新文本框。"""
        if not self.log_path:
            return
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return
            tail = "".join(lines[-200:]).rstrip("\n")  # 末尾 200 行
            old = self._log_box.get("1.0", "end-1c")
            if old == tail:
                return
            self._log_box.configure(state="normal")
            self._log_box.delete("1.0", "end")
            self._log_box.insert("1.0", tail)
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        except (OSError, IOError):
            pass

    # ── 完成 / 关闭 ─────────────────────────────────────────

    def schedule_close(self, seconds: int = 8):
        """计划 N 秒后关闭（倒计时显示在 hint 中）。"""
        import time as _time
        self._close_at = _time.monotonic() + seconds

    def tick_close(self) -> bool:
        """每帧调用：更新倒计时，时间到则关闭。返回 True 表示已关闭。"""
        import time as _time
        if self._close_at is None:
            return False

        remaining = max(0, int(self._close_at - _time.monotonic()))
        if remaining > 0:
            self._status_var.set(
                f"部署完成，此窗口将在 {remaining} 秒后自动关闭。"
            )
            return False
        else:
            self.close()
            return True

    def set_close_callback(self, cb):
        """用户点 X 时回调。"""
        self._on_close_cb = cb

    def _on_user_close(self):
        """用户主动关闭窗口 → 执行回调 → 强制退出。"""
        if self._on_close_cb:
            self._on_close_cb()
        self.close()
        os._exit(0)

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


# ── 9b: File Picker Dialog ──────────────────────────────────

class FilePickerDialog:
    """
    「未找到游戏助手服务」弹窗。
    白色卡片风格 · 柔和阴影 · 8px 圆角 · 深色模式自适应。
    """

    _LIGHT = {
        "bg": "#f5f5f5", "card": "#ffffff", "text": "#333333",
        "text_sec": "#666666", "link_bg": "#f0f2f5", "link_text": "#0078D4",
        "btn_p": "#0078D4", "btn_p_h": "#106ebe", "btn_p_t": "#ffffff",
        "btn_s": "#e8e8e8", "btn_s_h": "#dcdcdc", "btn_s_t": "#333333",
        "border": "#e0e0e0",
    }
    _DARK = {
        "bg": "#1e1e1e", "card": "#2d2d2d", "text": "#e0e0e0",
        "text_sec": "#a0a0a0", "link_bg": "#383838", "link_text": "#4daafc",
        "btn_p": "#0078D4", "btn_p_h": "#1a8ce8", "btn_p_t": "#ffffff",
        "btn_s": "#3a3a3a", "btn_s_h": "#4a4a4a", "btn_s_t": "#cccccc",
        "border": "#404040",
    }

    def __init__(self, parent: "tk.Tk", download_url: str = SERVICE_URL):
        import tkinter as tk

        self._tk = tk
        self.url = download_url
        self.result: Path | None = None
        self._dark = self._detect_dark()
        self.c = self._DARK if self._dark else self._LIGHT
        self._build(parent)

    @staticmethod
    def _detect_dark() -> bool:
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return val == 0
        except Exception:
            return False

    def _build(self, parent):
        tk = self._tk
        c = self.c

        self.win = tk.Toplevel(parent)
        self.win.title("未找到游戏助手服务")
        self.win.resizable(False, False)
        self.win.configure(bg=c["bg"])

        # 卡片容器
        card = tk.Frame(self.win, bg=c["card"],
                        highlightthickness=1, highlightbackground=c["border"])
        card.pack(padx=1, pady=1, fill="both", expand=True)
        inner = tk.Frame(card, bg=c["card"], padx=24, pady=20)
        inner.pack(fill="both", expand=True)

        # 标题
        tk.Label(inner, text="未找到游戏助手服务",
                 font=("Microsoft YaHei UI", 14, "bold"),
                 fg=c["text"], bg=c["card"]).pack(anchor="w", pady=(0, 10))

        # 说明：首尾两句加粗并对齐，中间两句缩进两个全角字符
        tk.Label(inner,
                 text="【重要】必须先手动下载服务包（约 300 MB）",
                 font=("Microsoft YaHei UI", 11, "bold"),
                 fg=c["text"], bg=c["card"], padx=0, bd=0,
                 justify="left").pack(anchor="w", pady=(0, 4))
        tk.Label(inner,
                 text="　　请优先使用 Chrome 浏览器进行下载。",
                 font=("Microsoft YaHei UI", 10),
                 fg=c["text_sec"], bg=c["card"], padx=0, bd=0,
                 justify="left").pack(anchor="w", pady=(0, 0))
        tk.Label(inner,
                 text="　　Edge 不建议使用，下载完成后可能出现文件扫描耗时过长。",
                 font=("Microsoft YaHei UI", 10),
                 fg=c["text_sec"], bg=c["card"], padx=0, bd=0,
                 justify="left").pack(anchor="w", pady=(0, 4))
        # 末行：仅"选择文件"四个字加粗（tkinter 单 Label 无法局部加粗，用并排两个 Label）
        _tail = tk.Frame(inner, bg=c["card"], bd=0, highlightthickness=0)
        tk.Label(_tail,
                 text="下载完毕后，请点击下方按钮",
                 font=("Microsoft YaHei UI", 11),
                 fg=c["text"], bg=c["card"], padx=0, bd=0).pack(side="left")
        tk.Label(_tail,
                 text="选择文件",
                 font=("Microsoft YaHei UI", 11, "bold"),
                 fg=c["text"], bg=c["card"], padx=0, bd=0).pack(side="left")
        _tail.pack(anchor="w", pady=(0, 10))

        # ── 下载链接 ──
        lf = tk.Frame(inner, bg=c["link_bg"], padx=12, pady=10)
        lf.pack(fill="x", pady=(0, 16))

        tk.Label(lf, text="📥 下载地址（点击可选中复制）：",
                 font=("Microsoft YaHei UI", 9),
                 fg=c["text_sec"], bg=c["link_bg"]).pack(anchor="w")

        self._link = tk.Text(
            lf, height=2, width=42, wrap="word",
            font=("Consolas", 9), bg=c["card"], fg=c["link_text"],
            bd=1, relief="solid", padx=6, pady=4, cursor="hand2",
        )
        self._link.insert("1.0", self.url)
        self._link.configure(state="disabled")
        self._link.pack(fill="x", pady=(4, 0))
        self._link.bind("<Button-1>", self._on_copy)

        # Toast
        self._toast = tk.Label(inner, text="",
                               font=("Microsoft YaHei UI", 9),
                               fg=c["text_sec"], bg=c["card"])
        self._toast.pack(anchor="w", pady=(0, 8))

        # ── 按钮 ──
        bf = tk.Frame(inner, bg=c["card"])
        bf.pack(fill="x", pady=(4, 0))
        tk.Frame(bf, bg=c["card"]).pack(side="left", fill="x", expand=True)

        self._mk_btn(bf, "取消", c["btn_s"], c["btn_s_h"], c["btn_s_t"],
                     self._on_cancel).pack(side="right", padx=(8, 0))
        self._mk_btn(bf, "选择文件", c["btn_p"], c["btn_p_h"], c["btn_p_t"],
                     self._on_select, bold=True).pack(side="right")

        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._center(460, 370)
        self.win.transient(parent)

    def _mk_btn(self, parent, text, bg, bg_h, fg, cmd, bold=False):
        w = "bold" if bold else "normal"
        btn = self._tk.Button(
            parent, text=text, command=cmd,
            font=("Microsoft YaHei UI", 10, w),
            bg=bg, fg=fg,
            activebackground=bg_h, activeforeground=fg,
            bd=0, padx=18, pady=6, cursor="hand2",
        )
        btn.bind("<Enter>", lambda e, b=btn, h=bg_h: b.config(bg=h))
        btn.bind("<Leave>", lambda e, b=btn, n=bg: b.config(bg=n))
        return btn

    def _center(self, w, h):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    def _on_copy(self, event):
        self.win.clipboard_clear()
        self.win.clipboard_append(self.url)
        self._toast.config(text="[OK] 已复制到剪贴板")
        self.win.after(2000, lambda: self._toast.config(text=""))

    def _on_select(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择服务包文件",
            filetypes=[("zip 压缩包", "*.zip"), ("7z 压缩包", "*.7z"), ("所有文件", "*.*")],
            parent=self.win,
        )
        if path:
            self.result = Path(path)
        self.win.destroy()

    def _on_cancel(self):
        self.result = None
        self.win.destroy()

    def show(self) -> Path | None:
        """阻塞直到用户关闭弹窗，返回选择的路径或 None。"""
        self.win.grab_set()
        self.win.wait_window()
        return self.result


# ── 8c: 确认/选择弹窗（带倒计时默认项） ─────────────────────

class _ThemedDialog:
    """FilePickerDialog 同款明暗色卡片风格的对话框基类。

    提供：主题检测、卡片容器、按钮行、倒计时自动执行默认项、选择结果
    写入部署日志（区分用户主动点击 / 倒计时超时自动选择 / 关闭窗口）。
    子类设置 self.result 后调 show()（grab_set + wait_window）。
    """

    _LIGHT = FilePickerDialog._LIGHT
    _DARK = FilePickerDialog._DARK
    _detect_dark = staticmethod(FilePickerDialog._detect_dark)

    _BY_CLICK = "用户主动点击"
    _BY_TIMEOUT = "倒计时超时，自动选择默认项"
    _BY_CLOSE = "用户关闭窗口，按默认项处理"

    def __init__(self, parent, title: str):
        import tkinter as tk

        self._tk = tk
        self._dark = self._detect_dark()
        self.c = self._DARK if self._dark else self._LIGHT
        self.result = None
        self._chosen_by = self._BY_CLICK
        self._cd_stopped = False
        self._cd_after = None
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.configure(bg=self.c["bg"])
        self.win.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _log_choice(self, option: str) -> None:
        """把选择结果写入部署日志，自动选择与主动点击一眼可辨。"""
        logging.getLogger("deploy").info(f"[弹窗选择] {self._chosen_by}：{option}")

    # ── 布局 ──

    def _build_card(self):
        tk = self._tk
        c = self.c
        card = tk.Frame(self.win, bg=c["card"],
                        highlightthickness=1, highlightbackground=c["border"])
        card.pack(padx=1, pady=1, fill="both", expand=True)
        self.inner = tk.Frame(card, bg=c["card"], padx=24, pady=20)
        self.inner.pack(fill="both", expand=True)

    def _label(self, parent, text, size=10, bold=False, color=None,
               wraplength=470):
        tk = self._tk
        return tk.Label(
            parent, text=text,
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
            fg=color or self.c["text"], bg=self.c["card"],
            wraplength=wraplength, justify="left").pack(anchor="w")

    def _mk_btn(self, parent, text, bg, bg_h, fg, cmd, bold=False):
        w = "bold" if bold else "normal"
        btn = self._tk.Button(
            parent, text=text, command=cmd,
            font=("Microsoft YaHei UI", 10, w),
            bg=bg, fg=fg,
            activebackground=bg_h, activeforeground=fg,
            bd=0, padx=18, pady=6, cursor="hand2",
        )
        btn.bind("<Enter>", lambda e, b=btn, h=bg_h: b.config(bg=h))
        btn.bind("<Leave>", lambda e, b=btn, n=bg: b.config(bg=n))
        return btn

    def _add_buttons(self, primary, secondary=None):
        """底部按钮行（右对齐）。primary/secondary = (text, cmd, bold)。"""
        tk = self._tk
        c = self.c
        bf = tk.Frame(self.inner, bg=c["card"])
        bf.pack(fill="x", pady=(16, 0))
        self._btn_primary = self._mk_btn(
            bf, primary[0], c["btn_p"], c["btn_p_h"], c["btn_p_t"],
            primary[1], bold=primary[2] if len(primary) > 2 else True)
        self._btn_primary.pack(side="right")
        if secondary:
            self._btn_secondary = self._mk_btn(
                bf, secondary[0], c["btn_s"], c["btn_s_h"], c["btn_s_t"],
                secondary[1],
                bold=secondary[2] if len(secondary) > 2 else False)
            self._btn_secondary.pack(side="right", padx=(0, 8))

    def _center(self, w, h):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ── 倒计时（超时自动执行默认项；用户点击任意按钮即停表） ──

    def _start_countdown(self, seconds: int, btn, cmd):
        self._cd_left = max(int(seconds), 0)
        self._cd_btn = btn
        self._cd_cmd = cmd
        self._cd_base = btn["text"]
        self._cd_tick()

    def _cd_tick(self):
        if self._cd_stopped:
            return
        if self._cd_left <= 0:
            self._chosen_by = self._BY_TIMEOUT
            self._cd_cmd()
            return
        self._cd_btn["text"] = f"{self._cd_base}（{self._cd_left}s 后自动执行）"
        self._cd_left -= 1
        self._cd_after = self.win.after(1000, self._cd_tick)

    def _stop_countdown(self):
        self._cd_stopped = True
        if self._cd_after:
            try:
                self.win.after_cancel(self._cd_after)
            except Exception:
                pass
            self._cd_after = None

    def _on_window_close(self):
        """点 X：由子类覆盖，通常等同默认项/取消。"""

    def show(self):
        self.win.grab_set()
        self.win.wait_window()
        return self.result


class UpdateConfirmDialog(_ThemedDialog):
    """「发现游戏助手更新」确认窗。默认项 = 暂不更新（10 秒倒计时自动选）。

    summary 携带 github 键时为 GitHub 兜底变体：顶部明示"直连速度有较大概率
    很慢"，倒计时超时同样按暂不更新处理（放弃 GitHub 渠道）。
    """

    def __init__(self, parent, summary: dict):
        super().__init__(parent, "发现游戏助手更新")
        self._build(summary)
        self._center(560, 460)
        self.win.transient(parent)

    def _build(self, s: dict):
        tk = self._tk
        c = self.c
        self._build_card()
        inner = self.inner
        gh = s.get("github")
        svc = s.get("service")

        if gh:
            self._label(inner, "⚠ ModelScope 下载失败，可改用 GitHub 源",
                        size=12, bold=True, color="#c0392b")
            self._label(inner,
                        "GitHub 直连国内速度有较大概率很慢，"
                        "可能需要数十分钟甚至失败，请有心理准备。",
                        color=c["text_sec"])

        self._label(inner, "发现游戏助手更新", size=14, bold=True)

        if svc:
            line = f"服务端：{svc.get('label') or '新版本'}"
            if svc.get("size_s"):
                line += f"（{svc['size_s']}）"
            if s.get("old_service_sha256") and svc.get("sha8"):
                line += (f"  当前 {s['old_service_sha256'][:8]}"
                         f" → 新 {svc['sha8']}")
            self._label(inner, line)

        models = s.get("models") or []
        if models:
            self._label(inner, "模型更新（仅已安装的模型）：", bold=True)
            box = tk.Text(
                inner, height=min(len(models) + 1, 6), wrap="word",
                font=("Microsoft YaHei UI", 9), bg=c["card"], fg=c["text_sec"],
                bd=0, highlightthickness=0)
            for m in models:
                box.insert("end", f"· {m['name']}  {m['repo_id']}"
                                  f"  {m.get('size_s', '')}"
                                  f"  {m.get('reason', '')}\n")
            box.configure(state="disabled")
            box.pack(fill="x", pady=(2, 4))

        if s.get("inflight_prev"):
            self._label(inner, "上次更新未完成，建议本次完成"
                               "（缓存已保留，无需重新下载）。",
                        color=c["text_sec"])
        if s.get("service_running"):
            self._label(inner, "更新期间需要关闭正在运行的游戏助手服务。",
                        bold=True)
        self._label(inner,
                    "更新采用事务方式：全部下载成功才会应用；"
                    "任一下载失败会自动继续使用当前版本。",
                    color=c["text_sec"])

        if gh:
            self._accept_label = "仍要用 GitHub 下载"
            self._add_buttons(primary=(self._accept_label, self._accept),
                              secondary=("暂不更新", self._decline))
        else:
            self._accept_label = "立即更新"
            self._add_buttons(primary=(self._accept_label, self._accept),
                              secondary=("暂不更新", self._decline))
        self._decline_label = "暂不更新"
        # 默认项 = 暂不更新（GitHub 变体也默认不下载，超时即放弃兜底渠道）
        self._start_countdown(CONFIRM_COUNTDOWN_SECONDS,
                              self._btn_secondary, self._decline)

    def _accept(self):
        self._stop_countdown()
        self.result = True
        self._log_choice(self._accept_label)
        self.win.destroy()

    def _decline(self):
        self._stop_countdown()
        self.result = False
        self._log_choice(self._decline_label)
        self.win.destroy()

    def _on_window_close(self):
        self._chosen_by = self._BY_CLOSE
        self._decline()


class InstallChoiceDialog(_ThemedDialog):
    """「未找到游戏助手服务」安装方式选择窗。

    默认项 = 自动下载并安装（10 秒倒计时自动选）；也可改用已下载好的
    本地服务包，或取消部署（点 X 等同取消）。
    """

    def __init__(self, parent, install_dir: str):
        super().__init__(parent, "未找到游戏助手服务")
        self._parent = parent
        self._install_dir = install_dir
        self.result = "cancel"
        self._build()
        self._center(540, 380)
        self.win.transient(parent)

    def _build(self):
        c = self.c
        self._build_card()
        inner = self.inner
        self._label(inner, "未找到游戏助手服务", size=14, bold=True)
        self._label(inner,
                    f"服务安装目录：{self._install_dir}\n"
                    "该目录不存在或尚未安装服务端，请选择安装方式：")
        self._label(inner,
                    "· 自动下载并安装（推荐，默认）：安装包下载与解压位置为 "
                    "%LOCALAPPDATA%\\GameAssistant",
                    color=c["text_sec"])
        self._label(inner,
                    "· 选择已解压的服务端目录：直接使用该目录（需包含服务端 exe），"
                    "不下载不复制",
                    color=c["text_sec"])

        bf = self._tk.Frame(inner, bg=c["card"])
        bf.pack(fill="x", pady=(16, 0))
        self._btn_auto = self._mk_btn(
            bf, "自动下载并安装", c["btn_p"], c["btn_p_h"], c["btn_p_t"],
            self._choose_auto, bold=True)
        self._btn_auto.pack(side="right")
        self._btn_local = self._mk_btn(
            bf, "选择已解压的服务端目录…", c["btn_s"], c["btn_s_h"], c["btn_s_t"],
            self._choose_local)
        self._btn_local.pack(side="right", padx=(0, 8))
        self._btn_cancel = self._mk_btn(
            bf, "取消", c["btn_s"], c["btn_s_h"], c["btn_s_t"],
            self._choose_cancel)
        self._btn_cancel.pack(side="right", padx=(0, 8))
        # 默认项 = 自动下载并安装
        self._start_countdown(CONFIRM_COUNTDOWN_SECONDS,
                              self._btn_auto, self._choose_auto)

    def _choose_auto(self):
        self._stop_countdown()
        self.result = "auto"
        self._log_choice("自动下载并安装")
        self.win.destroy()

    def _choose_local(self):
        self._stop_countdown()
        from tkinter import filedialog
        initial = self._install_dir
        picked = filedialog.askdirectory(
            parent=self.win, mustexist=True,
            title="选择已解压的服务端目录（需包含服务端 exe）",
            initialdir=initial if Path(initial).exists() else str(Path.home()),
        )
        if picked:
            self.result = Path(picked)
            self._log_choice(f"使用已解压的服务端目录 {picked}")
            self.win.destroy()
        # 选择器被取消 → 回到本窗（不再倒计时，等用户再选或取消）

    def _choose_cancel(self):
        self._stop_countdown()
        self.result = "cancel"
        self._log_choice("取消部署")
        self.win.destroy()

    def _on_window_close(self):
        self._chosen_by = self._BY_CLOSE
        self._choose_cancel()


# ============================================================================
# Section 9: Deployer
# ============================================================================

@dataclass
class ServiceRemote:
    """远端服务包描述（一个可用下载渠道）。

    sha256 是「版本标记」：与 DeployState.service_sha256 同源可比对。
    transport=direct/github 时即服务端 7z 自身的哈希（跨渠道一致）；
    transport=zip 时为外层 zip 的哈希（内层哈希下载前不可知）。
    remote_mtime 是远端压缩包的修改时间（epoch 秒），参与"exe 更旧 + 哈希
    不一致"的双条件更新判定；API 不提供时为 None（由 HEAD Last-Modified 兜底）。
    """
    url: str
    sha256: str
    size: int
    layout: str            # new | legacy
    transport: str         # direct | zip | github
    version_label: str = ""
    zip_sha256: str = ""   # transport=zip 时整包完整性校验值（可选）
    remote_mtime: float | None = None


@dataclass
class ModelRemote:
    """有更新的单个【已安装】模型（未安装过的模型不进更新计划）。"""
    name: str
    repo_id: str
    target: Path
    fingerprint: str
    size: int
    reason: str            # 仓库内容有更新 | 清单变更


@dataclass
class UpdatePlan:
    """一次待应用的更新：服务端 + 模型作为一个整体事务。"""
    service: ServiceRemote | None = None
    models: dict = field(default_factory=dict)       # name -> ModelRemote
    script_sha256: str = ""                          # 远端清单脚本哈希（已知时）
    manifest_normalized: dict = field(default_factory=dict)  # 解析成功的清单缓存

    def is_empty(self) -> bool:
        return self.service is None and not self.models

    def snapshot(self) -> dict:
        """跳过记录/提交标记用的规范化快照（与远端状态一一对应）。"""
        return {
            "service_sha256": self.service.sha256 if self.service else None,
            "models_script_sha256": self.script_sha256,
            "models_fingerprint": {n: m.fingerprint
                                   for n, m in self.models.items()},
        }

    def summary(self, state: "DeployState") -> dict:
        """弹窗展示用摘要。"""
        def size_s(n: int) -> str:
            if n >= 1024 ** 3:
                return f"{n / 1024 ** 3:.2f} GB"
            if n > 0:
                return f"{n / (1024 * 1024):.0f} MB"
            return ""

        svc = None
        if self.service is not None:
            svc = {
                "label": self.service.version_label or self.service.transport,
                "sha8": (self.service.sha256 or "")[:8],
                "size_s": size_s(self.service.size),
                "transport": self.service.transport,
            }
        out = {
            "service": svc,
            "old_service_sha256": state.service_sha256 or "",
            "models": [{"name": m.name, "repo_id": m.repo_id,
                        "size_s": size_s(m.size), "reason": m.reason}
                       for m in self.models.values()],
            "service_running": any(is_port_open(SERVICE_HOST, p)
                                   for p in service_ports()),
            "inflight_prev": bool(state._data.get("update_commit_inflight")),
        }
        if self.service is not None and self.service.transport == "github":
            # GitHub 渠道：确认弹窗必须展示慢速警示（summary 携带 github 键即变体）
            out["github"] = {"tag": self.service.version_label,
                             "size": self.service.size}
        return out


class Deployer:
    """部署编排：检测 → 获取服务包 → 安装 → 模型 → 启动。"""

    def __init__(
        self,
        install_dir: Path,
        mode: str = "auto-fallback",
        package_path: Path | None = None,
        lang: str = "zh",
        skip_models: bool = False,
        only_models: list[str] | None = None,
        start_only: bool = False,
        force: bool = False,
        service_url: str = SERVICE_URL,
    ):
        self.install_dir = install_dir
        # frontmatter/命令行里配置的原始预装目录（main 会在解析后覆盖；
        # install_dir 本身则已解析为可写位置——见 _resolve_startup_dir）
        self.requested_install_dir: Path = install_dir
        self.mode = mode
        self.package_path = package_path
        self.lang = lang
        self.skip_models = skip_models
        self.only_models = only_models
        self.start_only = start_only
        self.force = force
        self.service_url = service_url

        self.models_config = build_models_config(lang)
        if only_models:
            self.models_config = {
                k: v for k, v in self.models_config.items() if k in only_models
            }

        self.log = setup_logger(install_dir)
        self.log.info("=" * 50)
        self.log.info("游戏助手服务部署脚本")
        self.log.info(f"安装目录: {install_dir}")
        self.log.info(f"模式: {mode}")
        self.log.info(f"Splitter 语言: {lang}")
        if only_models:
            self.log.info(f"仅下载模型: {', '.join(self.models_config)}")

        # 单实例保护：setup_logger 已创建 install_dir，锁直接放 install_dir 下。
        # 抢不到锁说明已有另一个部署实例在跑，抛异常让 main() 退出。
        self._single_lock = _acquire_single_instance_lock(install_dir)
        if self._single_lock is None:
            raise RuntimeError("检测到另一个部署实例正在运行，本次跳过")

        check_dependencies()

        self.state = DeployState(install_dir)
        self.pkg_dl = PackageDownloader(self.log)

        self.installer = Installer(self.log)
        self.svc = ServiceManager(install_dir, self.log)
        self._service_root: Path | None = None

        if force:
            self.log.info("--force: 重置状态")
            self.state.reset()

        # GUI 通信队列 (worker → main thread)
        self._gui_q = queue.Queue()
        # 反向通信 (main → worker): 文件选择结果
        self._picker_result: queue.Queue = queue.Queue(1)
        # 反向通信 (main → worker): 更新确认 / 安装方式选择结果
        self._confirm_result: queue.Queue = queue.Queue(1)
        self._install_choice_result: queue.Queue = queue.Queue(1)
        # 更新事务成功后置位：_download_models 不再补装从未下载过的模型
        self._update_applied = False
        # 本次运行为全新安装（首装记录指纹用）
        self._fresh_install = False
        # 远端清单解析出的模型配置（更新事务提交成功后替换 self.models_config）
        self._manifest_models_config: dict | None = None

    # ── 进度回调 → GUI 队列 ──────────────────────────────────

    def _emit(self, msg_type: str, **kwargs):
        self._gui_q.put((msg_type, kwargs))

    # ── 主流程 ──────────────────────────────────────────────

    def execute(self):
        """执行部署（从后台线程调用）。"""
        try:
            self._run()
            self._emit("done", message="部署成功！")
        except Exception as e:
            self.log.error(f"部署失败: {e}")
            self.log.debug(traceback.format_exc())
            self._emit("error", message=str(e))

    def _find_service_root(self) -> Path:
        """找到服务可执行文件所在目录，模型放到该目录下。"""
        exe = self.installer.find_executable(self.install_dir)
        if exe:
            self.log.info(f"服务根目录: {exe.parent}")
            return exe.parent
        self.log.warning("未找到可执行文件，模型放到安装根目录")
        return self.install_dir

    def _run(self):
        # Step 0 — 仅启动
        if self.start_only:
            self._patch_config()
            return self._step_start()

        # Step 1 — 安装服务包
        _exe = self.installer.find_executable(self.install_dir)
        fresh = self.force or (not self.state.service_installed and _exe is None)
        if fresh:
            # 配置的 install_dir 不存在或里面没有解压好的服务端时：
            #  - 自动下载（弹窗默认项）：安装包下载与解压都回落默认目录
            #    %LOCALAPPDATA%\GameAssistant（_ask_install_missing 里已切目录）
            #  - 用户选择"已解压的服务端目录"：直接以该目录为安装目录，
            #    不下载不解压（返回值非 None）
            picked_dir = self._ask_install_missing()
            if picked_dir is None:
                archive = self._acquire_package()
                self._extract_and_install(archive)
            else:
                self.state.mark_service_installed()
                self.log.info(f"[OK] 使用已解压的服务端目录作为安装目录: {self.install_dir}")
            # 解压后重新定位 exe：首次安装时上面的 _exe 捕获于切换目录前（为 None），
            # 不复查的话下方"自动修复标记"分支永远不触发，service_installed 一直为 false。
            _exe = self.installer.find_executable(self.install_dir)
            self._fresh_install = True
        # exe 已存在但状态文件丢失 — 自动修复
        if _exe is not None and not self.state.service_installed:
            self.state.mark_service_installed()

        # 确定服务根目录（exe 所在位置）
        self._service_root = self._find_service_root()

        # 修改配置（解压后 / 非首次部署都会走到这里）
        self._patch_config()

        if fresh:
            # 首装记录服务端版本标记 + 模型指纹（后续更新检测的比对基线）
            self._capture_installed_fingerprints()
        else:
            # 已安装机器：检测并（经用户确认后）以事务方式应用服务端/模型更新。
            # 检测失败/无更新/用户跳过 → 静默继续旧版本，不影响启动。
            self._run_update_flow()

        # Step 2 — 模型
        if self.skip_models:
            self.log.info("--skip-models: 跳过模型下载")
        else:
            self._download_models()
        if self._fresh_install:
            self._capture_model_fingerprints()

        # Step 3 — 启动
        self._step_start()

    # ── Step 1a: 获取服务包 ─────────────────────────────────

    def _acquire_package(self) -> Path:
        if self.package_path:
            if not self.package_path.exists():
                raise FileNotFoundError(f"文件不存在: {self.package_path}")
            self.log.info(f"使用指定本地包: {self.package_path}")
            return self.package_path

        if self.mode == "manual":
            return self._manual_pick()
        if self.mode == "auto":
            return self._auto_download()

        # auto-fallback
        try:
            return self._auto_download()
        except Exception as e:
            self.log.warning(f"自动下载失败，回退手动模式: {e}")
            return self._manual_pick()

    def _auto_download(self) -> Path:
        dest = self.install_dir / _filename_from_url(self.service_url)

        def on_progress(dl, total, speed):
            pct = dl / max(total, 1) * 100
            speed_s = f"{speed/(1024*1024):.1f} MB/s" if speed else ""
            size_s = f"{dl/(1024*1024):.0f} / {total/(1024*1024):.0f} MB"
            self._emit("progress", stage=f"下载服务包 {size_s}  {speed_s}",
                       value=pct)

        return self.pkg_dl.download(
            self.service_url, dest, progress_cb=on_progress,
            verify_cb=self.installer.verify_archive,
        )

    def _manual_pick(self) -> Path:
        """通过 GUI 弹窗获取本地文件路径。"""
        self.log.info("等待用户选择服务包...")
        self._emit("need_picker", url=self.service_url)

        # 阻塞等待 GUI 线程放入结果（不设超时：手动下载服务包可能耗时 1~2 小时）
        result = self._picker_result.get()

        if result is None:
            raise RuntimeError("用户取消操作")
        self.log.info(f"用户选择: {result}")
        return result

    def _manual_confirm(self, summary: dict) -> bool:
        """阻塞等用户在 GUI 上确认更新（弹窗由主线程创建）。

        倒计时超时/点 X/拒绝 → False（默认不更新）。
        """
        self._emit("need_confirm", summary=summary)
        return bool(self._confirm_result.get())

    # ── Step 1a-2: 服务端/模型版本检测与事务更新 ─────────────

    def _retarget_install_dir(self, new_dir: Path) -> None:
        """把安装目录切换到 new_dir，并让日志/状态文件跟随落位。

        new_dir 须已可写（调用方保证：默认目录由 _resolve_startup_dir 建好，
        用户选择的"已解压服务端目录"天然存在）。
        """
        new_dir = Path(new_dir)
        if Path(self.install_dir) == new_dir:
            return
        self.install_dir = new_dir
        for h in self.log.handlers:
            try:
                h.close()  # FileHandler 释放旧日志文件（StreamHandler 不受影响）
            except Exception:
                pass
        self.log = setup_logger(new_dir)
        self.state = DeployState(new_dir)
        self.svc = ServiceManager(new_dir, self.log)
        self.log.info(f"安装目录: {new_dir}")

    def _load_chosen_server_dir(self) -> Path | None:
        """上次用户选择的服务端目录；已失效（不存在/无服务端 exe）→ None。"""
        return _load_chosen_server_dir()

    def _save_chosen_server_dir(self, chosen: Path) -> None:
        try:
            _CHOSEN_SERVER_DIR_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CHOSEN_SERVER_DIR_FILE.write_text(str(chosen), encoding="utf-8")
        except OSError as e:
            self.log.debug(f"记录服务端目录失败（不影响本次部署）: {e}")

    def _clear_chosen_server_dir(self) -> None:
        try:
            _CHOSEN_SERVER_DIR_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def _ask_install_missing(self):
        """install_dir 里没有解压好的服务端时，决定安装目录。

        优先级：上次用户选择且仍可用的服务端目录（直接使用，不再询问）→
        弹窗询问。弹窗返回 Path = 用户本次选择的"已解压服务端目录"（直接
        作为安装目录使用，不下载不复制，并记住供下次使用）；返回 None =
        自动下载，安装包与解压目标都落在回落目录（默认
        %LOCALAPPDATA%\\GameAssistant，也是倒计时超时的默认项，同时清除
        已失效的选择记录）。用户取消 → 抛 RuntimeError 结束部署。
        未配置预装目录（frontmatter 为空）且无可用记录时不弹窗，静默安装
        到默认目录。
        """
        if self.package_path is not None or self.mode == "manual":
            return None   # 已指定本地包/手动模式：无需询问
        if not self.force:
            remembered = self._load_chosen_server_dir()
            if remembered is not None:
                self._retarget_install_dir(remembered)
                self.log.info(f"[OK] 使用上次选择的服务端目录: {remembered}")
                return remembered
        if find_server_exe(self.install_dir) is not None:
            return None   # 配置的目录里已有解压好的服务端，直接使用
        if not self.force and find_server_exe(DEFAULT_INSTALL_DIR) is not None:
            # 默认目录里已有解压好的服务端：直接使用，绝不重新解压覆盖
            # （解压落位会 rmtree 同名旧目录，里面可能有用户数据）
            self._retarget_install_dir(DEFAULT_INSTALL_DIR)
            self.log.info(f"[OK] 使用默认目录中已解压的服务端: {DEFAULT_INSTALL_DIR}")
            return DEFAULT_INSTALL_DIR
        if self.requested_install_dir == DEFAULT_INSTALL_DIR:
            return None   # 未配置预装目录：静默自动安装到默认目录

        while True:
            self.log.info("等待用户确认服务安装方式...")
            self._emit("need_install_choice",
                       install_dir=str(self.requested_install_dir))
            choice = self._install_choice_result.get()  # "auto" | Path | "cancel"
            if choice == "cancel":
                raise RuntimeError("用户取消了部署")
            if isinstance(choice, Path):
                if self.installer.find_executable(choice) is None:
                    self.log.warning(
                        f"所选目录里没有服务端可执行文件（*.exe）：{choice}，请重新选择")
                    continue
                self.log.info(f"安装方式：使用已解压的服务端目录 {choice}")
                self._save_chosen_server_dir(choice)
                self._retarget_install_dir(choice)
                return choice
            # "auto"：安装包与解压目标都回落默认目录；记录里的目录已不可用，清掉
            self._clear_chosen_server_dir()
            self._retarget_install_dir(DEFAULT_INSTALL_DIR)
            return None

    def _run_update_flow(self):
        """已安装机器上的更新检测与事务更新总控。

        任何异常只记日志、绝不上抛——更新失败不能影响旧版本继续启动。
        """
        if os.environ.get(UPDATE_CHECK_ENV) == "0":
            self.log.info("GA_UPDATE_CHECK=0，跳过服务端/模型更新检测")
            return
        if self.package_path is not None:
            self.log.info("使用本地服务包，跳过服务端远端版本检测（模型检测照常）")
        try:
            plan = self._plan_update()
        except Exception as e:
            self.log.warning(f"更新检测失败，跳过（继续使用当前版本）: {e}")
            self.log.debug(traceback.format_exc())
            return

        # 清单解析成功即缓存（无论是否需要更新），供下次解析失败时降级复用
        if plan.manifest_normalized and plan.script_sha256:
            self.state.mark_manifest(plan.manifest_normalized, plan.script_sha256)

        if plan.is_empty():
            self.state._data["update_checked_at"] = datetime.now().isoformat()
            self.state._save()
            self.log.info("服务端与模型均为最新，无需更新")
            return
        if self.state.is_skipped(plan.snapshot()):
            self.log.info("远端有更新，但该版本已被用户跳过，本次不再询问")
            return
        if plan.service is not None and plan.service.transport == "github":
            self.log.warning("ModelScope 不可用，GitHub releases 有更新版本"
                             "（速度较慢，将请用户确认）")

        self.log.info("检测到更新，等待用户确认更新（弹窗已打开）...")
        if not self._manual_confirm(plan.summary(self.state)):
            self.log.info("结果为「暂不更新」——已记录此版本，后续启动不再询问")
            self.state.mark_skipped(plan.snapshot())
            return

        self.log.info("下载更新包: "
                      + (plan.service.url if plan.service else "(仅模型更新)"))
        got = self._download_updates(plan)
        if got is None:
            self._emit("progress", stage="更新下载失败，使用当前版本启动", value=0)
            return
        self.log.info("应用更新: 停止服务 → 覆盖安装 → 模型落位")
        if not self._commit_update(plan, got):
            self._emit("progress", stage="更新应用失败，使用当前版本启动", value=0)
            return
        self._update_applied = True
        if self._manifest_models_config:
            self.models_config = self._manifest_models_config
        self.log.info("[OK] 更新已应用")

    def _plan_update(self) -> UpdatePlan:
        """对比远端与本地记录，产出待应用更新计划；无更新/不可得 → 空 plan。"""
        self.log.info("检测服务端与模型更新 "
                      f"(本地 sha256={self.state.service_sha256 or '无记录'})")

        root_files = list_repo_files(REPO_ID, root="", recursive=False)
        bin_files = (list_repo_files(REPO_ID, root="bin", recursive=True)
                     if root_files else [])

        service = self._probe_service_remote(root_files, bin_files)
        if service is not None:
            remote_sha = service.sha256 or ""
            local_sha = self.state.service_sha256 or ""
            if remote_sha == local_sha:
                service = None           # SHA256 一致：无更新
            else:
                older, time_known = self._exe_older_than_remote_archive(service)
                if time_known and not older:
                    # 哈希不一致但本地 exe 不早于远端压缩包（本地已是更新的
                    # 版本，比如从 GitHub 装过）→ 不降级，跳过
                    service = None
                # time_known=False：所有修改时间来源均失败，逼不得已退化为
                # 仅按 SHA256 判定——不一致即保留 service（helper 内已记日志）
        plan = UpdatePlan(service=service)

        # 模型清单：下载远端脚本 ast 解析，失败三级降级（上次缓存 → 内置）
        models_config, script_sha, parsed_ok, manifest_norm = \
            self._fetch_manifest(root_files)
        plan.script_sha256 = script_sha or ""
        plan.manifest_normalized = manifest_norm if parsed_ok else {}
        self._manifest_models_config = models_config

        if not self.skip_models and root_files:
            plan.models = self._collect_model_updates(models_config, script_sha)
        return plan

    def _exe_older_than_remote_archive(self, svc: ServiceRemote) -> tuple[bool, bool]:
        """比对本地服务端 exe 与远端压缩包的修改时间。

        返回 (exe 是否更早, 远端时间是否可知)。远端时间来源依次为 files API
        时间字段（实测为 CommittedDate，unix 秒）→ 下载响应头 Last-Modified
        （stream GET：下载地址是签名 302 跳转、签名与 GET 绑定，HEAD 拿不到）。
        全部失败返回 (False, False)——调用方按"逼不得已只靠 SHA256"降级判定。

        正常判定为双条件 AND：本时间条件 与 SHA256 不一致 同时满足才更新。
        """
        exe = find_server_exe(self.install_dir)
        if exe is None:
            return False, True   # 本地没有 exe：交给既有安装流程，不算"有更新"
        remote_mtime = svc.remote_mtime
        fail_reason = ""
        if remote_mtime is None:
            try:
                import requests
                resp = requests.get(svc.url, stream=True, timeout=15,
                                    allow_redirects=True)
                try:
                    remote_mtime = _parse_remote_time(
                        resp.headers.get("Last-Modified"))
                    if remote_mtime is None:
                        fail_reason = (f"HTTP {resp.status_code} "
                                       "响应无 Last-Modified 头")
                finally:
                    resp.close()
            except Exception as e:
                fail_reason = f"请求失败: {type(e).__name__}: {e}"
        if remote_mtime is None:
            self.log.warning(
                "无法获取远端压缩包的修改时间（" + fail_reason + "），"
                "退化为仅按 SHA256 判定：不一致即提示更新")
            return False, False
        try:
            older = exe.stat().st_mtime < remote_mtime
            fmt = "%Y-%m-%d %H:%M"
            self.log.info(
                "服务端版本时间比对: 本地 exe "
                + time.strftime(fmt, time.localtime(exe.stat().st_mtime))
                + " / 远端压缩包 "
                + time.strftime(fmt, time.localtime(remote_mtime))
                + (" → exe 较旧" if older else " → exe 不比远端旧"))
            return older, True
        except OSError:
            return False, True

    def _probe_service_remote(self, root_files: list, bin_files: list):
        """探测服务端下载渠道：新直链 → 旧直链 → zip 整包 → GitHub 兜底。

        版本标记（ServiceRemote.sha256）与 DeployState.service_sha256 同源：
        - 有 files API 时：新布局=7z 哈希；仅 zip 可见时=zip 哈希（同渠道内自洽）。
        - API 不可用时：GitHub digest（同为 7z 哈希）。与本地记录不同即视为有更新。
        """
        entry = repo_entry(bin_files, SERVICE_7Z_NEW)
        if entry and entry.get("Sha256"):
            return ServiceRemote(
                url=_repo_file_url(SERVICE_7Z_NEW),
                sha256=entry["Sha256"],
                size=int(entry.get("Size") or 0),
                layout="new", transport="direct",
                remote_mtime=_entry_remote_mtime(entry))

        legacy = []
        for e in bin_files:
            p = e.get("Path") or ""
            m = SERVICE_7Z_LEGACY_RE.match(p)
            if m and e.get("Sha256"):
                legacy.append((tuple(int(x) for x in m.group(1).split(".")),
                               p, "v" + m.group(1), e))
        if legacy:
            _, path, ver, e = max(legacy)
            return ServiceRemote(
                url=_repo_file_url(path), sha256=e["Sha256"],
                size=int(e.get("Size") or 0),
                layout="legacy", transport="direct", version_label=ver,
                remote_mtime=_entry_remote_mtime(e))

        zip_entry = repo_entry(root_files, "GameAssistant.zip")
        if zip_entry and zip_entry.get("Sha256"):
            return ServiceRemote(
                url=SERVICE_URL, sha256=zip_entry["Sha256"],
                size=int(zip_entry.get("Size") or 0),
                layout="legacy", transport="zip",
                zip_sha256=zip_entry["Sha256"],
                remote_mtime=_entry_remote_mtime(zip_entry))

        # files API 完全不可用：试 GitHub 兜底（版本最及时，ModelScope 延时几天）
        if not root_files and not bin_files:
            gh = github_latest_asset()
            if gh and gh.get("url"):
                local = self.state.service_sha256
                if local and gh.get("sha256") and gh["sha256"] == local:
                    return None   # GitHub 最新 = 本地已装
                return ServiceRemote(
                    url=gh["url"], sha256=gh.get("sha256") or "",
                    size=int(gh.get("size") or 0),
                    layout="", transport="github",
                    version_label=gh.get("tag") or "GitHub 最新",
                    remote_mtime=gh.get("mtime"))
        return None

    def _fetch_manifest(self, root_files: list):
        """下载远端清单脚本并 ast 解析；失败三级降级（本轮 → 上次缓存 → 内置）。

        返回 (models_config, script_sha256|None, parsed_ok, manifest_normalized)。
        """
        remote = repo_entry(root_files, MANIFEST_SCRIPT)
        remote_sha = (remote or {}).get("Sha256") or None

        source = ""
        if remote_sha:
            cache = self.install_dir / UPDATE_CACHE_DIRNAME
            cache.mkdir(parents=True, exist_ok=True)
            dest = cache / Path(MANIFEST_SCRIPT).name
            if dest.exists():
                try:
                    if sha256_file(dest) == remote_sha:
                        source = dest.read_text(encoding="utf-8")
                except OSError:
                    source = ""
            if not source:
                try:
                    import requests
                    resp = requests.get(_manifest_url(), timeout=20)
                    resp.raise_for_status()
                    source = resp.text
                    dest.write_text(source, encoding="utf-8")
                except Exception as e:
                    self.log.debug(f"清单脚本下载失败: {e}")
                    if dest.exists():
                        try:
                            source = dest.read_text(encoding="utf-8")
                        except OSError:
                            source = ""

        manifest = parse_model_manifest(source, self.lang) if source else None
        if manifest:
            cfg = {name: {"model_id": v[0], "dir": Path(*v[1:])}
                   for name, v in manifest.items()}
            return cfg, remote_sha, True, manifest

        cached = self.state._data.get("models_manifest") or {}
        if cached:
            self.log.warning("远端清单脚本解析失败/不可得，沿用上次成功解析的清单")
            cfg = {name: {"model_id": v[0], "dir": Path(*v[1:])}
                   for name, v in cached.items()}
            return cfg, remote_sha, False, {}
        self.log.info("远端清单脚本不可用，使用内置模型清单")
        return build_models_config(self.lang), remote_sha, False, {}

    def _collect_model_updates(self, models_config: dict,
                               script_sha: str | None) -> dict:
        """找出需要更新的【已安装】模型；未安装过的模型一律不进更新计划。"""
        state_fps = self.state._data.get("models_fingerprint") or {}
        old_manifest = self.state._data.get("models_manifest") or {}
        script_changed = bool(script_sha) and \
            script_sha != self.state._data.get("models_script_sha256")

        installed = {n for n, cfg in models_config.items()
                     if state_fps.get(n) or self.state.is_model_downloaded(n)}
        if self.only_models:
            installed &= set(self.only_models)
        if not installed:
            return {}

        ids = {n: models_config[n]["model_id"] for n in installed}
        files_map = self._query_model_files(ids)
        out = {}
        for n in sorted(installed):
            cfg = models_config[n]
            entries = files_map.get(n) or []
            if not entries:
                if script_changed:
                    # 清单变了但该仓库指纹查不到：保守视为有更新（宁误报不漏报）
                    out[n] = ModelRemote(n, cfg["model_id"],
                                         self._service_root / cfg["dir"],
                                         "", 0, "清单变更")
                continue
            fp = fingerprint_files(entries)
            size = sum(int(f.get("Size") or 0) for f in entries)
            target = self._service_root / cfg["dir"]
            if state_fps.get(n) and fp != state_fps[n]:
                out[n] = ModelRemote(n, cfg["model_id"], target,
                                     fp, size, "仓库内容有更新")
            elif (script_changed and n in old_manifest
                  and old_manifest[n][0] != cfg["model_id"]):
                # 清单里该模型换了仓库且内容指纹相同（换仓库但内容一致则无需重下，
                # 走不到这；指纹查询失败时上面已按清单变更保守纳入）
                out[n] = ModelRemote(n, cfg["model_id"], target,
                                     fp, size, "清单变更")
        return out

    def _query_model_files(self, model_ids: dict) -> dict:
        """name -> 仓库文件清单（含 Sha256）。失败回退 HubApi（无哈希），再失败为空。"""
        out = {}
        api_dead = False
        for name, mid in model_ids.items():
            if api_dead:
                out[name] = []
                continue
            entries = list_repo_files(mid, root="", recursive=True)
            if not entries:
                try:
                    from modelscope.hub.api import HubApi
                    entries = [dict(f) for f in HubApi().get_model_files(mid) or []]
                except Exception as e:
                    self.log.debug(f"  {name}: 查询仓库文件失败（{e}）")
                    entries = []
                if not entries:
                    api_dead = True  # 首个仓库就查不到：大概率整体不可达，不再逐仓空耗
            out[name] = entries
        return out

    def _download_updates(self, plan: UpdatePlan):
        """Stage A：把服务包与待更新模型全部下载到缓存。

        全部成功返回 {"service": Path, "models": {name: Path}}（键按需存在）；
        任何失败返回 None——绝不提交，现有安装不受影响。
        """
        need = (plan.service.size if plan.service else 0) \
            + sum(m.size for m in plan.models.values())
        try:
            import shutil as _shutil
            if need > 0 and _shutil.disk_usage(
                    str(self.install_dir)).free < need * 1.2:
                self.log.warning("磁盘剩余空间不足，放弃本次更新")
                return None
        except OSError:
            pass  # 查不到磁盘信息就放行，交由下载自然失败

        got: dict = {}

        # ── 服务包 ──
        if plan.service is not None:
            svc = plan.service
            dest = (self.install_dir / UPDATE_CACHE_DIRNAME
                    / _filename_from_url(svc.url))
            if svc.transport == "github":
                # 探测期已确定走 GitHub：plan 确认弹窗已含慢速警示，直接下载
                archive = self._download_service_package(
                    svc, dest, verify_sha=svc.sha256 or None)
            else:
                archive = self._download_service_package(
                    svc, dest,
                    verify_sha=(svc.zip_sha256 if svc.transport == "zip"
                                else svc.sha256))
                if archive is None:
                    # ModelScope 失败/不可用 → GitHub 兜底（单独确认，明示速度慢）
                    gh = self._offer_github_fallback(plan)
                    if gh is None:
                        return None
                    svc = dc_replace(plan.service,
                                     url=gh["url"],
                                     sha256=gh.get("sha256") or "",
                                     transport="github",
                                     version_label=gh.get("tag")
                                     or svc.version_label)
                    plan.service = svc
                    dest = (self.install_dir / UPDATE_CACHE_DIRNAME
                            / _filename_from_url(svc.url))
                    archive = self._download_service_package(
                        svc, dest, verify_sha=gh.get("sha256"))
            if archive is None:
                return None
            got["service"] = archive

        # ── 模型（只进缓存，不落位）──
        if plan.models:
            self.log.info(f"下载 AI 模型（更新，仅已安装的 {len(plan.models)} 个）")
            cache_root = self._service_root / "_modelscope_download_cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            prog = _ModelsByteProgress()
            cached: dict = {}
            todo = len(plan.models)
            done_models = [0]
            last_emit = [time.monotonic()]
            lo, hi = 60.0, 79.0

            def emit(force=False):
                now = time.monotonic()
                if not force and now - last_emit[0] < 0.5:
                    return
                last_emit[0] = now
                frac = prog.phase_frac(list(plan.models), {}, done_names)
                self._emit("progress",
                           stage=(f"下载模型更新 ({done_models[0]}/{todo})"),
                           value=lo + (hi - lo) * frac)
            prog.on_change = emit

            from concurrent.futures import ThreadPoolExecutor, as_completed
            done_names: set = set()
            with ThreadPoolExecutor(max_workers=min(todo, 3)) as pool:
                future_map = {
                    pool.submit(self._fetch_model_to_cache,
                                n, m.repo_id, cache_root, prog): n
                    for n, m in plan.models.items()}
                for future in as_completed(future_map):
                    n = future_map[future]
                    try:
                        cached[n] = future.result()
                        done_models[0] += 1
                        done_names.add(n)
                        self.log.info(f"  [{done_models[0]}/{todo}] {n} 下载完成")
                        emit(force=True)
                    except Exception as e:
                        self.log.error(f"  {n} 下载失败: {e}")
                        return None
            got["models"] = cached
        return got

    def _download_service_package(self, svc: ServiceRemote, dest: Path,
                                  verify_sha: str | None):
        """下载单个服务包到缓存目录；失败返回 None（不回退手动选包）。"""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)

            def on_progress(dl, total, speed):
                pct = dl / max(total, 1) * 100
                speed_s = f"{speed/(1024*1024):.1f} MB/s" if speed else ""
                size_s = f"{dl/(1024*1024):.0f} / {total/(1024*1024):.0f} MB"
                self._emit("progress",
                           stage=f"下载更新包 {size_s}  {speed_s}",
                           value=12.0 + pct * 0.47)  # 映射到 [12, 59]，不越阶段

            return self.pkg_dl.download(
                svc.url, dest, progress_cb=on_progress,
                verify_cb=self.installer.verify_archive,
                sha256=(verify_sha or None))
        except Exception as e:
            self.log.warning(f"服务包下载失败: {e}")
            return None

    def _offer_github_fallback(self, plan: UpdatePlan):
        """ModelScope 下载失败后的 GitHub 兜底询问。返回资产 dict 或 None。"""
        self.log.info("尝试 GitHub releases 兜底渠道...")
        gh = github_latest_asset()
        if not gh or not gh.get("url"):
            self.log.warning("GitHub releases 不可达或无可用资产，放弃本次更新")
            return None
        local = self.state.service_sha256
        if local and gh.get("sha256") and gh["sha256"] == local:
            self.log.info("GitHub 最新版本与本地一致，无需从 GitHub 下载")
            return None
        self.log.info("检测到更新，等待用户确认更新（弹窗已打开）...")
        summary = plan.summary(self.state)
        summary["github"] = {"tag": gh.get("tag"), "size": gh.get("size")}
        if not self._manual_confirm(summary):
            self.log.info("结果为「暂不更新」——本次放弃从 GitHub 下载")
            return None
        return gh

    def _commit_update(self, plan: UpdatePlan, got: dict) -> bool:
        """Stage B：停服 → 覆盖安装服务端 → 模型落位 → 全部成功才记录新指纹。

        中途任何失败返回 False：指纹未写入，下次启动会重新提示更新
        （Stage A 的下载缓存还在，无需重新下载）。
        """
        self.state.set_inflight(plan.snapshot())
        try:
            if not self._stop_local_service():
                self.log.warning("无法停止正在运行的服务，放弃本次更新"
                                 "（现有安装未受影响，服务将继续使用）")
                self.state.clear_inflight()
                return False

            if "service" in got:
                self._clear_caches_dir()   # 停服后先清：此刻句柄应已释放
                self._extract_and_install(got["service"])
                self._service_root = self._find_service_root()
                self._patch_config()
                self._clear_caches_dir()   # 包内若带了 caches，再兜底清一次
                self.state.mark_service_version(
                    plan.service.sha256,
                    layout=plan.service.layout,
                    source=plan.service.transport,
                    version_label=plan.service.version_label)

            if plan.models:
                installed = dict(self.state._data.get("models_fingerprint") or {})
                for i, (name, m) in enumerate(plan.models.items()):
                    self._emit("progress", stage="安装模型到目标目录...",
                               value=80.0 + 15.0 * (i + 1) / len(plan.models),
                               detail=f"{name}: 安装中")
                    self._install_model(name, got["models"][name], m.target)
                    if m.fingerprint:
                        installed[name] = m.fingerprint
                self.state.mark_models_fingerprint(installed)

            self.state.clear_skipped()
            self.state.clear_inflight()
            return True
        except Exception as e:
            self.log.error(f"应用更新失败: {e}")
            self.log.debug(traceback.format_exc())
            self.log.warning("下次启动将重新提示更新（下载缓存已保留，无需重新下载）")
            return False

    def _clear_caches_dir(self) -> None:
        """清理 caches 目录（服务端运行时创建，旧版缓存对新版可能不兼容）。

        实测位置在服务根目录下（GameAssistantToolServer/caches/models/emb/
        *.blob，含只读/被占用的缓存 blob），安装目录根部也可能有；两处都
        尽力清（只读先清属性、句柄未释放重试几轮），仍失败只告警不抛错——
        缓存清理失败绝不能阻断服务端更新，残留留给下次更新再尝试。
        """
        candidates = [self.install_dir / "caches"]
        if self._service_root:
            candidates.append(Path(self._service_root) / "caches")
        cleaned: set[str] = set()
        for caches in candidates:
            key = str(caches).lower()
            if key in cleaned or not caches.exists():
                continue
            cleaned.add(key)
            for attempt in range(3):
                _force_rmtree(caches)
                if not caches.exists():
                    if attempt:
                        self.log.info(
                            f"已清理缓存目录: {caches}（第 {attempt + 1} 次尝试）")
                    break
                time.sleep(1.0)
            else:
                self.log.warning(
                    f"缓存目录 {caches} 未能完全删除（可能被占用/只读），已跳过"
                    "——不影响本次更新，下次更新会再尝试")

    def _stop_local_service(self) -> bool:
        """停止本机正在运行的游戏助手服务（更新覆盖文件前必须）。

        依次尝试：本进程拉起的子进程 → HTTP /server/shutdown →
        /server/shutdown_kill。全部失败返回 False（调用方放弃提交；
        不允许 taskkill 任意进程）。
        """
        self.svc.stop()  # 本进程拉起的服务（没拉起过则 no-op）
        if not any(is_port_open(SERVICE_HOST, p) for p in service_ports()):
            return True
        import requests
        session = requests.Session()
        session.trust_env = False  # 本机调用绕过系统代理（见 _ensure_vision_enabled）
        for path in ("/server/shutdown", "/server/shutdown_kill"):
            for port in service_ports():
                if not is_port_open(SERVICE_HOST, port):
                    continue
                try:
                    session.post(f"http://{SERVICE_HOST}:{port}{path}", timeout=30)
                except Exception as e:
                    self.log.debug(f"  {path} ({port}): {e}")
            deadline = time.time() + 15
            while time.time() < deadline:
                if not any(is_port_open(SERVICE_HOST, p) for p in service_ports()):
                    self.log.info("  服务已停止")
                    return True
                time.sleep(1.0)
        return not any(is_port_open(SERVICE_HOST, p) for p in service_ports())

    def _install_model(self, name: str, cached_path: Path, target: Path):
        """把缓存里的模型安装到最终位置（move 优先，复制兜底）。"""
        import shutil
        if target.exists():
            # 旧目录删不掉不阻断安装：改为覆盖复制（同名文件以新版本为准）
            try:
                shutil.rmtree(target)
            except OSError as e:
                self.log.warning(
                    f"  {name}: 旧目录删除被拒（{e}），将覆盖复制安装")
        target.parent.mkdir(parents=True, exist_ok=True)
        installed = False
        if not target.exists():
            try:
                shutil.move(str(cached_path), str(target))
                installed = True
            except OSError:
                pass  # 移动被拒 → 复制兜底
        if not installed:
            try:
                shutil.copytree(str(cached_path), str(target),
                                dirs_exist_ok=True)
            except OSError as e:
                raise RuntimeError(
                    f"无法把模型 {name} 安装到 {target}"
                    f"（移动/复制均被拒绝，可能被 Agent 沙盒安全策略阻止，"
                    f"请放行后重试）: {e}"
                ) from e
            self.log.warning(
                f"  {name}: 移动/删除被沙盒拒绝，已改为覆盖复制安装；"
                f"下载缓存保留在 {cached_path}")
        self.state.mark_model_downloaded(name)
        self.log.info(f"  安装完成: {name} → {target}")

    def _capture_installed_fingerprints(self):
        """首装后记录服务端版本标记与模型指纹（尽力而为，失败只告警）。"""
        # 服务端：优先用 files API 当前布局的官方标记（与后续更新检测同源可比）
        try:
            root_files = list_repo_files(REPO_ID, root="", recursive=False)
            bin_files = (list_repo_files(REPO_ID, root="bin", recursive=True)
                         if root_files else [])
            remote = self._probe_service_remote(root_files, bin_files)
            if remote and remote.sha256:
                self.state.mark_service_version(
                    remote.sha256, layout=remote.layout,
                    source=remote.transport,
                    version_label=remote.version_label)
        except Exception as e:
            self.log.debug(f"查询服务端版本标记失败: {e}")
        if not self.state.service_sha256:
            # API 不可得 → 退化用本地测得的哈希（zip 解出的内层 7z，或本地 7z 包）
            local_sha = self.installer.last_inner_sha256
            if not local_sha and self.package_path is not None:
                try:
                    if (self.package_path.suffix.lower() == ".7z"
                            and self.package_path.exists()):
                        local_sha = sha256_file(self.package_path)
                except OSError:
                    local_sha = None
            if local_sha:
                self.state.mark_service_version(local_sha, source="local-measure")
        self._capture_model_fingerprints()

    def _capture_model_fingerprints(self):
        """记录本次安装的全部模型仓库指纹（更新候选判定用；尽力而为）。"""
        try:
            ids = {n: cfg["model_id"] for n, cfg in self.models_config.items()}
            fps = self._query_model_fingerprints(ids)
            if fps:
                merged = dict(self.state._data.get("models_fingerprint") or {})
                merged.update(fps)
                self.state.mark_models_fingerprint(merged)
                self.log.info(f"已记录 {len(fps)} 个模型的版本指纹")
        except Exception as e:
            self.log.debug(f"记录模型指纹失败: {e}")

    def _query_model_fingerprints(self, model_ids: dict) -> dict:
        """name -> 仓库内容指纹（查询失败的模型不产生条目）。"""
        out = {}
        for name, entries in self._query_model_files(model_ids).items():
            if entries:
                out[name] = fingerprint_files(entries)
        return out

    # ── Step 1b: 解压 ───────────────────────────────────────

    def _extract_and_install(self, archive: Path):
        self.log.info(f"校验: {archive}")
        self._emit("progress", stage="校验服务包...", value=0,
                   detail=str(archive))

        if not self.installer.verify_archive(archive):
            raise IOError("服务包校验失败，文件可能损坏")

        def on_extract_progress(stage, pct, detail):
            # 进度窗口的状态栏只显示 stage 文本（detail 会被丢弃），
            # 把百分比/文件数/耗时等实时信息拼进 stage 一起展示
            self._emit("progress",
                       stage=f"{stage} {pct:.1f}% {detail}".rstrip(),
                       value=pct)

        self.installer.extract(
            archive, self.install_dir, progress_cb=on_extract_progress,
        )
        self._stamp_installed_exe(archive)

        if not self.installer.verify(self.install_dir):
            raise RuntimeError(f"安装验证失败: {self.install_dir}")

        self.log.info("[OK] 服务安装完成")

    def _stamp_installed_exe(self, archive: Path) -> None:
        """安装后把服务端 exe 的修改时间对齐到安装包时间。

        py7zr 解压会保留压缩包内文件的构建时间——解压出来的 exe 天然比
        安装包"旧"，不对齐的话"exe 比安装包旧→视为有新版本"的判定会
        每次运行都误报更新。
        """
        try:
            exe = find_server_exe(self.install_dir)
            pkg_mtime = archive.stat().st_mtime
            if exe is not None and exe.stat().st_mtime < pkg_mtime:
                os.utime(exe, (pkg_mtime, pkg_mtime))
        except OSError:
            pass  # 对时失败只影响新旧判定，不影响安装本身

    # ── Step 2: 模型 ────────────────────────────────────────
    #
    # 参考: demo/dowload_models/download_modelscope_models.py
    # 流程: snapshot_download(repo_id, cache_dir=cache_root) → 下载到缓存
    #       shutil.move(cached, target)                        → 搬到最终位置
    # ModelScope SDK 内部的 snapshot_download 会逐文件比对缓存，
    # 仅下载缺失或不完整的文件，下载完成后返回缓存中的实际路径。

    def _download_models(self):
        self.log.info("─" * 40)
        self.log.info("下载 AI 模型")

        cache_root = self._service_root / "_modelscope_download_cache"
        cache_root.mkdir(parents=True, exist_ok=True)

        # 筛选需要下载的模型
        tasks = {}  # name -> (model_id, target)
        skipped = 0
        recorded_fps = self.state._data.get("models_fingerprint") or {}
        for name, cfg in self.models_config.items():
            target = self._service_root / cfg["dir"]
            if not self.force and target.exists() and any(target.iterdir()):
                self.log.info(f"  {name}: 已完成 → 跳过")
                skipped += 1
                if not self.state.is_model_downloaded(name):
                    self.state.mark_model_downloaded(name)
                continue
            if self._update_applied and not self.state.is_model_downloaded(name) \
                    and not recorded_fps.get(name):
                # 更新事务后不主动补装从未下载过的模型（用户要求：只更新已装的）
                self.log.info(f"  {name}: 未安装过 → 跳过（如需安装请用 --models {name}）")
                skipped += 1
                continue
            tasks[name] = (cfg["model_id"], target)

        total = len(self.models_config)
        todo = len(tasks)

        if not tasks:
            self.log.info("[OK] 所有模型已就绪")
            self._emit("progress", stage="模型已就绪", value=100, detail="")
            return

        self.log.info(f"需下载 {todo} 个模型（已完成 {skipped}/{total}）")

        # 各模型的仓库存储大小（进度条按体积加权用）：查询失败记 0，
        # 进度退化为按模型等权，不影响下载
        task_names = list(tasks.keys())
        model_totals = self._query_model_sizes(
            {n: mid for n, (mid, _) in tasks.items()})
        for n in task_names:
            size = model_totals.get(n, 0)
            self.log.info(f"  {n}: 仓库大小 {size / (1024 ** 3):.2f} GB"
                          if size else f"  {n}: 仓库大小未知，进度按模型等权估算")

        # 模型阶段在整体进度条上的区间 [lo, hi]（阶段 2 搬运占 [hi, 100]）
        lo = skipped / total * 100
        hi = lo + (100 - lo) * 0.95

        prog = _ModelsByteProgress()
        done_names: set[str] = set()
        done_models = [0]  # 线程可读的完成计数
        last_emit = time.monotonic()
        last_agg_log = [0.0]

        def emit_model_progress(force=False):
            """把真实字节进度映射到整体进度条的模型区间（节流 0.5s）。

            进度条分子 = 已下载字节、分母 = 模型仓库总字节（按体积加权），
            并每 5s 往日志写一条聚合进度——单文件的 tqdm 原始条已被
            _StderrToLogger 过滤，不再让"每文件从 0 涨到 100"的条目刷屏。
            """
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < 0.5:
                return
            last_emit = now
            frac = prog.phase_frac(task_names, model_totals, done_names)
            done_b, total_b = prog.byte_summary(task_names, model_totals,
                                                done_names)
            if total_b >= 1024 ** 3:
                size_s = (f"{done_b / 1024 ** 3:.2f}"
                          f"/{total_b / 1024 ** 3:.2f} GB")
            else:
                size_s = (f"{done_b / (1024 * 1024):.0f}"
                          f"/{max(total_b, 1) / (1024 * 1024):.0f} MB")
            self._emit("progress",
                       stage=(f"下载模型 {size_s} "
                              f"({done_models[0]}/{todo})"),
                       value=lo + (hi - lo) * frac,
                       detail="")
            if total_b > 0 and now - last_agg_log[0] >= 5.0:
                last_agg_log[0] = now
                self.log.info(f"  模型下载 {size_s} ({frac * 100:.0f}%)"
                              f" · {done_models[0]}/{todo} 个完成")

        prog.on_change = emit_model_progress
        emit_model_progress(force=True)

        # ── 阶段 1：多线程并行下载到缓存 ──
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # 拦截 stderr → logger.debug，捕获 tqdm 进度条到日志文件
        _stderr_tee = _StderrToLogger(self.log, sys.stderr)
        sys.stderr = _stderr_tee

        workers = min(todo, 3)
        downloaded: dict[str, Path] = {}  # name -> cached path
        failed: list[str] = []

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(self._fetch_model_to_cache,
                                name, model_id, cache_root, prog): name
                    for name, (model_id, _) in tasks.items()
                }
                for future in as_completed(future_map):
                    name = future_map[future]
                    try:
                        downloaded[name] = future.result()
                        done_models[0] += 1
                        done_names.add(name)
                        self.log.info(f"  [{done_models[0]}/{todo}] {name} 下载完成")
                        emit_model_progress(force=True)
                    except Exception as e:
                        done_models[0] += 1
                        self.log.error(f"  [{done_models[0]}/{todo}] {name} 下载失败: {e}")
                        failed.append(name)
        finally:
            sys.stderr = _stderr_tee._orig  # 恢复原始 stderr

        if failed:
            raise RuntimeError(
                f"{len(failed)} 个模型下载失败: {', '.join(failed)}"
            )

        # ── 阶段 2：串行搬到最终位置（搬运进度按模型数推进 [hi, 100]） ──
        # Agent 沙盒可能把"移动"视为删除源目录而拒绝：退化为纯复制安装，
        # 模型照常就位，下载缓存留在原地（后续运行按目标目录判断，不会重复下载）。
        self.log.info("全部下载完成，安装到目标目录...")
        self._emit("progress", stage="安装模型到目标目录...", value=hi, detail="")
        for i, (name, cached_path) in enumerate(downloaded.items()):
            _, target = tasks[name]
            self._install_model(name, cached_path, target)
            self._emit("progress", stage="安装模型到目标目录...",
                       value=hi + (100 - hi) * (i + 1) / len(downloaded),
                       detail=f"{name}: 完成")

        self.log.info("[OK] 模型下载完成")
        self._emit("progress", stage="模型下载完成", value=100, detail="")

    def _fetch_model_to_cache(self, name: str, model_id: str,
                              cache_root: Path, prog: "_ModelsByteProgress") -> Path:
        """单个模型下载到缓存（@retry 包装，线程安全；与更新事务共用）。"""
        from modelscope.hub.snapshot_download import snapshot_download
        cb_cls = prog.callback_cls(name) if prog is not None else None

        @retry
        def _do():
            try:
                if cb_cls is not None:
                    return snapshot_download(
                        model_id, cache_dir=str(cache_root),
                        progress_callbacks=[cb_cls])
                return snapshot_download(model_id, cache_dir=str(cache_root))
            except TypeError:
                # 旧版 ModelScope 无 progress_callbacks 参数：退化为无回调
                self.log.warning(f"  {name}: 当前 ModelScope 不支持进度回调，"
                                 "进度条将按模型完成数跳格")
                return snapshot_download(model_id, cache_dir=str(cache_root))

        return Path(_do()).resolve()

    def _query_model_sizes(self, model_ids: dict[str, str]) -> dict[str, int]:
        """查询各模型仓库的总字节大小（进度条按体积加权用）。

        查询失败（网络问题/旧版 SDK 无此接口）返回 0，进度条退化为按模型
        等权推进，不影响下载本身。
        """
        totals: dict[str, int] = {}
        for name, files in self._query_model_files(model_ids).items():
            size = sum(int(f.get("Size") or 0) for f in files)
            if not size:
                self.log.debug(f"  {name}: 查询仓库大小失败，进度按等权估算")
            totals[name] = size
        return totals

    # ── 配置修改 ────────────────────────────────────────────

    def _find_config_path(self) -> Path | None:
        """定位 gameassistanttoolserver.json。

        解压逻辑不保证压缩包顶层目录叫 GameAssistantToolServer（extract 会把内容
        拍平到安装目录），所以先按服务根目录（exe 所在处）找，再回退历史固定路径，
        最后整个安装目录兜底搜一次。找不到就返回 None。
        """
        service_root = self._service_root
        if service_root is None:
            exe = self.installer.find_executable(self.install_dir)
            service_root = exe.parent if exe else None

        roots = []
        if service_root is not None:
            roots.append(Path(service_root))
        roots.append(self.install_dir / "GameAssistantToolServer")
        roots.append(self.install_dir)

        for root in roots:
            candidate = root / "config" / "gameassistanttoolserver.json"
            if candidate.exists():
                return candidate
        # 不做全目录 rglob 兜底：安装目录里可能有备份副本，改错文件比不改更糟
        return None

    def _patch_config(self):
        """修改 gameassistanttoolserver.json 中 monitor_parent → false。

        改不到这个值时服务会在父进程退出后自杀，因此定位失败要显式告警。
        """
        config_path = self._find_config_path()

        if config_path is None:
            self.log.warning(
                f"未找到 gameassistanttoolserver.json（安装目录: {self.install_dir}），"
                "跳过 monitor_parent 修改；若服务随部署脚本退出而关闭，请手动改为 false"
            )
            return

        self.log.info(f"修改配置: {config_path}")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.log.warning(f"配置文件读取失败: {e}")
            return

        caller = config.get("caller", {})
        if caller.get("monitor_parent") is not True:
            self.log.info(f"monitor_parent 已是 {caller.get('monitor_parent')}，无需修改")
            return

        caller["monitor_parent"] = False
        tmp = config_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        tmp.replace(config_path)
        self.log.info("[OK] 已将 caller.monitor_parent: true → false")

    # ── Step 3: 启动 ────────────────────────────────────────

    def _step_start(self):
        self.log.info("─" * 40)
        self.log.info("启动游戏助手服务")

        running_port = next((p for p in service_ports() if is_port_open(SERVICE_HOST, p)), None)
        if running_port is not None:
            self.log.info(f"[OK] 服务已在运行 ({SERVICE_HOST}:{running_port})")
            self.state.mark_service_running(True)
            self._emit("done", message="服务已在运行")
            self._mark_deploy_task_done()
            return

        proc = self.svc.start()
        if proc is None:
            raise RuntimeError("服务启动失败")

        self._emit("progress", stage="等待服务就绪...", value=100,
                   detail=f"检查 {SERVICE_HOST} 端口 {'/'.join(map(str, service_ports()))}")

        def _on_wait(elapsed: int, timeout: int):
            # 进度窗口周期刷新等待时长：最长 300s 的静止画面会被误认为卡死
            self._emit("progress",
                       stage=(f"等待服务就绪...（已等待 {elapsed} 秒 / 最多 {timeout} 秒；"
                              f"首次启动要加载模型，可能明显更久）"),
                       value=100)

        if self.svc.wait_healthy(on_wait=_on_wait):
            port = resolve_service_port()
            self.state.mark_service_running(True)
            self.log.info("=" * 50)
            self.log.info("[OK] 部署成功！")
            self.log.info(f"   地址: http://{SERVICE_HOST}:{port}")
            self.log.info("=" * 50)

            # 确保 Vision 服务已启用（查询 → 未启用则启用 → 等待 15s → 再确认）
            if not self._ensure_vision_enabled():
                self.log.warning("Vision 服务启用确认未通过，请手动检查")

            self._emit("done", message=f"服务已就绪 http://{SERVICE_HOST}:{port}")
            self._mark_deploy_task_done()
        else:
            self.log.error("服务已启动但健康检查超时")
            self.log.info(f"   请手动验证 http://{SERVICE_HOST}:{SERVICE_PORT}")
            self._emit("error", message="服务启动失败（健康检查超时，请手动验证）")
            self._mark_deploy_task_error("健康检查超时，服务未就绪")

    def _ensure_vision_enabled(self) -> bool:
        """确保 Vision 服务已启用：查询 → 未启用则启用 → 等待 15s → 再确认。"""
        import requests
        # 本机服务调用绕过系统代理：Windows 上 requests 默认 trust_env=True 会读注册表
        # WinINET 代理，把 127.0.0.1 请求送进代理 → 403 HTML → json 解析失败。
        session = requests.Session()
        session.trust_env = False

        base = f"http://{SERVICE_HOST}:{resolve_service_port()}"

        # 1. 查询 Vision 是否已启用
        try:
            data = session.get(f"{base}/vision/service/enable", timeout=10).json()
            if data.get("code") == "ok" and data.get("data", {}).get("vision") is True:
                self.log.info("[OK] Vision 服务已启用，无需处理")
                return True
            self.log.info(f"Vision 服务未启用（查询: {data}），准备启用...")
        except Exception as e:
            self.log.warning(f"查询 Vision 启用状态失败: {e}")

        # 2. 启用 Vision 服务
        try:
            data = session.post(f"{base}/vision/service/enable/true", timeout=40).json()
            if data.get("code") != "ok":
                self.log.warning(f"启用 Vision 服务失败: {data}")
                return False
            self.log.info("[OK] 已提交启用 Vision 服务请求")
        except Exception as e:
            self.log.error(f"启用 Vision 服务失败: {e}")
            return False

        # 3. 等待 15 秒后再次查询确认
        self.log.info("等待 15 秒后确认 Vision 服务启用状态...")
        time.sleep(15)

        try:
            data = session.get(f"{base}/vision/service/enable", timeout=10).json()
            ok = data.get("code") == "ok" and data.get("data", {}).get("vision") is True
            if ok:
                self.log.info("[OK] Vision 服务启用确认通过")
            else:
                self.log.warning(f"Vision 服务启用确认失败: {data}")
            return ok
        except Exception as e:
            self.log.error(f"确认 Vision 启用状态失败: {e}")
            return False

    def _write_task_status(self, status: str, stage: str, progress: int, detail: str = ""):
        """写 task_status.json（写到数据目录，与 service_manager 对齐）。

        串联模式（GA_SKILL_CHAIN=1）下由 service_manager 统一维护任务状态，这里跳过。
        """
        if os.environ.get("GA_SKILL_CHAIN") == "1":
            return
        status_file = _resolve_data_dir(self.install_dir) / "task_status.json"
        try:
            status_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "task": "deploy",
                "status": status,
                "stage": stage,
                "progress": progress,
                "detail": detail,
            }
            tmp = status_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(status_file)
        except Exception as e:
            self.log.debug(f"更新 task_status.json 失败: {e}")

    def _mark_deploy_task_done(self):
        """标记部署任务完成。"""
        self._write_task_status(
            "done", "部署完成，服务已就绪", 100,
            f"服务地址: http://{SERVICE_HOST}:{resolve_service_port()}",
        )

    def _mark_deploy_task_error(self, stage: str):
        """标记部署任务失败。"""
        self._write_task_status("error", stage, 0)


# ============================================================================
# Section 10: Main — GUI 主循环 + 后台工作线程
# ============================================================================

def main():
    import tkinter as tk

    # ── CLI ──────────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="游戏助手服务一键部署脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python deploy.py                           # 自动下载，失败回退手动
    python deploy.py --mode manual             # 弹窗手动选择
    python deploy.py --package ./service.7z    # 指定本地包
    python deploy.py --skip-models             # 跳过模型下载
    python deploy.py --models LLM,Embedding    # 只下载指定模型
    python deploy.py --lang zh                 # Splitter 使用中文模型
    python deploy.py --start-only              # 仅启动已安装服务
    python deploy.py --force                   # 强制重装
        """,
    )
    parser.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR,
                        help="安装目录 (默认: %%LOCALAPPDATA%%\\GameAssistant)")
    parser.add_argument("--mode", choices=["auto", "manual", "auto-fallback"],
                        default="auto-fallback",
                        help="服务包获取方式 (默认: auto-fallback)")
    parser.add_argument("--package", type=Path, default=None,
                        help="本地服务包路径")
    parser.add_argument("--service-url", type=str, default=SERVICE_URL,
                        help="服务包下载地址（默认 ModelScope，也可用 GitHub 7z 直链）")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh",
                        help="Splitter 模型语言 (默认: zh)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--skip-models", action="store_true",
                       help="跳过模型下载")
    group.add_argument("--models", type=str, default=None, metavar="LIST",
                       help="只下载指定模型（逗号分隔，如 LLM,Embedding,Rerank）")
    parser.add_argument("--start-only", action="store_true",
                        help="仅启动已安装的服务")
    parser.add_argument("--force", action="store_true",
                        help="强制重新安装（忽略已有状态）")

    args = parser.parse_args()
    # 支持 %LOCALAPPDATA% 这类环境变量路径（bash/cmd/PowerShell 下 %VAR% 不一定被 shell 展开）
    requested_install_dir = Path(os.path.expandvars(str(args.install_dir)))
    # install_dir 是预装目录：不存在/不可写时 logger 与状态文件先落到默认目录；
    # fresh 安装的目标选择（默认目录 vs 用户指定的已解压目录）在 _run 里决定
    args.install_dir = _resolve_startup_dir(requested_install_dir)

    only_models = parse_models(args.models) if args.models else None

    # 单实例保护在 Deployer.__init__ 内（setup_logger 创建 install_dir 之后）执行：
    # 抢不到锁抛 RuntimeError，这里捕获后退出，避免再起一个实例造成两个进度窗口。
    try:
        deployer = Deployer(
            install_dir=args.install_dir,
            mode=args.mode,
            package_path=args.package,
            lang=args.lang,
            skip_models=args.skip_models,
            only_models=only_models,
            start_only=args.start_only,
            force=args.force,
            service_url=args.service_url,
        )
    except RuntimeError as e:
        print(f"跳过部署: {e}")
        return 0

    deployer.requested_install_dir = requested_install_dir

    # ── Tkinter 初始化：root 即进度窗口 ──────────────────────
    root = tk.Tk()
    log_path = str(deployer.install_dir / "log" / "deploy" / "deploy.log")
    dialog = ProgressDialog(root, log_path=log_path,
                            title="游戏助手服务部署")

    # 用户点 X → 停掉服务 → 标记取消 → 退出
    def _on_user_close():
        deployer.svc.stop()
        if os.environ.get("GA_SKILL_CHAIN") == "1":
            return  # 同上：串联模式下的任务状态由 service_manager 负责
        try:
            status_file = _resolve_data_dir(args.install_dir) / "task_status.json"
            status_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"task": "deploy", "status": "error", "stage": "用户取消了部署"}
            tmp = status_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(status_file)
        except Exception:
            pass

    dialog.set_close_callback(_on_user_close)

    final_result = {"success": False, "message": "", "finished": False}
    countdown_started = False

    # ── 主线程 GUI 消息处理 ──────────────────────────────────

    def process_messages():
        nonlocal countdown_started
        try:
            while True:
                msg_type, payload = deployer._gui_q.get_nowait()

                if msg_type == "progress":
                    dialog.update(
                        stage=payload.get("stage"),
                        progress=payload.get("value"),
                    )
                elif msg_type == "done":
                    final_result["success"] = True
                    final_result["message"] = payload.get("message", "")
                    final_result["finished"] = True
                    dialog.update(stage=payload.get("message", "完成"),
                                  progress=100)
                    if not countdown_started:
                        dialog.schedule_close(seconds=8)
                        countdown_started = True
                elif msg_type == "error":
                    final_result["success"] = False
                    final_result["message"] = payload.get("message", "")
                    final_result["finished"] = True
                    dialog.update(stage=f"错误: {payload.get('message', '')}",
                                  progress=0)
                    if not countdown_started:
                        dialog.schedule_close(seconds=10)
                        countdown_started = True
                elif msg_type == "need_picker":
                    # 工作线程需要弹窗选择文件 → 在主线程创建弹窗
                    picker = FilePickerDialog(root, payload.get("url", SERVICE_URL))
                    chosen = picker.show()
                    deployer._picker_result.put(chosen)
                elif msg_type == "need_confirm":
                    # 更新确认弹窗（默认/超时 = 暂不更新）
                    dlg = UpdateConfirmDialog(root, payload.get("summary", {}))
                    deployer._confirm_result.put(bool(dlg.show()))
                elif msg_type == "need_install_choice":
                    # 安装方式选择弹窗（默认/超时 = 自动下载并安装）
                    dlg = InstallChoiceDialog(root, payload.get("install_dir", ""))
                    deployer._install_choice_result.put(dlg.show())
        except queue.Empty:
            pass

        # 刷新日志显示
        dialog.refresh_log()

        # 倒计时关闭检查
        if countdown_started and dialog.tick_close():
            return  # 窗口已关闭

        root.after(500, process_messages)

    # ── 后台工作线程 ─────────────────────────────────────────

    worker = threading.Thread(target=deployer.execute, daemon=True)
    worker.start()

    # ── 启动主循环 ───────────────────────────────────────────

    root.after(100, process_messages)
    root.mainloop()

    # 等待工作线程结束
    worker.join(timeout=5)

    # ── 最终输出 ─────────────────────────────────────────────

    if final_result["success"]:
        msg = final_result["message"]
        print(f"\n  [OK] {msg}\n")
        return 0
    else:
        msg = final_result["message"] or "未知错误"
        print(f"\n  [FAIL] 部署失败: {msg}")
        print(f"  详细日志: {args.install_dir / 'log' / 'deploy' / 'deploy.log'}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
