"""Desktop client for the game walkthrough assistant.

Responsibilities:
  - Detect a running game through GPU engine usage (3D > threshold, decoder and
    encoder < threshold), applying config/exclude_processes.json and
    config/game_processes.json.
  - Capture the game window at ~10 fps into a single-slot queue.
  - When the previous vision query finishes, send the newest frame to the
    game assistant vision service and push the matched page URL + image
    position to the webserver.
  - Show slide-in toast notifications (game detected / page jump).

The webserver runs embedded in a background thread by default so the whole
system is a single executable entry point (run_app.py).

指定游戏名模式（run_app.py 位置参数）：跳过游戏检测，直接按传入的游戏名
下载/导入攻略，并抓取全屏画面（不绑定游戏窗口）送 vision 识别。
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from typing import Any

import requests

from app.config import AppConfig, safe_game_dir_name
from app.download_progress import DownloadProgressReporter
from app.file_logging import setup_logging
from app.qrcode_panel import QrcodePanel
from app.toast import ToastNotifier
from app.webserver.server import WalkthroughWebServer

try:
    from app.game_detection import (
        GpuEngineCounterReader,
        ProcessGpuUsage,
        _normalize_process_key,
        find_detected_game,
        get_window_bounds_for_pid,
        list_process_names,
    )
except ImportError:  # direct execution fallback
    from game_detection import (
        GpuEngineCounterReader,
        ProcessGpuUsage,
        _normalize_process_key,
        find_detected_game,
        get_window_bounds_for_pid,
        list_process_names,
    )

try:
    from app.game_walkthrough_downloader import GamerskyWalkthroughDownloader
    from app.walkthrough_service_importer import WalkthroughServiceImporter
except ImportError:
    from game_walkthrough_downloader import GamerskyWalkthroughDownloader
    from walkthrough_service_importer import WalkthroughServiceImporter

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None


class GameWalkthroughApp:
    REBUILD_COOLDOWN_SECONDS = 120.0  # 实例重建失败后的最小重试间隔

    def __init__(self, config: AppConfig) -> None:
        config.ensure_dirs()
        self.config = config
        self.stop_event = threading.Event()
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._app_log = logging.getLogger("client")  # 文件日志由入口 run_app.py 初始化

        self.root: tk.Tk | None = None
        self.toaster: ToastNotifier | None = None
        self.qr_panel: QrcodePanel | None = None
        self.web_server: WalkthroughWebServer | None = None

        # detection state
        self._state_lock = threading.Lock()
        self.current_game: str | None = None
        self.current_usage: ProcessGpuUsage | None = None
        self._miss_since: float = 0.0

        # screenshot / query
        self._frame_lock = threading.Lock()
        self._latest_frame: bytes | None = None
        self._query_inflight = False
        self._workers_started = False

        # scene map + readiness cache
        self._scene_map_cache: dict[str, tuple[Path, float, dict[str, Any]]] = {}
        self._instance_ready_cache: dict[str, tuple[float, bool]] = {}
        self._bootstrapping: set[str] = set()
        self._bootstrap_lock = threading.Lock()

        # 服务端 instance 被删除后的自动重建（重新 insert + build）
        self._rebuild_inflight: set[str] = set()
        self._rebuild_cooldown: dict[str, float] = {}

        # hit / push dedup
        self._streak_key: tuple[str, str, str] | None = None
        self._hit_streak = 0
        self._last_push_scene_key: tuple[str, str, str] | None = None

        # 左上角攻略浮窗
        self._overlay = None

        # SSH 反向隧道（外网访问，config/ssh_tunnel.json 可配置）
        self._tunnel = None

        self._session = requests.Session()
        self._vision_client: WalkthroughServiceImporter | None = None
        self._log_lock = threading.Lock()

    # ================================================================== run
    def run(self) -> None:
        self.config.ensure_dirs()
        if self.config.embedded_webserver:
            self._start_embedded_webserver()

        if ImageGrab is None:
            self._log_message("缺少依赖 Pillow，请安装: pip install Pillow")
            print("缺少依赖 Pillow，请安装: pip install Pillow", file=sys.stderr)
            return

        root = tk.Tk()
        root.withdraw()
        root.title("游戏攻略助手")
        root.report_callback_exception = self._report_tk_exception
        self.root = root
        self.toaster = ToastNotifier(
            root,
            margin=self.config.toast_margin,
            hold_seconds=self.config.toast_hold_seconds,
        )

        root.protocol("WM_DELETE_WINDOW", self.stop)
        root.after(40, self._pump_ui_queue)
        if self.web_server is not None:
            self._start_ssh_tunnel()
            self._start_qr_panel(root)
            self._start_overlay()
        self._start_workers()
        if self.config.fixed_game:
            threading.Thread(target=self._start_fixed_game, name="fixed-game", daemon=True).start()
            self._log_message(f"[client] 启动完成，指定游戏: {self.config.fixed_game}（跳过检测，识别全屏画面）")
        else:
            threading.Thread(target=self._detection_loop, name="game-detection", daemon=True).start()
            self._log_message("[client] 启动完成，等待检测游戏…")
        try:
            root.mainloop()
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        with contextlib.suppress(Exception):
            if self._overlay is not None:
                self._overlay.stop()
        with contextlib.suppress(Exception):
            if self.qr_panel is not None:
                self.qr_panel.stop()
        with contextlib.suppress(Exception):
            if self._vision_client is not None:
                self._vision_client.close()
        with contextlib.suppress(Exception):
            self._session.close()
        with contextlib.suppress(Exception):
            if self._tunnel is not None:
                self._tunnel.stop()
        with contextlib.suppress(Exception):
            if self.web_server is not None:
                self.web_server.stop()
        if self.root is not None and self.root.winfo_exists():
            with contextlib.suppress(Exception):
                self.root.destroy()
        self.root = None

    def _start_embedded_webserver(self) -> None:
        try:
            self.web_server = WalkthroughWebServer(self.config)
            # 页面“重试”按钮 -> /api/download-retry -> 本客户端从失败处续传
            self.web_server.on_download_retry = self._handle_download_retry_request
            self.web_server.start()
        except Exception as exc:
            self._log_message(f"[client] webserver 启动失败: {exc}")
            print(f"[client] webserver 启动失败: {exc}", file=sys.stderr)

    def _handle_download_retry_request(self, game_name: str) -> tuple[bool, str]:
        """页面点“重试”后由 webserver 调用：重新发起下载/导入（自动从失败处续传）。"""
        game = str(game_name or "").strip()
        if not game:
            return False, "缺少游戏名，无法重试"
        with self._bootstrap_lock:
            if game in self._bootstrapping:
                return False, "该游戏的下载/导入已在进行中"
            self._bootstrapping.add(game)
        self._log_message(f"[bootstrap] 页面请求重试下载，从失败处继续: {game}")
        threading.Thread(
            target=self._bootstrap_job, args=(game,), name=f"bootstrap-retry-{game}", daemon=True
        ).start()
        return True, "已开始重新下载（从失败处继续）"

    def _start_ssh_tunnel(self) -> None:
        """SSH 反向隧道（内网穿透）：配置了云服务器且可用时，扫码面板展示公网地址。"""
        from app.ssh_tunnel import SshTunnelManager, load_ssh_tunnel_config

        try:
            tunnel_cfg = load_ssh_tunnel_config(self.config.ssh_tunnel_file)
        except Exception as exc:
            self._log_message(f"[client] SSH 隧道配置读取失败: {exc}")
            return
        try:
            self._tunnel = SshTunnelManager(
                tunnel_cfg,
                local_port=self.config.webserver_port,
                log=self._log_message,
            )
            self._tunnel.start()
        except Exception as exc:
            self._log_message(f"[client] SSH 隧道启动失败: {exc}")
            self._tunnel = None

    def _start_qr_panel(self, root: tk.Tk) -> None:
        """右上角扫码面板：默认隐藏，鼠标移到屏幕右上角时弹出二维码 + 访问地址。

        SSH 隧道生效时展示公网地址（外网可访问），否则展示局域网地址。
        """
        if not self.config.qr_panel_enabled:
            return
        try:
            self.qr_panel = QrcodePanel(
                root,
                port=self.config.webserver_port,
                margin=self.config.toast_margin,
                hotzone_px=self.config.qr_hotzone_px,
                y_provider=self.toaster.occupied_bottom_y if self.toaster is not None else None,
                public_url_provider=self._public_access_url,
            )
            self.qr_panel.start()
        except Exception as exc:
            self._log_message(f"[client] 扫码面板启动失败: {exc}")
            self.qr_panel = None

    def _public_access_url(self) -> str | None:
        tunnel = self._tunnel
        if tunnel is None:
            return None
        try:
            return tunnel.status().get("url") or None
        except Exception:
            return None

    def _start_overlay(self) -> None:
        """左上角攻略浮窗：内容与浏览器查看器一致，图钉常驻/热区呼入。"""
        if not self.config.overlay_enabled:
            return
        try:
            from app.overlay_window import OverlayWindow

            self._overlay = OverlayWindow(self.config, log=self._log_message)
            self._overlay.start()
        except Exception as exc:
            self._log_message(f"[client] 浮窗启动失败: {exc}")
            self._overlay = None

    # ========================================================== ui pump
    def _pump_ui_queue(self) -> None:
        root = self.root
        if root is None:
            return
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:
                self._log_message(f"[client] ui callback error: {exc}")
        if not self.stop_event.is_set():
            root.after(40, self._pump_ui_queue)

    def _report_tk_exception(self, exc_type, exc_val, exc_tb) -> None:
        self._log_message("[client] tk exception")
        traceback.print_exception(exc_type, exc_val, exc_tb, file=sys.stderr)

    # ========================================================== detection
    def _start_fixed_game(self) -> None:
        """指定游戏名模式：跳过自动检测，直接按该游戏下载/导入并识别全屏画面。"""
        game_name = str(self.config.fixed_game or "").strip()
        if not game_name:
            return
        with self._state_lock:
            self.current_game = game_name
            self.current_usage = None
        self._log_message(f"[detection] 已跳过游戏检测，使用指定游戏: {game_name}")
        self._notify_game_detected(game_name, manual=True)

    def _detection_loop(self) -> None:
        cfg = self.config
        reader = GpuEngineCounterReader()
        try:
            while not self.stop_event.is_set():
                try:
                    result = find_detected_game(
                        reader,
                        three_d_threshold=cfg.three_d_threshold,
                        decoder_threshold=cfg.decoder_threshold,
                        encoder_threshold=cfg.encoder_threshold,
                        phys_index=cfg.phys_index,
                        exclude_path=cfg.exclude_processes_file,
                        game_processes_path=cfg.game_processes_file,
                    )
                except Exception as exc:
                    self._log_message(f"[detection] 检测异常: {exc}")
                    if self.stop_event.wait(max(1.0, cfg.detection_interval_seconds)):
                        break
                    continue
                try:
                    if result is not None:
                        usage, game_name = result
                        self._on_game_detected(usage, game_name)
                    else:
                        self._on_no_game()
                except Exception as exc:
                    self._log_message(f"[detection] 事件处理异常: {exc}")
                if self.stop_event.wait(cfg.detection_interval_seconds):
                    break
        finally:
            reader.close()

    def _on_game_detected(self, usage: ProcessGpuUsage, game_name: str) -> None:
        with self._state_lock:
            was_game = self.current_game
            self.current_game = game_name
            self.current_usage = usage
            self._miss_since = 0.0
        if was_game != game_name:
            self._log_message(f"[detection] 检测到游戏: {game_name} (pid={usage.pid})")
            self._notify_game_detected(game_name)
        elif self.current_usage is not None and self.current_usage.pid != usage.pid:
            self._log_message(f"[detection] 游戏进程变化: {game_name} (pid={usage.pid})")

    def _on_no_game(self) -> None:
        """处理一次“未检测到游戏”的结果。

        游戏进程仍存活时，GPU 瞬时波动（菜单/加载/暂停/过场/最小化）不应被判定为
        “游戏已退出”；只有进程确实消失且持续超过 game_exit_grace_seconds 才判定退出。
        """
        now = time.monotonic()
        with self._state_lock:
            if self.current_game is None:
                return
            usage = self.current_usage
        # 进程还活着 -> 只是当前没有达到 GPU 阈值，保留游戏状态
        if usage is not None and self._is_process_alive(usage.pid, usage.name):
            self._miss_since = 0.0
            return
        # 进程已不在 -> 需要持续一段时间才判定退出，避免误报
        if self._miss_since <= 0.0:
            self._miss_since = now
            return
        if now - self._miss_since >= self.config.game_exit_grace_seconds:
            self._miss_since = 0.0
            with self._state_lock:
                was = self.current_game
                self.current_game = None
                self.current_usage = None
            if was:
                self._log_message(f"[detection] 游戏已退出: {was}")

    def _is_process_alive(self, pid: int, name: str) -> bool:
        """按 PID + 进程名判断游戏进程是否仍存活。"""
        try:
            names = list_process_names()
        except OSError:
            return True  # 无法枚举进程时保守认为存活，避免误报退出
        current_name = names.get(int(pid))
        if current_name is None:
            return False
        return _normalize_process_key(current_name) == _normalize_process_key(name)

    def _notify_game_detected(self, game_name: str, *, manual: bool = False) -> None:
        title = "指定游戏" if manual else "检测到游戏"
        self._toast(title, f"游戏: {game_name}\n正在为你定位攻略页面…")
        first = self._first_guide_info(game_name)
        try:
            self._post_json(
                "/api/game-detected",
                {
                    "game": game_name,
                    "first_url": first["url"],
                    "first_image_src": first["image_src"],
                    "first_image_index": first["image_index"],
                    "first_title": first["title"],
                },
                timeout=5,
            )
        except Exception as exc:
            self._log_message(f"[detection] 通知 webserver 失败: {exc}")
        self._ensure_bootstrap(game_name)

    def _first_guide_info(self, game_name: str) -> dict[str, Any]:
        """返回该游戏攻略首页的场景信息（来自 scene_map.json），供 webserver
        在识别推送时定位到首页第一张图（而不是只给页面地址）。"""
        payload = self._load_scene_map(game_name)
        empty = {"url": "", "image_src": "", "image_index": -1, "title": ""}
        if payload is None:
            return empty
        scenes = payload.get("scenes") or []
        if not scenes or not isinstance(scenes[0], dict):
            return empty
        first_scene = scenes[0]
        images = first_scene.get("images") or []
        first_image = images[0] if images and isinstance(images[0], dict) else {}
        return {
            "url": str(first_scene.get("page_url") or "").strip(),
            "image_src": str(first_image.get("src") or "").strip(),
            "image_index": int(first_image.get("index") or 0) if first_image else -1,
            "title": str(first_image.get("section") or "").strip(),
        }

    def _current_game(self) -> str | None:
        with self._state_lock:
            return self.current_game

    def _current_usage(self) -> ProcessGpuUsage | None:
        with self._state_lock:
            return self.current_usage

    # ================================================= screenshot / query
    def _start_workers(self) -> None:
        if self._workers_started:
            return
        self._workers_started = True
        threading.Thread(target=self._capture_loop, name="frame-capture", daemon=True).start()
        threading.Thread(target=self._query_loop, name="vision-query", daemon=True).start()

    def _capture_loop(self) -> None:
        fps = max(1, int(self.config.screenshot_fps))
        interval = 1.0 / fps
        while not self.stop_event.is_set():
            usage = self._current_usage()
            bbox = None
            if usage is None:
                # 指定游戏模式：无游戏窗口可绑定，识别全屏画面；
                # 检测模式下未检测到游戏则不出帧
                if self._current_game() is None:
                    time.sleep(0.05)
                    continue
            else:
                window = get_window_bounds_for_pid(usage.pid)
                if window is None:
                    time.sleep(interval)
                    continue
                bbox = window.bbox
            try:
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                frame = buffer.getvalue()
            except Exception:
                time.sleep(interval)
                continue
            with self._frame_lock:
                self._latest_frame = frame
            time.sleep(interval)

    def _query_loop(self) -> None:
        while not self.stop_event.is_set():
            with self._frame_lock:
                if self._query_inflight or self._latest_frame is None:
                    frame = None
                else:
                    frame = self._latest_frame
                    self._latest_frame = None
                    self._query_inflight = True
            if frame is None:
                time.sleep(0.05)
                continue
            try:
                self._process_frame(frame)
            except Exception as exc:
                self._log_message(f"[query] 处理截图异常: {exc}")
            finally:
                with self._frame_lock:
                    self._query_inflight = False

    def _process_frame(self, image_bytes: bytes) -> None:
        game_name = self._current_game()
        if not game_name:
            return
        if game_name in self._rebuild_inflight:
            return  # 实例重建中：跳过识别，避免持续刷“instance 不存在”
        if game_name in self._bootstrapping and not self._is_instance_ready(game_name):
            return  # 增量导入未就绪（首页尚未插入+构建完成）：不开始 query；
            # 首页构建完成后识别即放开，剩余页面在后台继续边下边导入
        if not self._is_instance_ready(game_name):
            return
        hits = self._query_vision(game_name, image_bytes)
        if not hits:
            self._reset_streak()
            return
        top = hits[0]
        if top.score < self.config.match_score_threshold:
            self._reset_streak()
            return
        info = self._scene_lookup(game_name, top)
        if info is None:
            self._reset_streak()
            return

        key = (game_name, top.scene_id, top.picture_id)
        if key == self._streak_key:
            self._hit_streak += 1
        else:
            self._streak_key = key
            self._hit_streak = 1
        if self._hit_streak < self.config.confirm_hit_count:
            return
        self._hit_streak = 0

        scene_key = (game_name, top.scene_id, top.picture_id)
        if not self._should_push_scene(scene_key):
            return
        self._push_navigation(game_name, info)

    def _should_push_scene(self, scene_key: tuple[str, str, str]) -> bool:
        """同一场景不重复推送；识别到其它场景（并推送）后再回到该场景才再次推送。"""
        if scene_key == self._last_push_scene_key:
            return False
        self._last_push_scene_key = scene_key
        return True

    def _query_vision(self, game_name: str, image_bytes: bytes) -> list[Any]:
        client = self._get_vision_client()
        temp_path = self._write_temp_image(image_bytes)
        threshold, threshold_2 = self.config.thresholds_for(game_name)
        try:
            return client.query_vision(
                instance_id=game_name,
                image_path=temp_path,
                topk=max(1, int(self.config.query_topk)),
                threshold=float(threshold),
                threshold_2=float(threshold_2),
                mode=self.config.query_mode,
            )
        except Exception as exc:
            message = str(exc)
            self._log_message(f"[query] vision query 失败 game={game_name}: {exc}")
            if self._is_instance_missing(game_name, message):
                self._start_rebuild(game_name, reason=message)
            return []
        finally:
            with contextlib.suppress(Exception):
                temp_path.unlink(missing_ok=True)

    def _is_instance_missing(self, game_name: str, error_message: str) -> bool:
        """查询失败后判定服务端 instance 是否真的不存在。

        以 vision list 接口（/vision/service/list）的结果为准；list 调用本身
        失败（服务端不可达）时退回错误消息关键词判断。
        """
        try:
            return game_name not in self._get_vision_client().list_vision_instance_ids()
        except Exception as exc:
            self._log_message(f"[query] vision instance 列表查询失败: {exc}")
            return self._is_instance_missing_error(error_message)

    @staticmethod
    def _is_instance_missing_error(message: str) -> bool:
        """关键词兜底：识别“instance 不存在”类错误（404 / not found / does not exist 等）。"""
        text = str(message).lower()
        return any(
            keyword in text
            for keyword in (
                "not found", "404", "不存在", "does not exist",
                "no such", "unknown instance", "invalid instance",
            )
        )

    def _start_rebuild(self, game_name: str, *, reason: str = "") -> None:
        """服务端实例缺失时自动重新 insert + build（带冷却与并发保护）。"""
        now = time.monotonic()
        if game_name in self._rebuild_inflight:
            return
        last = self._rebuild_cooldown.get(game_name)
        if last is not None and now - last < self.REBUILD_COOLDOWN_SECONDS:
            return
        self._rebuild_cooldown[game_name] = now
        self._rebuild_inflight.add(game_name)
        self._instance_ready_cache.pop(game_name, None)
        reason_short = reason[:160]
        self._log_message(
            f"[rebuild] 检测到服务端实例缺失，开始重新 insert+build: {game_name} ({reason_short})"
        )
        threading.Thread(
            target=self._rebuild_job, args=(game_name,), name=f"rebuild-{game_name}", daemon=True
        ).start()

    def _rebuild_job(self, game_name: str) -> None:
        """用本地 images.json 重新 insert + build（实例被删时服务端会自动重建）。

        进度同样上报共享状态文件：没有下载阶段，直接从"导入场景图片"起报；
        本地 images.json 也缺失时转完整 bootstrap（其内部自带含下载阶段的上报）。
        """
        reporter = DownloadProgressReporter(game_name)
        try:
            images_json = self.config.walkthrough_dir / safe_game_dir_name(game_name) / "images.json"
            if not images_json.exists():
                self._log_message(f"[rebuild] 本地 images.json 缺失，转完整下载+导入: {game_name}")
                self._bootstrap_job(game_name)
                return
            reporter.start("导入场景图片", 88.0)
            importer = self._get_vision_client()
            result = importer.sync_images_from_json(
                instance_id=game_name,
                images_json_path=images_json,
                force_reimport=False,
                progress_callback=reporter.update_from_line,
            )
            self._instance_ready_cache[game_name] = (time.monotonic(), True)
            self._auto_navigate_after_build(game_name)
            reporter.finish(True, "攻略重新导入完成")
            self._log_message(f"[rebuild] 重新 insert+build 完成 game={game_name} result={result}")
        except Exception as exc:
            reporter.finish(False, f"攻略重新导入失败: {exc}")
            self._log_message(
                f"[rebuild] 重新 insert+build 失败 game={game_name}: {exc}"
                f"（{self.REBUILD_COOLDOWN_SECONDS:.0f}s 后可再次尝试）"
            )
        finally:
            self._rebuild_inflight.discard(game_name)
            # 从 _ensure_bootstrap 的重建分支进入时也带着 bootstrap 门，一并放开
            self._bootstrapping.discard(game_name)

    def _reset_streak(self) -> None:
        self._hit_streak = 0
        self._streak_key = None

    # ========================================================== scene map
    def _scene_lookup(self, game_name: str, hit: Any) -> dict[str, Any] | None:
        payload = self._load_scene_map(game_name)
        if payload is None:
            return None
        picture_to_scene = payload.get("picture_to_scene") or {}
        info = picture_to_scene.get(hit.picture_id)
        if info is None:
            info = picture_to_scene.get(str(hit.picture_id).rsplit("/", 1)[-1])
        if info is not None and isinstance(info, dict):
            return dict(info)
        # fallback: scene-level first image
        for scene in payload.get("scenes") or []:
            if isinstance(scene, dict) and scene.get("scene_id") == hit.scene_id:
                images = scene.get("images") or []
                if images and isinstance(images[0], dict):
                    return dict(images[0])
                break
        return None

    def _load_scene_map(self, game_name: str) -> dict[str, Any] | None:
        path = self.config.walkthrough_dir / safe_game_dir_name(game_name) / "scene_map.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._scene_map_cache.get(game_name)
        if cached is not None:
            cached_path, cached_mtime, cached_payload = cached
            if cached_path == path and cached_mtime == mtime:
                return cached_payload
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        self._scene_map_cache[game_name] = (path, mtime, payload)
        return payload

    def _is_instance_ready(self, game_name: str) -> bool:
        """就绪 = 本地 scene_map.json 存在。

        识别命中后要靠 scene_map 把 scene_id/picture_id 反查回页面位置——
        服务端 instance 还在但本地 scene_map 缺失（用户删了 walkthrough 目录、
        data 目录等）时并不是"可用"状态：查了也定位不了页面，只会空转。
        视为未就绪即可触发 _ensure_bootstrap 的自动恢复。
        """
        now = time.monotonic()
        cached = self._instance_ready_cache.get(game_name)
        if cached is not None and now - cached[0] < 20:
            return cached[1]
        scene_map = self.config.walkthrough_dir / safe_game_dir_name(game_name) / "scene_map.json"
        ready = scene_map.exists()
        self._instance_ready_cache[game_name] = (now, ready)
        return ready

    # ================================================= webserver push
    def _push_navigation(self, game_name: str, info: dict[str, Any]) -> None:
        payload = {
            "game": game_name,
            "url": str(info.get("page_url") or "").strip(),
            "image_src": str(info.get("src") or "").strip(),
            "image_index": int(info.get("index") or 0),
            "image_ref": str(info.get("local") or "").strip(),
            "title": str(info.get("section") or "").strip(),
        }
        self._log_message(
            f"[push] game={game_name} url={payload['url']} image={payload['image_index']}"
            f" src={payload['image_src'] or '(空)'}"
        )
        try:
            data = self._post_json("/api/navigate", payload, timeout=10)
        except Exception as exc:
            self._log_message(f"[push] 推送失败: {exc}")
            self._toast("跳转攻略页面", f"游戏: {game_name}\n推送失败: {exc}")
            return
        title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
        self._toast("跳转攻略页面", f"游戏: {game_name}\n{title or '已定位攻略页面'}")

    def _post_json(self, path: str, body: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        response = self._session.post(
            f"{self.config.webserver_base_url}{path}",
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def _last_viewed_target(self, game_name: str) -> dict[str, Any] | None:
        """该游戏上次在页面上展示的目标（webserver state 持久化）；从未展示过则 None。"""
        try:
            response = self._session.get(
                f"{self.config.webserver_base_url}/api/view",
                params={"game": game_name},
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None
        if isinstance(data, dict) and str(data.get("url") or "").strip():
            return data
        return None

    def _auto_navigate_after_build(self, game_name: str) -> None:
        """build 完成后把攻略页面导航到位（webserver 未开时静默跳过）：

        - 首次导入：跳到攻略第一页，页面不再停留在"进入游戏后开始展示攻略"；
        - 之前展示过：恢复上次浏览的页面（含定位图片与章节）；该页在本次下载的
          攻略里已不存在（换文/改版）时回退第一页。
        """
        try:
            scenes = (self._load_scene_map(game_name) or {}).get("scenes") or []
            page_urls = {
                str(scene.get("page_url") or "").strip()
                for scene in scenes
                if isinstance(scene, dict)
            }
            page_urls.discard("")

            target: dict[str, Any] | None = None
            last = self._last_viewed_target(game_name)
            if last and str(last.get("url") or "").strip() in page_urls:
                target = {
                    "url": str(last.get("url") or ""),
                    "image_src": str(last.get("image_src") or ""),
                    "title": str(last.get("title") or ""),
                }
            else:
                for scene in scenes:
                    if not isinstance(scene, dict) or not scene.get("page_url"):
                        continue
                    images = scene.get("images") or []
                    first_image = images[0] if images and isinstance(images[0], dict) else {}
                    target = {
                        "url": str(scene.get("page_url") or ""),
                        "image_src": str(first_image.get("src") or ""),
                        "title": str(scene.get("section") or ""),
                    }
                    break
            if not target or not target.get("url"):
                self._log_message(f"[bootstrap] scene_map 无可用攻略页，跳过自动导航: {game_name}")
                return
            self._post_json(
                "/api/navigate",
                {
                    "game": game_name,
                    "url": target["url"],
                    "image_src": target.get("image_src") or "",
                    "title": target.get("title") or "",
                },
                timeout=10,
            )
            self._log_message(f"[bootstrap] 已把攻略页面定位到: {target['url']}")
        except Exception as exc:
            self._log_message(f"[bootstrap] build 后自动导航失败: {exc}")

    # ========================================================== bootstrap
    def _is_partial_download(self, game_name: str) -> bool:
        """上次下载未正常完成（中途退出/失败）：images.json 与下载状态都在，
        但状态文件没有 finished 标记。重启后据此继续下载剩余页面。"""
        state_file = (
            self.config.walkthrough_dir / safe_game_dir_name(game_name) / "download_images_state.json"
        )
        images_json = self.config.walkthrough_dir / safe_game_dir_name(game_name) / "images.json"
        if not state_file.exists() or not images_json.exists():
            return False
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
        if not isinstance(payload, dict):
            return False
        return not payload.get("finished")

    def _ensure_bootstrap(self, game_name: str) -> None:
        if not self.config.auto_bootstrap:
            return
        if game_name in self._bootstrapping or game_name in self._rebuild_inflight:
            return
        # 未完成的下载（中途退出/失败）优先续传：即使首页已导入（scene_map
        # 已生成、识别可用），剩余页面也要继续下载+导入
        partial = self._is_partial_download(game_name)
        if self._is_instance_ready(game_name) and not partial:
            return
        with self._bootstrap_lock:
            if game_name in self._bootstrapping:
                return
            self._bootstrapping.add(game_name)
        if partial:
            self._log_message(
                f"[bootstrap] 检测到未完成的下载，继续下载剩余页面: {game_name}"
            )
            job, name = self._bootstrap_job, f"bootstrap-resume-{game_name}"
            threading.Thread(target=job, args=(game_name,), name=name, daemon=True).start()
            return
        # 未就绪的两种恢复路径：
        #   - 本地 images.json 还在（部分删除/仅 scene_map 丢失）→ 直接重插+build
        #     重建 scene_map，不需要网络；
        #   - 本地文件全无 → 完整下载+导入。
        if (self.config.walkthrough_dir / safe_game_dir_name(game_name) / "images.json").exists():
            self._log_message(
                f"[bootstrap] 本地 scene_map 缺失但 images.json 存在，直接重建索引与 scene_map: {game_name}"
            )
            self._rebuild_inflight.add(game_name)
            job, name = self._rebuild_job, f"rebuild-{game_name}"
        else:
            self._log_message(f"[bootstrap] 开始自动下载并导入攻略: {game_name}")
            job, name = self._bootstrap_job, f"bootstrap-{game_name}"
        threading.Thread(target=job, args=(game_name,), name=name, daemon=True).start()

    def _bootstrap_job(self, game_name: str) -> None:
        # 进度上报到共享状态文件，内嵌 webserver 轮询到变化后推给打开的攻略页面。
        # 增量模式：每下载完一页立即 插入+构建，玩家从第一页起就能开始识别，
        # 后续页面在后台继续边下边导入（进度按已完成的页数单调推进）。
        reporter = DownloadProgressReporter(game_name, incremental=True)
        try:
            reporter.start("正在搜索攻略", 2.0)
            downloader = GamerskyWalkthroughDownloader(
                base_output_dir=self.config.walkthrough_dir,
                timeout=20,
                progress_callback=reporter.update_from_line,
            )
            images_json = self.config.walkthrough_dir / safe_game_dir_name(game_name) / "images.json"
            importer = self._get_vision_client()
            imported_pages = {"count": 0}

            def import_page(page_record: dict[str, Any]) -> None:
                """页面下载完成回调（images.json 已落盘）：插入新场景并构建。"""
                result = importer.sync_images_from_json(
                    instance_id=game_name,
                    images_json_path=images_json,
                    force_reimport=False,
                    progress_callback=reporter.update_from_line,
                )
                imported_pages["count"] += 1
                if imported_pages["count"] == 1:
                    # 首页就绪即放开识别并导航到攻略位置，不等剩余页面
                    self._instance_ready_cache[game_name] = (time.monotonic(), True)
                    self._auto_navigate_after_build(game_name)
                self._log_message(
                    f"[bootstrap] 增量导入完成 game={game_name} "
                    f"page={page_record.get('page_index')} images={len(page_record.get('images') or [])}"
                )

            payload = downloader.download_images_only(game_name, on_page=import_page)
            # 收尾兜底：再同步一次（续传场景下无新页时也能建立索引/恢复导航）
            result = importer.sync_images_from_json(
                instance_id=game_name,
                images_json_path=images_json,
                force_reimport=False,
                progress_callback=reporter.update_from_line,
            )
            self._log_message(f"[bootstrap] 导入完成 game={game_name} result={result}")
            pages = payload.get("pages") or []
            total_images = sum(len(p.get("images") or []) for p in pages if isinstance(p, dict))
            self._instance_ready_cache[game_name] = (time.monotonic(), True)
            if imported_pages["count"] == 0:
                # 没有新页（断点续传全部命中）：补导航到上次位置
                self._auto_navigate_after_build(game_name)
            self._log_message(
                f"[bootstrap] 下载完成 game={game_name} pages={len(pages)} "
                f"images={total_images} 增量导入页数={imported_pages['count']}"
            )
            reporter.finish(True, "攻略下载并导入完成")
            self._toast("攻略已就绪", f"游戏: {game_name}\n已下载并导入攻略图片。")
        except Exception as exc:
            self._log_message(f"[bootstrap] 失败 game={game_name}: {exc}")
            reporter.finish(False, f"下载/导入失败: {exc}", detail=str(exc))
            self._toast("攻略准备失败", f"游戏: {game_name}\n{exc}")
        finally:
            with self._bootstrap_lock:
                self._bootstrapping.discard(game_name)

    # =============================================================== util
    def _get_vision_client(self) -> WalkthroughServiceImporter:
        if self._vision_client is None:
            self._vision_client = WalkthroughServiceImporter(
                # 未显式指定时在 22919/9190 间自动探测（结果缓存，本次运行内固定）
                host=self.config.resolve_service_host(log=self._log_message),
                timeout=30.0,
            )
        return self._vision_client

    @staticmethod
    def _write_temp_image(image_bytes: bytes) -> Path:
        tmp_dir = Path(tempfile.gettempdir()) / "game-walkthrough"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="vision-query-", suffix=".png", dir=tmp_dir, delete=False) as handle:
            handle.write(image_bytes)
            return Path(handle.name)

    def _toast(self, title: str, message: str) -> None:
        toaster = self.toaster
        if toaster is None:
            return
        with contextlib.suppress(Exception):
            toaster.notify(title, message)

    def _log_message(self, message: str) -> None:
        with self._log_lock:
            print(message, file=sys.stderr, flush=True)
            self._app_log.info(message)  # 文件日志
