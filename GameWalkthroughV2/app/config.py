from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass(slots=True)
class AppConfig:
    """Central configuration for the walkthrough assistant.

    All paths default to locations under the project root. Every value can be
    overridden through command line options in run_app.py / run_webserver.py.
    """

    project_root: Path = field(default_factory=_project_root)

    # 游戏助手服务端（9190 与部分 Windows 服务冲突，新版默认 22919；未显式指定时
    # resolve_service_host() 会在新端口不通时回退探测旧端口 9190）
    service_host: str = "127.0.0.1:22919"
    service_host_explicit: bool = False

    # webserver
    webserver_host: str = "0.0.0.0"
    webserver_port: int = 22818
    embedded_webserver: bool = True

    # 游戏检测
    three_d_threshold: float = 20.0
    decoder_threshold: float = 10.0
    encoder_threshold: float = 10.0
    phys_index: int | None = None
    detection_interval_seconds: float = 1.0

    # 检测到游戏后，进程消失需持续多久才判定“游戏已退出”
    game_exit_grace_seconds: float = 5.0

    # 指定游戏名（run_app.py 位置参数）：跳过自动检测，直接下载/导入攻略并
    # 识别全屏画面（不绑定游戏窗口）
    fixed_game: str | None = None

    # 截图与 vision query
    screenshot_fps: int = 10
    query_topk: int = 1
    query_threshold: float = 0.80
    query_threshold_2: float = 0.01
    query_mode: str = "accurate"
    match_score_threshold: float = 0.7
    confirm_hit_count: int = 2

    # 弹窗
    toast_hold_seconds: float = 10.0
    toast_margin: int = 16

    # 扫码访问面板（鼠标移到屏幕右上角时弹出二维码 + 局域网地址）
    qr_panel_enabled: bool = True
    qr_hotzone_px: int = 16

    # 左上角攻略浮窗（--no-overlay 或 config/overlay.json 的 enabled 可关闭）
    overlay_enabled: bool = True
    overlay_config_file: Path | None = None

    # 自动下载/导入（当游戏尚未导入攻略时）
    auto_bootstrap: bool = True

    # 路径
    exclude_processes_file: Path | None = None
    game_processes_file: Path | None = None
    game_thresholds_file: Path | None = None
    ssh_tunnel_file: Path | None = None
    walkthrough_dir: Path | None = None
    data_dir: Path | None = None
    state_file: Path | None = None

    # 命令行是否显式指定了阈值（显式指定时优先于每游戏配置文件）
    cli_thresholds_given: bool = False
    _thresholds_cache: dict = field(default_factory=dict)

    def _resolve(self) -> None:
        root = self.project_root
        if self.exclude_processes_file is None:
            self.exclude_processes_file = root / "config" / "exclude_processes.json"
        if self.game_processes_file is None:
            self.game_processes_file = root / "config" / "game_processes.json"
        if self.game_thresholds_file is None:
            self.game_thresholds_file = root / "config" / "game_thresholds.json"
        if self.overlay_config_file is None:
            self.overlay_config_file = root / "config" / "overlay.json"
        if self.ssh_tunnel_file is None:
            self.ssh_tunnel_file = root / "config" / "ssh_tunnel.json"
        if self.walkthrough_dir is None:
            self.walkthrough_dir = root / "walkthrough"
        if self.data_dir is None:
            self.data_dir = root / "data"
        if self.state_file is None:
            self.state_file = root / "data" / "state.json"
        self.project_root = Path(self.project_root).expanduser()
        for field_name in ("exclude_processes_file", "game_processes_file", "game_thresholds_file", "overlay_config_file", "ssh_tunnel_file", "walkthrough_dir", "data_dir", "state_file"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                setattr(self, field_name, Path(value).expanduser())

    def ensure_dirs(self) -> None:
        self._resolve()
        for directory in (self.data_dir, self.walkthrough_dir):
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)

    @property
    def webserver_base_url(self) -> str:
        return f"http://127.0.0.1:{self.webserver_port}"

    def _game_thresholds_data(self) -> dict:
        """读取 config/game_thresholds.json（按内容对比监测变化，热加载）。

        每次视觉查询都会调用本方法，因此保存文件后下一次查询即生效，无需重启。
        文件极小（<1KB），每次直接读内容与上次对比：同一秒内、等长内容的修改
        也能感知（mtime+size 方案会漏掉这种修改）。
        加载失败（文件不可读/JSON 非法/顶层不是对象）时什么都不做——沿用上一次
        成功加载的配置，避免存盘瞬间或写错内容导致阈值回退到默认值。
        """
        path = self.game_thresholds_file
        if path is None:
            return {}
        try:
            raw = path.read_bytes()
        except OSError:
            return self._thresholds_cache.get("data") or {}
        if self._thresholds_cache.get("raw") == raw:
            return self._thresholds_cache.get("data") or {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top-level must be an object")
        except (ValueError, UnicodeDecodeError):
            return self._thresholds_cache.get("data") or {}
        self._thresholds_cache.clear()
        self._thresholds_cache.update({"raw": raw, "data": data})
        return data

    def thresholds_for(self, game: str) -> tuple[float, float]:
        """每游戏视觉阈值 (threshold, threshold_2)。

        优先级：命令行显式指定 > config/game_thresholds.json 中该游戏的配置
        > 内置默认（0.80/0.01）。键需与 game_processes.json 里的游戏名一致；
        条目可只写其中一个字段，缺省字段用默认值。
        """
        threshold = float(self.query_threshold)
        threshold_2 = float(self.query_threshold_2)
        if not self.cli_thresholds_given:
            entry = self._game_thresholds_data().get(str(game or "").strip())
            if isinstance(entry, dict):
                for key, default in (("threshold", threshold), ("threshold_2", threshold_2)):
                    value = entry.get(key)
                    if value is None:
                        continue
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if key == "threshold":
                        threshold = value
                    else:
                        threshold_2 = value
        return threshold, threshold_2

    @property
    def vision_base_url(self) -> str:
        host = self.service_host.strip()
        if host.startswith(("http://", "https://")):
            return host
        return f"http://{host}"

    def resolve_service_host(self, log=None) -> str:
        """游戏助手服务端实际地址。

        显式指定（命令行 --service-host 或环境变量 GAME_SERVICE_HOST）时原样
        使用；未指定时在 22919/9190 间自动探测（兼容已发布的旧版服务端，
        逻辑见 walkthrough_service_importer.resolve_service_host）。
        """
        if self.service_host_explicit:
            return self.service_host
        from app.walkthrough_service_importer import resolve_service_host as _resolve

        return _resolve(log=log)


def config_from_args(namespace: object) -> AppConfig:
    """Build an AppConfig from a parsed argparse namespace (ignore unset fields)."""
    cfg = AppConfig()
    for key in (
        "service_host",
        "webserver_host",
        "webserver_port",
        "three_d_threshold",
        "decoder_threshold",
        "encoder_threshold",
        "phys_index",
        "detection_interval_seconds",
        "game_exit_grace_seconds",
        "fixed_game",
        "screenshot_fps",
        "query_topk",
        "query_threshold",
        "query_threshold_2",
        "query_mode",
        "match_score_threshold",
        "confirm_hit_count",
        "toast_hold_seconds",
        "toast_margin",
        "qr_hotzone_px",
        "auto_bootstrap",
        "embedded_webserver",
    ):
        if not hasattr(namespace, key):
            continue
        value = getattr(namespace, key)
        if value is None:
            continue
        if key in ("phys_index", "screenshot_fps", "query_topk", "confirm_hit_count", "webserver_port", "qr_hotzone_px"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif key in ("embedded_webserver", "auto_bootstrap", "overlay_enabled"):
            value = bool(value)
        elif isinstance(value, str):
            value = value.strip() or None
            if value is None:
                continue
        setattr(cfg, key, value)
    # 命令行显式指定阈值时，优先于每游戏配置文件
    if getattr(namespace, "query_threshold", None) is not None or getattr(
        namespace, "query_threshold_2", None
    ) is not None:
        cfg.cli_thresholds_given = True
    # 服务端地址显式指定（命令行或环境变量）时不再做 22919 -> 9190 回退探测
    cfg.service_host_explicit = (
        getattr(namespace, "service_host", None) is not None
        or bool(os.environ.get("GAME_SERVICE_HOST"))
    )
    # env var override for service host / port (keeps scripts usable in CI or docker)
    env_service = os.environ.get("GAME_SERVICE_HOST")
    if env_service:
        cfg.service_host = env_service
    env_web_port = os.environ.get("WALKTHROUGH_WEB_PORT")
    if env_web_port:
        try:
            cfg.webserver_port = int(env_web_port)
        except ValueError:
            pass
    cfg.ensure_dirs()
    return cfg


def safe_game_dir_name(name: str) -> str:
    import re

    cleaned = re.sub(r'[<>:"/\\|?*]', "_", str(name or "").strip())
    return cleaned or "unknown_game"
