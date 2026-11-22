from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache

from config_store import config_store, normalize_url
from providers.base import CatalogItem, StreamServer

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

GENRE_MAP: dict[str, str] = {
    "Hành Động": "hanh-dong",
    "Tình Cảm": "tinh-cam",
    "Hài Hước": "hai-huoc",
    "Cổ Trang": "co-trang",
    "Tâm Lý": "tam-ly",
    "Hình Sự": "hinh-su",
    "Chiến Tranh": "chien-tranh",
    "Thể Thao": "the-thao",
    "Võ Thuật": "vo-thuat",
    "Viễn Tưởng": "vien-tuong",
    "Phiêu Lưu": "phieu-luu",
    "Khoa Học": "khoa-hoc",
    "Kinh Dị": "kinh-di",
    "Âm Nhạc": "am-nhac",
    "Thần Thoại": "than-thoai",
    "Tài Liệu": "tai-lieu",
    "Gia Đình": "gia-dinh",
    "Chính Kịch": "chinh-kich",
    "Bí Ẩn": "bi-an",
    "Học Đường": "hoc-duong",
    "Hoạt Hình": "hoat-hinh",
    "TV Shows": "tv-shows",
}

CATALOG_PATH_MAP: dict[str, str] = {
    "vn-moi": "/danh-sach/phim-moi",
    "vn-bo": "/danh-sach/phim-bo",
    "vn-le": "/danh-sach/phim-le",
    "vn-hoat-hinh": "/the-loai/hoat-hinh",
    "vn-thuyet-minh": "/danh-sach/phim-thuyet-minh",
}


def _extract_slug(href: str) -> str:
    path = urlparse(href).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if "phim" in parts:
        idx = parts.index("phim")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else ""


def _extract_bg_image(style: str, base_url: str) -> str | None:
    if not style:
        return None
    m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
    if not m:
        return None
    return _abs_url(m.group(1).strip(), base_url)


def _abs_url(url: str | None, base_url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        if urlparse(url).netloc == urlparse(base_url).netloc and base_url.startswith("https://"):
            return url.replace("http://", "https://", 1)
        return url
    return urljoin(base_url + "/", url.lstrip("/"))


def _infer_type_from_badge(badge: str, default_type: str) -> str:
    b = (badge or "").lower()
    if any(k in b for k in ["full", "hd", "fhd", "cam", "trailer"]):
        if "tập" not in b and "/" not in b:
            return "movie"
    if "tập" in b or re.search(r"\(\d+/\d+\)", b):
        if "(1/1)" in b:
            return "movie"
        return "series"
    return default_type


def _episode_sort_key(ep_slug: str, ep_label: str) -> tuple[int, float, str]:
    m = re.search(r"(\d+(?:\.\d+)?)", ep_label or ep_slug)
    if m:
        return (0, float(m.group(1)), ep_slug)
    if "full" in (ep_slug or "").lower():
        return (0, 1.0, ep_slug)
    return (1, 0.0, ep_slug)


class MotchillProvider:
    """Provider hỗ trợ cả 2 giao diện Motchill: myui-vodlist (motchillv) và WordPress li.item (motchillti)."""

    def __init__(self, source_id: str, client: httpx.AsyncClient) -> None:
        self.source_id = source_id
        self.client = client
        self._catalog_cache: TTLCache[str, list[CatalogItem]] = TTLCache(maxsize=256, ttl=300)
        self._meta_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=512, ttl=600)

    def clear_cache(self) -> None:
        self._catalog_cache.clear()
        self._meta_cache.clear()

    @property
    def source_cfg(self) -> dict[str, Any]:
        return config_store.get_source(self.source_id)

    @property
    def display_name(self) -> str:
        return self.source_cfg.get("name") or self.source_id

    @property
    def enabled(self) -> bool:
        return bool(self.source_cfg.get("enabled", True)) and bool(self.source_cfg.get("base_url"))

    def _candidate_bases(self, override: str | None = None) -> list[str]:
        bases: list[str] = []
        if override:
            try:
                bases.append(normalize_url(override))
            except ValueError:
                pass
        scfg = self.source_cfg
        if scfg.get("base_url") and scfg["base_url"] not in bases:
            bases.append(scfg["base_url"])
        for fb in scfg.get("fallback_urls", []):
            if fb and fb not in bases:
                bases.append(fb)
        return bases

    async def _fetch_html(
        self,
        path_or_url: str,
        *,
        base_override: str | None = None,
    ) -> tuple[str, str]:
        last_err: Exception | None = None
        for base in self._candidate_bases(base_override):
            if path_or_url.startswith(("http://", "https://")):
                parsed = urlparse(path_or_url)
                url = f"{base}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
            else:
                url = f"{base}/{path_or_url.lstrip('/')}"
            try:
                resp = await self.client.get(
                    url,
                    headers={**DEFAULT_HEADERS, "Referer": base + "/"},
                    timeout=min(config_store.timeout, 15),
                )
                if resp.status_code == 200 and len(resp.text) > 1000:
                    return resp.text, base
                last_err = RuntimeError(f"HTTP {resp.status_code} từ {url}")
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"[{self.display_name}] Lỗi kết nối: {last_err}")

    async def check_connection(self, url: str) -> dict[str, Any]:
        try:
            clean = normalize_url(url)
        except ValueError as exc:
            return {"ok": False, "url": url, "error": str(exc)}
        try:
            resp = await self.client.get(clean, headers=DEFAULT_HEADERS, timeout=12)
            soup = BeautifulSoup(resp.text, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            cards = soup.select(".myui-vodlist__box a.myui-vodlist__thumb, li.item a[href*='/phim/']")
            phim_links = soup.select("a[href*='/phim/']")
            ok = resp.status_code == 200 and (len(cards) > 0 or len(phim_links) > 0)
            return {
                "ok": ok,
                "url": clean,
                "status_code": resp.status_code,
                "title": title,
                "movie_cards": len(cards) or len(phim_links),
            }
        except Exception as exc:
            return {"ok": False, "url": clean, "error": str(exc)}

    def _parse_cards(
        self,
        html: str,
        base_url: str,
        default_type: str,
        filter_type: str | None = None,
    ) -> list[CatalogItem]:
        soup = BeautifulSoup(html, "lxml")
        items: list[CatalogItem] = []
        seen_slugs: set[str] = set()

        # Layout 1: myui-vodlist__box (motchillv.online)
        for box in soup.select(".myui-vodlist__box"):
            thumb = box.select_one("a.myui-vodlist__thumb")
            if not thumb:
                continue
            slug = _extract_slug(thumb.get("href", ""))
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            title_el = box.select_one("h4.title")
            orig_el = box.select_one("p.text")
            badge_el = box.select_one(".pic-tag")

            title = (title_el.get_text(" ", strip=True) if title_el else thumb.get("title", "")).strip()
            orig = orig_el.get_text(" ", strip=True) if orig_el else ""
            badge = badge_el.get_text(" ", strip=True) if badge_el else ""

            item_type = filter_type or _infer_type_from_badge(badge, default_type)
            poster = _extract_bg_image(thumb.get("style", ""), base_url)
            if not poster:
                img = thumb.select_one("img")
                if img:
                    poster = _abs_url(img.get("data-original") or img.get("data-src") or img.get("src"), base_url)

            desc_parts = [p for p in [orig, badge] if p]
            items.append(
                CatalogItem(
                    id=f"vn:{self.source_id}:{slug}",
                    type=item_type,
                    name=title or slug,
                    slug=slug,
                    source_id=self.source_id,
                    origin_name=orig,
                    poster=poster,
                    description=" • ".join(desc_parts) if desc_parts else None,
                    release_info=badge or None,
                )
            )

        # Layout 2: WordPress li.item (motchillti.mx) — ưu tiên vùng danh sách chính để không lấy nhầm sidebar
        if not items:
            wp_list = soup.select(".list-films li.item, .film-list li.item, #content li.item, main li.item")
            if not wp_list:
                wp_list = soup.select("li.item")
            for li in wp_list:
                a = li.select_one("a[href*='/phim/']")
                if not a:
                    continue
                slug = _extract_slug(a.get("href", ""))
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                title_el = li.select_one("h3.name-title a, .name a")
                orig_el = li.select_one(".original-title-home")
                badge_el = li.select_one("span.label")
                img = li.select_one("img")

                title = (title_el.get_text(" ", strip=True) if title_el else a.get("title", "")).strip()
                orig = orig_el.get_text(" ", strip=True) if orig_el else ""
                badge = badge_el.get_text(" ", strip=True) if badge_el else ""

                # Tách năm từ title attribute nếu có (vd: "Casper 1997")
                year = ""
                if title_el and title_el.get("title"):
                    my = re.search(r"\b(19|20)\d{2}\b", title_el.get("title", ""))
                    if my:
                        year = my.group(0)

                item_type = filter_type or _infer_type_from_badge(badge, default_type)
                poster = (
                    _abs_url(img.get("data-original") or img.get("data-src") or img.get("src"), base_url)
                    if img
                    else None
                )
                desc_parts = [p for p in [orig, badge] if p]
                items.append(
                    CatalogItem(
                        id=f"vn:{self.source_id}:{slug}",
                        type=item_type,
                        name=title or slug,
                        slug=slug,
                        source_id=self.source_id,
                        origin_name=orig,
                        year=year,
                        poster=poster,
                        description=" • ".join(desc_parts) if desc_parts else None,
                        release_info=year or badge or None,
                    )
                )

        return items

    async def get_catalog_items(
        self,
        catalog_id: str,
        content_type: str,
        *,
        search: str | None = None,
        genre: str | None = None,
        skip: int = 0,
    ) -> list[CatalogItem]:
        if not self.enabled:
            return []
        page = (max(0, skip) // 20) + 1
        cache_key = f"{self.source_cfg.get('base_url')}:{catalog_id}:{content_type}:{search or ''}:{genre or ''}:{page}"
        if cache_key in self._catalog_cache:
            return self._catalog_cache[cache_key]

        items: list[CatalogItem] = []
        try:
            if search:
                q = quote_plus(search.strip())
                # Thử /?search= trước cho layout myui (motchillv); nếu trang là WordPress (không có myui-vodlist__box) thì dùng /?s= (motchillti)
                sp_myui = f"/?search={q}" + (f"&page={page}" if page > 1 else "")
                html, used_base = await self._fetch_html(sp_myui)
                if "myui-vodlist__box" in html:
                    items = self._parse_cards(html, used_base, default_type=content_type)
                else:
                    sp_wp = f"/?s={q}" + (f"&paged={page}" if page > 1 else "")
                    html_wp, used_base_wp = await self._fetch_html(sp_wp)
                    items = self._parse_cards(html_wp, used_base_wp, default_type=content_type)
            elif genre and genre in GENRE_MAP:
                path = f"/the-loai/{GENRE_MAP[genre]}" + (f"?page={page}" if page > 1 else "")
                html, used_base = await self._fetch_html(path)
                items = self._parse_cards(html, used_base, default_type=content_type, filter_type=content_type)
            else:
                base_path = CATALOG_PATH_MAP.get(catalog_id, "/danh-sach/phim-moi")
                path = f"{base_path}?page={page}" if page > 1 else base_path
                html, used_base = await self._fetch_html(path)
                ft = content_type if catalog_id in ("vn-le", "vn-bo") else None
                items = self._parse_cards(html, used_base, default_type=content_type, filter_type=ft)
        except Exception:
            return []

        self._catalog_cache[cache_key] = items
        return items

    async def get_meta(self, content_type: str, slug: str) -> dict[str, Any] | None:
        if not self.enabled or not slug:
            return None
        cache_key = f"{self.source_cfg.get('base_url')}:{content_type}:{slug}"
        if cache_key in self._meta_cache:
            return self._meta_cache[cache_key]

        try:
            html, used_base = await self._fetch_html(f"/phim/{slug}")
        except Exception:
            return None
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one(".myui-content__detail h1.title") or soup.select_one("h1")
        title = title_el.get_text(" ", strip=True) if title_el else slug

        orig_el = soup.select_one(".myui-content__detail h2.title2 span:first-child, .real-name, h2.title2")
        orig_title = orig_el.get_text(" ", strip=True) if orig_el else ""

        year_el = soup.select_one(".publish-year")
        year_text = year_el.get_text(" ", strip=True) if year_el else orig_title
        m_year = re.search(r"(19|20)\d{2}", year_text)
        year = m_year.group(0) if m_year else None

        poster_el = soup.select_one(".myui-content__thumb img, .poster img")
        poster = _abs_url(
            poster_el.get("data-original") or poster_el.get("data-src") or poster_el.get("src") if poster_el else None,
            used_base,
        )

        og_images = [
            _abs_url(m.get("content"), used_base)
            for m in soup.select('meta[property="og:image"]')
            if m.get("content")
        ]
        if not poster and og_images:
            poster = og_images[0]
        background = og_images[1] if len(og_images) > 1 else poster

        desc_el = soup.select_one('[itemprop="description"], .film-content, .tab-content')
        if desc_el:
            for h3 in desc_el.select("h3"):
                h3.decompose()
            description = desc_el.get_text(" ", strip=True)
        else:
            og_desc = soup.select_one('meta[property="og:description"]')
            description = og_desc.get("content", "").strip() if og_desc else ""
        if orig_title and orig_title.lower() not in title.lower():
            description = f"{orig_title}\n\n{description}" if description else orig_title

        genres = list(
            dict.fromkeys(
                a.get_text(" ", strip=True)
                for a in soup.select(".myui-content__detail a[href*='/the-loai/'], .myui-media-info a[href*='/the-loai/'], a[href*='/the-loai/']")[:10]
                if a.get_text(strip=True)
            )
        )

        watch_href = None
        for a in soup.select(f"a[href*='/phim/{slug}/']"):
            href = a.get("href", "")
            if "/rate" not in href:
                watch_href = href
                break

        episodes: list[tuple[str, str]] = []
        if watch_href:
            try:
                watch_html, _ = await self._fetch_html(watch_href, base_override=used_base)
                w_soup = BeautifulSoup(watch_html, "lxml")
                seen_ep: set[str] = set()
                for a in w_soup.select(f"a[href*='/phim/{slug}/']"):
                    href = a.get("href", "")
                    parts = urlparse(href).path.strip("/").split("/")
                    if len(parts) >= 3 and parts[0] == "phim" and parts[1] == slug:
                        ep_slug = parts[2]
                        if ep_slug == "rate" or ep_slug in seen_ep:
                            continue
                        seen_ep.add(ep_slug)
                        label = a.get_text(" ", strip=True) or ep_slug
                        episodes.append((ep_slug, label))
            except Exception:
                pass
            if not episodes:
                parts = urlparse(watch_href).path.strip("/").split("/")
                if len(parts) >= 3:
                    episodes.append((parts[2], "Full" if "full" in parts[2] else parts[2]))

        episodes.sort(key=lambda x: _episode_sort_key(x[0], x[1]))

        meta: dict[str, Any] = {
            "id": f"vn:{self.source_id}:{slug}",
            "type": content_type,
            "name": title,
            "poster": poster,
            "background": background,
            "description": description,
        }
        if year:
            meta["releaseInfo"] = year
        if genres:
            meta["genres"] = genres

        if content_type == "series" or len(episodes) > 1:
            videos = []
            seen_ep_nums: set[int] = set()
            for idx, (ep_slug, ep_label) in enumerate(episodes, start=1):
                m_num = re.search(r"(\d+)", ep_label or ep_slug)
                ep_num = int(m_num.group(1)) if m_num else idx
                # Loại trừ tập trùng lặp khi web tách nhiều server thành tap-1-sv-0, tap-1-sv-1...
                if m_num and ep_num in seen_ep_nums:
                    continue
                seen_ep_nums.add(ep_num)
                clean_title = (
                    f"Tập {ep_label}"
                    if ep_label.isdigit()
                    else (ep_label if ep_label.lower().startswith("tập") else f"Tập {ep_label}")
                )
                videos.append(
                    {
                        "id": f"vn:{self.source_id}:{slug}:{ep_slug}",
                        "title": clean_title,
                        "season": 1,
                        "episode": ep_num,
                    }
                )
            if videos:
                meta["videos"] = videos

        self._meta_cache[cache_key] = meta
        return meta

    async def get_streams_for_slug(
        self,
        slug: str,
        ep_slug: str | None = None,
        ep_num: int | None = None,
    ) -> list[StreamServer]:
        if not self.enabled or not slug:
            return []
        try:
            watch_path: str | None = None
            used_base: str | None = None

            if ep_slug:
                watch_path = f"/phim/{slug}/{ep_slug}"
            else:
                detail_html, used_base = await self._fetch_html(f"/phim/{slug}")
                d_soup = BeautifulSoup(detail_html, "lxml")
                ep_links: list[tuple[str, str]] = []
                for a in d_soup.select(f"a[href*='/phim/{slug}/']"):
                    href = a.get("href", "")
                    if "/rate" in href:
                        continue
                    ep_links.append((a.get_text(" ", strip=True), urlparse(href).path))
                if not ep_links:
                    return []
                if ep_num is not None:
                    for lbl, p in ep_links:
                        m = re.search(r"tap-(\d+)", p) or re.search(r"(\d+)", lbl)
                        if m and int(m.group(1)) == ep_num:
                            watch_path = p
                            break
                    if not watch_path:
                        # Thử vào tập đầu rồi tìm tập ep_num trong danh sách tập trên trang xem
                        first_html, used_base = await self._fetch_html(ep_links[0][1], base_override=used_base)
                        fs = BeautifulSoup(first_html, "lxml")
                        for a in fs.select(f"a[href*='/phim/{slug}/']"):
                            p = urlparse(a.get("href", "")).path
                            m = re.search(r"tap-(\d+)", p) or re.search(r"(\d+)", a.get_text(" ", strip=True))
                            if m and int(m.group(1)) == ep_num:
                                watch_path = p
                                break
                if not watch_path:
                    watch_path = ep_links[0][1]

            watch_html, used_base = await self._fetch_html(watch_path, base_override=used_base)
        except Exception:
            return []

        w_soup = BeautifulSoup(watch_html, "lxml")
        servers: list[StreamServer] = []
        seen_links: set[str] = set()
        referer = f"{used_base}/{watch_path.lstrip('/')}"

        for idx, btn in enumerate(w_soup.select(".streaming-server"), start=1):
            link = (btn.get("data-link") or "").strip()
            sv_type = (btn.get("data-type") or "embed").strip().lower()
            label = btn.get_text(" ", strip=True) or f"Server #{idx}"
            if not link:
                continue
            if link.startswith("http://"):
                link = "https://" + link[len("http://"):]
            if link in seen_links:
                continue
            seen_links.add(link)
            servers.append(
                StreamServer(
                    label=label,
                    server_type=sv_type,
                    link=link,
                    referer=referer,
                    source_name=self.display_name,
                )
            )
        return servers
