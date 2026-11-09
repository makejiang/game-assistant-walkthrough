from __future__ import annotations

import argparse
import json
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


def _consume_pending_update(images_json: Path) -> tuple[set[str], bool]:
    """读取下载器留下的更新清单（_update_tmp/pending_update.json，每次下载
    都会覆写，陈旧文件自然失效）。

    返回 (标题变化页 URL 集合, 是否有下架页待清理)。文件缺失/损坏按
    (空, False) 处理——只影响更新动作，不影响正常下载导入。"""
    pending_file = images_json.parent / "_update_tmp" / "pending_update.json"
    try:
        pending = json.loads(pending_file.read_text(encoding="utf-8"))
        force_urls = {
            str(u)
            for u in (pending.get("changed_urls") or [])
            if isinstance(u, str) and u.strip()
        }
        prune_pending = bool(pending.get("prune"))
    except (ValueError, OSError, AttributeError):
        return set(), False
    try:
        pending_file.unlink(missing_ok=True)
    except OSError:
        pass
    return force_urls, prune_pending


def _run_targeted_force_imports(
    *,
    game_name: str,
    images_json: Path,
    force_urls: set[str],
    host: str,
    timeout: float,
    reporter: "DownloadProgressReporter",
) -> None:
    """标题变化页进程内定向强导：每页用只含该页的临时 json 走
    force_reimport=True（删除范围=该页场景），不影响其它已入库页的正常使用。
    临时 json 放 _update_tmp 独立子目录，避免导入器把正式 scene_map.json
    冲成单页；单页失败只记日志，不中断其余页。"""
    from app.walkthrough_service_importer import WalkthroughServiceImporter

    try:
        payload = json.loads(images_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return
    importer = WalkthroughServiceImporter(host=host, timeout=timeout)
    temp_dir = images_json.parent / "_update_tmp"
    temp_dir.mkdir(exist_ok=True)
    try:
        for url in sorted(force_urls):
            record = next(
                (
                    p
                    for p in pages
                    if isinstance(p, dict) and str(p.get("url") or "").strip() == url
                ),
                None,
            )
            if record is None:
                continue
            temp_json = temp_dir / "force_page.json"
            temp_json.write_text(
                json.dumps(
                    {"game": game_name, "pages": [record]},
                    ensure_ascii=False,
                    indent=4,
                ),
                encoding="utf-8",
            )
            try:
                reporter.update_from_line(f"[update] 定向重导标题变化页: {url}")
                importer.sync_images_from_json(
                    instance_id=game_name,
                    images_json_path=temp_json,
                    force_reimport=True,
                    progress_callback=reporter.update_from_line,
                )
            except Exception as exc:
                print(f"[update] 定向重导失败 {url}: {exc}", flush=True)
            finally:
                try:
                    temp_json.unlink(missing_ok=True)
                except OSError:
                    pass
    finally:
        try:
            importer.close()
        except Exception:
            pass


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

    # 2) 消费下载器的更新清单（_update_tmp/pending_update.json）：标题变化页
    #    进程内定向强导（每页独立，不影响其它已入库页的正常使用）；下架页经
    #    --prune-missing 交由导入器按差集删除对应服务端场景。
    service_host = resolve_service_host(args.host, log=print)
    force_urls, prune_pending = _consume_pending_update(images_json)
    if force_urls:
        _run_targeted_force_imports(
            game_name=game_name,
            images_json=images_json,
            force_urls=force_urls,
            host=service_host,
            timeout=args.import_timeout,
            reporter=reporter,
        )

    # 3) 导入（vision 模式：把每张攻略图片作为 scene 插入并 build）
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
    if prune_pending:
        import_command.append("--prune-missing")
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
