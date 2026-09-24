"""Offline test: build 完成后自动把攻略页面导航到位（client._auto_navigate_after_build）。

覆盖：首次导入跳第一页、恢复上次浏览页面、上次页面已存在性校验（失效回退
第一页）、scene_map 缺失安全跳过。webserver 侧用桩替换，不依赖真实 HTTP。

Run:  python tests/test_client_navigate.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCENES: list[dict[str, Any]] = [
    {
        "scene_id": "s1",
        "page_url": "http://example.com/p1",
        "section": "第1页：开端",
        "images": [{"src": "http://example.com/i1.jpg", "local": "g/pages/page_1/images/image_0001.jpg",
                    "index": 0, "section": "第1页：开端"}],
    },
    {
        "scene_id": "s2",
        "page_url": "http://example.com/p2",
        "section": "第2页：寺院",
        "images": [{"src": "http://example.com/i2.jpg", "local": "g/pages/page_2/images/image_0001.jpg",
                    "index": 0, "section": "第2页：寺院"}],
    },
]


def write_scene_map(walkthrough_dir: Path, game: str, scenes: list[dict]) -> None:
    game_dir = walkthrough_dir / game
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scene_map.json").write_text(
        json.dumps({"game": game, "scenes": scenes}, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> int:
    from app.client import GameWalkthroughApp
    from app.config import AppConfig

    tmp = Path(tempfile.mkdtemp(prefix="gwa-nav-"))
    cfg = AppConfig()
    cfg.walkthrough_dir = tmp / "walkthrough"
    cfg.data_dir = tmp / "data"
    cfg.state_file = tmp / "data" / "state.json"
    cfg.ensure_dirs()

    def make_app() -> GameWalkthroughApp:
        return GameWalkthroughApp(cfg)

    def stub_http(app: GameWalkthroughApp, posted: list, last_target) -> None:
        app._post_json = (lambda path, body, timeout=10.0:
                          posted.append((path, body)) or {"ok": True})
        app._last_viewed_target = (lambda g: dict(last_target) if last_target else None)

    # 1) 首次导入（页面从未展示过该游戏）-> 自动跳到攻略第一页（带首图与章节）
    game1 = "首次游戏"
    write_scene_map(cfg.walkthrough_dir, game1, SCENES)
    app = make_app()
    posted: list = []
    stub_http(app, posted, None)
    app._auto_navigate_after_build(game1)
    assert len(posted) == 1 and posted[0][0] == "/api/navigate", posted
    body = posted[0][1]
    assert body["game"] == game1 and body["url"] == "http://example.com/p1", body
    assert body["image_src"] == "http://example.com/i1.jpg", body
    assert body["title"] == "第1页：开端", body
    print("PASS 1: 首次 build 完成 -> 自动跳到攻略第一页（不再停留在空提示页）")

    # 2) 之前展示过且页面仍在本次攻略里 -> 恢复上次浏览的页面（含定位图与章节）
    game2 = "回头客游戏"
    write_scene_map(cfg.walkthrough_dir, game2, SCENES)
    app2 = make_app()
    posted2: list = []
    stub_http(app2, posted2, {"url": "http://example.com/p2",
                              "image_src": "http://example.com/i2.jpg",
                              "title": "第2页：寺院"})
    app2._auto_navigate_after_build(game2)
    assert len(posted2) == 1, posted2
    body2 = posted2[0][1]
    assert body2["url"] == "http://example.com/p2", body2
    assert body2["image_src"] == "http://example.com/i2.jpg" and body2["title"] == "第2页：寺院", body2
    print("PASS 2: 之前展示过 -> build 完成后恢复上次浏览的页面与定位")

    # 3) 上次的页面在本次攻略里已不存在（换文/改版）-> 回退第一页
    game3 = "页面失效游戏"
    write_scene_map(cfg.walkthrough_dir, game3, SCENES)
    app3 = make_app()
    posted3: list = []
    stub_http(app3, posted3, {"url": "http://example.com/p999",
                              "image_src": "http://example.com/gone.jpg",
                              "title": "已不存在的章节"})
    app3._auto_navigate_after_build(game3)
    assert len(posted3) == 1, posted3
    body3 = posted3[0][1]
    assert body3["url"] == "http://example.com/p1", body3
    assert body3["image_src"] == "http://example.com/i1.jpg", body3
    print("PASS 3: 上次页面已不存在 -> 回退第一页")

    # 4) scene_map 缺失（导入异常等）-> 不导航也不崩
    game4 = "没有场景图的游戏"
    app4 = make_app()
    posted4: list = []
    stub_http(app4, posted4, None)
    app4._auto_navigate_after_build(game4)
    assert posted4 == [], posted4
    print("PASS 4: scene_map 缺失 -> 安全跳过导航")

    print("\nALL CLIENT NAVIGATE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
