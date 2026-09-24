"""Offline test: 服务端 instance 被删除后，查询失败应自动触发重新 insert + build。

Run:  python tests/test_client_rebuild.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    from app.client import GameWalkthroughApp
    from app.config import AppConfig

    tmp = Path(tempfile.mkdtemp(prefix="gwa-rebuild-"))
    cfg = AppConfig()
    cfg.walkthrough_dir = tmp / "walkthrough"
    cfg.data_dir = tmp / "data"
    cfg.state_file = tmp / "data" / "state.json"
    cfg.ensure_dirs()

    game = "测试游戏"
    game_dir = cfg.walkthrough_dir / game
    game_dir.mkdir(parents=True)
    images_json = game_dir / "images.json"
    images_json.write_text(
        json.dumps(
            {
                "game": game,
                "pages": [
                    {
                        "url": "http://example.com/p1",
                        "images": [{"src": "http://example.com/i.jpg", "local": "", "index": 0, "section": "第1页"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = GameWalkthroughApp(cfg)
    calls = {"sync": 0, "query": 0}

    class FakeImporter:
        exists = False  # vision list 接口是否包含该实例

        def query_vision(self, *args, **kwargs):
            calls["query"] += 1
            raise RuntimeError("http 404 for POST .../vision/service/query/测试游戏: instance not found")

        def list_vision_instance_ids(self):
            return {game} if self.exists else set()

        def sync_images_from_json(self, instance_id, images_json_path, force_reimport=False,
                                  progress_callback=None):
            calls["sync"] += 1
            if progress_callback is not None:
                progress_callback("[vision] inserting 1 scene(s)")
                progress_callback("[vision] insert ▓▓▓ 1/1 scene_id=abc")
            scene_map = Path(images_json_path).parent / "scene_map.json"
            scene_map.write_text("{}", encoding="utf-8")
            return {"inserted": 1, "build": "done"}

    app._vision_client = FakeImporter()

    # 1) 错误识别（list 接口失败时的关键词兜底）
    assert app._is_instance_missing_error("http 404 for POST ...: instance not found")
    assert app._is_instance_missing_error("服务端提示: 实例不存在")
    assert app._is_instance_missing_error("The vision instance (测试游戏) does not exist.")
    assert not app._is_instance_missing_error("connection timed out")
    print("PASS 1: instance 缺失类错误关键词识别（兜底路径）")

    # 2) 查询失败 + list 接口确认实例不存在 -> 自动触发重建
    hits = app._query_vision(game, b"\x89PNG-fake")
    assert hits == []
    deadline = time.time() + 5
    while game in app._rebuild_inflight and time.time() < deadline:
        time.sleep(0.05)
    assert calls["sync"] == 1, f"sync 应被调用一次，实际 {calls}"
    assert (game_dir / "scene_map.json").exists(), "重建后应生成 scene_map.json"
    assert app._instance_ready_cache.get(game, (0, False))[1] is True
    print("PASS 2: 查询失败且 list 确认实例不存在 -> 自动重新 insert+build")

    # 2b) 实例其实存在（其它原因导致的失败）-> 不重建
    app2 = GameWalkthroughApp(cfg)
    app2._vision_client = FakeImporter()
    app2._vision_client.exists = True
    app2._query_vision(game, b"\x89PNG-fake")
    time.sleep(0.3)
    assert game not in app2._rebuild_inflight and app2._rebuild_cooldown.get(game) is None
    print("PASS 2b: list 确认实例存在时（其它错误）不触发重建")

    # 3) 冷却期内不重复触发
    app._start_rebuild(game, reason="again")
    time.sleep(0.2)
    assert calls["sync"] == 1, "冷却期内不应重复重建"
    print("PASS 3: 冷却期内不重复触发（避免风暴）")

    # 4) 重建期间跳过识别（不再刷错误）
    calls["query"] = 0
    app.current_game = game  # 模拟检测循环已设置当前游戏
    app._rebuild_inflight.add(game)
    app._process_frame(b"\x89PNG-fake")
    assert calls["query"] == 0, "重建期间不应继续查询"
    app._rebuild_inflight.discard(game)
    app.current_game = None
    print("PASS 4: 重建期间跳过识别")

    # 4b) bootstrap 且首页尚未插入+构建完成（scene_map 不存在）时不开始 query：
    #     服务端 instance 在 build 前就已创建，不过滤会对着建到一半的索引
    #     导致无效查询/错误
    (game_dir / "scene_map.json").unlink()
    app._instance_ready_cache.pop(game, None)
    calls["query"] = 0
    app.current_game = game
    app._bootstrapping.add(game)
    app._process_frame(b"\x89PNG-fake")
    assert calls["query"] == 0, "bootstrap 未完成时不应开始 query"
    app.current_game = None
    print("PASS 4b: 首页未构建完成时跳过识别")

    # 4c) 增量导入（边下边导入）：首页插入+构建完成后（scene_map 已生成），
    #     即便剩余页面仍在下载（bootstrap 门未放开），识别也照常进行
    (game_dir / "scene_map.json").write_text("{}", encoding="utf-8")
    app._instance_ready_cache.pop(game, None)   # 模拟首页构建完成后的就绪刷新
    calls["query"] = 0
    app.current_game = game
    app._bootstrapping.add(game)   # bootstrap 门未放开（后续页面仍在下载）
    app._process_frame(b"\x89PNG-fake")
    assert calls["query"] == 1, "增量导入首页就绪后，bootstrap 期间也应开始识别"
    app._bootstrapping.discard(game)
    app.current_game = None
    print("PASS 4c: 边下边导入：首页就绪后识别即放开（不等全部页面下完）")

    # 5) 本地 images.json 缺失时转完整 bootstrap（此处仅验证走 bootstrap 分支不崩）
    app3 = GameWalkthroughApp(cfg)
    app3._vision_client = FakeImporter()
    game3 = "没有本地文件的游戏"
    app3._start_rebuild(game3, reason="missing")
    deadline = time.time() + 5
    while game3 in app3._rebuild_inflight and time.time() < deadline:
        time.sleep(0.05)
    # FakeImporter 的 sync 不会被调用到（bootstrap 下载器在无网时失败），只要不崩即可
    print("PASS 5: 本地攻略缺失时回退完整 bootstrap 分支")

    # 6) 实例被删但本地攻略还在 -> 重新 insert+build 期间进度也上报到页面
    import app.download_progress as download_progress
    dl_status_file = tmp / "download_status.json"
    real_status_file = download_progress.STATUS_FILE
    download_progress.STATUS_FILE = dl_status_file
    try:
        calls["sync"] = 0
        app._rebuild_cooldown.clear()
        app._start_rebuild(game, reason="instance deleted by user")
        deadline = time.time() + 5
        while game in app._rebuild_inflight and time.time() < deadline:
            time.sleep(0.05)
        assert calls["sync"] == 1, calls
        st = download_progress.active_status()
        assert st["status"] == "done" and st["game"] == game, st
        assert "重新导入完成" in st["stage"], st
        assert st["progress"] == 100.0, st
    finally:
        download_progress.STATUS_FILE = real_status_file
    print("PASS 6: 重新 insert+build（实例被删）期间进度上报到页面")

    # 7) 就绪判定以本地 scene_map 为准：instance 存在但用户删了 walkthrough
    #    目录时不算就绪（否则既不重新下载、命中也没法反查位置，直接死锁）
    calls["list"] = 0
    orig_list = FakeImporter.list_vision_instance_ids

    def counting_list(self):
        calls["list"] += 1
        return orig_list(self)

    FakeImporter.list_vision_instance_ids = counting_list
    game4 = "scene_map缺失的游戏"
    (cfg.walkthrough_dir / game4).mkdir(parents=True, exist_ok=True)
    (cfg.walkthrough_dir / game4 / "images.json").write_text(
        json.dumps({"game": game4, "pages": []}, ensure_ascii=False), encoding="utf-8")
    app4 = GameWalkthroughApp(cfg)
    fake4 = FakeImporter()
    fake4.exists = True
    app4._vision_client = fake4
    assert app4._is_instance_ready(game4) is False, "instance 在但 scene_map 缺失不应算就绪"
    assert calls["list"] == 0, "就绪判定只看本地 scene_map，不应再咨询服务端列表"
    print("PASS 7: instance 存在但本地 scene_map 缺失 -> 判定未就绪（触发自动恢复）")

    # 8) 恢复分支：本地 images.json 还在 -> 不重新下载，直接重插+build 重建
    #    scene_map，完成后自动定位页面，两道门集合都清空
    download_progress.STATUS_FILE = dl_status_file
    try:
        navigated = []
        app4._auto_navigate_after_build = lambda g: navigated.append(g)
        sync_before = calls["sync"]
        app4._ensure_bootstrap(game4)
        deadline = time.time() + 5
        while (game4 in app4._rebuild_inflight or game4 in app4._bootstrapping) and time.time() < deadline:
            time.sleep(0.05)
        assert calls["sync"] == sync_before + 1, calls
        assert (cfg.walkthrough_dir / game4 / "scene_map.json").exists(), "重建后应生成 scene_map"
        assert game4 not in app4._rebuild_inflight and game4 not in app4._bootstrapping
        assert navigated == [game4], navigated
        assert app4._is_instance_ready(game4) is True, "重建后应恢复就绪"
    finally:
        download_progress.STATUS_FILE = real_status_file
    print("PASS 8: scene_map 缺失但 images.json 在 -> 自动重建并定位页面（不重新下载）")

    # 9) 未完成的下载（中途退出）：重启识别到同一游戏 -> 继续下载剩余页面，
    #    而不是被判“已就绪”跳过或走本地重建
    game5 = "中途退出的游戏"
    game5_dir = cfg.walkthrough_dir / game5
    game5_dir.mkdir(parents=True, exist_ok=True)
    (game5_dir / "images.json").write_text(
        json.dumps({"game": game5, "pages": [{"url": "http://example.com/p1", "images": []}]},
                   ensure_ascii=False),
        encoding="utf-8")
    (game5_dir / "scene_map.json").write_text("{}", encoding="utf-8")   # 首页已导入
    (game5_dir / "download_images_state.json").write_text(
        json.dumps({"downloaded_pages": ["http://example.com/p1"], "image_url_map": {}},
                   ensure_ascii=False),
        encoding="utf-8")   # 无 finished 标记 = 未完成
    app5 = GameWalkthroughApp(cfg)
    app5._vision_client = FakeImporter()
    download_progress.STATUS_FILE = dl_status_file
    try:
        app5._ensure_bootstrap(game5)
        assert game5 in app5._bootstrapping, "未完成的下载应走续传 bootstrap"
        assert game5 not in app5._rebuild_inflight, "未完成的下载不应走本地重建"
        app5._bootstrapping.discard(game5)
    finally:
        download_progress.STATUS_FILE = real_status_file

    # 9b) 已完成的下载（带 finished 标记）+ scene_map 在 -> 维持“已就绪”跳过
    (game5_dir / "download_images_state.json").write_text(
        json.dumps({"downloaded_pages": ["p1"], "image_url_map": {}, "finished": True},
                   ensure_ascii=False),
        encoding="utf-8")
    app6 = GameWalkthroughApp(cfg)
    app6._vision_client = FakeImporter()
    app6._ensure_bootstrap(game5)
    assert game5 not in app6._bootstrapping and game5 not in app6._rebuild_inflight, \
        "已完成的下载不应再次触发下载/重建"
    print("PASS 9: 未完成的下载重启后续传；已完成的下载不再触发")

    print("\nALL CLIENT REBUILD TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
