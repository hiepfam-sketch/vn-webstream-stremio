from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config_store import config_store, normalize_url
from providers.base import CatalogItem, StreamServer

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Thể loại trên f-moviess.org (chuẩn TMDB Genre ID)
FMOVIES_GENRE_MAP: dict[str, str] = {
    "Action": "28",
    "Adventure": "12",
    "Animation": "16",
    "Comedy": "35",
    "Crime": "80",
    "Documentary": "99",
    "Drama": "18",
    "Family": "10751",
    "Fantasy": "14",
    "History": "36",
    "Horror": "27",
    "Music": "10402",
    "Mystery": "9648",
    "Romance": "10749",
    "Science Fiction": "878",
    "Thriller": "53",
    "War": "10752",
}

# Backend của vidnest.fun (upstream player trong Server 1 emboser.com của f-moviess.org)
VIDNEST_API_BASE = "https://new.vidnest.fun"
VIDNEST_ORIGIN = "https://vidnest.fun"
VIDNEST_DEFAULT_SECRET = "K3p8vQ2mX9rL5wZ0fN7bG1sH4tJ6dC9_y8S3wM1nQ5pL2"

# Các sub-server ổn định nhất của vidnest (đã kiểm thử trả HLS/MKV trực tiếp)
VIDNEST_SUBSERVERS: list[tuple[str, str]] = [
    ("ophim", "Lamda 1080p"),
    ("flixhq", "Beta HD"),
    ("yflix", "Catflix HD"),
    ("vidzee", "Gama Direct"),
    ("rogflix", "Filxer HD"),
    ("allmovies", "Delta HD"),
]


def _extract_tmdb_id(slug_or_id: str) -> str:
    s = (slug_or_id or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"-(\d+)$", s)
    return m.group(1) if m else ""


def _parse_season_episode(ep_slug: str | None) -> tuple[int, int]:
    if not ep_slug:
        return 1, 1
    m = re.search(r"s(\d+)e(\d+)", ep_slug, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.search(r"(\d+)", ep_slug)
    return 1, (int(m2.group(1)) if m2 else 1)


def _b64u_decode(token: str) -> bytes:
    pad = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode((token + pad).encode("ascii"))


def _make_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def _decrypt_vidnest_payload(data: Any, secret: str) -> Any:
    if isinstance(data, dict) and data.get("v") == 2 and data.get("d") and data.get("n"):
        key = hashlib.sha256(f"{data['n']}|{secret}".encode("utf-8")).digest()
        raw = _b64u_decode(str(data["d"]))
        iv, ct = raw[:12], raw[12:]
        plain = AESGCM(key).decrypt(iv, ct, None).decode("utf-8")
        return json.loads(plain)
    return data


def _abs_url(url: str | None, base_url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base_url + "/", url.lstrip("/"))


class FMoviesProvider:
    """Provider cho f-moviess.org (TMDB-indexed) & giải mã stream HLS trực tiếp từ upstream vidnest."""

    def __init__(self, source_id: str, client: httpx.AsyncClient) -> None:
        self.source_id = source_id
        self.client = client
        self._catalog_cache: TTLCache[str, list[CatalogItem]] = TTLCache(maxsize=256, ttl=300)
        self._meta_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=512, ttl=600)
        self._vidnest_secret: str = VIDNEST_DEFAULT_SECRET
        self._secret_refreshed: bool = False

    def clear_cache(self) -> None:
        self._catalog_cache.clear()
        self._meta_cache.clear()
        self._secret_refreshed = False

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

    async def _fetch_text(
        self,
        path: str,
        *,
        base_override: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        last_err: Exception | None = None
        for base in self._candidate_bases(base_override):
            url = f"{base}/{path.lstrip('/')}"
            hdrs = {**DEFAULT_HEADERS, "Referer": base + "/"}
            if extra_headers:
                hdrs.update(extra_headers)
            try:
                resp = await self.client.get(
                    url,
                    headers=hdrs,
                    timeout=min(config_store.timeout, 15),
                )
                if resp.status_code == 200 and resp.text:
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
            resp = await self.client.get(f"{clean}/movies", headers=DEFAULT_HEADERS, timeout=12)
            soup = BeautifulSoup(resp.text, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            cards = soup.select("a[href^='/movie/'], a[href^='/tv/']")
            ok = resp.status_code == 200 and len(cards) > 0
            return {
                "ok": ok,
                "url": clean,
                "status_code": resp.status_code,
                "title": title,
                "movie_cards": len(cards),
            }
        except Exception as exc:
            return {"ok": False, "url": clean, "error": str(exc)}

    def _parse_cards(self, html: str, base_url: str, content_type: str) -> list[CatalogItem]:
        soup = BeautifulSoup(html, "lxml")
        items: list[CatalogItem] = []
        seen_slugs: set[str] = set()

        prefix = "/tv/" if content_type == "series" else "/movie/"
        for a in soup.select(f"a[href^='{prefix}']"):
            href = (a.get("href") or "").strip()
            parts = [p for p in urlparse(href).path.strip("/").split("/") if p]
            if len(parts) < 2:
                continue
            slug = parts[1]
            if not slug or slug in seen_slugs or not _extract_tmdb_id(slug):
                continue
            seen_slugs.add(slug)

            img = a.select_one("img")
            poster = _abs_url(img.get("src") if img else None, base_url)
            title = (img.get("alt") if img else "") or ""
            if not title:
                h_el = a.select_one("h2, h3, h4, p")
                title = h_el.get_text(" ", strip=True) if h_el else slug

            card_text = a.get_text(" ", strip=True)
            m_year = re.search(r"\b(19|20)\d{2}\b", card_text)
            year = m_year.group(0) if m_year else ""

            badge_el = a.select_one("span")
            badge = badge_el.get_text(" ", strip=True) if badge_el else ""
            desc_parts = [p for p in [year, badge] if p and p != title]

            items.append(
                CatalogItem(
                    id=f"vn:{self.source_id}:{slug}",
                    type=content_type,
                    name=title.strip() or slug,
                    slug=slug,
                    source_id=self.source_id,
                    origin_name=title.strip(),
                    year=year,
                    poster=poster,
                    description=" • ".join(desc_parts) if desc_parts else None,
                    release_info=year or None,
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
        # Chỉ phục vụ các catalog riêng của FMovies
        if not catalog_id.startswith("fmovies-"):
            return []

        page = (max(0, skip) // 20) + 1
        cache_key = f"{self.source_cfg.get('base_url')}:{catalog_id}:{content_type}:{search or ''}:{genre or ''}:{page}"
        if cache_key in self._catalog_cache:
            return self._catalog_cache[cache_key]

        items: list[CatalogItem] = []
        try:
            if search:
                if page > 1:
                    return []
                q = quote_plus(search.strip())
                raw_json, _ = await self._fetch_text(
                    f"/api/search?q={q}",
                    extra_headers={"Accept": "application/json"},
                )
                data = json.loads(raw_json)
                target_media = "tv" if content_type == "series" else "movie"
                for it in data.get("results") or []:
                    if not isinstance(it, dict):
                        continue
                    mtype = (it.get("media_type") or "").lower()
                    if mtype and mtype != target_media:
                        continue
                    href = str(it.get("href") or "").strip()
                    parts = [p for p in urlparse(href).path.strip("/").split("/") if p]
                    slug = parts[1] if len(parts) >= 2 else str(it.get("id") or "")
                    if not slug:
                        continue
                    title = str(it.get("title") or it.get("name") or slug).strip()
                    poster_path = it.get("poster_path")
                    poster = (
                        f"https://image.tmdb.org/t/p/w500{poster_path}"
                        if poster_path
                        else it.get("poster_url")
                    )
                    rel = str(it.get("release_date") or it.get("first_air_date") or "")
                    year = rel[:4] if len(rel) >= 4 and rel[:4].isdigit() else ""
                    overview = str(it.get("overview") or "").strip()
                    items.append(
                        CatalogItem(
                            id=f"vn:{self.source_id}:{slug}",
                            type=content_type,
                            name=title,
                            slug=slug,
                            source_id=self.source_id,
                            origin_name=str(it.get("original_title") or it.get("original_name") or title),
                            year=year,
                            poster=poster,
                            description=overview[:240] if overview else None,
                            release_info=year or None,
                        )
                    )
            else:
                base_path = "/tv-series" if content_type == "series" else "/movies"
                params: list[str] = []
                if genre and genre in FMOVIES_GENRE_MAP:
                    params.append(f"genre={FMOVIES_GENRE_MAP[genre]}")
                if page > 1:
                    params.append(f"page={page}")
                path = base_path + ("?" + "&".join(params) if params else "")
                html, used_base = await self._fetch_text(path)
                items = self._parse_cards(html, used_base, content_type)
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

        prefix = "tv" if content_type == "series" else "movie"
        try:
            html, used_base = await self._fetch_text(f"/{prefix}/{slug}")
        except Exception:
            return None

        soup = BeautifulSoup(html, "lxml")

        # Ưu tiên JSON-LD (Schema.org Movie/TVSeries) vì rất chuẩn và đầy đủ
        title = ""
        description = ""
        poster = None
        year = None
        genres: list[str] = []

        for sc in soup.select('script[type="application/ld+json"]'):
            try:
                ld = json.loads(sc.string or "{}")
                nodes = ld.get("@graph") if isinstance(ld, dict) and isinstance(ld.get("@graph"), list) else [ld]
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    if node.get("@type") in ("Movie", "TVSeries", "TVShow"):
                        title = str(node.get("name") or title).strip()
                        description = str(node.get("description") or description).strip()
                        img = node.get("image")
                        if isinstance(img, str):
                            poster = img
                        elif isinstance(img, list) and img:
                            poster = str(img[0])
                        dp = str(node.get("datePublished") or "")
                        if len(dp) >= 4 and dp[:4].isdigit():
                            year = dp[:4]
                        g = node.get("genre")
                        if isinstance(g, list):
                            genres = [str(x) for x in g if x]
                        elif isinstance(g, str) and g:
                            genres = [g]
            except Exception:
                pass

        if not title:
            h1 = soup.select_one("h1")
            title = h1.get_text(" ", strip=True) if h1 else slug
        if not description:
            og_desc = soup.select_one('meta[property="og:description"], meta[name="description"]')
            description = og_desc.get("content", "").strip() if og_desc else ""
        if not poster:
            og_img = soup.select_one('meta[property="og:image"]')
            poster = _abs_url(og_img.get("content") if og_img else None, used_base)

        background = poster
        preload_bg = soup.select_one('link[rel="preload"][as="image"][href*="image.tmdb.org/t/p/original"]')
        if preload_bg and preload_bg.get("href"):
            background = preload_bg["href"]

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

        # Nếu là phim bộ -> thu thập tất cả Season & Episode
        if content_type == "series":
            season_nums = sorted(
                {int(s) for s in re.findall(r"\?season=(\d+)(?!&)", html)}
            )
            if not season_nums:
                season_nums = [1]

            def _extract_eps_from_html(page_html: str) -> list[tuple[int, int, str]]:
                p_soup = BeautifulSoup(page_html, "lxml")
                found: list[tuple[int, int, str]] = []
                seen: set[tuple[int, int]] = set()
                for a in p_soup.select("a[href*='episode=']"):
                    href = a.get("href") or ""
                    m = re.search(r"season=(\d+)&(?:amp;)?episode=(\d+)", href)
                    if not m:
                        continue
                    sn, en = int(m.group(1)), int(m.group(2))
                    if (sn, en) in seen:
                        continue
                    seen.add((sn, en))
                    lbl = a.get_text(" ", strip=True) or f"Episode {en}"
                    found.append((sn, en, lbl))
                return found

            all_eps = _extract_eps_from_html(html)
            # Lấy thêm các season còn lại song song (tối đa 12 season để tránh quá tải)
            extra_seasons = [s for s in season_nums if s != (all_eps[0][0] if all_eps else 1)][:12]
            if extra_seasons:
                async def _fetch_season(sn: int) -> list[tuple[int, int, str]]:
                    try:
                        sh, _ = await self._fetch_text(f"/tv/{slug}?season={sn}", base_override=used_base)
                        return _extract_eps_from_html(sh)
                    except Exception:
                        return []

                extra_results = await asyncio.gather(*(_fetch_season(sn) for sn in extra_seasons))
                for chunk in extra_results:
                    all_eps.extend(chunk)

            # Lọc trùng & sắp xếp theo (season, episode)
            dedup_eps: dict[tuple[int, int], str] = {}
            for sn, en, lbl in all_eps:
                if (sn, en) not in dedup_eps:
                    dedup_eps[(sn, en)] = lbl

            videos = []
            for (sn, en) in sorted(dedup_eps.keys()):
                videos.append(
                    {
                        "id": f"vn:{self.source_id}:{slug}:s{sn}e{en}",
                        "title": dedup_eps[(sn, en)],
                        "season": sn,
                        "episode": en,
                    }
                )
            if videos:
                meta["videos"] = videos

        self._meta_cache[cache_key] = meta
        return meta

    async def _refresh_vidnest_secret(self) -> bool:
        """Tự động quét lại AES secret từ JS chunk của vidnest.fun nếu server xoay key."""
        if self._secret_refreshed:
            return False
        self._secret_refreshed = True
        try:
            r = await self.client.get(
                f"{VIDNEST_ORIGIN}/movie/533535",
                headers=DEFAULT_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                return False
            chunks = list(dict.fromkeys(re.findall(r"/_next/static/chunks/[a-zA-Z0-9_.-]+\.js", r.text)))
            for ch in chunks[:25]:
                rch = await self.client.get(f"{VIDNEST_ORIGIN}{ch}", headers=DEFAULT_HEADERS, timeout=8)
                if "https://new.vidnest.fun" in rch.text:
                    m = re.search(
                        r'["\']([A-Za-z0-9_-]{32,64})["\']\s*,\s*[a-zA-Z_$][\w$]*\s*=\s*["\']https://new\.vidnest\.fun["\']',
                        rch.text,
                    )
                    if m and m.group(1) != self._vidnest_secret:
                        self._vidnest_secret = m.group(1)
                        return True
        except Exception:
            pass
        return False

    async def _fetch_vidnest_subserver(
        self,
        sub_id: str,
        sub_label: str,
        content_type: str,
        tmdb_id: str,
        season: int | None,
        episode: int | None,
    ) -> list[StreamServer]:
        if content_type == "series":
            s = season or 1
            e = episode or 1
            path = f"/{sub_id}/tv/{tmdb_id}/{s}/{e}"
        else:
            path = f"/{sub_id}/movie/{tmdb_id}"

        url = f"{VIDNEST_API_BASE}{path}?_n={_make_nonce()}"
        headers = {
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Origin": VIDNEST_ORIGIN,
            "Referer": VIDNEST_ORIGIN + "/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        try:
            resp = await self.client.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                return []
            raw_json = resp.json()
            try:
                dec = _decrypt_vidnest_payload(raw_json, self._vidnest_secret)
            except Exception:
                if await self._refresh_vidnest_secret():
                    dec = _decrypt_vidnest_payload(raw_json, self._vidnest_secret)
                else:
                    return []
        except Exception:
            return []

        if not isinstance(dec, dict):
            return []

        servers: list[StreamServer] = []
        raw_streams: list[dict[str, Any]] = []
        if isinstance(dec.get("streams"), list):
            raw_streams.extend(s for s in dec["streams"] if isinstance(s, dict))
        if isinstance(dec.get("sources"), list):
            raw_streams.extend(s for s in dec["sources"] if isinstance(s, dict))
        if dec.get("url"):
            raw_streams.append(dec)

        for idx, st in enumerate(raw_streams, start=1):
            link = str(st.get("url") or "").strip()
            if not link or not link.startswith(("http://", "https://")):
                continue
            st_type = str(st.get("type") or "").lower()
            quality = str(st.get("quality") or "").strip()
            lang = str(st.get("language") or "").strip()
            st_headers = st.get("headers") if isinstance(st.get("headers"), dict) else {}
            clean_headers = {str(k).strip(): str(v).strip() for k, v in st_headers.items() if k and v}
            referer = clean_headers.get("Referer") or (VIDNEST_ORIGIN + "/")

            is_direct_file = st_type in ("mp4", "mkv", "video/mp4") or link.lower().endswith((".mp4", ".mkv"))
            server_type = "direct_file" if is_direct_file else "m3u8"

            extra_tag = " • ".join(p for p in [quality if quality.lower() != "auto" else "", lang] if p)
            label = f"{sub_label} ({extra_tag})" if extra_tag else sub_label
            if len(raw_streams) > 1:
                label = f"{label} #{idx}"

            servers.append(
                StreamServer(
                    label=label,
                    server_type=server_type,
                    link=link,
                    referer=referer,
                    source_name=self.display_name,
                    headers=clean_headers,
                    skip_verify=True,
                )
            )
        return servers

    async def get_streams_for_tmdb(
        self,
        content_type: str,
        tmdb_id: str,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[StreamServer]:
        if not self.enabled or not tmdb_id or not str(tmdb_id).isdigit():
            return []

        tasks = [
            self._fetch_vidnest_subserver(sub_id, sub_label, content_type, str(tmdb_id), season, episode)
            for sub_id, sub_label in VIDNEST_SUBSERVERS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[StreamServer] = []
        seen_links: set[str] = set()
        for res in results:
            if not isinstance(res, list):
                continue
            for sv in res:
                if sv.link not in seen_links:
                    seen_links.add(sv.link)
                    merged.append(sv)
        return merged

    async def get_streams_for_slug(
        self,
        slug: str,
        ep_slug: str | None = None,
        ep_num: int | None = None,
        *,
        content_type: str = "movie",
        season: int | None = None,
    ) -> list[StreamServer]:
        tmdb_id = _extract_tmdb_id(slug)
        if not tmdb_id:
            return []
        if content_type == "series":
            if ep_slug:
                sn, en = _parse_season_episode(ep_slug)
            else:
                sn = season or 1
                en = ep_num or 1
            return await self.get_streams_for_tmdb("series", tmdb_id, season=sn, episode=en)
        return await self.get_streams_for_tmdb("movie", tmdb_id)
