from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup, Tag

    from app.net_retry import RetryingSession
except ModuleNotFoundError as exc:
    requests = None
    BeautifulSoup = None
    Tag = Any
    RetryingSession = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_WALKTHROUGH_DIR = Path(__file__).resolve().parent.parent / "walkthrough"

_logger = logging.getLogger("downloader")


@dataclass
class _Event:
    kind: str
    value: str


@dataclass
class _RoadbookNode:
    """新版“攻略路书”单页里的一个节点卡（相当于旧版的一页）。"""

    url: str  # <路书页地址>#reader-node-<id>，作为续传/映射的唯一键
    title: str  # “A1 序章及概要”形式的节点标题
    content: Any  # 节点正文 .reader-rich-content
    position: int  # 在路书页中的顺序（1 起）


class GamerskyWalkthroughDownloader:
    """Download Gamersky game strategies and build text-image mapping JSON."""

    SEARCH_URL_TEMPLATE = "https://so.gamersky.com/all/handbook?s={query}&type=hot&sort=des"
    # 搜索结果按热度排序，新发布的攻略可能被老热门新闻压在后面几页。
    # 第 1 页无关键词命中时继续翻页兜底（只在原本必失败的路径上生效），扫完仍无命中报同样的错。
    SEARCH_MAX_PAGES = 5
    SEARCH_PAGE_DELAY = 2.0  # 翻页间隔（秒），降低被站点限流(429)的概率
    IMAGE_PROXY_PREFIX = "https://www.gamersky.com/showimage/id_gamersky.shtml?"
    PAGE_STOP_TEXT = "更多相关内容请关注"
    SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    REDIRECT_TIP_ID = "redirectTips"  # 新版跳转壳页里携带真实地址的元素 id
    ROADBOOK_NODE_CLASS = "reader-node-card"  # 新版路书页的节点卡
    HTML_CACHE_SIZE = 4  # 同一页面会被估算页数/翻页/正文解析各取一次，做个小缓存
    MAX_CONSECUTIVE_FAILURES = 3  # 连续失败阈值：网络已不可用时中止整次下载（等页面重试）

    def __init__(
        self,
        base_output_dir: str | Path = _DEFAULT_WALKTHROUGH_DIR,
        keywords: tuple[str, ...] = ("全地图", "图文攻略", "图文流程攻略", "全探索", "100%", "全收集", "白金攻略"),
        timeout: int = 20,
        max_pages: int | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._ensure_dependencies_available()
        self.base_output_dir = Path(base_output_dir)
        self.keywords = keywords
        self.timeout = timeout
        self.max_pages = max_pages
        self.progress_callback = progress_callback
        # 网络请求统一走自动重试：失败最多重试 3 次、间隔 20 秒（含页面与图片下载）
        self.session = RetryingSession(requests.Session(), notify=self._print_progress)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
        )
        self.last_changed_urls: set[str] = set()  # 最近一次下载检出的标题变化页（进程内调用方可读）
        self._html_cache: dict[str, str] = {}

    def download_walkthrough(
        self,
        game_name: str,
        start_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """同游戏目录的下载入口：先取跨进程下载锁（防并发写坏数据），
        再进入实际下载流程。"""
        game_dir = self.base_output_dir / self._safe_name(game_name)
        with self._game_download_lock(game_dir, game_name):
            return self._download_walkthrough_locked(game_name, start_url)

    def _download_walkthrough_locked(
        self,
        game_name: str,
        start_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Download all pages of the selected walkthrough, save assets to disk,
        and return the final text-image mapping.
        """
        walkthrough_url = start_url or self._find_walkthrough_url(game_name)
        walkthrough_url = self._resolve_start_url(walkthrough_url)
        total_pages = self._estimate_total_pages(walkthrough_url)

        game_dir_name = self._safe_name(game_name)
        game_dir = self.base_output_dir / game_dir_name
        game_dir.mkdir(parents=True, exist_ok=True)
        pages_dir = game_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        mapping_path = game_dir / "text_images.json"
        state_path = game_dir / "download_state.json"

        mapping = self._load_mapping(mapping_path)
        mapping_changed = self._normalize_mapping_image_paths(mapping, game_dir_name)
        state = self._load_state(state_path)
        downloaded_pages = set(state.get("downloaded_pages", []))
        image_url_map: dict[str, str] = dict(state.get("image_url_map", {}))

        self._print_progress(f"开始下载攻略: {game_name}")
        self._print_progress(f"输出目录: {game_dir.as_posix()}")
        self._print_progress(f"历史已下载页面: {len(downloaded_pages)}")
        if total_pages:
            self._print_progress(f"攻略共约 {total_pages} 页")

        page_index = 0
        new_page_count = 0
        skipped_page_count = 0
        attempted_download_page_count = 0
        new_text_count = 0
        new_image_count = 0
        reused_image_count = 0
        skipped_image_count = 0

        max_pages = self.max_pages

        # 新页编号永不复用已有编号：网站“下架-重发”改版时，新节点可能落在
        # 旧节点的扫描位置上，按位置编号会把新内容写进已存在的 page_N 目录，
        # 静默污染旧章节。改为取现有最大页号 +1 顺延（全新下载时与位置一致）。
        used_page_nos: set[int] = set()
        for entry in pages_dir.iterdir():
            m = re.fullmatch(r"page_(\d+)", entry.name)
            if m:
                used_page_nos.add(int(m.group(1)))
        next_page_no = max(used_page_nos, default=0) + 1

        stop_download = False
        for page_url in self._iterate_pages(walkthrough_url):
            if stop_download:
                break
            processed_any = False
            for content_url, content, section_title in self._iter_page_contents(page_url):
                processed_any = True
                page_index += 1
                label = f"「{section_title}」" if section_title else ""
                if total_pages:
                    self._print_progress(f"处理第{page_index}/{total_pages}页{label}: {content_url}")
                else:
                    self._print_progress(f"处理第{page_index}页{label}: {content_url}")
                if content_url in downloaded_pages:
                    skipped_page_count += 1
                    self._print_progress(f"已下载过第{page_index}页，跳过")
                    continue

                if max_pages is not None and attempted_download_page_count >= max_pages:
                    self._print_progress(f"达到页面下载上限 {max_pages}，停止继续下载")
                    stop_download = True
                    break

                attempted_download_page_count += 1

                page_no = next_page_no
                next_page_no += 1
                page_dir = pages_dir / f"page_{page_no}"
                page_texts_dir = page_dir / "texts"
                page_images_dir = page_dir / "images"
                page_rel_prefix = f"{game_dir_name}/pages/page_{page_no}/"
                page_texts_dir.mkdir(parents=True, exist_ok=True)
                page_images_dir.mkdir(parents=True, exist_ok=True)

                image_counter = self._next_index(page_images_dir, "image_", "*")
                text_counter = self._next_index(page_texts_dir, "text_", ".txt")

                page_events = self._extract_events(content)
                normalized_events: list[_Event] = []
                page_text_count = 0
                page_new_image_count = 0
                page_reused_image_count = 0
                page_skipped_image_count = 0
                for event in page_events:
                    if event.kind == "text":
                        normalized_events.append(event)
                        text_path = page_texts_dir / f"text_{text_counter:04d}.txt"
                        text_path.write_text(event.value, encoding="utf-8")
                        text_counter += 1
                        page_text_count += 1
                        new_text_count += 1
                    else:
                        image_url = event.value
                        if not self._is_supported_image_url(image_url):
                            page_skipped_image_count += 1
                            skipped_image_count += 1
                            self._print_progress(f"图片映射(跳过): {image_url} -> URL 末尾不是受支持的图片扩展名")
                            continue

                        cached_path = image_url_map.get(image_url)
                        if (
                            cached_path
                            and self._cached_image_exists(cached_path)
                            and self._to_game_relative_image_path(cached_path, game_dir_name).startswith(page_rel_prefix)
                        ):
                            event.value = self._to_game_relative_image_path(cached_path, game_dir_name)
                            image_url_map[image_url] = event.value
                            self._print_progress(
                                f"图片映射(复用): {image_url} -> {Path(event.value).name} ({event.value})"
                            )
                            normalized_events.append(event)
                            page_reused_image_count += 1
                            reused_image_count += 1
                            continue

                        suffix = self._guess_suffix(image_url)
                        image_path = page_images_dir / f"image_{image_counter:04d}{suffix}"
                        self._download_binary(image_url, image_path)
                        image_counter += 1
                        event.value = self._to_game_relative_image_path(image_path.as_posix(), game_dir_name)
                        image_url_map[image_url] = event.value
                        self._print_progress(
                            f"图片映射(下载): {image_url} -> {image_path.name} ({event.value})"
                        )
                        normalized_events.append(event)
                        page_new_image_count += 1
                        new_image_count += 1

                page_mapping = self._build_mapping(normalized_events, content_url)
                if page_mapping:
                    mapping.extend(page_mapping)
                new_page_count += 1

                downloaded_pages.add(content_url)
                mapping_path.write_text(
                    json.dumps(mapping, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
                self._save_state(state_path, downloaded_pages, image_url_map)
                self._print_progress(
                    "本页完成: "
                    f"文本{page_text_count}段, "
                    f"新图{page_new_image_count}张, "
                    f"复用图{page_reused_image_count}张, "
                    f"跳过图{page_skipped_image_count}张, "
                    f"新增映射{len(page_mapping)}条"
                )
            if not processed_any:
                downloaded_pages.add(page_url)
                self._save_state(state_path, downloaded_pages, image_url_map)
                self._print_progress("未找到正文容器 Mid2L_con，标记后跳过")

        if not mapping_path.exists():
            mapping_path.write_text("[]", encoding="utf-8")
        elif mapping_changed:
            mapping_path.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
        if not state_path.exists():
            self._save_state(state_path, downloaded_pages, image_url_map)
        self._print_progress(
            "下载结束: "
            f"扫描{page_index}页, "
            f"新处理{new_page_count}页, "
            f"跳过{skipped_page_count}页, "
            f"新增文本{new_text_count}段, "
            f"新增图片{new_image_count}张, "
            f"复用图片{reused_image_count}张, "
            f"跳过图片{skipped_image_count}张, "
            f"当前映射总数{len(mapping)}条"
        )
        return mapping

    def _find_walkthrough_url(self, game_name: str) -> str:
        query = quote(game_name, safe="")
        search_url = self.SEARCH_URL_TEMPLATE.format(query=query)

        for page in range(1, self.SEARCH_MAX_PAGES + 1):
            if page > 1:
                time.sleep(self.SEARCH_PAGE_DELAY)
            # 第 1 页沿用原始搜索地址；后续页追加 &p=N，与游民星空搜索翻页格式一致。
            page_url = search_url if page == 1 else f"{search_url}&p={page}"
            self._print_progress(f"检索攻略第{page}页: {page_url}")
            html = self._get_html(page_url)
            soup = BeautifulSoup(html, "html.parser")

            result_container = soup.find("div", class_="Mid2_L")
            if not isinstance(result_container, Tag):
                if page == 1:
                    raise RuntimeError("未找到搜索结果容器 Mid2_L")
                continue

            for anchor in result_container.find_all("a", href=True):
                title = anchor.get_text(" ", strip=True)
                if not title:
                    continue
                if any(keyword in title for keyword in self.keywords):
                    return urljoin(search_url, anchor["href"])

        raise RuntimeError("未找到包含关键词的攻略链接")

    def _extract_redirect_link(self, html: str, base_url: str) -> str:
        """新版“攻略路书”没有常规正文页：.shtml 只是跳转壳，页内 #redirectTips
        的 data-link 属性才指向真正的新版路书页（tools/guide-map/roadbooks/<id>）。
        无跳转壳时返回空串。"""
        soup = BeautifulSoup(html, "html.parser")
        tip = soup.find(id=self.REDIRECT_TIP_ID)
        if not isinstance(tip, Tag):
            return ""
        link = str(tip.get("data-link") or "").strip()
        return urljoin(base_url, link) if link else ""

    def _resolve_start_url(self, url: str) -> str:
        """跟随新版跳转壳（最多 3 跳），返回真正的正文页地址。"""
        current = url
        for _ in range(3):
            html = self._get_html(current)
            link = self._extract_redirect_link(html, current)
            if not link:
                return current
            self._print_progress(f"新版攻略跳转: {current} -> {link}")
            current = link
        return current

    def _find_roadbook_nodes(self, soup: Any, page_url: str) -> list[_RoadbookNode]:
        """新版“攻略路书”页把全部内容以 reader-node-card 节点卡内联在单页里。

        每个节点视为一个虚拟页，地址记作 <路书页>#reader-node-<id>，
        断点续传与下游 scene_id 都按节点区分。非路书页返回空列表。
        """
        nodes: list[_RoadbookNode] = []
        articles = soup.find_all("article", class_=self.ROADBOOK_NODE_CLASS)
        for position, article in enumerate(articles, start=1):
            if not isinstance(article, Tag):
                continue
            content = article.find(class_="reader-rich-content")
            if not isinstance(content, Tag):
                continue
            anchor_id = str(article.get("id") or "").strip()
            node_url = f"{page_url}#{anchor_id}" if anchor_id else page_url
            nodes.append(
                _RoadbookNode(
                    url=node_url,
                    title=self._roadbook_node_title(article),
                    content=content,
                    position=position,
                )
            )
        return nodes

    @classmethod
    def _roadbook_node_title(cls, article: Tag) -> str:
        parts: list[str] = []
        for class_name in ("reader-node-card__node-id", "reader-node-card__title"):
            tag = article.find(class_=class_name)
            if not isinstance(tag, Tag):
                continue
            text = cls._clean_text(tag.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return " ".join(parts)

    @classmethod
    def fetch_roadbook_titles(cls, roadbook_url: str, timeout: int = 20) -> dict[str, str]:
        """拉取一次路书页，返回线上节点映射 {URL: 标题}（独立调用入口）。

        供打开路径的新鲜度检查使用；解析失败或无节点时返回空 dict，
        由调用方按“无法判断”处理。旧版 Mid2L_con 页面自然返回空 dict。"""
        return cls(timeout=timeout, progress_callback=lambda m: None)._extract_roadbook_titles(
            roadbook_url
        )

    def _guide_titles(self, entry_url: str) -> dict[str, str]:
        """统一获取线上攻略各页标题：新版路书取节点标题；
        旧版文章沿分页链接逐页提取正文标题行。两者都无法识别时返回空 dict
        （调用方按"无法判断"跳过）。失败原因记入日志。

        结果必须是线上页的**完整**列表：调用方会把"本地有、线上无"的页当作
        已下架并删除。因此枚举中途任何一页失败/取不到正文时，整体返回空 dict
        （无法判断），绝不返回只含已抓到页的半截结果。"""
        titles = self._extract_roadbook_titles(entry_url)
        if titles:
            return titles
        try:
            for page_url in self._iterate_pages(entry_url):
                html = self._get_html(page_url)
                soup = BeautifulSoup(html, "html.parser")
                content = soup.find("div", class_="Mid2L_con")
                if not isinstance(content, Tag):
                    _logger.warning(f"线上攻略页缺少正文容器，线上列表不完整，放弃本次比对: {page_url}")
                    return {}
                # 无标题行的页也记录（空串）：它确实存在于线上，不能被当作下架；
                # 空标题不参与"标题变化"判定（diff_pages 要求双方标题非空）
                titles[page_url] = self._extract_old_format_title(content)
        except Exception as exc:
            _logger.warning(f"线上攻略页枚举/解析失败，线上列表不完整，放弃本次比对: {entry_url} 错误={exc!r}")
            return {}
        if not any(titles.values()):
            # 整份攻略都没有标题行：与改动前一致，视为无法判断（不进入比对/重排/重编号）
            return {}
        return titles

    @classmethod
    def fetch_old_format_titles(cls, entry_url: str, timeout: int = 20) -> dict[str, str]:
        """旧版文章：枚举全部分页页返回 {URL: 标题}（独立调用入口）。

        供打开路径的新鲜度检查使用；解析失败或无正文时返回空 dict，
        由调用方按"无法判断"处理。"""
        return cls(timeout=timeout, progress_callback=lambda m: None)._guide_titles(entry_url)

    def _extract_roadbook_titles(self, roadbook_url: str) -> dict[str, str]:
        """实例版节点标题提取：复用 self 的会话与 _html_cache（同一页面在
        估算页数/翻页/正文解析时已取过，这里零额外网络请求）。任何失败
        返回空 dict，调用方按“无法判断”跳过比对（失败原因记入日志）。"""
        try:
            html = self._get_html(roadbook_url)
            soup = BeautifulSoup(html, "html.parser")
            nodes = self._find_roadbook_nodes(soup, roadbook_url)
            if not nodes:
                _logger.warning(
                    f"线上路书页未解析出路书节点（可能为旧版文章格式）: {roadbook_url} html长度={len(html)}"
                )
            return {node.url: node.title for node in nodes}
        except Exception as exc:
            _logger.warning(f"线上路书页获取/解析失败: {roadbook_url} 错误={exc!r}")
            return {}

    @staticmethod
    def diff_pages(
        local_payload: dict[str, Any], online_titles: dict[str, str]
    ) -> tuple[set[str], set[str], set[str]]:
        """比对本地 images.json 与线上节点 {URL: 标题}，返回 (missing, changed, pruned)。

        missing = 线上有、本地无（追加回填）。
        changed = URL 相同、双方标题均非空但不一致（约定“标题变⇒内容变”）。
        pruned  = 本地有、线上无（线上已下架：清理本地记录并删除服务端场景，
                  使展示与线上保持一致）。
        任一方标题为空则不判 changed（无图页天然落在此保护内）；
        线上节点为空时调用方应整体跳过比对，不得产生 pruned。"""
        local_urls: set[str] = set()
        local_titles: dict[str, str] = {}
        for item in local_payload.get("pages") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            local_urls.add(url)
            for image in item.get("images") or []:
                section = (
                    str(image.get("section") or "").strip()
                    if isinstance(image, dict)
                    else ""
                )
                if section:
                    local_titles[url] = section
                    break
        missing = {url for url in online_titles if url not in local_urls}
        changed = {
            url
            for url, title in online_titles.items()
            if url in local_titles and title.strip() and title.strip() != local_titles[url]
        }
        pruned = local_urls - set(online_titles)
        return missing, changed, pruned

    @staticmethod
    def remove_downloaded_pages(state_path: Path, urls: set[str]) -> None:
        """把指定 URL 从下载状态 downloaded_pages 中移除，让增量续传把它们
        当作“未下载”重新抓取。状态文件不存在/损坏时静默返回。"""
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("downloaded_pages"), list):
            return
        payload["downloaded_pages"] = [
            u for u in payload["downloaded_pages"] if u not in urls
        ]
        try:
            state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def remove_image_cache_entries(state_path: Path, srcs: set[str]) -> None:
        """把指定图片 src 的缓存条目从 state.image_url_map 中移除。

        标题变化/下架页重下前必须清缓存：src 还在缓存映射且本地文件还在时，
        下载器会直接复用旧文件（“标题新、图旧”），永远拿不到网站新图。
        状态文件不存在/损坏时静默返回。"""
        if not srcs:
            return
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("image_url_map"), dict):
            return
        cache = payload["image_url_map"]
        for src in srcs:
            cache.pop(src, None)
        try:
            state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def remove_image_pages(images_json_path: Path, urls: set[str]) -> None:
        """从 images.json 中删除指定 URL 的页记录：续传（resumed_urls）与
        新鲜度检查不再视其为“已拥有”，重下后按最新内容重建。"""
        try:
            payload = json.loads(images_json_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, list):
            return
        payload["pages"] = [
            p
            for p in pages
            if not (isinstance(p, dict) and str(p.get("url") or "").strip() in urls)
        ]
        try:
            images_json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        except OSError:
            pass

    def _iter_page_contents(self, page_url: str):
        """产出一个翻页地址下的全部正文块 (地址, 正文节点, 章节提示)。

        旧版页面一个地址对应一个 Mid2L_con，章节提示取正文首行的
        "第N页：标题"行（站点模板生成，可作该页标题）；
        新版路书页则按节点卡逐个产出，地址带 #reader-node-<id> 片段，
        章节提示为节点标题。两者都没有时不产出任何内容（由调用方标记后跳过）。
        """
        html = self._get_html(page_url)
        soup = BeautifulSoup(html, "html.parser")
        nodes = self._find_roadbook_nodes(soup, page_url)
        if nodes:
            for node in nodes:
                yield node.url, node.content, node.title
            return
        content = soup.find("div", class_="Mid2L_con")
        if isinstance(content, Tag):
            yield page_url, content, self._extract_old_format_title(content)

    @staticmethod
    def _extract_old_format_title(content: Tag) -> str:
        """旧版文章页标题：正文容器内首个"第N页：xxx"形式的段落
        （站点模板生成，每页唯一）。不存在返回空串（与旧行为一致）。"""
        for p in content.find_all("p"):
            txt = p.get_text(" ", strip=True)
            m = re.match(r"第\d+页[：:](.+)", txt)
            if m:
                return txt
        return ""

    def _find_pagers(self, soup: Any) -> list[Any]:
        """Collect pager elements (real pages use span.pagecss; older ones use div.page_css)."""
        pagers: list[Any] = []
        for class_name in ("pagecss", "page_css"):
            found = soup.find_all(class_=class_name)
            if isinstance(found, list):
                pagers.extend(found)
        return pagers

    def _estimate_total_pages(self, first_page_url: str) -> int:
        """Estimate the total number of guide pages from the first page.

        Prefers the bottom directory (div.post_ding > li, which lists every
        chapter/page), falls back to the pager's highest numbered link.
        Returns 0 when the count cannot be determined.
        """
        try:
            html = self._get_html(first_page_url)
        except Exception:
            return 0
        soup = BeautifulSoup(html, "html.parser")

        # 新版路书页没有目录/翻页器，节点卡数量即总“页”数
        roadbook_nodes = soup.find_all("article", class_=self.ROADBOOK_NODE_CLASS)
        if roadbook_nodes:
            return len(roadbook_nodes)

        nav = soup.find("div", class_="post_ding")
        if isinstance(nav, Tag):
            items = nav.find_all("li")
            if items:
                return len(items)

        numbered: set[int] = set()
        for pager in self._find_pagers(soup):
            if not isinstance(pager, Tag):
                continue
            for anchor in pager.find_all("a", href=True):
                text = anchor.get_text(strip=True)
                if text.isdigit():
                    numbered.add(int(text))
        if numbered:
            return max(numbered)
        return 0

    def _iterate_pages(self, first_page_url: str):
        current = first_page_url
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            yield current

            html = self._get_html(current)
            soup = BeautifulSoup(html, "html.parser")
            pagers = self._find_pagers(soup)

            next_url = ""
            for pager in pagers:
                if not isinstance(pager, Tag):
                    continue
                for anchor in pager.find_all("a", href=True):
                    text = anchor.get_text(strip=True)
                    if "下一页" in text:
                        next_url = urljoin(current, anchor["href"])
                        break
                if next_url:
                    break

            if not next_url:
                break
            current = next_url

    def _extract_events(self, content: Any) -> list[_Event]:
        events: list[_Event] = []
        block_tags = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]

        for node in content.find_all(block_tags):
            if not isinstance(node, Tag):
                continue

            classes = set(node.get("class") or [])
            if "GsImageLabel" in classes:
                image_url = self._extract_image_url(node)
                if image_url:
                    events.append(_Event(kind="image", value=image_url))
                continue

            text = self._clean_text(node.get_text("\n", strip=True))
            if text:
                if self.PAGE_STOP_TEXT in text:
                    break
                events.append(_Event(kind="text", value=text))

        return events

    def _extract_image_url(self, node: Any) -> str:
        anchor = node.find("a", href=True)
        if not isinstance(anchor, Tag):
            return ""

        href = (anchor.get("href") or "").strip()
        if not href:
            return ""

        if href.startswith(self.IMAGE_PROXY_PREFIX):
            raw = href[len(self.IMAGE_PROXY_PREFIX) :]
        elif "showimage/id_gamersky.shtml?" in href:
            raw = href.split("?", 1)[1]
        else:
            raw = href

        raw = unquote(raw)
        if raw.startswith("//"):
            return "https:" + raw
        return raw

    def _build_mapping(self, events: list[_Event], page_url: str) -> list[dict[str, Any]]:
        mapping: list[dict[str, Any]] = []
        pending_texts: list[str] = []

        for event in events:
            if event.kind == "text":
                pending_texts.append(event.value)
                continue

            if pending_texts:
                text_blob = "\n".join(pending_texts)
                pending_texts = []
                mapping.append({"text": text_blob, "images": [event.value], "url": page_url})
                continue

            if not mapping:
                mapping.append({"text": "", "images": [event.value], "url": page_url})
            else:
                mapping[-1]["images"].append(event.value)

        if pending_texts:
            trailing = "\n".join(pending_texts)
            if mapping:
                if mapping[-1]["text"]:
                    mapping[-1]["text"] += "\n\n" + trailing
                else:
                    mapping[-1]["text"] = trailing
            else:
                mapping.append({"text": trailing, "images": [], "url": page_url})

        return mapping

    def _download_binary(self, url: str, save_path: Path) -> None:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        save_path.write_bytes(response.content)

    def _cached_image_exists(self, cached_path: str) -> bool:
        path = Path(cached_path)
        if path.exists():
            return True
        return (self.base_output_dir / cached_path).exists()

    def _to_game_relative_image_path(self, raw_path: str, game_dir_name: str) -> str:
        text = raw_path.replace("\\", "/")
        marker = f"/{game_dir_name}/"
        idx = text.find(marker)
        if idx >= 0:
            return text[idx + 1 :]
        if text.startswith(game_dir_name + "/"):
            return text
        if text.startswith("images/"):
            return f"{game_dir_name}/{text}"
        filename = Path(text).name
        return f"{game_dir_name}/images/{filename}"

    def _normalize_mapping_image_paths(self, mapping: list[dict[str, Any]], game_dir_name: str) -> bool:
        changed = False
        for row in mapping:
            images = row.get("images")
            if not isinstance(images, list):
                continue
            normalized: list[str] = []
            for item in images:
                if not isinstance(item, str):
                    normalized.append(item)
                    continue
                current = self._to_game_relative_image_path(item, game_dir_name)
                normalized.append(current)
                if current != item:
                    changed = True
            row["images"] = normalized
        return changed

    def _print_progress(self, message: str) -> None:
        line = f"[walkthrough] {message}"
        print(line)
        _logger.info(message)  # 文件日志
        if self.progress_callback is not None:
            self.progress_callback(line)

    @staticmethod
    def _ensure_dependencies_available() -> None:
        if _IMPORT_ERROR is None:
            return
        raise RuntimeError(
            "缺少运行依赖，请先安装: pip install requests beautifulsoup4"
        ) from _IMPORT_ERROR

    @staticmethod
    def _load_mapping(mapping_path: Path) -> list[dict[str, Any]]:
        if not mapping_path.exists():
            return []
        try:
            payload = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _load_state(state_path: Path) -> dict[str, Any]:
        if not state_path.exists():
            return {"downloaded_pages": [], "image_url_map": {}}
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"downloaded_pages": [], "image_url_map": {}}
        if not isinstance(payload, dict):
            return {"downloaded_pages": [], "image_url_map": {}}
        pages = payload.get("downloaded_pages")
        image_map = payload.get("image_url_map")
        return {
            "downloaded_pages": pages if isinstance(pages, list) else [],
            "image_url_map": image_map if isinstance(image_map, dict) else {},
        }

    @staticmethod
    def _save_state(state_path: Path, downloaded_pages: set[str], image_url_map: dict[str, str]) -> None:
        payload = {
            "downloaded_pages": sorted(downloaded_pages),
            "image_url_map": image_url_map,
        }
        state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")

    @staticmethod
    def _next_index(directory: Path, prefix: str, suffix_pattern: str) -> int:
        max_index = 0
        normalized_suffix = suffix_pattern.lower()
        for item in directory.iterdir():
            if not item.is_file():
                continue
            if normalized_suffix != "*" and item.suffix.lower() != normalized_suffix:
                continue
            stem = item.stem
            if not stem.startswith(prefix):
                continue
            seq = stem[len(prefix) :]
            if seq.isdigit():
                max_index = max(max_index, int(seq))
        return max_index + 1

    def _get_html(self, url: str) -> str:
        cached = self._html_cache.get(url)
        if cached is not None:
            return cached
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
        if len(self._html_cache) >= self.HTML_CACHE_SIZE:
            self._html_cache.pop(next(iter(self._html_cache)))
        self._html_cache[url] = html
        return html

    def _page_images_intact(self, payload: dict[str, Any], content_url: str) -> bool:
        """页面文件完整性校验：该页记录引用的本地图片文件是否全部存在。

        状态清单只记录 URL，不感知文件丢失（误删/杀软隔离/磁盘故障等）；
        缺文件的页据此转入重新下载，实现文件级自愈。记录不存在或该页
        无图时视为完好。"""
        for item in payload.get("pages", []):
            if isinstance(item, dict) and str(item.get("url") or "").strip() == content_url:
                for image in item.get("images") or []:
                    if isinstance(image, dict):
                        local = str(image.get("local") or "").strip()
                        if local and not (self.base_output_dir / local).exists():
                            return False
                return True
        return True

    def _repair_missing_page_images(self, record: Any) -> int | None:
        """完整性自愈的定向补齐：把记录里缺失的图片按原 local 路径重下写回。

        只动缺失的文件：不新分配目录、不改任何记录路径、不碰完好文件——
        避免一条页记录横跨多个目录（横跨会让目录对齐改名时把其它目录的
        文件错位）。record 非 dict 或无 images 时返回 None，由调用方退回
        整页重下；返回补齐张数（可能为 0，如 src/local 缺失无法补）。
        """
        images = record.get("images") if isinstance(record, dict) else None
        if not images:
            return None
        repaired = 0
        for image in images:
            if not isinstance(image, dict):
                continue
            local = str(image.get("local") or "").strip()
            src = str(image.get("src") or "").strip()
            if not local or not src:
                continue
            target = self.base_output_dir / local
            if target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._download_binary(src, target)
                repaired += 1
            except Exception as exc:
                _logger.warning(f"完整性自愈补齐失败 {src} -> {local}: {exc!r}")
        return repaired

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        return "\n".join(lines)

    @staticmethod
    def _safe_name(name: str) -> str:
        # Remove characters that are invalid in Windows file names.
        cleaned = re.sub(r'[<>:"/\\|?*]', "_", name.strip())
        return cleaned or "unknown_game"

    @staticmethod
    def _guess_suffix(url: str) -> str:
        path = urlparse(url).path
        suffix = Path(path).suffix.lower()
        if suffix in GamerskyWalkthroughDownloader.SUPPORTED_IMAGE_SUFFIXES:
            return suffix
        return ".jpg"

    @staticmethod
    def _is_supported_image_url(url: str) -> bool:
        path = urlparse(url).path
        suffix = Path(path).suffix.lower()
        return suffix in GamerskyWalkthroughDownloader.SUPPORTED_IMAGE_SUFFIXES

    @staticmethod
    def _resolve_proxy_url(raw_url: str) -> str:
        """Resolve a gamersky showimage proxy URL back to the direct image URL."""
        raw = str(raw_url or "").strip()
        if not raw:
            return ""
        prefix = GamerskyWalkthroughDownloader.IMAGE_PROXY_PREFIX
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
        elif "showimage/id_gamersky.shtml?" in raw:
            raw = raw.split("?", 1)[1]
        raw = unquote(raw)
        if raw.startswith("//"):
            return "https:" + raw
        return raw

    def _collect_page_images(
        self,
        content: Any,
        page_url: str,
        initial_section: str = "",
    ) -> list[dict[str, Any]]:
        """Walk a page's main content and return images in document order.

        Each item: {"src": direct image URL, "section": nearest preceding heading}.
        initial_section 用于路书节点：节点标题在正文之外，作为缺省章节名。
        """
        images: list[dict[str, Any]] = []
        last_heading = initial_section
        for tag in content.find_all(True):
            if not isinstance(tag, Tag):
                continue
            raw_classes = tag.get("class") or []
            if not isinstance(raw_classes, list):
                raw_classes = list(raw_classes)
            classes = set(raw_classes)
            if "GsImageLabel" in classes:
                src = self._extract_image_url(tag)
                if src and self._is_supported_image_url(src):
                    images.append({"src": src, "section": last_heading})
                continue
            if tag.name == "img":
                if tag.find_parent(class_="GsImageLabel") is not None:
                    continue
                raw_src = tag.get("data-original") or tag.get("data-src") or tag.get("src") or ""
                if not raw_src:
                    continue
                raw_src = urljoin(page_url, raw_src)
                if (
                    raw_src.startswith(self.IMAGE_PROXY_PREFIX)
                    or "showimage/id_gamersky.shtml?" in raw_src
                ):
                    raw_src = self._resolve_proxy_url(raw_src)
                if self._is_supported_image_url(raw_src):
                    images.append({"src": raw_src, "section": last_heading})
                continue
            if tag.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                heading_text = self._clean_text(tag.get_text(" ", strip=True))
                if heading_text:
                    last_heading = heading_text
        return images

    @contextmanager
    def _game_download_lock(self, game_dir: Path, game_name: str):
        """同一游戏目录的下载互斥锁（跨进程，修复并发写坏数据）。

        用操作系统级文件锁（Windows msvcrt / POSIX fcntl）而非自管理锁文件：
        持锁进程无论正常结束还是被强杀/断电，操作系统都会自动释放锁，
        不存在"残留锁文件卡死后续下载"的问题。第二个下载进程拿锁失败时
        明确报错退出，绝不与正在进行的下载并发写同一目录。
        """
        game_dir.mkdir(parents=True, exist_ok=True)
        lock_path = game_dir / "download.lock"
        handle = open(lock_path, "a+")
        try:
            locked = False
            try:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                raise RuntimeError(
                    f"已有下载在进行（{game_name} 的下载锁被其它进程占用）。"
                    "为避免并发写坏数据，本次下载退出；请等当前下载结束后再试。"
                ) from None
            except ImportError:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (ImportError, OSError):
                    pass  # 无文件锁能力的环境：退化为不加锁，不阻断下载
            try:
                yield
            finally:
                if locked:
                    try:
                        handle.seek(0)
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except (OSError, ImportError):
                        try:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        except (ImportError, OSError):
                            pass
        finally:
            handle.close()

    def download_images_only(
        self,
        game_name: str,
        start_url: str | None = None,
        on_page: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """同游戏目录的下载入口：先取跨进程下载锁（防并发写坏数据），
        再进入实际下载流程。"""
        game_dir = self.base_output_dir / self._safe_name(game_name)
        with self._game_download_lock(game_dir, game_name):
            return self._download_images_only_locked(game_name, start_url, on_page)

    def _download_images_only_locked(
        self,
        game_name: str,
        start_url: str | None = None,
        on_page: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Download walkthrough images only (no text).

        Records per image: src (direct URL), local (game-relative file path),
        index (document order within the page) and section (nearest heading).
        Returns the images.json payload and writes it to
        <output>/<game>/images.json.

        on_page: 每下载完一页（节点）回调一次，参数为该页记录
        {url, page_index, images:[...]}；回调在 images.json 落盘之后执行，
        抛出异常会中止整次下载（供“边下边导入”的调用方逐页触发导入）。
        """
        max_pages = getattr(self, "max_pages", None)
        walkthrough_url = start_url or self._find_walkthrough_url(game_name)
        walkthrough_url = self._resolve_start_url(walkthrough_url)
        total_pages = self._estimate_total_pages(walkthrough_url)
        game_dir_name = self._safe_name(game_name)
        game_dir = self.base_output_dir / game_dir_name
        pages_dir = game_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        # 启动清扫：清掉上次异常退出（断电/强杀/崩溃）遗留的目录重编号
        # 临时目录 __renum_*，避免残留越积越多或干扰本轮改名。
        # 正常完成的运行不会留下任何 __renum_*。
        swept = [entry.name for entry in pages_dir.glob("__renum_*") if entry.is_dir()]
        if swept:
            for name in swept:
                shutil.rmtree(pages_dir / name, ignore_errors=True)
            self._print_progress(
                f"启动清扫: 清理 {len(swept)} 个上次运行遗留的临时目录（示例 {sorted(swept)[:5]}）"
            )

        images_json_path = game_dir / "images.json"
        state_path = game_dir / "download_images_state.json"

        payload: dict[str, Any] = {"game": game_name, "base_url": walkthrough_url, "pages": []}
        resumed_urls: set[str] = set()
        if images_json_path.exists():
            try:
                old = json.loads(images_json_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = {}
            if isinstance(old, dict) and isinstance(old.get("pages"), list):
                payload = old
                resumed_urls = {
                    str(item.get("url") or "")
                    for item in old["pages"]
                    if isinstance(item, dict) and item.get("url")
                }
        # 旧文件/外部文件可能缺 game/base_url（如手工编辑过）：补齐自愈，
        # 否则打开路径的新鲜度检查会因缺 base_url 永远跳过
        payload.setdefault("game", game_name)
        payload.setdefault("base_url", walkthrough_url)

        state = self._load_state(state_path)
        downloaded_pages = set(state.get("downloaded_pages", []))
        image_url_map: dict[str, str] = dict(state.get("image_url_map", {}))

        # 新鲜度比对（与打开线共用同一套 diff）：追加页照常走续传；标题变化
        # 页按约定“标题变⇒内容变”，就地清理内存与磁盘记录后本轮重新下载；
        # 本地有、线上无的页视为已下架，同样清理（使展示与线上保持一致）。
        # 检出变化时打印约定标记行——download_and_import_walkthrough.py 靠
        # “标题变化重导:”“本地多出页清理:”两行决定导入时是否追加
        # --force-reimport / --prune-missing（跨进程关键词契约，与“导入完成”同款）。
        online_titles = self._guide_titles(walkthrough_url)
        changed_urls: set[str] = set()
        pruned_urls: set[str] = set()
        if online_titles:
            missing, changed_urls, pruned_urls = self.diff_pages(payload, online_titles)
            if missing or changed_urls or pruned_urls:
                stale = missing | changed_urls | pruned_urls
                if changed_urls:
                    self._print_progress(
                        f"标题变化重导: {len(changed_urls)} — 标题变化按约定视为内容已变，"
                        "本轮将重新下载并在导入时强制重导"
                    )
                    # 从续传集合移除但保留 payload 记录：页面会被重新处理，
                    # 重下后按 URL 原位替换记录（不删记录，避免误伤并发读）
                    resumed_urls -= changed_urls
                if pruned_urls:
                    self._print_progress(
                        f"本地多出页清理: {len(pruned_urls)} — 线上已下架，"
                        "本轮导入将删除对应服务端场景"
                    )
                    # 下架清理：该页的图片缓存条目一并移除——下架即清除该页的
                    # 一切本地痕迹，避免遗留指向已删目录的悬空缓存条目
                    pruned_srcs: set[str] = set()
                    for item in payload.get("pages", []):
                        if (
                            isinstance(item, dict)
                            and str(item.get("url") or "").strip() in pruned_urls
                        ):
                            for image in item.get("images") or []:
                                if isinstance(image, dict):
                                    src = str(image.get("src") or "").strip()
                                    if src:
                                        pruned_srcs.add(src)
                    payload["pages"] = [
                        p
                        for p in payload.get("pages", [])
                        if not (
                            isinstance(p, dict)
                            and str(p.get("url") or "").strip() in pruned_urls
                        )
                    ]
                    resumed_urls -= pruned_urls
                    for src in pruned_srcs:
                        image_url_map.pop(src, None)
                downloaded_pages -= stale
                self._save_state(state_path, downloaded_pages, image_url_map)
        self.last_changed_urls = set(changed_urls)
        # 写机器可读的更新清单（下载线枢纽进程 download_and_import 消费；
        # 每次下载都覆写，陈旧清单自然失效）
        try:
            update_dir = game_dir / "_update_tmp"
            update_dir.mkdir(exist_ok=True)
            (update_dir / "pending_update.json").write_text(
                json.dumps(
                    {"changed_urls": sorted(changed_urls), "prune": bool(pruned_urls)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        self._print_progress(f"开始下载攻略图片(纯图片): {game_name}")
        self._print_progress(f"输出目录: {game_dir.as_posix()}")
        self._print_progress(f"历史已下载页面: {len(downloaded_pages)}")
        if total_pages:
            self._print_progress(f"攻略共约 {total_pages} 页")

        page_index = 0
        new_page_count = 0
        skipped_page_count = 0
        new_image_count = 0
        reused_image_count = 0
        skipped_image_count = 0
        integrity_repairs = 0  # 完整性自愈：检出文件缺失并自动重下的页数

        # 新页编号永不复用已有编号（原因同 download_walkthrough）：以现有
        # pages 目录与 images.json 已有页号的最大值 +1 顺延；全新下载时与
        # 位置编号一致，重下的页也拿全新页号，不会写脏其它章节的同号目录。
        used_page_nos: set[int] = set()
        for entry in pages_dir.iterdir():
            m = re.fullmatch(r"page_(\d+)", entry.name)
            if m:
                used_page_nos.add(int(m.group(1)))
        for item in payload.get("pages", []):
            if isinstance(item, dict):
                try:
                    used_page_nos.add(int(item.get("page_index")))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    pass
        next_page_no = max(used_page_nos, default=0) + 1

        stop_download = False
        consecutive_failures = 0  # 连续失败的图片数（成功一张即清零；触网即断防雪崩）
        for page_url in self._iterate_pages(walkthrough_url):
            if stop_download:
                break
            processed_any = False
            for content_url, content, section_title in self._iter_page_contents(page_url):
                processed_any = True
                page_index += 1
                label = f"「{section_title}」" if section_title else ""
                if total_pages:
                    self._print_progress(f"处理第{page_index}/{total_pages}页{label}(仅图片): {content_url}")
                else:
                    self._print_progress(f"处理第{page_index}页{label}(仅图片): {content_url}")
                already = content_url in downloaded_pages or content_url in resumed_urls
                if already and not self._page_images_intact(payload, content_url):
                    # 完整性自愈：状态清单认为已下载，但引用的本地图片文件
                    # 缺失（误删/杀软隔离等）→ 定向补齐缺失文件（原路径写回，
                    # 不新分配目录、不动完好文件）。仅当记录无法定位时才退回
                    # 整页重下（历史行为）。
                    record = next(
                        (
                            p
                            for p in payload.get("pages", [])
                            if isinstance(p, dict)
                            and str(p.get("url") or "").strip() == content_url
                        ),
                        None,
                    )
                    repaired = self._repair_missing_page_images(record)
                    if repaired is None:
                        already = False
                        downloaded_pages.discard(content_url)
                        resumed_urls.discard(content_url)
                        integrity_repairs += 1
                        self._print_progress(
                            f"完整性自愈: 第{page_index}页存在缺失的图片文件，自动重新下载"
                        )
                    else:
                        if repaired:
                            integrity_repairs += 1
                            self._print_progress(
                                f"完整性自愈: 第{page_index}页补齐 {repaired} 张缺失图片（原路径写回）"
                            )
                        already = True
                if already:
                    skipped_page_count += 1
                    self._print_progress(f"已下载过第{page_index}页，跳过")
                    continue

                if max_pages is not None and new_page_count >= max_pages:
                    self._print_progress(f"达到页面下载上限 {max_pages}，停止继续下载")
                    stop_download = True
                    break

                # 标题变化页：原目录清空重建——目录号不变，目录内旧文件全部
                # 清除后按新内容重新下载（图片缓存同时绕过，杜绝同 URL 旧图
                # 复用导致的“标题新、图旧”）。新章节（missing）不受影响，
                # 仍按最大页号+1 分配新目录。
                rebuild_dir = ""
                if content_url in changed_urls:
                    old = next(
                        (
                            p
                            for p in payload.get("pages", [])
                            if isinstance(p, dict) and str(p.get("url") or "").strip() == content_url
                        ),
                        None,
                    )
                    if isinstance(old, dict) and old.get("images"):
                        parts = str(old["images"][0].get("local") or "").split("/")
                        if len(parts) >= 3 and re.fullmatch(r"page_(\d+)", parts[-3]):
                            rebuild_dir = parts[-3]
                if rebuild_dir:
                    page_no = int(re.fullmatch(r"page_(\d+)", rebuild_dir).group(1))
                    page_dir = pages_dir / rebuild_dir
                    for child in page_dir.iterdir():
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            try:
                                child.unlink()
                            except OSError:
                                pass
                    self._print_progress(f"原目录重建: {rebuild_dir}（清空后按最新内容重新下载）")
                else:
                    page_no = next_page_no
                    next_page_no += 1
                    page_dir = pages_dir / f"page_{page_no}"
                page_images_dir = page_dir / "images"
                page_images_dir.mkdir(parents=True, exist_ok=True)
                image_counter = self._next_index(page_images_dir, "image_", "*")

                page_images: list[dict[str, Any]] = []
                page_reused = 0
                page_downloaded = 0
                for item in self._collect_page_images(content, content_url, initial_section=section_title):
                    src = item["src"]
                    # 变化页绕过图片缓存：标题变视为内容已变，逐张强制重新下载
                    cached_path = None if content_url in changed_urls else image_url_map.get(src)
                    if cached_path and self._cached_image_exists(cached_path):
                        local = self._to_game_relative_image_path(cached_path, game_dir_name)
                        page_reused += 1
                        reused_image_count += 1
                    else:
                        suffix = self._guess_suffix(src)
                        image_path = page_images_dir / f"image_{image_counter:04d}{suffix}"
                        try:
                            self._download_binary(src, image_path)
                        except Exception as exc:
                            skipped_image_count += 1
                            consecutive_failures += 1
                            self._print_progress(
                                f"图片下载失败(跳过): {src} 错误={exc}"
                                f"（连续失败 {consecutive_failures}/{self.MAX_CONSECUTIVE_FAILURES}）"
                            )
                            if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                                stop_download = True
                                raise RuntimeError(
                                    f"连续 {consecutive_failures} 张图片下载失败（已自动重试），"
                                    f"判定网络不可用，中止本次下载: {exc}"
                                ) from exc
                            continue
                        consecutive_failures = 0
                        image_counter += 1
                        local = self._to_game_relative_image_path(image_path.as_posix(), game_dir_name)
                        image_url_map[src] = local
                        page_downloaded += 1
                        new_image_count += 1
                    page_images.append(
                        {
                            "src": src,
                            "local": local,
                            "index": len(page_images),
                            "section": item.get("section", ""),
                        }
                    )

                page_record = {"url": content_url, "page_index": page_no, "images": page_images}
                replaced = False
                for existing in payload.get("pages", []):
                    if isinstance(existing, dict) and str(existing.get("url") or "") == content_url:
                        existing.clear()
                        existing.update(page_record)
                        replaced = True
                        break
                if not replaced:
                    payload.setdefault("pages", []).append(page_record)
                new_page_count += 1

                downloaded_pages.add(content_url)
                images_json_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
                self._save_state(state_path, downloaded_pages, image_url_map)
                if on_page is not None:
                    on_page(page_record)
                self._print_progress(
                    f"本页完成(仅图片): 新下载{page_downloaded}张, 复用{page_reused}张, "
                    f"跳过{skipped_image_count}张, 累计图片{len(page_images)}张"
                )
            if not processed_any:
                downloaded_pages.add(page_url)
                self._save_state(state_path, downloaded_pages, image_url_map)
                self._print_progress("未找到正文容器 Mid2L_con，标记后跳过")

        # 同步后按线上节点顺序重排页记录（阅读顺序与网站保持一致）：
        # 目录文件一律不动——文件夹编号只是磁盘上的稳定标签，这里只调整
        # 记录列表顺序并把 page_index 重新标记为 1..N；scene_map 由下次
        # 导入按记录顺序重建，客户端导航按列表顺序走。无线上参照（旧版
        # 攻略/解析失败）时跳过重排。
        if online_titles:
            online_order = {url: pos for pos, url in enumerate(online_titles)}
            payload["pages"] = sorted(
                (p for p in payload.get("pages", []) if isinstance(p, dict)),
                key=lambda p: online_order.get(str(p.get("url") or "").strip(), len(online_order)),
            )
            for pos, page in enumerate(payload["pages"], start=1):
                page["page_index"] = pos

            # 孤儿目录清理：重排落盘后，被记录引用的目录集合精确可知——
            # pages/ 下不在集合里的 page_N 目录是历史重下/下架遗留的旧版本，
            # 直接删除，使磁盘目录与记录严格一一对应。被引用的目录绝不在此列；
            # 只清理 page_N 形态的目录，pages/ 下的其它文件一律不碰。
            referenced_dirs: set[str] = set()
            for page in payload.get("pages", []):
                if not isinstance(page, dict):
                    continue
                for image in page.get("images") or []:
                    parts = (
                        str(image.get("local") or "").split("/")
                        if isinstance(image, dict)
                        else []
                    )
                    if len(parts) >= 3:
                        referenced_dirs.add(parts[-3])
            removed_dirs: list[str] = []
            for entry in pages_dir.iterdir():
                if (
                    entry.is_dir()
                    and entry.name not in referenced_dirs
                    and re.fullmatch(r"page_(\d+)", entry.name)
                ):
                    shutil.rmtree(entry, ignore_errors=True)
                    removed_dirs.append(entry.name)
            if removed_dirs:
                # 被删目录的图片缓存条目一并清除，防悬空缓存
                removed_set = set(removed_dirs)
                for src in [
                    s
                    for s, v in image_url_map.items()
                    if len(v.split("/")) >= 3 and v.split("/")[2] in removed_set
                ]:
                    image_url_map.pop(src, None)
                self._save_state(state_path, downloaded_pages, image_url_map)
                self._print_progress(
                    f"孤儿目录清理: 删除 {len(removed_dirs)} 个无引用的旧目录（示例 {sorted(removed_dirs)[:5]}）"
                )

            # 目录按网站顺序重编号：目录名、page_index、网站位置三者严格一致
            # （约定：删除后回填回到原号，不再产生分配序号漂移）。
            # 两阶段改名（先挪临时名再改终名）避免改名过程中的名字冲突；
            # 改名后同步修正记录 local 路径，任何时刻记录都指向真实存在的文件。
            # 0 图占位页无目录，其网站位置自然空缺编号。
            rename_plan: list[tuple[int, dict[str, Any], str]] = []
            for pos, page in enumerate(payload["pages"], start=1):
                if isinstance(page, dict) and page.get("images"):
                    seg = str(page["images"][0].get("local") or "").split("/")
                    if len(seg) >= 3 and seg[2] != f"page_{pos}":
                        rename_plan.append((pos, page, seg[2]))
            if rename_plan:
                # 两阶段改名（全部挪到唯一临时名 __renum_N → 再统一落位）。
                # 加固：目录缺失/被多条记录重复引用 → 剔除该条并告警，绝不硬改；
                # 目标位被意外占用（异常状态残留）→ 先隔离为 __renum_stale_N，
                # 留给下次下载的"启动清扫"统一清理；全程 try/finally，任何异常
                # 都清掉本轮 __renum_N 临时目录，绝不让半成品滞留磁盘
                # （临时目录里的图片可通过完整性自愈重新下载恢复）。
                valid_plan: list[tuple[int, dict[str, Any], str]] = []
                claimed: set[str] = set()
                for pos, page, old_name in rename_plan:
                    if old_name in claimed:
                        self._print_progress(
                            f"目录对齐: 目录 {old_name} 被多条记录引用，位置 {pos} 本轮跳过"
                        )
                        continue
                    if not (pages_dir / old_name).is_dir():
                        self._print_progress(
                            f"目录对齐: 目录 {old_name} 不存在，位置 {pos} 本轮跳过"
                        )
                        continue
                    claimed.add(old_name)
                    valid_plan.append((pos, page, old_name))
                # 每个缓存条目按“原始目录”判定归属：先快照原始目录名再改写，
                # 防止前一轮改写的结果（已是终名）被后一轮当成旧名再次改写
                src_orig_dir: dict[str, Any] = {}
                for src, cached in image_url_map.items():
                    parts = cached.split("/")
                    src_orig_dir[src] = parts[2] if len(parts) >= 3 else None
                try:
                    for pos, _page, old_name in valid_plan:
                        tmp = pages_dir / f"__renum_{pos}"
                        if tmp.exists():
                            shutil.rmtree(tmp, ignore_errors=True)
                        (pages_dir / old_name).rename(tmp)
                    for pos, page, old_name in valid_plan:
                        dst = pages_dir / f"page_{pos}"
                        if dst.exists():
                            stale = pages_dir / f"__renum_stale_{pos}"
                            if stale.exists():
                                shutil.rmtree(stale, ignore_errors=True)
                            dst.rename(stale)
                            self._print_progress(
                                f"目录对齐: 目标位 {dst.name} 被意外占用，已隔离为 {stale.name}"
                            )
                        (pages_dir / f"__renum_{pos}").rename(dst)
                        for image in page.get("images") or []:
                            if isinstance(image, dict):
                                seg = str(image.get("local") or "").split("/")
                                if len(seg) >= 3 and seg[2] == old_name:
                                    seg[2] = dst.name
                                    image["local"] = "/".join(seg)
                        # 图片缓存映射同步改写：目录改名后 state 里指向旧目录名
                        # 的条目必须跟着改，否则留下悬空缓存条目
                        for src, orig in src_orig_dir.items():
                            if orig == old_name:
                                parts = image_url_map[src].split("/")
                                parts[2] = dst.name
                                image_url_map[src] = "/".join(parts)
                finally:
                    leftovers = [
                        entry.name
                        for entry in pages_dir.glob("__renum_*")
                        if entry.is_dir() and re.fullmatch(r"__renum_\d+", entry.name)
                    ]
                    for entry in leftovers:
                        shutil.rmtree(pages_dir / entry, ignore_errors=True)
                    if leftovers:
                        self._print_progress(
                            f"目录对齐: 本轮中断，已清理 {len(leftovers)} 个临时目录"
                            "（涉及图片可由完整性自愈重新下载）"
                        )
                if valid_plan:
                    self._save_state(state_path, downloaded_pages, image_url_map)
                    self._print_progress(
                        f"目录对齐: 按网站顺序重编号（改名 {len(valid_plan)} 个，目录现为 page_1..page_{len(payload['pages'])}）"
                    )
            images_json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )

        self._print_progress(
            "下载结束(仅图片): "
            f"扫描{page_index}页, "
            f"新处理{new_page_count}页, "
            f"完整性修复{integrity_repairs}页, "
            f"跳过{skipped_page_count}页, "
            f"新下载图片{new_image_count}张, "
            f"复用图片{reused_image_count}张, "
            f"跳过图片{skipped_image_count}张, "
            f"输出文件 {images_json_path.as_posix()}"
        )
        # 标记本次下载正常完成：中途退出/失败时不会有该标记，重启后
        # _ensure_bootstrap 据此判定“未完成的下载”并继续续传
        state = self._load_state(state_path)
        state["finished"] = True
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载游民星空图文攻略并导出 text_images.json")
    parser.add_argument("game_name", help="要下载攻略的游戏名")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=_DEFAULT_WALKTHROUGH_DIR.as_posix(),
        type=str,
        help="保存目录，将在其下创建游戏子目录",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP 请求超时时间，单位秒，默认 20",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="本次最多下载的页面数，默认不限制",
    )
    parser.add_argument(
        "--images-only",
        action="store_true",
        help="只下载图片并生成 images.json（不下载文本）",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="直接指定攻略第一页的网址，跳过“搜索攻略”步骤",
    )
    return parser.parse_args()


def main() -> int:
    from app.file_logging import setup_logging

    setup_logging()
    args = _parse_args()
    if args.max_pages is not None and args.max_pages <= 0:
        print("参数错误: --max-pages 必须是正整数", file=sys.stderr)
        return 2

    downloader = GamerskyWalkthroughDownloader(
        base_output_dir=args.output_dir,
        timeout=args.timeout,
        max_pages=args.max_pages,
    )
    game_dir = Path(args.output_dir) / downloader._safe_name(args.game_name)

    if args.images_only:
        try:
            payload = downloader.download_images_only(args.game_name, start_url=args.url)
        except Exception as exc:
            print(f"下载失败: {exc}", file=sys.stderr)
            return 1
        pages = payload.get("pages", [])
        total_images = sum(len(p.get("images") or []) for p in pages if isinstance(p, dict))
        print(
            "导出完成(仅图片): "
            f"{len(pages)} 页, {total_images} 张图片, "
            f"输出文件 {game_dir.joinpath('images.json').as_posix()}"
        )
        return 0

    try:
        mapping = downloader.download_walkthrough(args.game_name, start_url=args.url)
    except Exception as exc:
        print(f"下载失败: {exc}", file=sys.stderr)
        return 1

    print(
        "导出完成: "
        f"{len(mapping)} 条映射, "
        f"输出文件 {game_dir.joinpath('text_images.json').as_posix()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

