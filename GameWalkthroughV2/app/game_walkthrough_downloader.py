from __future__ import annotations

import argparse
import json
import logging
import re
import sys
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
        self._html_cache: dict[str, str] = {}

    def download_walkthrough(
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

                page_dir = pages_dir / f"page_{page_index}"
                page_texts_dir = page_dir / "texts"
                page_images_dir = page_dir / "images"
                page_rel_prefix = f"{game_dir_name}/pages/page_{page_index}/"
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
        html = self._get_html(search_url)
        soup = BeautifulSoup(html, "html.parser")

        result_container = soup.find("div", class_="Mid2_L")
        if not isinstance(result_container, Tag):
            raise RuntimeError("未找到搜索结果容器 Mid2_L")

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

    def _iter_page_contents(self, page_url: str):
        """产出一个翻页地址下的全部正文块 (地址, 正文节点, 章节提示)。

        旧版页面一个地址对应一个 Mid2L_con；新版路书页则按节点卡逐个产出，
        地址带 #reader-node-<id> 片段，章节提示为节点标题。两者都没有时
        不产出任何内容（由调用方标记后跳过）。
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
            yield page_url, content, ""

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

    def download_images_only(
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

        state = self._load_state(state_path)
        downloaded_pages = set(state.get("downloaded_pages", []))
        image_url_map: dict[str, str] = dict(state.get("image_url_map", {}))

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
                if content_url in downloaded_pages or content_url in resumed_urls:
                    skipped_page_count += 1
                    self._print_progress(f"已下载过第{page_index}页，跳过")
                    continue

                if max_pages is not None and new_page_count >= max_pages:
                    self._print_progress(f"达到页面下载上限 {max_pages}，停止继续下载")
                    stop_download = True
                    break

                page_dir = pages_dir / f"page_{page_index}"
                page_images_dir = page_dir / "images"
                page_images_dir.mkdir(parents=True, exist_ok=True)
                image_counter = self._next_index(page_images_dir, "image_", "*")

                page_images: list[dict[str, Any]] = []
                page_reused = 0
                page_downloaded = 0
                for item in self._collect_page_images(content, content_url, initial_section=section_title):
                    src = item["src"]
                    cached_path = image_url_map.get(src)
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

                page_record = {"url": content_url, "page_index": page_index, "images": page_images}
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

        self._print_progress(
            "下载结束(仅图片): "
            f"扫描{page_index}页, "
            f"新处理{new_page_count}页, "
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

