#!/usr/bin/env python3
"""验证：预装目录语义（记忆目录 → 预装目录 → 默认目录 → 才下载解压）、
目录切换时日志/状态跟随、wait_healthy 1 秒计时。
DEFAULT_INSTALL_DIR 被替换为临时目录，隔离真实环境。全部通过输出 ALL_OK。"""
import sys, io, time, queue, tempfile, socket, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
import logging
import deploy

results = []
def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)

# ── 用伪造默认目录替换真实环境 ──
REAL_DEFAULT = deploy.DEFAULT_INSTALL_DIR
FAKE_DEFAULT = Path(tempfile.mkdtemp(prefix="_fdef_"))
deploy.DEFAULT_INSTALL_DIR = FAKE_DEFAULT
deploy._CHOSEN_SERVER_DIR_FILE = FAKE_DEFAULT / "chosen_server_dir.txt"
MARKER = deploy._CHOSEN_SERVER_DIR_FILE

def clear_marker():
    MARKER.unlink(missing_ok=True)

def make_server_dir(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=name))
    (d / "GameAssistantToolServer.exe").write_text("MZ", encoding="ascii")
    return d

def make_deployer(install_dir: Path, requested: Path, force: bool = False) -> deploy.Deployer:
    d = object.__new__(deploy.Deployer)
    d.install_dir = install_dir
    d.requested_install_dir = requested
    d.mode = "auto"
    d.package_path = None
    d.force = force
    d.log = logging.getLogger("deploy-vtest")
    d.log.setLevel(logging.INFO)
    if not d.log.handlers:
        d.log.addHandler(logging.StreamHandler(sys.stdout))
    d.state = deploy.DeployState(install_dir)
    d.svc = deploy.ServiceManager(install_dir, d.log)
    d.installer = deploy.Installer(d.log)
    d._install_choice_result = queue.Queue()  # 无界：测试要预放多个选择结果
    d._gui_q = queue.Queue()
    return d

clear_marker()

# ── 1) _resolve_startup_dir ──
missing = Path("Q:/0-Intel_SDK/GameAssistantToolServer")
assert not Path("Q:/").exists()
r = deploy._resolve_startup_dir(missing)
check("预装目录不存在→默认目录", r == FAKE_DEFAULT and r.is_dir())
check("不替不存在的预装目录造目录", not missing.exists())
empty_dir = Path(tempfile.mkdtemp(prefix="_empty_"))
r2 = deploy._resolve_startup_dir(empty_dir)
check("预装目录存在但无服务端→默认目录（日志/状态跟随）",
      r2 == FAKE_DEFAULT)
with_server = make_server_dir("_withsrv_")
r3 = deploy._resolve_startup_dir(with_server)
check("预装目录有服务端→原样使用", r3 == with_server)

# 记忆目录优先于预装目录（优先级：记忆 → 预装 → 默认）
chosen0 = make_server_dir("_chosen0_")
MARKER.write_text(str(chosen0), encoding="utf-8")
r4 = deploy._resolve_startup_dir(with_server)
check("记忆目录优先于预装目录（首条日志即真实目录）", r4 == chosen0)
(chosen0 / "GameAssistantToolServer.exe").unlink()
r5 = deploy._resolve_startup_dir(with_server)
check("记忆目录失效→回退预装目录", r5 == with_server)
MARKER.unlink(missing_ok=True)

# ── 2) 优先级 1：记忆中的目录 ──
chosen = make_server_dir("_chosen_")
MARKER.write_text(str(chosen), encoding="utf-8")
cfg_dir = Path(tempfile.mkdtemp(prefix="_cfg_"))
d = make_deployer(FAKE_DEFAULT, cfg_dir)   # 队列为空：命中记忆就不该弹窗
ret = d._ask_install_missing()
check("记忆目录可用→直接使用（无弹窗）", ret == chosen
      and Path(d.install_dir) == chosen and d._install_choice_result.empty())
check("记忆目录命中→日志/状态切过去",
      d.state._path.parent == chosen)

# ── 3) 优先级 2：配置的预装目录里有服务端 ──
clear_marker()   # 清掉用例 2 的记忆，否则记忆优先级更高
d2 = make_deployer(with_server, with_server)
ret2 = d2._ask_install_missing()
check("预装目录有服务端→直接使用", ret2 is None
      and Path(d2.install_dir) == with_server)

# ── 4) 优先级 3：默认目录里有服务端（绝不重新解压覆盖） ──
(pre_rmtree := FAKE_DEFAULT / "GameAssistantToolServer").mkdir(exist_ok=True)
(pre_rmtree / "GameAssistantToolServer.exe").write_text("MZ", encoding="ascii")
(pre_rmtree / "userdata.json").write_text("用户数据", encoding="utf-8")
d3 = make_deployer(cfg_dir, cfg_dir)       # 记忆已失效/不存在，预装目录为空
ret3 = d3._ask_install_missing()
check("默认目录有服务端→直接使用不重装", ret3 == FAKE_DEFAULT
      and Path(d3.install_dir) == FAKE_DEFAULT
      and d3._install_choice_result.empty())
check("默认目录已有内容未被删除",
      (pre_rmtree / "userdata.json").exists()
      and (pre_rmtree / "userdata.json").read_text(encoding="utf-8") == "用户数据")

# ── 5) 全都不存在 → 弹窗；auto → 回落默认目录下载解压 ──
clear_marker()
(pre_rmtree / "userdata.json").unlink()
shutil = __import__("shutil"); shutil.rmtree(pre_rmtree)
d4 = make_deployer(cfg_dir, cfg_dir)
d4._install_choice_result.put("auto")
ret4 = d4._ask_install_missing()
check("全部不存在→询问后 auto 回落默认目录", ret4 is None
      and Path(d4.install_dir) == FAKE_DEFAULT
      and d4.state._path.parent == FAKE_DEFAULT)
check("auto 清除已失效的选择记录", not MARKER.exists())

# ── 6) 弹窗选择目录：无效重问；有效使用并记住 ──
chosen2 = make_server_dir("_chosen2_")
d5 = make_deployer(cfg_dir, cfg_dir)
d5._install_choice_result.put(cfg_dir)   # 无 exe → 拒绝重问
d5._install_choice_result.put(chosen2)   # 有效
ret5 = d5._ask_install_missing()
check("无效目录拒绝、有效目录使用", ret5 == chosen2
      and Path(d5.install_dir) == chosen2)
check("选择已记住", MARKER.exists()
      and MARKER.read_text(encoding="utf-8").strip() == str(chosen2))
d5b = make_deployer(cfg_dir, cfg_dir)    # 全新上下文：直接用记住的目录
ret5b = d5b._ask_install_missing()
check("下次启动直接使用记住的目录", ret5b == chosen2
      and d5b._install_choice_result.empty())

# ── 7) 记住的目录失效 → 重新询问 ──
(chosen2 / "GameAssistantToolServer.exe").unlink()
d6 = make_deployer(cfg_dir, cfg_dir)
d6._install_choice_result.put("auto")
ret6 = d6._ask_install_missing()
check("记住的目录失效→重新询问", ret6 is None and not MARKER.exists())

# ── 8) --force 跳过记忆与默认目录检查 ──
clear_marker()
MARKER.write_text(str(chosen), encoding="utf-8")
d7 = make_deployer(cfg_dir, cfg_dir, force=True)
d7._install_choice_result.put("auto")
ret7 = d7._ask_install_missing()
check("force 跳过记忆直接询问", ret7 is None and d7._install_choice_result.empty())
clear_marker()

# ── 9) 取消 → RuntimeError；未配置预装目录 → 静默自动 ──
d8 = make_deployer(cfg_dir, cfg_dir)
d8._install_choice_result.put("cancel")
try:
    d8._ask_install_missing()
    check("取消应抛异常", False)
except RuntimeError:
    check("取消应抛异常", True)

d9 = make_deployer(FAKE_DEFAULT, FAKE_DEFAULT)
n_q = d9._install_choice_result.qsize()
ret9 = d9._ask_install_missing()
check("未配置预装目录且无服务端→静默自动", ret9 is None
      and d9._install_choice_result.qsize() == n_q)

# ── 10) wait_healthy：1 秒频率回调 ──
probe = socket.socket(); probe.bind(("127.0.0.1", 0))
dead_port = probe.getsockname()[1]; probe.close()
ticks = []
svc = deploy.ServiceManager(tempfile.mkdtemp(), logging.getLogger("t2"))
ok = svc.wait_healthy(host="127.0.0.1", ports=(dead_port,), timeout=5.0,
                      interval=5.0, on_wait=lambda e, m: ticks.append(e))
check("超时路径 1 秒频率回调", not ok and len(ticks) >= 3
      and ticks == sorted(ticks) and max(ticks) <= 5)

# ── 恢复真实环境 ──
deploy.DEFAULT_INSTALL_DIR = REAL_DEFAULT
deploy._CHOSEN_SERVER_DIR_FILE = REAL_DEFAULT / "chosen_server_dir.txt"
MARKER.unlink(missing_ok=True)

print(f"\n{sum(results)}/{len(results)} 通过")
print("ALL_OK" if all(results) else "HAS_FAILURES")
sys.exit(0 if all(results) else 1)
