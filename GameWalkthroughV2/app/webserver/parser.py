"""Parse gamersky walkthrough pages into chapters and image-text pages.

Based on the real DOM structure of gamersky guide pages (verified against
https://www.gamersky.com/handbook/202303/1575258.shtml):

  - Main content lives in <div class="Mid2L_con">. Inside it, text lives in
    <p> and images live in <p class="GsImageLabel"><a href="showimage...">.
  - Each guide page (第N页) is a "chapter". The bottom of every page has a
    directory box 文章内容导航:

        <div class="post_ding"><div class="Content_Paging">
          <div class="hd">文章内容导航</div>
          <div class="bd"><span class="pagecss"><ul>
            <li><b>第1页：第1章-猎人小屋</b></li>
            <li><a href="..._2.shtml">第2页：第1章-初遇电锯男</a></li>
            ...
          </ul></span></div>
        </div></div>

    That directory lists every chapter with a human readable title, so the
    webserver TOC dropdown is built from it (all chapters), and chapter order
    follows the directory order.
  - Page-to-page navigation uses <span class="pagecss"> (older pages may use
    div.page_css) with "上一页" / "下一页" anchors.

The parsed guide is a dict:

    {
      "game": str,
      "toc": [{"name": "第2页：第1章-初遇电锯男", "chapter": 2}, ...],
      "chapters": [
        {
          "url": str,
          "title": str,
          "index": int,
          "pages": [{"index": int, "text": str, "image_src": str, "image_local": str}]
        }
      ]
    }
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from app.game_walkthrough_downloader import GamerskyWalkthroughDownloader as _DL

IMAGE_PROXY_PREFIX = _DL.IMAGE_PROXY_PREFIX
PAGE_STOP_TEXT = _DL.PAGE_STOP_TEXT
AD_KEYWORDS = (
    "ad",
    "advert",
    "banner",
    "ggad",
    "gg",
    "floatgg",
    "tjmw",
    "tjgg",
    "promo",
    "sgg",
    "gs_nc_editor",
    "referencecontent",
    "blockreference",
    "gs_ccs_solve",
    "post_ding_top",
    "GsWeTxt",
    "pagecss",
    "page_css",
    "Content_Paging",
)


def _clean_text(text: str) -> str:
    return _DL._clean_text(text)


def _resolve_proxy_url(raw_url: str) -> str:
    return _DL._resolve_proxy_url(raw_url)


def _is_supported_image_url(url: str) -> bool:
    return _DL._is_supported_image_url(url)


class GuideParser:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
        )

    def close(self) -> None:
        self.session.close()

    # ------------------------------------------------------------- top level
    def parse_guide(self, game: str, start_url: str, images_map: dict[str, str] | None = None) -> dict[str, Any]:
        """Parse every chapter of a walkthrough guide and return chapters + TOC."""
        images_map = images_map or {}
        first_soup = self._fetch_soup(start_url)
        entries = self._collect_chapter_entries(first_soup, start_url)

        chapters: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            url = entry.get("url") or start_url
            if index == 0:
                soup = first_soup
            else:
                try:
                    soup = self._fetch_soup(url)
                except Exception:
                    continue
            try:
                chapter = self._parse_page(url, soup, entry.get("name", ""), images_map)
            except Exception:
                continue
            if chapter is None:
                continue
            chapter["index"] = index + 1
            chapters.append(chapter)

        if not chapters:
            raise RuntimeError(f"无法解析攻略页面: {start_url}")

        toc = [
            {"name": entry.get("name") or f"第{index + 1}章", "chapter": index + 1}
            for index, entry in enumerate(entries)
            if entry.get("url") is not None
        ]
        if not toc:
            toc = [{"name": f"第{index + 1}章", "chapter": index + 1} for index in range(len(chapters))]

        return {"game": game, "toc": toc, "chapters": chapters}

    # ------------------------------------------------------------ chapter TOC
    def _collect_chapter_entries(self, soup: Any, current_url: str) -> list[dict[str, str]]:
        """Extract the ordered chapter list from the bottom directory.

        Prefers the 文章内容导航 directory (div.post_ding). Falls back to pager
        numbered links. Returns a list of {"name": str, "url": str|None}.
        """
        entries: list[dict[str, str]] = []
        nav = soup.find("div", class_="post_ding")
        if isinstance(nav, Tag):
            for li in nav.find_all("li"):
                name = _clean_text(li.get_text("", strip=True))
                if not name:
                    continue
                anchor = li.find("a", href=True)
                url = urljoin(current_url, anchor["href"]) if isinstance(anchor, Tag) else ""
                entries.append({"name": name, "url": url})
        if entries:
            return entries

        # fallback: pager numbered links
        for class_name in ("pagecss", "page_css"):
            for el in soup.find_all(class_=class_name):
                if not isinstance(el, Tag):
                    continue
                for anchor in el.find_all("a", href=True):
                    text = anchor.get_text(strip=True)
                    if text.isdigit():
                        entries.append(
                            {"name": text, "url": urljoin(current_url, anchor["href"])}
                        )
        if entries:
            return entries

        return [{"name": "", "url": current_url}]

    # -------------------------------------------------------- single chapter
    def _parse_page(
        self,
        url: str,
        soup: Any,
        fallback_title: str,
        images_map: dict[str, str],
    ) -> dict[str, Any] | None:
        content = soup.find("div", class_="Mid2L_con")
        if not isinstance(content, Tag):
            return None

        title = _clean_text(fallback_title)
        if not title:
            h1_tag = soup.find("h1")
            if isinstance(h1_tag, Tag):
                title = _clean_text(h1_tag.get_text(" ", strip=True))
        if not title:
            title_tag = soup.find("title")
            if isinstance(title_tag, Tag):
                title = _clean_text(title_tag.get_text(" ", strip=True))
        if not title:
            title = url.rstrip("/").rsplit("/", 1)[-1]

        self._remove_ads(content)
        pages = self._build_pages(content, url, images_map)
        if not pages:
            return None
        return {"url": url, "title": title, "pages": pages}

    # -------------------------------------------------------------- content
    @staticmethod
    def _classes_of(tag: Any) -> list[str]:
        """Return the class tokens of a tag, tolerating odd elements (attrs=None)."""
        if not isinstance(tag, Tag):
            return []
        attrs = tag.attrs if isinstance(tag.attrs, dict) else {}
        value = attrs.get("class") or []
        return value if isinstance(value, list) else list(value)

    def _remove_ads(self, content: Tag) -> None:
        for tag in list(content.find_all(True)):
            if not isinstance(tag, Tag):
                continue
            attrs = tag.attrs if isinstance(tag.attrs, dict) else {}
            classes = " ".join(self._classes_of(tag))
            ids = attrs.get("id") or ""
            hay = f"{classes} {ids}".lower()
            if tag.name in ("script", "style", "iframe", "noscript"):
                tag.decompose()
                continue
            if any(keyword in hay for keyword in AD_KEYWORDS):
                tag.decompose()

    def _build_pages(
        self,
        content: Tag,
        page_url: str,
        images_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Split content into pages. 每页 = 一段文本 + 一张或多张图片。

        连续的图片并入同一页；只有图片 / 只有文本的页面并入上一页；
        整章只有文本或只有图片时保留单页。
        """
        events: list[tuple[str, Any]] = []
        for tag in content.find_all(True):
            if not isinstance(tag, Tag):
                continue
            classes = set(self._classes_of(tag))
            if "GsImageLabel" in classes:
                src = self._resolve_tag_image_src(tag, page_url)
                if src and _is_supported_image_url(src):
                    events.append(("image", src))
                continue
            if tag.name == "img" and tag.find_parent(class_="GsImageLabel") is None:
                src = self._resolve_img_src(tag, page_url)
                if src and _is_supported_image_url(src):
                    events.append(("image", src))
                continue
            if tag.name in ("p", "li", "td", "blockquote"):
                text = _clean_text(tag.get_text("\n", strip=True))
                if not text:
                    continue
                if PAGE_STOP_TEXT in text:
                    break
                events.append(("text", text))

        # 分组：文本作为分页边界；图片累积到当前页
        pages: list[dict[str, Any]] = []
        current: dict[str, Any] = {"text": [], "images": []}
        for kind, value in events:
            if kind == "text":
                if current["images"] and current["text"]:
                    pages.append(current)
                    current = {"text": [], "images": []}
                current["text"].append(value)
            else:
                current["images"].append(value)
        if current["text"] or current["images"]:
            pages.append(current)

        # 合并：图片-only / 文本-only 页面并入上一页
        merged: list[dict[str, Any]] = []
        for page in pages:
            if not merged:
                merged.append(page)
                continue
            last = merged[-1]
            if page["images"] and not page["text"]:
                last["images"].extend(page["images"])
            elif page["text"] and not page["images"]:
                last["text"].extend(page["text"])
            else:
                merged.append(page)

        return [
            {
                "index": index + 1,
                "text": "\n".join(page["text"]),
                "images": [
                    {"src": src, "local": images_map.get(src, "")}
                    for src in page["images"]
                ],
            }
            for index, page in enumerate(merged)
        ]

    # ------------------------------------------------------------ image src
    def _resolve_tag_image_src(self, tag: Tag, page_url: str) -> str:
        anchor = tag.find("a", href=True)
        if isinstance(anchor, Tag):
            href = (anchor.get("href") or "").strip()
            if href:
                if href.startswith(IMAGE_PROXY_PREFIX) or "showimage/id_gamersky.shtml?" in href:
                    resolved = _resolve_proxy_url(href)
                    if resolved:
                        return resolved
                return urljoin(page_url, href)
        return self._resolve_img_src(tag, page_url)

    def _resolve_img_src(self, tag: Tag, page_url: str) -> str:
        raw = tag.get("data-original") or tag.get("data-src") or tag.get("src") or ""
        raw = str(raw).strip()
        if not raw:
            return ""
        if raw.startswith("data:"):
            return ""
        raw = urljoin(page_url, raw)
        if raw.startswith(IMAGE_PROXY_PREFIX) or "showimage/id_gamersky.shtml?" in raw:
            resolved = _resolve_proxy_url(raw)
            if resolved:
                return resolved
        return raw

    # ---------------------------------------------------------------- util
    def _fetch_soup(self, url: str) -> Any:
        """Fetch and parse a page, retrying politely on transient errors (e.g. rate limits)."""
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                response.encoding = response.apparent_encoding or "utf-8"
                return BeautifulSoup(response.text, "html.parser")
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"无法获取页面: {url}")
