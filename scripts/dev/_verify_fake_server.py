#!/usr/bin/env python3
"""集成验证：本地假 ModelScope + GitHub 服务器，驱动更新事务全链路。

场景：A 新布局检测+事务更新 / B 旧 zip 布局 / C skipped 静默 / D ModelScope 死+GitHub 兜底
     / E 无更新静默 / F 首装指纹记录。全部通过输出 ALL_OK。
"""
import sys, io, os, json, hashlib, threading, tempfile, shutil, queue, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

import logging
import deploy
import py7zr
import zipfile

# ── 测试夹具：假服务端 7z / 外层 zip / 清单脚本 ──────────────
# 注意：源 exe 的 mtime 故意设为 90 天前——py7zr 解压会保留包内时间，
# 借此验证"安装后对时"（_stamp_installed_exe）真的生效
FIXDIR = Path(tempfile.mkdtemp(prefix="_fake_fx_"))
INNER_7Z = FIXDIR / "GameAssistantToolServer.7z"
_src_exe = FIXDIR / "_src_GameAssistantToolServer.exe"
_src_exe.write_text("MZ fake server exe", encoding="ascii")
_old = time.time() - 90 * 86400
os.utime(_src_exe, (_old, _old))
with py7zr.SevenZipFile(INNER_7Z, "w") as sz:
    # 真实服务端包包着一层 GameAssistantToolServer/ 目录（用户数据在里层）
    sz.write(_src_exe, "GameAssistantToolServer/GameAssistantToolServer.exe")
    # 包内骨架目录：验证覆盖合并时用户数据目录被保留、骨架不落地
    sz.write(_src_exe, "GameAssistantToolServer/saves/readme.txt")
    sz.write(_src_exe, "GameAssistantToolServer/models/placeholder.txt")
    sz.write(_src_exe, "GameAssistantToolServer/caches/placeholder")
    sz.write(_src_exe, "GameAssistantToolServer/_internal/new.dll")
INNER_BYTES = INNER_7Z.read_bytes()
INNER_SHA = hashlib.sha256(INNER_BYTES).hexdigest()

# 远端压缩包的"上传时间"（files API LastModifiedDate / HEAD Last-Modified 共用）
REMOTE_MTIME = time.time() - 86400   # 1 天前
from datetime import datetime, timezone
REMOTE_MTIME_ISO = datetime.fromtimestamp(REMOTE_MTIME,
                                          tz=timezone.utc).isoformat()

ZIP_PATH = FIXDIR / "GameAssistant.zip"
with zipfile.ZipFile(ZIP_PATH, "w") as zf:
    zf.write(INNER_7Z, "bin/v1.1.0/windows/GameAssistantToolServer.7z")
ZIP_BYTES = ZIP_PATH.read_bytes()
ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()

MANIFEST_TEXT = b'''
COMMON_MODELS = {
    "LLM": ("FakeOrg/llm-int4-ov", ["models", "llm"]),
    "Rerank": ("FakeOrg/rerank-int4-sym-ov", ["models", "rerank"]),
}
SPLITTER_MODELS = {"zh": ("FakeOrg/zh_sp", ["models", "splitter"])}
'''
MANIFEST_SHA = hashlib.sha256(MANIFEST_TEXT).hexdigest()

LLM_FP_OLD = "fp-old"
# 注意：指纹必须用 fingerprint_files 对清单条目计算（与产品逻辑一致），
# 不能用裸 Sha256 字符串——见 _collect_model_updates 的比较方式
RERANK_FP = None  # 在 MODEL_FILES 定义后计算
LLM_FP_NEW = None

# ── 假 HTTP 服务器 ───────────────────────────────────────────
class FakeState:
    """场景间可变的服务器状态。"""
    root_files = []       # files API Root="" 清单
    bin_files = []        # files API Root="bin" 清单
    dead = False          # True → files API 返回 500（模拟 ModelScope 不可达）
    head_last_modified = True  # 下载响应是否带 Last-Modified（测远端时间兜底开关）

STATE = FakeState()
GITHUB_LATEST = {"tag_name": "v9.9.9", "assets": [],
                 "published_at": REMOTE_MTIME_ISO}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data: bytes, code=200, ctype="application/octet-stream",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _lm_headers(self) -> dict:
        """按开关模拟 CDN 的 Last-Modified 响应头。"""
        if not STATE.head_last_modified:
            return {}
        return {"Last-Modified": time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(REMOTE_MTIME))}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.endswith("/files"):
            if STATE.dead:
                self._send(b"boom", 500)
                return
            root = (q.get("Root") or [""])[0]
            files = STATE.root_files if root == "" else STATE.bin_files
            self._send(json.dumps({"Data": {"Files": files}}).encode(),
                       ctype="application/json")
        elif u.path.endswith("/repo"):
            fp = unquote((q.get("FilePath") or [""])[0])
            if fp.endswith(".7z") or fp.endswith(".zip"):
                # 模拟 CDN：包文件响应带 Last-Modified（受开关控制，测兜底）
                self._send(INNER_BYTES if fp.endswith(".7z") else ZIP_BYTES,
                           extra=self._lm_headers())
            elif fp == "demo/dowload_models/download_modelscope_models.py":
                self._send(MANIFEST_TEXT, ctype="text/x-python")
            else:
                self._send(b"not found", 404)
        elif u.path == "/github/api/releases/latest":
            self._send(json.dumps(GITHUB_LATEST).encode(), ctype="application/json")
        elif u.path == "/github/download/GameAssistantToolServer.7z":
            self._send(INNER_BYTES)
        else:
            self._send(b"not found", 404)

srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ── deploy 指向假服务器 ──────────────────────────────────────
# 注意：REPO_API 须保持生产形态（以 /repo/files 结尾），下载直链由它推导
deploy.REPO_API = f"http://127.0.0.1:{PORT}/repo/files"
deploy.SERVICE_URL = (f"http://127.0.0.1:{PORT}/repo?Revision=master"
                      "&FilePath=GameAssistant.zip")
deploy.MANIFEST_URL = (f"http://127.0.0.1:{PORT}/repo?Revision=master&FilePath="
                       "demo%2Fdowload_models%2Fdownload_modelscope_models.py")
deploy.GITHUB_RELEASES_API = f"http://127.0.0.1:{PORT}/github/api/releases/latest"

# 模型仓库清单：按 repo_id 注入（服务端仓库仍走假 HTTP）
MODEL_FILES = {
    "FakeOrg/llm-int4-ov": [{"Path": "m.bin", "Sha256": "newcontent", "Size": 100, "Type": "blob"}],
    "FakeOrg/rerank-int4-sym-ov": [{"Path": "r.bin", "Sha256": "rerankcontent", "Size": 50, "Type": "blob"}],
    "OldOrg/llm": [{"Path": "m.bin", "Sha256": "oldentry", "Size": 100, "Type": "blob"}],
}
LLM_FP_NEW = deploy.fingerprint_files(MODEL_FILES["FakeOrg/llm-int4-ov"])
RERANK_FP = deploy.fingerprint_files(MODEL_FILES["FakeOrg/rerank-int4-sym-ov"])

_orig_lrf = deploy.list_repo_files
def fake_list_repo_files(repo_id, root="", recursive=False, timeout=15.0):
    if repo_id in MODEL_FILES:
        return MODEL_FILES[repo_id]
    return _orig_lrf(repo_id, root, recursive, timeout)
deploy.list_repo_files = fake_list_repo_files

def fake_fetch_model(self, name, model_id, cache_root, prog):
    d = Path(cache_root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.bin").write_bytes(b"x" * 100)
    return d.resolve()
deploy.Deployer._fetch_model_to_cache = fake_fetch_model

# ── Deployer 测试实例构造 ────────────────────────────────────
def make_deployer(install_dir: Path) -> deploy.Deployer:
    d = object.__new__(deploy.Deployer)
    d.install_dir = install_dir
    d.mode = "auto"
    d.package_path = None
    d.lang = "zh"
    d.skip_models = False
    d.only_models = None
    d.force = False
    d.start_only = False
    d.service_url = deploy.SERVICE_URL
    d.models_config = deploy.build_models_config("zh")
    d._manifest_models_config = None
    d._update_applied = False
    d._fresh_install = False
    d.log = logging.getLogger("deploy-test")
    d.log.setLevel(logging.DEBUG)
    if not d.log.handlers:
        d.log.addHandler(logging.StreamHandler(sys.stdout))
    d.state = deploy.DeployState(install_dir)
    d.pkg_dl = deploy.PackageDownloader(d.log)
    d.installer = deploy.Installer(d.log)
    d.svc = deploy.ServiceManager(install_dir, d.log)
    d._service_root = install_dir
    d._gui_q = queue.Queue()
    d._picker_result = queue.Queue(1)
    d._confirm_result = queue.Queue(1)
    d._install_choice_result = queue.Queue(1)
    return d

def drain(d):
    msgs = []
    try:
        while True:
            msgs.append(d._gui_q.get_nowait())
    except queue.Empty:
        pass
    return msgs

results = []
def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)

def new_7z_entry():
    # 用实测确认的真实字段 CommittedDate（unix 秒）
    return {"Path": deploy.SERVICE_7Z_NEW, "Size": len(INNER_BYTES),
            "Sha256": INNER_SHA, "Type": "blob",
            "CommittedDate": REMOTE_MTIME}

def new_7z_entry_no_mtime():
    """无时间字段的条目：测下载响应头 Last-Modified 兜底。"""
    return {"Path": deploy.SERVICE_7Z_NEW, "Size": len(INNER_BYTES),
            "Sha256": INNER_SHA, "Type": "blob"}

def zip_entry():
    return {"Path": "GameAssistant.zip", "Size": len(ZIP_BYTES),
            "Sha256": ZIP_SHA, "Type": "blob",
            "CommittedDate": REMOTE_MTIME}

SERVER_SUBDIR = "GameAssistantToolServer"   # 真实包结构：服务根 = install_dir 下的这一层

def add_old_exe(install_dir: Path):
    """给安装目录放一个比远端压缩包旧的 exe（在服务根 GameAssistantToolServer/ 下）。"""
    server_root = install_dir / SERVER_SUBDIR
    server_root.mkdir(parents=True, exist_ok=True)
    exe = server_root / "GameAssistantToolServer.exe"
    exe.write_text("MZ old", encoding="ascii")
    os.utime(exe, (REMOTE_MTIME - 3600,) * 2)

# ── 场景 A：新布局 → 检测+确认+事务更新 ─────────────────────
def scenario_A():
    print("\n== 场景 A：新布局更新事务 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scA_"))
    STATE.root_files = [
        zip_entry(),
        {"Path": deploy.MANIFEST_SCRIPT, "Size": len(MANIFEST_TEXT), "Sha256": MANIFEST_SHA, "Type": "blob"},
    ]
    STATE.bin_files = [new_7z_entry()]
    STATE.dead = False
    d = make_deployer(tmp)
    d.state.mark_service_version("0" * 64, layout="legacy", source="zip")
    d.state.mark_models_fingerprint({"LLM": LLM_FP_OLD, "Rerank": RERANK_FP})
    d.state.mark_manifest({"LLM": ["OldOrg/llm", "models", "llm"]}, "old-script")
    add_old_exe(tmp)   # 双条件之一：本地 exe 早于远端压缩包
    d._service_root = tmp / SERVER_SUBDIR   # 与生产一致：服务根在包一层目录
    # 用户数据：更新服务端后必须原样保留（saves 存档 / 未更新的模型 / caches 应被清理）
    srv = tmp / SERVER_SUBDIR
    (srv / "saves").mkdir(parents=True)
    (srv / "saves" / "db.sqlite").write_text("用户存档", encoding="utf-8")
    (srv / "models" / "rerank").mkdir(parents=True)
    (srv / "models" / "rerank" / "old.bin").write_text(
        "旧模型（无新版，保留）", encoding="utf-8")
    (srv / "caches").mkdir(parents=True)
    (srv / "caches" / "junk.bin").write_text("cache", encoding="utf-8")

    plan = d._plan_update()
    check("A 服务端检测为新布局直链",
          plan.service and plan.service.transport == "direct"
          and plan.service.sha256 == INNER_SHA and plan.service.layout == "new")
    check("A 模型候选只含指纹变化的 LLM",
          list(plan.models) == ["LLM"] and plan.models["LLM"].reason == "仓库内容有更新")
    check("A 清单解析成功", plan.manifest_normalized.get("LLM", [""])[0] == "FakeOrg/llm-int4-ov")

    d._confirm_result.put(True)   # 用户点「立即更新」
    d._run_update_flow()
    check("A 事务提交后版本标记更新", d.state.service_sha256 == INNER_SHA)
    check("A LLM 指纹已刷新且 Rerank 保留",
          d.state._data["models_fingerprint"].get("LLM") == LLM_FP_NEW
          and d.state._data["models_fingerprint"].get("Rerank") == RERANK_FP)
    check("A 清单缓存已记录", d.state._data["models_script_sha256"] == MANIFEST_SHA)
    check("A 服务端 exe 已落位",
          (tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe").exists())
    check("A 模型已落位",
          (tmp / SERVER_SUBDIR / "models" / "llm" / "model.bin").exists())
    # 覆盖安装语义：用户数据保留、骨架不落地、旧缓存清理
    check("A 用户存档 saves 原样保留",
          (tmp / SERVER_SUBDIR / "saves" / "db.sqlite").read_text(
              encoding="utf-8") == "用户存档")
    check("A 包内 saves 骨架未覆盖用户目录",
          not (tmp / SERVER_SUBDIR / "saves" / "readme.txt").exists())
    check("A 未更新的模型保留",
          (tmp / SERVER_SUBDIR / "models" / "rerank" / "old.bin").exists())
    check("A 包内 models 骨架未覆盖用户目录",
          not (tmp / SERVER_SUBDIR / "models" / "placeholder.txt").exists())
    check("A 旧版本 caches 已删除",
          not (tmp / SERVER_SUBDIR / "caches").exists())
    check("A _internal 整树替换（新 dll 落位）",
          (tmp / SERVER_SUBDIR / "_internal" / "new.dll").exists())
    check("A _internal 旧残留已清除（防链接错误崩溃）",
          not (tmp / SERVER_SUBDIR / "_internal" / "old.dll").exists())
    check("A 无 _internal 改名备份残留",
          not list(tmp.rglob("_internal.old_*")))
    check("A skipped/inflight 已清",
          d.state._data.get("skipped_update") is None
          and d.state._data.get("update_commit_inflight") is None)
    check("A _update_applied 置位", d._update_applied)
    _pkg = tmp / "_update_cache" / "GameAssistantToolServer.7z"
    _exe = tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe"
    check("A 安装后 exe 修改时间已对齐到安装包（防 mtime 规则误报）",
          _pkg.exists() and _exe.exists()
          and _exe.stat().st_mtime >= _pkg.stat().st_mtime - 1)
    kinds = [m[0] for m in drain(d)]
    check("A GUI 收到确认与进度消息", "need_confirm" in kinds and "progress" in kinds)

# ── 场景 B：旧 zip 布局 ─────────────────────────────────────
def scenario_B():
    print("\n== 场景 B：旧 zip 布局 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scB_"))
    STATE.root_files = [zip_entry()]
    STATE.bin_files = []
    d = make_deployer(tmp)
    d.state.mark_service_version("0" * 64)
    d.skip_models = True   # B 只验证服务端 zip 链路
    add_old_exe(tmp)
    plan = d._plan_update()
    check("B 探测为 zip 传输", plan.service and plan.service.transport == "zip"
          and plan.service.sha256 == ZIP_SHA)
    d._confirm_result.put(True)
    d._run_update_flow()
    check("B zip 内层 exe 已解压落位",
          (tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe").exists())
    check("B 版本标记=zip 哈希", d.state.service_sha256 == ZIP_SHA)

# ── 场景 C：skipped 静默 ────────────────────────────────────
def scenario_C():
    print("\n== 场景 C：用户跳过后的静默 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scC_"))
    STATE.root_files = [
        zip_entry(),
        {"Path": deploy.MANIFEST_SCRIPT, "Size": len(MANIFEST_TEXT), "Sha256": MANIFEST_SHA, "Type": "blob"},
    ]
    STATE.bin_files = [new_7z_entry()]
    d = make_deployer(tmp)
    d.state.mark_service_version("0" * 64)
    d.state.mark_models_fingerprint({"LLM": LLM_FP_OLD, "Rerank": RERANK_FP})
    add_old_exe(tmp)
    plan = d._plan_update()
    d.state.mark_skipped(plan.snapshot())
    d._run_update_flow()
    msgs = drain(d)
    check("C skipped 命中→无确认弹窗", len(msgs) == 0
          and d.state.service_sha256 == "0" * 64)

# ── 场景 D：ModelScope 不可达 + GitHub 兜底 ─────────────────
def scenario_D():
    print("\n== 场景 D：ModelScope 死 → GitHub 兜底 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scD_"))
    STATE.dead = True
    GITHUB_LATEST["assets"] = [{
        "name": "GameAssistantToolServer.7z", "size": len(INNER_BYTES),
        "digest": f"sha256:{INNER_SHA}",
        "browser_download_url": f"http://127.0.0.1:{PORT}/github/download/GameAssistantToolServer.7z"}]
    d = make_deployer(tmp)
    d.state.mark_service_version("0" * 64)
    d.skip_models = True
    add_old_exe(tmp)
    gh = deploy.github_latest_asset()
    check("D github_latest_asset 解析 digest", gh and gh["sha256"] == INNER_SHA
          and gh["tag"] == "v9.9.9" and gh["mtime"] is not None)
    plan = d._plan_update()
    check("D 探测为 GitHub 渠道", plan.service and plan.service.transport == "github")
    d._confirm_result.put(True)
    d._run_update_flow()
    check("D GitHub 下载并提交完成",
          (tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe").exists()
          and d.state.service_sha256 == INNER_SHA)
    STATE.dead = False
    GITHUB_LATEST["assets"] = []

# ── 场景 E：无更新静默 + 存量机器只问服务端不碰未装模型 ──────
def scenario_E():
    print("\n== 场景 E：无更新静默 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scE_"))
    STATE.root_files = [
        {"Path": "GameAssistant.zip", "Size": len(ZIP_BYTES), "Sha256": ZIP_SHA, "Type": "blob"},
        {"Path": deploy.MANIFEST_SCRIPT, "Size": len(MANIFEST_TEXT), "Sha256": MANIFEST_SHA, "Type": "blob"},
    ]
    STATE.bin_files = [new_7z_entry()]
    d = make_deployer(tmp)
    d.state.mark_service_version(INNER_SHA)   # 与远端一致
    d.state.mark_manifest({"LLM": ["FakeOrg/llm-int4-ov", "models", "llm"],
                           "Rerank": ["FakeOrg/rerank-int4-sym-ov", "models", "rerank"]},
                          MANIFEST_SHA)
    d.state.mark_models_fingerprint({"LLM": LLM_FP_NEW, "Rerank": RERANK_FP})
    d._run_update_flow()
    check("E 无更新→无弹窗", len(drain(d)) == 0)
    check("E 指纹未变", d.state._data["models_fingerprint"]["LLM"] == LLM_FP_NEW)

# ── 场景 G：双条件更新判定（exe 早于远端压缩包 且 SHA256 不一致） ──
def scenario_G():
    print("\n== 场景 G：双条件更新判定 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scG_"))
    STATE.root_files = [zip_entry()]
    STATE.bin_files = [new_7z_entry()]
    STATE.dead = False
    STATE.head_last_modified = True
    d = make_deployer(tmp)
    d.skip_models = True
    exe = tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("MZ", encoding="ascii")

    # G1: SHA256 不一致 + exe 早于远端压缩包 → 有更新
    d.state.mark_service_version("0" * 64)
    os.utime(exe, (REMOTE_MTIME - 3600,) * 2)
    p1 = d._plan_update()
    check("G1 哈希不一致且 exe 早于远端→有更新", p1.service is not None)

    # G2: SHA256 不一致 + exe 比远端新（本地从 GitHub 装过更新的）→ 跳过不降级
    # 条目换用旧式 LastModifiedDate（ISO）：顺带覆盖多字段扫描的兼容路径
    STATE.bin_files = [dict(new_7z_entry_no_mtime(),
                            LastModifiedDate=REMOTE_MTIME_ISO)]
    os.utime(exe, (REMOTE_MTIME + 3600,) * 2)
    p2 = d._plan_update()
    check("G2 哈希不一致但 exe 比远端新→跳过不降级",
          p2.service is None and p2.is_empty())

    # G3: SHA256 一致 → 无更新（无论 exe 新旧）
    d.state.mark_service_version(INNER_SHA)
    os.utime(exe, (REMOTE_MTIME - 3600,) * 2)
    p3 = d._plan_update()
    check("G3 哈希一致→无更新", p3.is_empty())

    # G4: 所有修改时间来源失败（条目无时间字段 + 下载响应无 Last-Modified）
    #     → 逼不得已退化为仅按 SHA256 判定：不一致即更新
    STATE.bin_files = [new_7z_entry_no_mtime()]
    STATE.head_last_modified = False
    d.state.mark_service_version("0" * 64)
    os.utime(exe, (REMOTE_MTIME - 3600,) * 2)
    p4 = d._plan_update()
    check("G4 时间来源全失败→退化为仅 SHA256 判定", p4.service is not None)

    # G5: API 条目无时间字段但下载响应有 Last-Modified → 双条件正常判定
    STATE.head_last_modified = True
    p5 = d._plan_update()
    check("G5 下载响应头 Last-Modified 兜底→双条件判定", p5.service is not None)
    os.utime(exe, (REMOTE_MTIME + 3600,) * 2)   # exe 比远端新 → 双条件不满足
    p5b = d._plan_update()
    check("G5b 双条件不满足→跳过", p5b.service is None and p5b.is_empty())
    STATE.head_last_modified = False

    # G6: 本地没有 exe → 交给安装流程，不算"有更新"（即使哈希不一致）
    exe.unlink()
    STATE.bin_files = [new_7z_entry()]
    p6 = d._plan_update()
    check("G6 本地无 exe→跳过", p6.service is None and p6.is_empty())

# ── 场景 H：caches 被占用 → 更新不被阻断 ────────────────────
def scenario_H():
    print("\n== 场景 H：caches 被占用 → 更新不被阻断 ==")
    tmp = Path(tempfile.mkdtemp(prefix="_scH_"))
    STATE.root_files = [zip_entry()]
    STATE.bin_files = [new_7z_entry()]
    STATE.dead = False
    STATE.head_last_modified = True
    d = make_deployer(tmp)
    d.state.mark_service_version("0" * 64)
    d.skip_models = True
    add_old_exe(tmp)
    d._service_root = tmp / SERVER_SUBDIR
    srv = tmp / SERVER_SUBDIR
    (srv / "saves").mkdir(parents=True)
    (srv / "saves" / "db.sqlite").write_text("用户存档", encoding="utf-8")
    caches = srv / "caches"
    caches.mkdir(parents=True)
    (caches / "placeholder").write_text("x", encoding="utf-8")
    locked = open(caches / "locked.bin", "w+b")   # 模拟服务端句柄未释放
    try:
        d._confirm_result.put(True)
        d._run_update_flow()
        check("H caches 被占用→更新仍完成",
              d.state.service_sha256 == INNER_SHA
              and (tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe").exists())
        check("H 被占用的 caches 保留原地（不阻断）", caches.exists())
        check("H 服务端 saves/models 数据保留",
              (tmp / SERVER_SUBDIR / "saves" / "db.sqlite").exists())
    finally:
        locked.close()
    # 句柄释放后再走一次更新流程（哈希一致→静默），验证不再报错即可
    d2 = make_deployer(tmp)
    d2.skip_models = True
    d2.state.mark_service_version("stale")
    os.utime(tmp / SERVER_SUBDIR / "GameAssistantToolServer.exe",
             (REMOTE_MTIME - 3600,) * 2)
    p = d2._plan_update()
    check("H 句柄释放后远端时间可比对", p.service is not None)


# ── 场景 F：首装指纹记录 ────────────────────────────────────
def scenario_F():
    print("\n== 场景 F：首装指纹记录 ==")
    # F 只验证服务端标记：短路模型指纹路径（否则内置 DeviLeo 仓库会走到
    # 真实 modelscope.cn 的超时/SDK 重试，沙盒内不可达且时长不定）
    _orig_cmfp = deploy.Deployer._capture_model_fingerprints
    deploy.Deployer._capture_model_fingerprints = lambda self: None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="_scF_"))
        d = make_deployer(tmp)
        d.installer.last_inner_sha256 = "local-measured-sha"
        d._capture_installed_fingerprints()
        check("F 优先记录远端官方标记（新布局 7z 哈希）",
              d.state.service_sha256 == INNER_SHA
              and d.state._data.get("service_source") == "direct")
        STATE.dead = True
        tmp2 = Path(tempfile.mkdtemp(prefix="_scF2_"))
        d2 = make_deployer(tmp2)
        d2.installer.last_inner_sha256 = "local-measured-sha"
        d2._capture_installed_fingerprints()
        check("F API 不可达→退化记录本地测得哈希",
              d2.state.service_sha256 == "local-measured-sha")
    finally:
        deploy.Deployer._capture_model_fingerprints = _orig_cmfp
        STATE.dead = False

try:
    scenario_A()
    scenario_B()
    scenario_C()
    scenario_D()
    scenario_E()
    scenario_G()
    scenario_H()
    scenario_F()
finally:
    srv.shutdown()

passed = sum(results)
print(f"\n{passed}/{len(results)} 通过")
print("ALL_OK" if passed == len(results) else "HAS_FAILURES")
sys.exit(0 if passed == len(results) else 1)
