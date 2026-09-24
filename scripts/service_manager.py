#!/usr/bin/env python3
"""
游戏助手服务管理脚本
  部署 / 健康检查 / 关闭服务 / 攻略下载 / 进度查询

用法:
    python service_manager.py health              # 健康检查（瞬间返回）
    python service_manager.py deploy              # 后台部署，立即返回
    python service_manager.py download <游戏名>    # 后台下载攻略，立即返回
    python service_manager.py launch [游戏名]      # 启动攻略助手客户端；带游戏名=直启（跳过检测）
    python service_manager.py shutdown            # 关闭服务（连同攻略助手客户端）
    python service_manager.py status              # 查询后台任务进度
    python service_manager.py ensure              # 健康检查 + 按需部署
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request
from urllib.error import URLError

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "GameAssistant"

# 本脚本的 stdout 是 agent 的解析契约（纯 JSON），日志一律走 stderr。
# 同时把两个流强制成 UTF-8，避免中文在 GBK 控制台下变成乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
_LOG_DIR = _DEFAULT_DATA_DIR
_LOG_SUB_DIR = _LOG_DIR / "log"
_STATUS_FILE = _LOG_DIR / "task_status.json"

SERVICE_HOST = "127.0.0.1"
# 9190 与部分 Windows 服务冲突，新版服务端默认 22919；旧版已发布出去仍在用
# 9190 —— 运行时经 resolve_service_port() 探测（校验服务身份），显示用新默认。
SERVICE_PORT = 22919
SERVICE_PORT_LEGACY = 9190
SERVICE_URL = f"http://{SERVICE_HOST}:{SERVICE_PORT}"
DEPLOY_SCRIPT = _SCRIPT_DIR / "deploy.py"
DOWNLOAD_SCRIPT = _SCRIPT_DIR / "download_with_progress.py"
# v2 客户端：GameWalkthroughV2/run_app.py（自带检测/截图/推送/浮窗/扫码面板）
GAME_CLIENT_SCRIPT = _SCRIPT_DIR.parent / "GameWalkthroughV2" / "run_app.py"
_GAME_CLIENT_PID_FILE = _LOG_DIR / "game_client.pid"



def _setup_logger(name: str) -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_SUB_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 日志轮转：跨天后第一次配置 logger 时，把旧日志改名成带日期的历史文件
    # （只保留最近一份，更早的删除）。不清空直接覆盖——跨天后的 status 轮询
    # 也会触发 setup，清空会让人以为"日志没了"。改名失败（旧文件被长任务
    # 占用）就保留原文件继续追加，混排两天内容好过丢日志。
    log_path = _LOG_SUB_DIR / "service_manager.log"
    try:
        if log_path.exists() and datetime.fromtimestamp(
            log_path.stat().st_mtime
        ).date() != datetime.now().date():
            prev_date = datetime.fromtimestamp(
                log_path.stat().st_mtime).strftime("%Y-%m-%d")
            keep = _LOG_SUB_DIR / f"service_manager.{prev_date}.log"
            keep.unlink(missing_ok=True)
            log_path.rename(keep)
    except OSError:
        pass
    # delay=True：第一条日志真正写入时才创建文件——status/health 这类
    # 只打印 JSON 的命令不应留下空文件误导排障（flush 不受影响：
    # logging 每条记录写完都会显式 flush）。
    fh = logging.FileHandler(str(log_path), mode="a", encoding="utf-8",
                             delay=True)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log = _setup_logger("service_manager")


def _resolve_data_dir(install_dir: str) -> tuple[Path, bool]:
    """数据目录：install_dir 非空且可写→用它；否则回落默认。返回 (目录, 是否回落)。"""
    install_dir = (install_dir or "").strip()
    if not install_dir:
        return _DEFAULT_DATA_DIR, False
    p = Path(install_dir)
    if not p.is_dir():
        return _DEFAULT_DATA_DIR, True  # 不存在就回落，不要凭一个拼错的路径现造目录
    try:
        probe = p / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        return p, False
    except OSError:
        return _DEFAULT_DATA_DIR, True


def _apply_data_dir(install_dir: str) -> None:
    """将日志/状态根目录切到 install_dir（可写时）或回落默认目录。"""
    global _LOG_DIR, _LOG_SUB_DIR, _STATUS_FILE, _GAME_CLIENT_PID_FILE, log

    old_dir = _LOG_DIR
    _LOG_DIR, fell_back = _resolve_data_dir(install_dir)
    _LOG_SUB_DIR = _LOG_DIR / "log"
    _STATUS_FILE = _LOG_DIR / "task_status.json"
    _GAME_CLIENT_PID_FILE = _LOG_DIR / "game_client.pid"
    log = _setup_logger("service_manager")

    if _LOG_DIR != old_dir:
        # 日志位置变了就明说，避免按旧路径找文件时看到"空的/不存在"
        log.info("日志/状态目录: %s", _LOG_DIR)
    if fell_back:
        msg = f"预装路径不可写，日志/状态目录已回落到默认目录: {_LOG_DIR}"
        log.warning(msg)
        print(f"[warning] {msg}", file=sys.stderr)


# ── 状态文件 ────────────────────────────────────────────────

def _read_status() -> dict:
    if not _STATUS_FILE.exists():
        return {"task": "none", "status": "idle"}
    try:
        with open(_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"task": "none", "status": "idle"}
    except (json.JSONDecodeError, OSError):
        return {"task": "none", "status": "idle"}


def _write_status(task: str, status: str, stage: str = "",
                  progress: float = -1, detail: str = "",
                  **extra) -> None:
    payload: dict = {
        "task": task,
        "status": status,
        "stage": stage,
        "progress": max(0, min(100, progress)) if progress >= 0 else -1,
        "detail": detail,
        **extra,
    }
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATUS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # Windows 上另一个进程正在读该文件时 replace 会失败，重试几次即可
    for attempt in range(3):
        try:
            tmp.replace(_STATUS_FILE)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.2)


def _is_pid_alive(pid: int) -> bool:
    """检查进程是否存活（Windows 用）。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


# ── 进度解析 ────────────────────────────────────────────────

# 攻略页码，如 "处理第3页"
_PAGE_RE = re.compile(r"处理第(\d+)页")

# 分段标记。关键词必须挑**只有真正进入该阶段时才会打印**的原文片段：
# 像 "模型"、"下载" 这种泛化词会被配置announcement行命中（例如 deploy 启动时打的
# "--skip-models: 跳过模型下载"），把进度一上来就顶到高位，后面真实阶段反而被压住。
# 列表顺序敏感：先匹配到的赢。
_DEPLOY_PHASES = [
    ("已在运行", ("服务已在运行", 100)),
    ("部署成功", ("部署完成", 100)),
    ("[OK] 就绪", ("服务就绪", 100)),
    # —— 版本更新检测（关键词与 deploy.py 日志原文精确对应，措辞勿改）。
    # pct 必须随执行顺序单调（status 对整份日志取最大值）：
    # 8 检测 → 10 等用户确认（更新/安装方式弹窗，可能停很久）→ 12 下载更新包
    # →（20 下载服务包 / 45 解压 / 60 模型）→ 80 应用更新 → 90 启动 → 100 就绪
    ("检测服务端与模型更新", ("检测更新", 8)),
    ("等待用户确认", ("等待用户确认", 10)),
    ("下载更新包", ("下载更新", 12)),
    ("应用更新", ("应用更新", 80)),
    ("模型下载完成", ("模型下载完成", 85)),
    ("安装到目标目录", ("安装模型", 80)),
    ("下载 AI 模型", ("下载 AI 模型", 60)),
    ("解压", ("安装服务包", 45)),
    ("下载: http", ("下载服务包", 20)),
    # 手动模式下弹窗等用户选包，可能停留很久，必须能显示出来
    ("等待用户选择服务包", ("等待手动选择服务包", 5)),
    ("启动:", ("启动服务", 90)),
    ("启动游戏助手服务", ("启动服务", 90)),
    ("就绪 (最多", ("启动服务", 90)),
]

# 下载链路分段标记。带方括号的是 importer 的固定前缀，避免误吃正文里的 vision/knowledge 字样
_DOWNLOAD_PHASES = [
    ("导入完成", ("全部完成", 100)),
    ("[vision]", ("导入场景图片", 92)),
    ("[knowledge]", ("导入文本知识库", 88)),
    ("下载结束", ("攻略下载完成", 85)),
    ("处理第", ("下载攻略页面", 10)),
    ("输出目录", ("准备下载目录", 8)),
    ("开始下载攻略", ("搜索攻略", 2)),
]


def _parse_progress(task: str, line: str) -> tuple[str, float, str]:
    """从子进程输出行中提取 (stage, progress, detail)。"""
    stage = ""
    pct = -1.0

    phases = _DEPLOY_PHASES if task == "deploy" else _DOWNLOAD_PHASES
    for keyword, (phase_label, phase_pct) in phases:
        if keyword in line:
            stage = phase_label
            pct = float(phase_pct)
            break

    # 翻页是攻略下载里最长的一段，用页码细化，否则会长时间停在 10%
    if task != "deploy":
        m = _PAGE_RE.search(line)
        if m:
            pct = 10.0 + 70.0 * min(int(m.group(1)), 20) / 20.0

    # detail: 取行末尾的进度信息，清理掉只有空白/符号的行
    detail = line.strip()
    if detail and len(detail) > 100:
        detail = detail[:100] + "..."

    return stage, pct, detail


# ── 后台执行 ────────────────────────────────────────────────


def _child_env() -> dict:
    """后台子进程一律用 UTF-8 输出。

    否则子进程（deploy.py / download_with_progress.py）在中文 Windows 上按 GBK 写日志，
    而本脚本自己的日志行是 UTF-8，同一个文件混两种编码 → 读取时 UnicodeDecodeError
    → 进度解析和成功标记判定全部静默失效（异常被 except 吞掉）。

    同时把 127.0.0.1/localhost 合并进 no_proxy：Agent 沙盒常注入 http_proxy，
    会把子进程对本机服务（健康检查、浮窗/webserver 访问）的请求也送进代理而
    失败。只影响回环，外网下载照常走代理。no_proxy/NO_PROXY 两个键取并集后
    写成同一份（env 是普通 dict，键区分大小写；两键值不一致时子进程会漏掉
    回环免代理）。与 GameWalkthroughV2/app/env_setup.py 的 ensure_no_proxy 对齐。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"  # 只改 std 流；不用 PYTHONUTF8，那会连带改掉三方库 open() 的默认编码
    entries: list[str] = []
    for key in ("no_proxy", "NO_PROXY"):
        for item in env.get(key, "").split(","):
            item = item.strip()
            if item and item.lower() not in {e.lower() for e in entries}:
                entries.append(item)
    entries += [h for h in ("127.0.0.1", "localhost")
                if h.lower() not in {e.lower() for e in entries}]
    merged = ",".join(entries)
    env["no_proxy"] = merged
    env["NO_PROXY"] = merged
    return env


def _clear_stale_status():
    """清理旧任务状态，避免残留干扰。"""
    if _STATUS_FILE.exists():
        _STATUS_FILE.unlink(missing_ok=True)


def _launch_background(task: str, cmd: list[str], initial_stage: str = "") -> int | None:
    """启动独立后台子进程，stdout 重定向到日志文件。主进程立即返回。

    返回子进程 pid；启动失败返回 None（调用方必须据此报错，不要照样回 started）。
    """
    _clear_stale_status()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_log_dir = _LOG_SUB_DIR / task
    task_log_dir.mkdir(parents=True, exist_ok=True)

    stdout_log = task_log_dir / "stdout.log"
    log.info("[%s] 后台启动: %s, 日志: %s", task, cmd, stdout_log)

    try:
        with open(stdout_log, "w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
            )
    except Exception as e:
        log.error("[%s] 启动子进程失败: %s", task, e)
        _write_status(task, "error", stage="启动失败", detail=str(e),
                      stdout_log=str(stdout_log), started_at=started_at)
        return None

    _write_status(task, "running",
                  stage=initial_stage or ("正在部署服务" if task == "deploy" else "正在搜索攻略"),
                  progress=0, detail="",
                  pid=proc.pid, stdout_log=str(stdout_log), started_at=started_at)

    return proc.pid


# ── 端口检测 ────────────────────────────────────────────────

def is_port_open(host: str = SERVICE_HOST, port: int = SERVICE_PORT,
                 timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


_resolved_port: int | None = None


def _service_answers_on(port: int, timeout: float = 2.0) -> bool:
    """端口上应答的是不是游戏助手服务（响应 dict 且含 code 字段）。

    只探端口通不够：端口可能被别的程序占着。本机调用绕开系统代理，
    同 _post_shutdown。
    """
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(f"http://{SERVICE_HOST}:{port}/vision/service/enable",
                         timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return isinstance(body, dict) and "code" in body
    except Exception:
        return False


def resolve_service_port() -> int:
    """服务端实际监听端口：先试新版默认 22919，身份校验通过才用；不通再回退
    旧版 9190（旧服务端已发布出去）。结果缓存；两个端口都无应答时返回 22919，
    让后续请求自然报连接错误。"""
    global _resolved_port
    if _resolved_port is None:
        for port in (SERVICE_PORT, SERVICE_PORT_LEGACY):
            if _service_answers_on(port):
                _resolved_port = port
                break
        else:
            _resolved_port = SERVICE_PORT
    return _resolved_port


def _service_url() -> str:
    return f"http://{SERVICE_HOST}:{resolve_service_port()}"


# ── 健康检查 ────────────────────────────────────────────────

def check_health(host: str | None = None, port: int | None = None) -> bool:
    host = host or SERVICE_HOST
    port = port or resolve_service_port()
    ok = is_port_open(host, port)
    log.info("健康检查 %s:%d -> %s", host, port, "通过" if ok else "失败")
    return ok


def probe_service(attempts: int = 3, interval: float = 4.0) -> str:
    """区分三种情况：ok=游戏助手服务在跑；down=端口没开；foreign=端口开着但不是它。

    只探端口会把"端口被别的程序占用"误判成健康，后续所有请求都打到别人身上，
    报错对小白毫无意义。服务冷启动要十几秒，因此失败会重试几次再下结论。
    新版服务在 22919，旧版（已发布的部署）在 9190：按此顺序探测，命中哪个
    后续请求就打哪个（缓存在 _resolved_port）。
    """
    global _resolved_port
    any_open = False
    for port in (SERVICE_PORT, SERVICE_PORT_LEGACY):
        if not is_port_open(SERVICE_HOST, port):
            continue
        any_open = True
        opener = request.build_opener(request.ProxyHandler({}))  # 绕开系统代理，同 _post_shutdown
        for attempt in range(1, attempts + 1):
            try:
                with opener.open(f"http://{SERVICE_HOST}:{port}/vision/service/enable",
                                 timeout=5) as resp:
                    body = json.loads(resp.read().decode("utf-8", "replace"))
                if isinstance(body, dict) and "code" in body:
                    _resolved_port = port
                    return "ok"
                log.warning("%d 应答不像游戏助手服务: %s", port, str(body)[:120])
            except Exception as e:
                log.info("服务探测第 %d/%d 次未通过: %s", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(interval)
    if not any_open or not any(
            is_port_open(SERVICE_HOST, p) for p in (SERVICE_PORT, SERVICE_PORT_LEGACY)):
        return "down"
    return "foreign"


def _foreign_port_payload() -> dict:
    return {
        "status": "error",
        "message": (f"端口 {SERVICE_PORT}/{SERVICE_PORT_LEGACY} 被其他程序占用（应答的不是游戏助手服务），"
                    "或服务仍在启动中。请关闭占用该端口的程序后重试。"),
    }



def _is_bg_running() -> bool:
    """检查当前是否有后台任务在执行。"""
    data = _read_status()
    if data.get("status") != "running":
        return False
    pid = data.get("pid")
    if pid is not None and _is_pid_alive(int(pid)):
        return True
    # PID 已死但状态未更新 — 检查退出码
    return False


def _check_and_finalize_status(data: dict) -> dict:
    """后台进程已退出时，根据日志内容判定最终状态。"""
    pid = data.get("pid")
    if pid is None:
        return data
    if _is_pid_alive(int(pid)):
        return data

    stdout_log = data.get("stdout_log", "")
    task = data.get("task", "")

    # deploy 任务不看日志关键词，直接确认服务真的能应答（只看端口会把
    # "9190 被别的程序占用" 误报成部署成功）
    if task == "deploy":
        # 这个判决会落盘且不会再被重新评估（status 只对 running 任务 finalize），
        # 所以不能只探一次就把还在预热的服务判死。
        state = probe_service(attempts=3)
        if state == "ok":
            data["status"] = "done"
            data["progress"] = 100
            data["stage"] = "部署完成，服务已就绪"
        else:
            data["status"] = "error"
            data["stage"] = "部署失败"
            data["detail"] = (_foreign_port_payload()["message"] if state == "foreign"
                              else "部署进程已退出，服务仍不可用，详见部署日志")
            log.warning("[deploy] PID %s 已退出，服务探测=%s", pid, state)
        _write_status(**data)
        return data

    # 成功标记（按任务类型区分）。注意不能用 "导出完成"——那只是攻略抓取阶段的收尾，
    # 后面还有导入服务这一步，用它判定会把"下载成功但导入失败"报成完成。
    done_markers = {
        "download": ["导入完成"],
    }
    # 失败标记
    error_markers = ["失败", "error:", "Error:", "异常", "找不到", "不存在"]

    markers = done_markers.get(task, [])
    done = False
    has_error = False

    if stdout_log:
        try:
            # errors="replace"：旧版本留下的 GBK 日志不至于让整段判定失效
            with open(stdout_log, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            for m in markers:
                if m in txt:
                    done = True
                    break
            if not done:
                for m in error_markers:
                    if m in txt:
                        has_error = True
                        break
        except Exception:
            pass

    if done:
        data["status"] = "done"
        data["progress"] = 100
        data["stage"] = "全部完成"
    elif has_error:
        data["status"] = "error"
        data["stage"] = "任务失败"
        data["detail"] = "任务执行失败，详见日志"
    else:
        data["status"] = "error"
        data["stage"] = "任务失败"
        data["detail"] = "进程已退出但未检测到成功标记"
        log.warning("[%s] PID %s 已退出，未匹配到成功标记", task, pid)

    _write_status(**data)
    return data


# ── 部署（后台） ──────────────────────────────────────────────

# 合法模型名（需与 deploy.py 的 _COMMON + Splitter 保持一致）
_MODEL_NAMES = {"LLM", "Embedding", "Rerank", "MMR", "ASR", "OCR", "Action", "VLM", "Splitter"}


def _validate_models(models: str) -> str:
    """校验 --models 参数合法性。合法返回空串，非法返回错误信息。"""
    models = (models or "").strip()
    if not models:
        return "必须指定 --models（all / skip / 模型名列表）"
    if models in ("all", "skip"):
        return ""
    names = [n.strip() for n in models.split(",") if n.strip()]
    invalid = [n for n in names if n not in _MODEL_NAMES]
    if invalid:
        return f"非法模型名: {', '.join(invalid)}；可选: all / skip / {', '.join(sorted(_MODEL_NAMES))}"
    return ""


_SKILL_FILE = _SCRIPT_DIR.parent / "SKILL.md"


def _read_skill_defaults(skill_file: Path | None = None) -> dict:
    """从 SKILL.md 的 frontmatter 读 models / install_dir。

    这两个值本来就声明在 SKILL.md 里。让脚本自己读，agent 就不必每条命令
    原样转述一遍，也不会出现 download / launch / status 三处传得不一致而
    读错状态文件。读不到（文件缺失或格式不符）就返回空，由 CLI 参数兜底。
    """
    path = skill_file or _SKILL_FILE
    values: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").lstrip("﻿").splitlines()  # 记事本存的 UTF-8 带 BOM
    except OSError:
        return values
    if not lines or lines[0].strip() != "---":
        return values
    for line in lines[1:]:
        if line.strip() == "---":
            break
        # 只认顶层键（不允许缩进），否则 frontmatter 里嵌套块中的同名键会被当成顶层值
        m = re.match(r"^(models|install_dir)\s*:\s*(.*)$", line)
        if not m:
            continue
        # 只有 # 前面有空白才当行内注释，否则 "D:/Game#1" 这类路径会被截断
        raw = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip()
        values[m.group(1)] = raw.strip("\"'")
    return values


def _validate_install_dir(install_dir: str) -> str:
    """校验 install_dir（预装服务目录）有效性。空串表示用默认路径，返回空串；非法返回错误信息。

    「目录不存在 / 目录里还没有 exe」不再算致命错误：这两种情况会照常透传给
    deploy.py，由其弹窗询问用户安装方式（自动下载 / 指定本地服务包 / 取消）。
    只有指向一个普通文件这种真正非法的路径才直接报错。
    """
    install_dir = (install_dir or "").strip()
    if not install_dir:
        return ""
    p = Path(install_dir)
    if p.exists() and not p.is_dir():
        return f"预装路径不是目录: {p}"
    return ""


def _deploy_cmd(models: str, install_dir: str) -> tuple[list[str] | None, str, str]:
    """拼部署命令，返回 (cmd, 模型策略描述, 错误信息)。cmd 为 None 表示不可执行。"""
    err = _validate_models(models)
    if err:
        return None, "", err
    if not DEPLOY_SCRIPT.exists():
        return None, "", f"部署脚本不存在: {DEPLOY_SCRIPT}"

    cmd = [sys.executable, str(DEPLOY_SCRIPT), "--mode", "auto-fallback"]
    if install_dir:
        cmd += ["--install-dir", install_dir]
    if models == "all":
        strategy = "全部下载"
    elif models == "skip":
        cmd += ["--skip-models"]
        strategy = "跳过下载"
    else:
        # 已通过 _validate_models 校验，此处是合法模型名列表
        cmd += ["--models", models]
        strategy = f"只下载 {models}"
    return cmd, strategy, ""


def deploy(models: str = "", install_dir: str = "") -> None:
    err = _validate_install_dir(install_dir)
    if err:
        _write_status("deploy", "error", stage="预装路径无效", detail=err)
        print(json.dumps({"status": "error", "message": err}, ensure_ascii=False))
        return

    if _is_bg_running():
        log.warning("已有后台任务在运行")
        print(json.dumps({"status": "busy", "message": "已有后台任务在运行，请等待完成"},
                         ensure_ascii=False))
        return

    cmd, strategy, err = _deploy_cmd(models, install_dir)
    if cmd is None:
        _write_status("deploy", "error", stage="无法启动部署", detail=err)
        print(json.dumps({"status": "error", "message": err}, ensure_ascii=False))
        return

    log.info("后台启动部署: %s --mode auto-fallback（模型策略: %s）", DEPLOY_SCRIPT, strategy)
    if _launch_background("deploy", cmd) is None:
        print(json.dumps({"status": "error",
                          "message": "部署进程启动失败: " + _read_status().get("detail", "")},
                         ensure_ascii=False))
        return
    print(json.dumps({"status": "started", "task": "deploy", "models": strategy},
                     ensure_ascii=False))


# ── 攻略下载（后台） ─────────────────────────────────────────

def download(game_name: str, models: str = "", install_dir: str = "") -> None:
    if _is_bg_running():
        log.warning("已有后台任务在运行")
        print(json.dumps({"status": "busy", "message": "已有后台任务在运行，请等待完成"},
                         ensure_ascii=False))
        return

    if not DOWNLOAD_SCRIPT.exists():
        msg = f"下载脚本不存在: {DOWNLOAD_SCRIPT}"
        log.error(msg)
        print(json.dumps({"status": "error", "message": msg}, ensure_ascii=False))
        return

    state = probe_service()
    if state == "foreign":
        print(json.dumps(_foreign_port_payload(), ensure_ascii=False))
        return

    if state == "down":
        # 服务没起来：后台一口气做完「部署 → 下载」，不要求 agent 事后重下命令
        log.info("服务未运行，后台串联部署+下载: %s", game_name)
        if _launch_background("download", _chain_cmd("chain-download", game_name, models, install_dir),
                              initial_stage="正在部署服务") is None:
            print(json.dumps({"status": "error",
                              "message": "后台任务启动失败: " + _read_status().get("detail", "")},
                             ensure_ascii=False))
            return
        print(json.dumps({"status": "started", "task": "download", "game_name": game_name,
                          "message": "服务未就绪，已开始自动部署，完成后会继续下载攻略"},
                         ensure_ascii=False))
        return

    log.info("后台启动下载: %s", game_name)
    if _launch_background("download", _download_cmd(game_name, install_dir)) is None:
        print(json.dumps({"status": "error",
                          "message": "下载进程启动失败: " + _read_status().get("detail", "")},
                         ensure_ascii=False))
        return
    print(json.dumps({"status": "started", "task": "download",
                      "game_name": game_name}, ensure_ascii=False))


# ── 关闭服务 ────────────────────────────────────────────────

def _post_shutdown(endpoint: str) -> bool:
    url = f"{_service_url()}{endpoint}"
    data = json.dumps({}).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    # 本机服务调用绕过系统代理：urllib 默认 getproxies() 在 Windows 会读注册表代理，
    # 把 127.0.0.1 请求送进代理 → 403 Forbidden。与 deploy.py 的 Vision 修复对齐。
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("code") == "ok":
                log.info("关闭成功: %s -> %s", endpoint, body)
                return True
            log.warning("关闭返回非 ok: %s -> %s", endpoint, body)
            return False
    except URLError as e:
        log.warning("关闭请求失败: %s -> %s", endpoint, e)
        return False


# ── 启动攻略窗口（独立进程）────────────────────────────────

def _running_client_pid() -> int | None:
    """PID 文件里那个攻略窗口还活着就返回它的 pid，否则清掉过期文件。

    只判"PID 存在"不够：机器重启后同一个号可能被别的进程占用，那会让 launch
    永远返回 already_running、用户再也打不开攻略窗口。这里连进程名一起核对。
    """
    if not _GAME_CLIENT_PID_FILE.exists():
        return None
    try:
        pid = int(_GAME_CLIENT_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        _GAME_CLIENT_PID_FILE.unlink(missing_ok=True)
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        if str(pid) in result.stdout and "python" in result.stdout.lower():
            return pid
    except Exception:
        return None
    _GAME_CLIENT_PID_FILE.unlink(missing_ok=True)
    return None


def _start_game_client(game_name: str = "") -> int:
    """拉起攻略助手客户端（run_app.py），返回 pid。

    带游戏名 = 直启：客户端跳过自动检测，直接下载/导入该游戏攻略并识别
    全屏画面；不带 = 自动检测正在运行的游戏（依赖 config/game_processes.json）。
    """
    # 优先 pythonw（脱离终端，agent 超时/关窗都不影响）；精简版 Python 可能没有它
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        log.warning("未找到 pythonw.exe，回退用 %s 启动", sys.executable)
        exe = Path(sys.executable)
    cmd = [str(exe), str(GAME_CLIENT_SCRIPT)]
    if game_name:
        cmd.append(game_name)

    log.info("启动攻略助手客户端: %s", cmd)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=_child_env())
    _GAME_CLIENT_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _GAME_CLIENT_PID_FILE.write_text(str(proc.pid))
    return proc.pid


def launch(game_name: str = "", models: str = "", install_dir: str = "") -> None:
    """启动攻略助手客户端，PID 文件防重复。

    带游戏名 = 直启（跳过检测，直接准备该游戏攻略并识别全屏画面）；
    不带 = 客户端自动检测正在运行的游戏。
    """
    running_pid = _running_client_pid()
    if running_pid is not None:
        log.info("攻略助手客户端已在运行: pid=%s", running_pid)
        print(json.dumps({"status": "already_running", "pid": running_pid,
                          "message": "攻略助手客户端已经开着了，无需重复启动"},
                         ensure_ascii=False))
        return

    state = probe_service()
    if state == "foreign":
        print(json.dumps(_foreign_port_payload(), ensure_ascii=False))
        return

    if state == "down":
        if _is_bg_running():
            print(json.dumps({"status": "busy", "message": "已有后台任务在运行，请等待完成"},
                             ensure_ascii=False))
            return
        # 服务没起来：后台一口气做完「部署 → 打开攻略窗口」
        log.info("服务未运行，后台串联部署+启动窗口")
        if _launch_background("launch", _chain_cmd("chain-launch", game_name, models, install_dir),
                              initial_stage="正在部署服务") is None:
            print(json.dumps({"status": "error",
                              "message": "后台任务启动失败: " + _read_status().get("detail", "")},
                             ensure_ascii=False))
            return
        print(json.dumps({"status": "started", "task": "launch", "game_name": game_name or "auto",
                          "message": "服务未就绪，已开始自动部署，完成后会自动启动攻略助手客户端"},
                         ensure_ascii=False))
        return

    # 攻略数据由客户端自己负责：查 vision 实例，没有就自动下载+导入。这里不再
    # 同步跑一遍下载（那会阻塞 launch 数分钟、撞上 agent 的命令超时，并与客户端
    # 自己的下载抢同一批文件）。
    pid = _start_game_client(game_name)
    print(json.dumps({"status": "started", "pid": pid,
                      "game_name": game_name or "auto"}, ensure_ascii=False))


# ── 关闭服务 ────────────────────────────────────────────────

def _find_client_pids_by_cmdline() -> list[int]:
    """按命令行扫描攻略助手客户端进程（PID 文件丢失/用户手动启动时的兜底）。

    以客户端脚本的绝对路径匹配（launch 拉起时写的就是这个路径），避免
    误伤其它项目里同名 run_app.py；排除扫描进程自身（其命令行里含匹配文本）。
    """
    script = str(GAME_CLIENT_SCRIPT)
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' "
          "-and $_.CommandLine -like '*" + script + "*' "
          "-and $_.ProcessId -ne $PID } | Select-Object -ExpandProperty ProcessId")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                capture_output=True, text=True, timeout=15)
        return [int(x) for x in result.stdout.split() if x.strip().isdigit()]
    except Exception as e:
        log.warning("扫描客户端进程失败: %s", e)
        return []


def _sweep_orphan_overlays() -> None:
    """兜底清理残留浮窗子进程（幂等；正常场景已被 Job Object/看门狗带走）。

    浮窗命令行不含项目路径，只能按 `-m app.overlay_window` 宽匹配；排除
    扫描进程自身（$PID），且 Name 限 python* 不会误伤无关进程。
    """
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' "
          "-and $_.CommandLine -like '*-m app.overlay_window*' "
          "-and $_.ProcessId -ne $PID } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=15)
    except Exception as e:
        log.debug("清理浮窗失败（可忽略）: %s", e)


def _sweep_ssh_tunnel() -> None:
    """兜底清理残留的 ssh 反向隧道子进程（幂等；正常场景已被 Job Object 带走）。

    ssh 是客户端拉起的独立子进程（GameWalkthroughV2/app/ssh_tunnel.py），
    Job Object 分配失败时会变成孤儿——持续占着云端转发端口，导致之后每次
    启动都 "remote port forwarding failed"。按隧道特有标记精确匹配：内嵌
    私钥落地目录名（game-walkthrough-tunnel）或转发端口（config 里的
    remote_port）；Name 限 ssh.exe，不会误伤用户自己的其它 ssh 会话。
    """
    markers = ["game-walkthrough-tunnel"]
    try:
        cfg_file = GAME_CLIENT_SCRIPT.parent / "config" / "ssh_tunnel.json"
        port = int(json.loads(cfg_file.read_text(encoding="utf-8"))
                   .get("remote_port") or 0)
        if port:
            markers.append(f":{port}:")
    except Exception:
        pass  # 配置读不到就只用私钥目录标记
    conds = " -or ".join(f"$_.CommandLine -like '*" + m + "*'" for m in markers)
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ssh.exe' "
          "-and (" + conds + ") -and $_.ProcessId -ne $PID } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=15)
    except Exception as e:
        log.debug("清理 ssh 隧道失败（可忽略）: %s", e)


def _stop_game_client() -> bool:
    """关闭攻略助手客户端及其整个子进程树（浮窗、WebView2 等）。

    主路径：launch 写入的 pid 文件（已核对进程名）。PID 文件丢失或客户端是
    用户手动启动时，兜底按命令行扫描 run_app.py 进程。taskkill /T 连子进程
    树一起结束；浮窗自身另有 Job Object + 父进程看门狗双保险，随后再做一次
    残留浮窗清扫（幂等）。
    """
    pids: list[int] = []
    pid = _running_client_pid()
    if pid:
        pids.append(pid)
    else:
        pids.extend(_find_client_pids_by_cmdline())
    if not pids:
        return False

    stopped = False
    for p in pids:
        log.info("关闭攻略助手客户端: pid=%s", p)
        try:
            subprocess.run(["taskkill", "/PID", str(p), "/F", "/T"],
                           capture_output=True, text=True, timeout=10)
            stopped = True
        except Exception as e:
            log.warning("taskkill 失败: %s", e)
    if pid:
        _GAME_CLIENT_PID_FILE.unlink(missing_ok=True)
    return stopped


def shutdown() -> int:
    log.info("正在关闭服务...")
    client_stopped = _stop_game_client()
    _sweep_orphan_overlays()  # 残留浮窗兜底清扫（幂等，正常场景无进程可清）
    _sweep_ssh_tunnel()       # 残留 ssh 反向隧道兜底清扫（占云端转发端口）

    if not any(is_port_open(SERVICE_HOST, p) for p in (SERVICE_PORT, SERVICE_PORT_LEGACY)):
        log.info("服务未在运行（端口不可达）")
        print(json.dumps({"status": "done", "client_stopped": client_stopped,
                          "message": "攻略助手客户端已关闭，服务本来就没在运行"
                          if client_stopped else "服务未在运行"},
                         ensure_ascii=False))
        return 0

    if _post_shutdown("/server/shutdown"):
        print(json.dumps({"status": "done", "client_stopped": client_stopped,
                          "message": "服务已关闭"}, ensure_ascii=False))
        return 0

    log.info("尝试强制关闭 /server/shutdown_kill ...")
    if _post_shutdown("/server/shutdown_kill"):
        print(json.dumps({"status": "done", "client_stopped": client_stopped,
                          "message": "服务已强制关闭"}, ensure_ascii=False))
        return 0

    log.error("关闭服务失败")
    print(json.dumps({"status": "error", "client_stopped": client_stopped,
                      "message": "关闭服务失败"}, ensure_ascii=False))
    return 1


# ── 部署 → 原任务 串联（后台执行体） ─────────────────────────

def _download_cmd(game_name: str, install_dir: str) -> list[str]:
    cmd = [sys.executable, str(DOWNLOAD_SCRIPT), game_name]
    if install_dir:
        cmd += ["--install-dir", install_dir]
    return cmd


def _chain_cmd(action: str, game_name: str, models: str, install_dir: str) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), action]
    if game_name:
        cmd.append(game_name)
    if models:
        cmd += ["--models", models]
    if install_dir:
        cmd += ["--install-dir", install_dir]
    return cmd


def _update_stage(task: str, stage: str, progress: float, stdout_log: str = "") -> None:
    """保留 pid 等字段，只推进任务标签和阶段（阶段换日志文件时一并更新 stdout_log）。"""
    data = _read_status()
    data.update({"task": task, "status": "running", "stage": stage, "progress": progress})
    data.setdefault("detail", "")
    if stdout_log:
        data["stdout_log"] = stdout_log
    _write_status(**data)


def chain(action: str, game_name: str = "", models: str = "", install_dir: str = "") -> int:
    """后台执行：先部署服务，再接着做原任务（下载攻略 / 打开攻略窗口）。

    这样 agent 只需要「发一次命令 → 轮询 status」，不必在部署完成后记得把原命令
    再下一遍——漏掉那一步的表现是"什么都没发生"，用户完全无从判断。
    """
    final_task = "download" if action == "chain-download" else "launch"
    # 本进程 stdout 所在的任务日志。自己按同一个公式算，不去读父进程的状态文件：
    # _launch_background 是"先 Popen 再写状态"，中间有窗口，读到空值会让下载阶段
    # 一直指着部署日志、进度永远停在 5%。
    task_log = str(_LOG_SUB_DIR / final_task / "stdout.log")

    deploy_cmd, _strategy, err = _deploy_cmd(models, install_dir)
    if deploy_cmd is None:
        _write_status(final_task, "error", stage="无法启动部署", detail=err)
        return 1

    # 部署阶段单独写一份日志：与后续任务共用一份的话，status 在任务标签翻成
    # download 之后会拿下载阶段的关键词表去重扫部署日志，解释出完全错误的进度
    # （例如部署里那句 "Vision 服务未启用（查询: ...）" 会被当成"导入场景图片 92%"）。
    deploy_log = _LOG_SUB_DIR / "deploy" / "stdout.log"
    deploy_log.parent.mkdir(parents=True, exist_ok=True)
    _update_stage("deploy", "正在部署服务", 2, stdout_log=str(deploy_log))

    env = _child_env()
    env["GA_SKILL_CHAIN"] = "1"  # 让 deploy.py 不要抢写 task_status.json
    log.info("[chain] 部署: %s", deploy_cmd)
    try:
        with open(deploy_log, "w", encoding="utf-8") as fh:
            subprocess.run(deploy_cmd, env=env, stdout=fh, stderr=subprocess.STDOUT)
    except Exception as e:
        _write_status(final_task, "error", stage="部署失败", detail=str(e))
        return 1

    # deploy.py 只等端口 listen 就宣告成功，HTTP 层还要再热一会儿；这里预算要给足，
    # 否则部署明明成功却被判成"未就绪"，整条链白白终止。
    _update_stage("deploy", "正在确认服务就绪", 95, stdout_log=str(deploy_log))
    state = probe_service(attempts=8, interval=5.0)
    if state != "ok":
        detail = (_foreign_port_payload()["message"] if state == "foreign"
                  else "服务未能就绪（可能用户取消了部署，或服务包/模型未装好），详见部署日志")
        _write_status(final_task, "error", stage="部署失败", detail=detail)
        return 1

    if final_task == "download":
        # 切回本进程的任务日志：下载子进程继承 stdout 写在这里
        _update_stage("download", "开始下载攻略", 5, stdout_log=task_log)
        log.info("[chain] 下载攻略: %s", game_name)
        return subprocess.run(_download_cmd(game_name, install_dir), env=_child_env()).returncode

    _update_stage("launch", "启动攻略助手客户端", 90)
    pid = _start_game_client(game_name)
    _write_status("launch", "done", stage="攻略助手客户端已启动", progress=100,
                  detail=f"pid={pid}")
    return 0


# ── ensure ──────────────────────────────────────────────────

def ensure(models: str = "", install_dir: str = "") -> int:
    """健康检查 -> 不通过则后台部署 + 等待就绪（不限时轮询）。"""
    if check_health():
        print(json.dumps({"status": "healthy", "message": "服务已在运行"}, ensure_ascii=False))
        return 0

    log.info("服务未运行，后台启动部署...")
    deploy(models, install_dir)
    while True:
        if check_health():
            print(json.dumps({"status": "healthy", "message": "部署完成，服务已就绪"}, ensure_ascii=False))
            return 0
        data = _read_status()
        if data.get("task") == "deploy" and data.get("status") == "error":
            msg = "部署失败: " + data.get("detail", "服务未能就绪")
            print(json.dumps({"status": "error", "message": msg}, ensure_ascii=False))
            return 1
        # 部署进程已退出但端口不通 → 用户可能取消了部署
        pid = data.get("pid")
        if pid and not _is_pid_alive(int(pid)):
            print(json.dumps({"status": "error", "message": "部署进程已退出，可能用户取消了部署"}, ensure_ascii=False))
            return 1
        time.sleep(30)
    return 1


# ── 状态查询 ────────────────────────────────────────────────

def status() -> None:
    data = _read_status()
    if data.get("status") == "running":
        data = _check_and_finalize_status(data)
    # 只有确实还在跑才去解析日志：任务已终结时再覆盖 stage/progress，会出现
    # "打给 agent 的" 和 "落盘的" 不一致，同一个已完成任务在两次轮询间反复横跳
    if data.get("status") == "running":
        stdout_log = data.get("stdout_log", "")
        task = data.get("task", "")
        if stdout_log:
            try:
                best_pct = -1.0
                with open(stdout_log, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        stripped = line.rstrip("\n\r")
                        if stripped:
                            stage, pct, detail = _parse_progress(task, stripped)
                            # 进度只增不减：日志里晚出现的行未必是更靠后的阶段
                            # （如模型逐个完成时的 "MMR 下载完成"），倒退会误导用户。
                            # stage 跟着进度走，避免出现 60% 却显示"下载服务包"。
                            if pct >= 0 and pct >= best_pct:
                                best_pct = pct
                                data["progress"] = pct
                                if stage:
                                    data["stage"] = stage
                            if detail:
                                data["detail"] = detail
            except Exception:
                pass
    print(json.dumps(data, ensure_ascii=False))


# ── CLI ─────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="游戏助手服务管理")
    parser.add_argument(
        "action",
        choices=["health", "deploy", "download", "launch", "shutdown", "status", "ensure",
                 "chain-download", "chain-launch"],
        help=(
            "health=健康检查 | deploy=后台部署 | download=后台下载攻略 | "
            "launch=启动攻略助手客户端 | shutdown=关闭服务 | status=查进度 | ensure=检查并按需部署"
            "（chain-* 为内部使用：后台串联部署+任务，不要直接调用）"
        ),
    )
    parser.add_argument("game_name", nargs="?", default="", help="download/launch 时指定游戏名")
    parser.add_argument("--models", default="",
                        help="覆盖 SKILL.md 的 models：all=全部，skip=跳过，<模型名列表>=只下载指定（如 MMR,LLM）")
    parser.add_argument("--install-dir", default="",
                        help="覆盖 SKILL.md 的 install_dir（空=默认 %%LOCALAPPDATA%%\\GameAssistant）")
    args = parser.parse_args()

    # 默认值来自 SKILL.md frontmatter，命令行显式传值则覆盖它
    skill_defaults = _read_skill_defaults()
    models = args.models.strip() or skill_defaults.get("models", "")
    install_dir = args.install_dir.strip() or skill_defaults.get("install_dir", "")
    # 支持 %LOCALAPPDATA% 这类环境变量路径：bash/cmd/PowerShell 下 %VAR% 并不总是会被
    # shell 展开，这里统一展开成真实路径，再交给校验、数据目录与子进程传参。
    install_dir = os.path.expandvars(install_dir).strip()

    # 只有可能触发部署的命令才强校验预装目录：status / health / shutdown 只是读状态，
    # 不该因为预装目录里还没有 exe 就整个失败。
    if args.action in ("deploy", "download", "launch", "ensure", "chain-download", "chain-launch"):
        install_dir_err = _validate_install_dir(install_dir)
        if install_dir_err:
            print(json.dumps({"status": "error", "message": install_dir_err}, ensure_ascii=False))
            return 2

    _apply_data_dir(install_dir)

    if args.action in ("chain-download", "chain-launch"):
        return chain(args.action, args.game_name.strip(), models, install_dir)

    if args.action == "health":
        state = probe_service()
        if state == "foreign":
            print(json.dumps(_foreign_port_payload(), ensure_ascii=False))
            return 1
        ok = state == "ok"
        result = {"status": "healthy" if ok else "unhealthy"}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if ok else 1

    if args.action == "deploy":
        deploy(models, install_dir)
        return 0

    if args.action == "download":
        game_name = args.game_name.strip()
        if not game_name:
            print(json.dumps({"status": "error", "message": "请指定游戏名"},
                             ensure_ascii=False))
            return 2
        download(game_name, models, install_dir)
        return 0

    if args.action == "launch":
        launch(args.game_name.strip(), models, install_dir)
        return 0

    if args.action == "shutdown":
        return shutdown()

    if args.action == "status":
        status()
        return 0

    if args.action == "ensure":
        return ensure(models, install_dir)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # 兜底：agent 拿到的永远是一条可解析的 JSON，而不是 traceback
        logging.getLogger("service_manager").exception("未预期的错误")
        print(json.dumps({"status": "error", "message": f"脚本异常: {exc}"}, ensure_ascii=False))
        sys.exit(1)
