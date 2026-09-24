from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent
# v2 工程根目录：下载/导入统一调用 GameWalkthroughV2 里的模块
_PROJECT_ROOT = _SCRIPT_DIR.parent / "GameWalkthroughV2"
_DEFAULT_WALKTHROUGH_DIR = _PROJECT_ROOT / "walkthrough"

# 进度上报模块在 GameWalkthroughV2/app 下，先把工程根加进 sys.path 再导入
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.download_progress import DownloadProgressReporter  # noqa: E402
from app.walkthrough_service_importer import resolve_service_host  # noqa: E402

# 子进程输出全是中文：本脚本被独立（管道）调用时 stdout 可能是 cp1252 等本地
# 编码，转发会直接 UnicodeEncodeError 崩掉。统一强制成 UTF-8（真实链路里
# service_manager / download_with_progress 也是这么设置的）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _safe_game_dir_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name.strip())
    return cleaned or "unknown_game"


def _resolve_output_dir(repo_root: Path, raw_output_dir: str) -> Path:
    output_dir = Path(raw_output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir
    return output_dir.resolve()


def _run_command(command: list[str], cwd: Path, on_line=None) -> int:
    """跑子进程并逐行转发其 stdout。

    输出内容必须原样经过本进程的 stdout（上游 service_manager 靠日志关键词判
    成败、download_with_progress.py 靠同一批行刷新弹窗进度），管道化只是为了在
    转发的同时把进度行喂给下载进度上报器（写共享状态文件 -> 网页展示）。
    """
    print(f"[run] {' '.join(command)}", flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"    # 子进程不缓冲，进度行随产随到
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程中文输出不随本地编码变形
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip("\r\n")
        if line:
            try:
                print(line, flush=True)
            except Exception:
                pass  # 转发失败（如上游管道关闭）不能断掉任务生命周期
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass  # 进度上报失败不影响任务本身
    return int(process.wait())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download a walkthrough (images only) then import it into the helper service."
    )
    parser.add_argument("game_name", help="Resolved game name to download and import")
    parser.add_argument(
        "--output-dir",
        default=_DEFAULT_WALKTHROUGH_DIR.as_posix(),
        help="Walkthrough output root, default: GameWalkthroughV2/walkthrough",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Walkthrough service host, default: auto-detect"
             " (127.0.0.1:22919, fallback 127.0.0.1:9190)",
    )
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=20,
        help="Downloader HTTP timeout in seconds, default: 20",
    )
    parser.add_argument(
        "--import-timeout",
        type=float,
        default=30.0,
        help="Importer request timeout in seconds, default: 30",
    )
    parser.add_argument(
        "--force-reimport",
        action="store_true",
        help="Delete existing matching records before import",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed importer logs",
    )
    return parser


def main() -> int:
    from app.file_logging import setup_logging

    setup_logging()  # 下载/导入过程进 logs/game-assistant-YYYYMMDD.log（下载子进程自行记录）
    args = _build_parser().parse_args()
    game_name = str(args.game_name).strip()
    if not game_name:
        print("error: game_name cannot be empty", file=sys.stderr)
        return 2
    if not _PROJECT_ROOT.is_dir():
        print(f"error: project root not found: {_PROJECT_ROOT}", file=sys.stderr)
        return 2

    output_dir = _resolve_output_dir(_PROJECT_ROOT, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    images_json = output_dir / _safe_game_dir_name(game_name) / "images.json"

    # 进度落到共享状态文件（GameWalkthroughV2/data/download_status.json），
    # 内嵌 webserver 轮询到变化后经 SSE 推给打开的攻略页面
    reporter = DownloadProgressReporter(game_name)
    reporter.start("正在搜索攻略", 2.0)

    # 1) 下载（纯图片模式，生成 images.json；与 v2 客户端自动下载同一份格式）
    download_command = [
        sys.executable,
        "-m",
        "app.game_walkthrough_downloader",
        game_name,
        str(output_dir),
        "--timeout",
        str(args.download_timeout),
        "--images-only",
    ]
    download_code = _run_command(download_command, _PROJECT_ROOT, on_line=reporter.update_from_line)
    if download_code != 0:
        reporter.finish(False, f"攻略下载失败（退出码 {download_code}）")
        return download_code

    if not images_json.exists():
        reporter.finish(False, "下载结果 images.json 缺失")
        print(f"error: downloaded walkthrough json not found: {images_json}", file=sys.stderr)
        return 2

    # 2) 导入（vision 模式：把每张攻略图片作为 scene 插入并 build）
    # 未显式指定 --host 时先探测服务端端口（22919，不通回退旧版 9190）
    service_host = resolve_service_host(args.host, log=print)
    import_command = [
        sys.executable,
        "-m",
        "app.walkthrough_service_importer",
        str(images_json),
        "--instance-id",
        game_name,
        "--host",
        service_host,
        "--timeout",
        str(args.import_timeout),
        "--images-json",
    ]
    if args.force_reimport:
        import_command.append("--force-reimport")
    if args.verbose:
        import_command.append("--verbose")

    code = _run_command(import_command, _PROJECT_ROOT, on_line=reporter.update_from_line)
    if code == 0:
        # service_manager 与 download_with_progress 都以 "导入完成" 作为整个下载
        # 任务的成功标记，缺了这一行会把成功判成失败
        reporter.finish(True, "攻略下载并导入完成")
        print(f"导入完成: {game_name} -> {images_json.as_posix()}", flush=True)
    else:
        reporter.finish(False, f"攻略导入失败（退出码 {code}）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
