from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import httpx
from cachetools import TTLCache

from config_store import config_store, normalize_url
from providers.base import CatalogItem, StreamServer

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

NGUONC_CATALOG_MAP: dict[str, str] = {
    "vn-trung-quoc": "/api/films/quoc-gia/trung-quoc",
    "vn-han-quoc": "/api/films/quoc-gia/han-quoc",
    "vn-le": "/api/films/danh-sach/phim-le",
    "vn-bo": "/api/films/danh-sach/phim-bo",
}

NGUONC_GENRE_SLUG: dict[str, str] = {
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


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_iso_ts(val: str | None) -> int:
    if not val:
        return 0
    try:
        s = str(val).strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


class NguonCProvider:
    """Provider dùng JSON API mở của NguonC (phim.nguonc.com), ổn định 100% trên Render/Cloud."""

    def __init__(self, source_id: str, client: httpx.AsyncClient) -> None:
        self.source_id = source_id
        self.client = client
        self._catalog_cache: TTLCache[str, list[CatalogItem]] = TTLCache(maxsize=256, ttl=300)
        self._detail_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=512, ttl=600)

    def clear_cache(self) -> None:
        self._catalog_cache.clear()
        self._detail_cache.clear()

    @property
    def source_cfg(self) -> dict[str, Any]:
        return config_store.get_source(self.source_id)

    @property
    def display_name(self) -> str:
        return self.source_cfg.get("name") or self.source_id.upper()

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

    async def _fetch_json(self, path: str, *, base_override: str | None = None) -> tuple[dict[str, Any], str]:
        last_err: Exception | None = None
        for base in self._candidate_bases(base_override):
            url = f"{base}/{path.lstrip('/')}"
            try:
                r = await self.client.get(url, headers=DEFAULT_HEADERS, timeout=min(config_store.timeout, 12))
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        return data, base
                last_err = RuntimeError(f"HTTP {r.status_code} từ {url}")
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"[{self.display_name}] Lỗi kết nối: {last_err}")

    async def check_connection(self, url: str) -> dict[str, Any]:
        try:
            clean = normalize_url(url)
        except ValueError as exc:
            return {"ok": False, "url": url, "error": str(exc)}
        try:
            r = await self.client.get(
                f"{clean}/api/films/phim-moi-cap-nhat?page=1",
                headers=DEFAULT_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                return {"ok": False, "url": clean, "status_code": r.status_code, "error": f"HTTP {r.status_code}"}
            data = r.json()
            items = data.get("items") or []
            return {
                "ok": len(items) > 0,
                "url": clean,
                "status_code": r.status_code,
                "movie_cards": len(items),
                "title": f"{self.display_name} API",
            }
        except Exception as exc:
            return {"ok": False, "url": clean, "error": str(exc)}

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
        # NguonC trả 10 phim/trang
        page = (max(0, skip) // 10) + 1
        cache_key = f"{self.source_cfg.get('base_url')}:{catalog_id}:{content_type}:{search or ''}:{genre or ''}:{page}"
        if cache_key in self._catalog_cache:
            return self._catalog_cache[cache_key]

        if search:
            path = f"/api/films/search?keyword={quote_plus(search.strip())}&page={page}"
        elif genre and genre in NGUONC_GENRE_SLUG:
            path = f"/api/films/the-loai/{NGUONC_GENRE_SLUG[genre]}?page={page}"
        else:
            ep = NGUONC_CATALOG_MAP.get(catalog_id, "/api/films/quoc-gia/trung-quoc")
            path = f"{ep}?page={page}"

        try:
            data, _ = await self._fetch_json(path)
        except Exception:
            return []

        raw_items = data.get("items") or []
        results: list[CatalogItem] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            slug = (it.get("slug") or "").strip()
            name = (it.get("name") or "").strip()
            if not slug or not name:
                continue

            origin = (it.get("original_name") or "").strip()
            year = str(it.get("year") or "").strip()
            ep_cur = (it.get("current_episode") or "").strip()
            lang = (it.get("language") or "").strip()
            poster = (it.get("thumb_url") or it.get("poster_url") or "").strip() or None
            publish_ts = _parse_iso_ts(it.get("modified") or it.get("created"))

            desc_parts = [p for p in [origin, year, ep_cur, lang] if p]
            results.append(
                CatalogItem(
                    id=f"vn:{self.source_id}:{slug}",
                    type=content_type,
                    name=name,
                    slug=slug,
                    source_id=self.source_id,
                    origin_name=origin,
                    year=year,
                    publish_ts=publish_ts,
                    poster=poster,
                    description=" • ".join(desc_parts) if desc_parts else None,
                    release_info=year or ep_cur or None,
                )
            )

        self._catalog_cache[cache_key] = results
        return results

    async def get_detail_raw(self, slug: str) -> dict[str, Any] | None:
        if not self.enabled or not slug:
            return None
        cache_key = f"{self.source_cfg.get('base_url')}:{slug}"
        if cache_key in self._detail_cache:
            return self._detail_cache[cache_key]
        try:
            data, _ = await self._fetch_json(f"/api/film/{slug}")
            if data.get("status") != "success" or not isinstance(data.get("movie"), dict):
                return None
            self._detail_cache[cache_key] = data
            return data
        except Exception:
            return None

    async def get_meta(self, content_type: str, slug: str) -> dict[str, Any] | None:
        data = await self.get_detail_raw(slug)
        if not data:
            return None
        movie = data.get("movie") or {}
        episodes = movie.get("episodes") or []

        title = (movie.get("name") or slug).strip()
        origin = (movie.get("original_name") or "").strip()
        year = str(movie.get("year") or "").strip()
        poster = (movie.get("thumb_url") or movie.get("poster_url") or "").strip() or None
        background = (movie.get("poster_url") or poster or "").strip() or None
        desc = _strip_html(movie.get("description"))
        if origin and origin.lower() not in title.lower():
            desc = f"{origin}\n\n{desc}" if desc else origin

        genres: list[str] = []
        cat_obj = movie.get("category")
        if isinstance(cat_obj, dict):
            for grp in cat_obj.values():
                if not isinstance(grp, dict):
                    continue
                g_name = ((grp.get("group") or {}).get("name") or "").lower()
                if "thể loại" in g_name or "the loai" in g_name:
                    for item in grp.get("list") or []:
                        if isinstance(item, dict) and item.get("name"):
                            genres.append(str(item["name"]).strip())

        meta: dict[str, Any] = {
            "id": f"vn:{self.source_id}:{slug}",
            "type": content_type,
            "name": title,
            "poster": poster,
            "background": background,
            "description": desc,
        }
        if year:
            meta["releaseInfo"] = year
        if genres:
            meta["genres"] = genres

        best_eps: list[dict[str, Any]] = []
        for sv in episodes:
            sd = sv.get("items") or []
            if len(sd) > len(best_eps):
                best_eps = sd

        if content_type == "series" or len(best_eps) > 1:
            videos = []
            for idx, ep in enumerate(best_eps, start=1):
                ep_slug = (ep.get("slug") or f"tap-{idx}").strip()
                ep_name = (ep.get("name") or str(idx)).strip()
                m_num = re.search(r"(\d+)", ep_name or ep_slug)
                ep_num = int(m_num.group(1)) if m_num else idx
                clean_title = ep_name if ep_name.lower().startswith("tập") else f"Tập {ep_name}"
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

        return meta

    async def get_streams_for_slug(
        self,
        slug: str,
        ep_slug: str | None = None,
        ep_num: int | None = None,
    ) -> list[StreamServer]:
        data = await self.get_detail_raw(slug)
        if not data:
            return []
        movie = data.get("movie") or {}
        episodes = movie.get("episodes") or []
        servers: list[StreamServer] = []
        base = self.source_cfg.get("base_url") or "https://phim.nguonc.com"

        for sv in episodes:
            sv_name = (sv.get("server_name") or self.display_name).strip()
            sd = sv.get("items") or []
            if not sd:
                continue

            matched = None
            if ep_slug:
                for ep in sd:
                    if (ep.get("slug") or "").strip().lower() == ep_slug.lower():
                        matched = ep
                        break
            if not matched and ep_num is not None:
                for ep in sd:
                    ep_label = f"{ep.get('name', '')} {ep.get('slug', '')}"
                    m = re.search(r"(\d+)", ep_label)
                    if m and int(m.group(1)) == ep_num:
                        matched = ep
                        break
            if not matched and not ep_slug and ep_num is None:
                matched = sd[0]

            if matched:
                m3u8 = (matched.get("m3u8") or "").strip()
                embed = (matched.get("embed") or "").strip()
                if m3u8:
                    servers.append(
                        StreamServer(
                            label=sv_name,
                            server_type="m3u8",
                            link=m3u8,
                            referer=base + "/",
                            source_name=self.display_name,
                        )
                    )
                elif embed:
                    servers.append(
                        StreamServer(
                            label=sv_name,
                            server_type="embed",
                            link=embed,
                            referer=base + "/",
                            source_name=self.display_name,
                        )
                    )
        return servers
