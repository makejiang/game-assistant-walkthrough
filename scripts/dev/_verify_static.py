#!/usr/bin/env python3
"""静态验证：编译、纯函数单点断言、阶段表解析。全部通过输出 ALL_OK。"""
import sys, io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import deploy
import service_manager as sm

ok = []
def check(name, cond):
    ok.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)

# ── _filename_from_url ──
check("filename 新直链取 basename",
      deploy._filename_from_url(
          "https://modelscope.cn/api/v1/models/OpenVINO/GameAssist/repo?Revision=master"
          "&FilePath=bin%2Fwindows%2FGameAssistantToolServer.7z")
      == "GameAssistantToolServer.7z")
check("filename 旧 zip 不变",
      deploy._filename_from_url(deploy.SERVICE_URL) == "GameAssistant.zip")
check("filename GitHub 直链",
      deploy._filename_from_url(
          "https://github.com/x/y/releases/download/v1.1.1/GameAssistantToolServer.7z")
      == "GameAssistantToolServer.7z")

# ── _find_inner_7z ──
n = ["bin/windows/GameAssistantToolServer.7z", "README.md"]
check("inner 新布局", deploy._find_inner_7z(n) == "bin/windows/GameAssistantToolServer.7z")
check("inner 旧布局 v1.1.0",
      deploy._find_inner_7z(["bin/v1.1.0/windows/GameAssistantToolServer.7z"])
      == "bin/v1.1.0/windows/GameAssistantToolServer.7z")
check("inner 多版本取最大",
      deploy._find_inner_7z(["bin/v1.9.0/windows/GameAssistantToolServer.7z",
                             "bin/v1.10.0/windows/GameAssistantToolServer.7z"])
      == "bin/v1.10.0/windows/GameAssistantToolServer.7z")
check("inner 任意层级兜底",
      deploy._find_inner_7z(["pkg/GameAssistantToolServer.7z"])
      == "pkg/GameAssistantToolServer.7z")
try:
    deploy._find_inner_7z(["README.md"])
    check("inner 找不到应抛异常", False)
except RuntimeError:
    check("inner 找不到应抛异常", True)

# ── parse_model_manifest ──
SAMPLE = '''
COMMON_MODELS = {
    "LLM": ("DeviLeo/Qwen3-4B-int4-ov", ["models", "llm"]),
    "Embedding": ("DeviLeo/bge-m3-int4-sym-ov", ["models", "emb"]),
    "Rerank": ("DeviLeo/bge-reranker-v2-m3-int4-sym-ov", ["models", "rerank"]),
    "OCR": ("DeviLeo/PaddleOCR-ov", ["models", "ocr"]),
}
SPLITTER_MODELS = {
    "zh": ("DeviLeo/zh_core_web_sm-3.8.0", ["models", "splitter"]),
    "en": ("DeviLeo/en_core_web_sm-3.8.0", ["models", "splitter"]),
}
def main(): pass
'''
m = deploy.parse_model_manifest(SAMPLE, "zh")
check("manifest 解析成功", bool(m) and m["LLM"][0] == "DeviLeo/Qwen3-4B-int4-ov")
check("manifest splitter 按 lang 合并", m and m.get("Splitter") == ["DeviLeo/zh_core_web_sm-3.8.0", "models", "splitter"])
check("manifest en", (deploy.parse_model_manifest(SAMPLE, "en") or {}).get("Splitter", [""])[0] == "DeviLeo/en_core_web_sm-3.8.0")
check("manifest 坏代码→None", deploy.parse_model_manifest("def broken(:", "zh") is None)
check("manifest 结构不认识→None", deploy.parse_model_manifest("X = 1", "zh") is None)
UNNAMED = '''
MODEL_TABLE = {
    "A": ("org/a", ["models", "a"]), "B": ("org/b", ["models", "b"]),
    "C": ("org/c", ["models", "c"]), "D": ("org/d", ["models", "d"]),
}
'''
check("manifest 变量名不认识按形态兜底", bool(deploy.parse_model_manifest(UNNAMED, "zh")))

# ── fingerprint_files ──
e1 = [{"Path": "a.bin", "Sha256": "aa", "Size": 5, "Type": "blob"},
      {"Path": "b.bin", "Sha256": "bb", "Size": 7, "Type": "blob"}]
check("指纹稳定", deploy.fingerprint_files(e1) == deploy.fingerprint_files(list(reversed(e1))))
e2 = [{"Path": "a.bin", "Sha256": "CHANGED", "Size": 5, "Type": "blob"},
      {"Path": "b.bin", "Sha256": "bb", "Size": 7, "Type": "blob"}]
check("指纹对内容敏感", deploy.fingerprint_files(e1) != deploy.fingerprint_files(e2))
e3 = [dict(e1[0]), dict(e1[1], Path="c.bin")]
check("文件数变化→指纹变", deploy.fingerprint_files(e1) != deploy.fingerprint_files(e3))
many = [{"Path": f"f{i}", "Sha256": "x", "Size": 1} for i in range(3000)]
check("截断标记生效",
      "truncated" not in deploy.fingerprint_files(e1) and
      deploy.fingerprint_files(many) != deploy.fingerprint_files(many[:2999] + [{"Path": "z", "Sha256": "x", "Size": 1}]))

# ── repo_entry ──
check("repo_entry 命中",
      deploy.repo_entry([{"Path": "x", "Type": "tree"}, {"Path": "x", "Type": "blob", "Sha256": "s"}],
                        "x")["Sha256"] == "s")
check("repo_entry 未命中→None", deploy.repo_entry([{"Path": "y"}], "x") is None)

# ── DeployState 新字段与 skipped/inflight 语义 ──
import tempfile, json
tmp = Path(tempfile.mkdtemp(prefix="_verify_state_"))
st = deploy.DeployState(tmp)
check("默认含新字段", st.service_sha256 is None
      and st._data.get("models_fingerprint") == {})
snap = {"service_sha256": "abc", "models_script_sha256": "s1", "models_fingerprint": {"LLM": "f1"}}
st.mark_skipped(snap)
check("is_skipped 命中", st.is_skipped(snap))
snap2 = dict(snap, service_sha256="zzz")
check("is_skipped 版本不同→不命中", not st.is_skipped(snap2))
st.set_inflight(snap)
st2 = deploy.DeployState(tmp)  # 重新加载，验证落盘
check("skipped/inflight 落盘", st2.is_skipped(snap) and st2._data.get("update_commit_inflight") == snap)
st2.clear_skipped(); st2.clear_inflight()
check("clear 后复位", not st2.is_skipped(snap) and st2._data.get("update_commit_inflight") is None)
old = {"service_installed": True, "models_downloaded": {"LLM": True}}
(tmp / deploy.STATE_FILE).write_text(json.dumps(old), encoding="utf-8")
st3 = deploy.DeployState(tmp)
check("旧状态文件兼容合并", st3.service_installed and st3.service_sha256 is None)

# ── 阶段表解析（service_manager） ──
cases = [
    ("检测服务端与模型更新 (本地 sha256=无记录)", 8.0, "检测更新"),
    ("检测到更新，等待用户确认更新（弹窗已打开）...", 10.0, "等待用户确认"),
    ("等待用户确认服务安装方式...", 10.0, "等待用户确认"),
    ("下载更新包: https://x/7z", 12.0, "下载更新"),
    ("下载: https://x/7z", 20.0, "下载服务包"),
    ("应用更新: 停止服务 → 覆盖安装 → 模型落位", 80.0, "应用更新"),
]
for line, pct, stage in cases:
    s2, p2, _ = sm._parse_progress("deploy", line)
    check(f"阶段[{line[:18]}…] → {pct}/{stage}", p2 == pct and s2 == stage)
for noise in ["--skip-models: 跳过模型下载", "  模型: 仓库大小未知，进度按模型等权估算",
              "等待 15 秒后确认 Vision 服务启用状态...", "等待用户选择服务包..."]:
    s3, p3, _ = sm._parse_progress("deploy", noise)
    check(f"无关行不命中新阶段[{noise[:16]}…]", p3 not in (8.0, 10.0, 12.0, 80.0))

# ── ServiceRemote/UpdatePlan ──
sr = deploy.ServiceRemote(url="u", sha256="abcd1234", size=10, layout="new",
                          transport="direct")
plan = deploy.UpdatePlan(service=sr, models={
    "LLM": deploy.ModelRemote("LLM", "o/r", Path("t"), "fp", 5, "仓库内容有更新")})
check("plan.snapshot", plan.snapshot() == {"service_sha256": "abcd1234",
                                           "models_script_sha256": "",
                                           "models_fingerprint": {"LLM": "fp"}})
check("plan.is_empty False", not plan.is_empty())
check("plan 空", deploy.UpdatePlan().is_empty())
summ = plan.summary(st2)
check("summary 结构", summ["service"]["sha8"] == "abcd1234"
      and summ["models"][0]["name"] == "LLM" and summ["old_service_sha256"] == "")

passed = sum(1 for _, c in ok if c)
print(f"\n{passed}/{len(ok)} 通过")
print("ALL_OK" if passed == len(ok) else "HAS_FAILURES")
sys.exit(0 if passed == len(ok) else 1)
