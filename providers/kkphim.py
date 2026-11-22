from __future__ import annotations

import re
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

KK_CATALOG_MAP: dict[str, str] = {
    "vn-trung-quoc": "/v1/api/quoc-gia/trung-quoc",
    "vn-han-quoc": "/v1/api/quoc-gia/han-quoc",
    "vn-le": "/v1/api/danh-sach/phim-le",
    "vn-bo": "/v1/api/danh-sach/phim-bo",
}

KK_COUNTRY_FILTER: dict[str, str] = {
    "vn-trung-quoc": "trung-quoc",
    "vn-han-quoc": "han-quoc",
}

KK_GENRE_SLUG: dict[str, str] = {
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


def _abs_img(url: str | None, img_cdn: str) -> str | None:
    if not url:
        return None
    u = url.strip()
    if u.startswith(("http://", "https://")):
        return u
    if img_cdn:
        return f"{img_cdn.rstrip('/')}/{u.lstrip('/')}"
    return None


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


class KKPhimApiProvider:
    """Provider dùng chuẩn JSON API của KKPhim (phimapi.com) và OPhim (ophim1.com)."""

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
                f"{clean}/danh-sach/phim-moi-cap-nhat?page=1",
                headers=DEFAULT_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                return {"ok": False, "url": clean, "status_code": r.status_code, "error": f"HTTP {r.status_code}"}
            data = r.json()
            items = data.get("items") or (data.get("data") or {}).get("items") or []
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
        page = (max(0, skip) // 20) + 1
        cache_key = f"{self.source_cfg.get('base_url')}:{catalog_id}:{content_type}:{search or ''}:{genre or ''}:{page}"
        if cache_key in self._catalog_cache:
            return self._catalog_cache[cache_key]

        country_slug = KK_COUNTRY_FILTER.get(catalog_id)
        if search:
            path = f"/v1/api/tim-kiem?keyword={quote_plus(search.strip())}&limit=24&sort_field=_id&sort_type=desc"
            if country_slug:
                path += f"&country={country_slug}"
            if page > 1:
                path += f"&page={page}"
        elif genre and genre in KK_GENRE_SLUG:
            path = f"/v1/api/the-loai/{KK_GENRE_SLUG[genre]}?page={page}&limit=24&sort_field=_id&sort_type=desc"
            if country_slug:
                path += f"&country={country_slug}"
        else:
            ep = KK_CATALOG_MAP.get(catalog_id, "/v1/api/quoc-gia/trung-quoc")
            path = f"{ep}?page={page}&limit=24&sort_field=_id&sort_type=desc"

        try:
            data, _ = await self._fetch_json(path)
        except Exception:
            return []

        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        img_cdn = (
            payload.get("APP_DOMAIN_CDN_IMAGE")
            or data.get("pathImage")
            or "https://phimimg.com"
        )
        raw_items = payload.get("items") or []

        results: list[CatalogItem] = []
        for it in raw_items:
            slug = (it.get("slug") or "").strip()
            name = (it.get("name") or "").strip()
            if not slug or not name:
                continue
            raw_type = (it.get("type") or "").lower()
            cats = [
                (c.get("slug") or "").lower()
                for c in (it.get("category") or [])
                if isinstance(c, dict)
            ]
            # Bỏ hoàn toàn phim hoạt hình
            if raw_type == "hoathinh" or "hoat-hinh" in cats:
                continue
            # Lọc đúng quốc gia Trung Quốc / Hàn Quốc nếu có metadata country
            if country_slug and isinstance(it.get("country"), list) and it["country"]:
                c_slugs = [(c.get("slug") or "").lower() for c in it["country"] if isinstance(c, dict)]
                if c_slugs and country_slug not in c_slugs:
                    continue

            origin = (it.get("origin_name") or "").strip()
            year = str(it.get("year") or "").strip()
            ep_cur = (it.get("episode_current") or "").strip()
            lang = (it.get("lang") or "").strip()
            poster = _abs_img(it.get("poster_url") or it.get("thumb_url"), img_cdn)

            # Lấy timestamp đăng phim từ 8 ký tự hex đầu của MongoDB _id (thời điểm tạo bản ghi)
            publish_ts = 0
            raw_oid = str(it.get("_id") or "").strip()
            if len(raw_oid) >= 8:
                try:
                    publish_ts = int(raw_oid[:8], 16)
                except ValueError:
                    publish_ts = 0

            item_type = content_type
            desc_parts = [p for p in [origin, year, ep_cur, lang] if p]
            results.append(
                CatalogItem(
                    id=f"vn:{self.source_id}:{slug}",
                    type=item_type,
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
            data, _ = await self._fetch_json(f"/phim/{slug}")
            if data.get("status") is False or not data.get("movie"):
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
        episodes = data.get("episodes") or []

        title = (movie.get("name") or slug).strip()
        origin = (movie.get("origin_name") or "").strip()
        year = str(movie.get("year") or "").strip()
        poster = movie.get("poster_url") or movie.get("thumb_url")
        background = movie.get("thumb_url") or poster
        desc = _strip_html(movie.get("content"))
        if origin and origin.lower() not in title.lower():
            desc = f"{origin}\n\n{desc}" if desc else origin
        genres = [c.get("name") for c in (movie.get("category") or []) if isinstance(c, dict) and c.get("name")]

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

        # Xây dựng danh sách tập từ server có nhiều tập nhất
        best_eps: list[dict[str, Any]] = []
        for sv in episodes:
            sd = sv.get("server_data") or []
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
        episodes = data.get("episodes") or []
        servers: list[StreamServer] = []
        base = self.source_cfg.get("base_url") or ""

        for sv in episodes:
            sv_name = (sv.get("server_name") or self.display_name).strip()
            sd = sv.get("server_data") or []
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
                m3u8 = (matched.get("link_m3u8") or "").strip()
                embed = (matched.get("link_embed") or "").strip()
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
