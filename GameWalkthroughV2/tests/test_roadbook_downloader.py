"""Offline test for the new-style gamersky “攻略路书” (roadbook) pages.

Covers the download-path side of the 2026-09 site redesign:
  1. `.shtml` article pages are now redirect stubs (#redirectTips[data-link])
     pointing at tools/guide-map/roadbooks/<id>;
  2. the roadbook page inlines all content as reader-node-card nodes, each of
     which is treated as a virtual page (fragment URL as resume key, node
     title as image section);
  3. old-format Mid2L_con pages keep working unchanged.

Run:  python tests/test_roadbook_downloader.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.game_walkthrough_downloader import GamerskyWalkthroughDownloader

STUB_URL = "https://www.gamersky.com/handbook/202609/2202333.shtml"
ROADBOOK_URL = "https://www.gamersky.com/tools/guide-map/roadbooks/47?appNavigationBarStyle=kNoneBarr&gsAppOpenWithNewWindow=true"
OLD_PAGE_URL = "https://www.gamersky.com/handbook/202608/2195434.shtml"
OLD_PAGE2_URL = "https://www.gamersky.com/handbook/202608/2195434_2.shtml"

STUB_HTML = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>跳转</title></head>
<body>
    <div id="redirectTips" data-itemid="2202333" data-link="{ROADBOOK_URL}"></div>
    <script>window.location.href = document.getElementById("redirectTips").getAttribute("data-link");</script>
</body></html>
"""

# 仿真实 SSR 结构：节点卡内联全部内容，图片走 showimage 代理 + _S 缩略图
ROADBOOK_HTML = """<!DOCTYPE html>
<html class="is-guide-roadbook-document"><head><meta charset="utf-8"><title>《测试游戏》全探索图文流程攻略</title></head>
<body>
<main class="guide-content-main">
<section class="guide-content-header"><h1 class="guide-content-header__title">《测试游戏》全探索图文流程攻略</h1></section>
<section class="reader-node-stream reader-node-stream--guide">
<article id="reader-node-268625960" class="reader-node-card is-active is-expanded">
  <div class="reader-node-card__header"><span class="reader-node-card__node-id">A1</span><h3 class="reader-node-card__title">序章及概要</h3></div>
  <div class="reader-node-card__content"><div class="reader-rich-content">
    <p>第一节点的文字说明。</p>
    <p class="GsImageLabel" align="center">
      <a target="_blank" href="https://www.gamersky.com/showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage2026%2F09%2F20260902_fxy_625_1%2F11.jpg" class="n1">
        <img class="picact reader-content-image" src="https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/11_S.jpg" width="600">
      </a>
    </p>
    <p>第二节点的文字说明。</p>
    <p class="GsImageLabel" align="center">
      <a target="_blank" href="https://www.gamersky.com/showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage2026%2F09%2F20260902_fxy_625_1%2F12.jpg" class="n1">
        <img class="picact reader-content-image" src="https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/12_S.jpg" width="600">
      </a>
    </p>
  </div></div>
</article>
<article id="reader-node-317911517" class="reader-node-card is-active is-expanded">
  <div class="reader-node-card__header"><span class="reader-node-card__node-id">A2</span><h3 class="reader-node-card__title">参道</h3></div>
  <div class="reader-node-card__content"><div class="reader-rich-content">
    <p>第二节点没有章节内标题，图片章节应回落到节点标题。</p>
    <p class="GsImageLabel" align="center">
      <a target="_blank" href="https://www.gamersky.com/showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage2026%2F09%2F20260902_fxy_625_1%2F18.jpg" class="n1">
        <img class="picact reader-content-image" src="https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18_S.jpg" width="600">
      </a>
    </p>
  </div></div>
</article>
</section>
</main>
</body></html>
"""

OLD_PAGE1_HTML = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>旧版攻略</title></head>
<body>
<div class="Mid2L_con">
  <p>旧版第一页文字</p>
  <p class="GsImageLabel"><a href="showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage2026%2F08%2Fshared.jpg"><img data-src="https://img1.gamersky.com/image2026/08/shared.jpg"></a></p>
</div>
<span class="pagecss"><ul><li><a href="{OLD_PAGE2_URL}">下一页</a></li></ul></span>
</body></html>
"""

OLD_PAGE2_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>旧版攻略</title></head>
<body>
<div class="Mid2L_con">
  <h2>旧版第二页小节</h2>
  <p>旧版第二页文字</p>
  <p class="GsImageLabel"><a href="showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage2026%2F08%2Fshared.jpg"><img data-src="https://img1.gamersky.com/image2026/08/shared.jpg"></a></p>
</div>
</body></html>
"""


class StubDownloader(GamerskyWalkthroughDownloader):
    """用内存页面替身替换网络访问；图片“下载”只写占位字节。"""

    def __init__(self, pages: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._pages = dict(pages)
        self.downloaded_urls: list[str] = []

    def _get_html(self, url: str) -> str:
        try:
            return self._pages[url]
        except KeyError:
            raise RuntimeError(f"意外的页面请求: {url}") from None

    def _download_binary(self, url: str, save_path: Path) -> None:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(f"bytes:{url}".encode("utf-8"))
        self.downloaded_urls.append(url)


def assert_eq(actual, expected, message: str) -> None:
    assert actual == expected, f"{message}: 期望 {expected!r}, 实际 {actual!r}"


def test_extract_redirect_link() -> None:
    downloader = StubDownloader({STUB_URL: STUB_HTML, ROADBOOK_URL: ROADBOOK_HTML, OLD_PAGE_URL: OLD_PAGE1_HTML})
    link = downloader._extract_redirect_link(STUB_HTML, STUB_URL)
    assert_eq(link, ROADBOOK_URL, "跳转壳应解析出 data-link")
    assert_eq(downloader._extract_redirect_link(OLD_PAGE1_HTML, OLD_PAGE_URL), "", "旧版页面应无跳转链接")
    assert_eq(
        downloader._resolve_start_url(STUB_URL),
        ROADBOOK_URL,
        "起始地址应跟随跳转壳",
    )
    assert_eq(downloader._resolve_start_url(OLD_PAGE_URL), OLD_PAGE_URL, "旧版起始地址应保持不变")
    print("PASS 1: 跳转壳解析与起始地址解析")


def test_images_only_roadbook() -> None:
    with tempfile.TemporaryDirectory(prefix="gwa-roadbook-") as tmp:
        downloader = StubDownloader(
            {STUB_URL: STUB_HTML, ROADBOOK_URL: ROADBOOK_HTML},
            base_output_dir=tmp,
        )
        payload = downloader.download_images_only("测试游戏", start_url=STUB_URL)

        pages = payload["pages"]
        assert_eq(len(pages), 2, "路书节点数应等于虚拟页数")
        assert_eq(payload["base_url"], ROADBOOK_URL, "base_url 应为跳转后的路书地址")
        assert_eq(pages[0]["url"], ROADBOOK_URL + "#reader-node-268625960", "节点地址应带片段")
        assert_eq(pages[0]["page_index"], 1, "节点页码按文档顺序")
        assert_eq(pages[1]["url"], ROADBOOK_URL + "#reader-node-317911517", "第二个节点地址")
        assert_eq([img["src"] for img in pages[0]["images"]][0],
                  "https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/11.jpg",
                  "showimage 代理应解析为直链")
        assert_eq(pages[0]["images"][0]["section"], "A1 序章及概要", "图片章节应为节点标题")
        assert_eq(pages[1]["images"][0]["section"], "A2 参道", "第二个节点图片章节")
        for page in pages:
            for image in page["images"]:
                assert (Path(tmp) / image["local"]).exists(), f"图片应已落盘: {image['local']}"

        # 续传：同一目录再次下载应全部跳过，不产生新的图片下载
        before = len(downloader.downloaded_urls)
        payload2 = downloader.download_images_only("测试游戏", start_url=STUB_URL)
        assert_eq(len(payload2["pages"]), 2, "续传后页数不变")
        assert_eq(len(downloader.downloaded_urls), before, "续传不应重复下载图片")

        # images.json 落盘结构
        on_disk = json.loads((Path(tmp) / "测试游戏" / "images.json").read_text(encoding="utf-8"))
        assert_eq(len(on_disk["pages"]), 2, "images.json 应包含两个节点页")
        # 正常完成后下载状态应带 finished 标记（重启后不再当作未完成的下载续传）
        state = json.loads(
            (Path(tmp) / "测试游戏" / "download_images_state.json").read_text(encoding="utf-8")
        )
        assert_eq(state.get("finished"), True, "正常完成后应有 finished 标记")
    print("PASS 2: 路书纯图片下载、节点章节与续传")


def test_text_images_roadbook() -> None:
    with tempfile.TemporaryDirectory(prefix="gwa-roadbook-") as tmp:
        downloader = StubDownloader(
            {ROADBOOK_URL: ROADBOOK_HTML},
            base_output_dir=tmp,
        )
        mapping = downloader.download_walkthrough("测试游戏", start_url=ROADBOOK_URL)
        assert len(mapping) >= 3, f"文本图片映射行数异常: {len(mapping)}"
        first_image_row = next(row for row in mapping if row["images"])
        assert_eq(
            first_image_row["url"],
            ROADBOOK_URL + "#reader-node-268625960",
            "映射行应指向节点片段地址",
        )
        assert any("第一节点的文字说明" in row["text"] for row in mapping), "正文文本应进入映射"
        game_dir = Path(tmp) / "测试游戏"
        assert (game_dir / "text_images.json").exists(), "text_images.json 应已生成"
        texts = list((game_dir / "pages" / "page_1" / "texts").glob("text_*.txt"))
        assert texts, "节点文本应按虚拟页落盘"
    print("PASS 3: 路书文本+图片下载")


def test_old_format_still_works() -> None:
    with tempfile.TemporaryDirectory(prefix="gwa-old-") as tmp:
        downloader = StubDownloader(
            {
                OLD_PAGE_URL: OLD_PAGE1_HTML,
                OLD_PAGE2_URL: OLD_PAGE2_HTML,
            },
            base_output_dir=tmp,
        )
        payload = downloader.download_images_only("旧版游戏", start_url=OLD_PAGE_URL)
        pages = payload["pages"]
        assert_eq(len(pages), 2, "旧版翻页仍应逐页下载")
        assert_eq(pages[0]["url"], OLD_PAGE_URL, "第一页地址无片段")
        assert_eq(pages[1]["url"], OLD_PAGE2_URL, "翻页器应指向第二页")
        assert_eq(pages[1]["images"][0]["section"], "旧版第二页小节", "旧版图片章节来自正文标题")
        # 同一图片在两页出现：第二页应复用第一页已下载的文件
        assert_eq(pages[0]["images"][0]["local"], pages[1]["images"][0]["local"], "重复图片应复用")
        assert_eq(len(downloader.downloaded_urls), 1, "同一图片只应下载一次")
    print("PASS 4: 旧版 Mid2L_con 页面行为不变")


def test_network_failure_aborts() -> None:
    """网络不可用时：图片连续失败达阈值即中止整次下载（等页面“重试”续传）。"""

    class BrokenDownloader(StubDownloader):
        def _download_binary(self, url: str, save_path: Path) -> None:
            raise RuntimeError("网络不可用(模拟)")

    with tempfile.TemporaryDirectory(prefix="gwa-netfail-") as tmp:
        downloader = BrokenDownloader(
            {STUB_URL: STUB_HTML, ROADBOOK_URL: ROADBOOK_HTML},
            base_output_dir=tmp,
        )
        try:
            downloader.download_images_only("测试游戏", start_url=STUB_URL)
        except RuntimeError as exc:
            assert "连续" in str(exc) and "中止" in str(exc), f"中止原因不明确: {exc}"
        else:
            raise AssertionError("连续下载失败应中止整次下载")
        # 第一节点已完全落盘（images.json 已写），续传可从第二节点继续
        on_disk = json.loads((Path(tmp) / "测试游戏" / "images.json").read_text(encoding="utf-8"))
        assert_eq(len(on_disk["pages"]), 1, "中止前完成的页面应已写入 images.json")
        # 中止的下载不应有 finished 标记：重启后据此判定“未完成的下载”并续传
        state = json.loads(
            (Path(tmp) / "测试游戏" / "download_images_state.json").read_text(encoding="utf-8")
        )
        assert_eq(state.get("finished", False), False, "中止的下载不应带 finished 标记")
    print("PASS 5: 网络不可用时连续失败即中止（可续传）")


def test_on_page_callback() -> None:
    """on_page 每页回调：在 images.json 落盘后逐页触发（供边下边导入）。"""
    with tempfile.TemporaryDirectory(prefix="gwa-onpage-") as tmp:
        seen: list[dict] = []
        images_json = Path(tmp) / "测试游戏" / "images.json"

        def on_page(record: dict) -> None:
            # 回调时该页必须已写入 images.json（导入方按文件读取）
            on_disk = json.loads(images_json.read_text(encoding="utf-8"))
            assert any(p["url"] == record["url"] for p in on_disk["pages"]), "回调时页面应已落盘"
            seen.append(record)

        downloader = StubDownloader(
            {STUB_URL: STUB_HTML, ROADBOOK_URL: ROADBOOK_HTML},
            base_output_dir=tmp,
        )
        payload = downloader.download_images_only("测试游戏", start_url=STUB_URL, on_page=on_page)
        assert_eq(len(seen), 2, "每个节点页应回调一次")
        assert_eq([r["page_index"] for r in seen], [1, 2], "回调按文档顺序")
        assert_eq(seen[0]["url"], ROADBOOK_URL + "#reader-node-268625960", "回调携带页面记录")
        assert_eq(len(payload["pages"]), 2, "最终 payload 完整")
    print("PASS 6: on_page 每页回调（images.json 先落盘）")


def main() -> int:
    test_extract_redirect_link()
    test_images_only_roadbook()
    test_text_images_roadbook()
    test_old_format_still_works()
    test_network_failure_aborts()
    test_on_page_callback()
    print("ALL ROADBOOK DOWNLOADER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
