from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import httpx
from cachetools import TTLCache

from config_store import config_store
from providers.aggregator import normalize_title

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}

_cinemeta_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=512, ttl=3600)
_resolve_cache: TTLCache[str, tuple[str, int | None]] = TTLCache(maxsize=512, ttl=1800)


def parse_external_id(item_id: str) -> tuple[str, str, int | None, int | None]:
    """Phân tích tt1234567[:season:episode] hoặc tmdb:12345[:season:episode]."""
    if item_id.startswith("tt"):
        parts = item_id.split(":")
        imdb_id = parts[0]
        season = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        episode = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
        return "imdb", imdb_id, season, episode
    if item_id.startswith("tmdb:"):
        parts = item_id.split(":")
        tmdb_id = parts[1] if len(parts) >= 2 else ""
        season = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
        episode = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else None
        return "tmdb", tmdb_id, season, episode
    return "", "", None, None


def _extract_year(val: Any) -> int | None:
    if not val:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", str(val))
    return int(m.group(0)) if m else None


def _extract_season_from_text(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(?:phần|phan|season|mùa|mua|s)\s*0*(\d+)", text, re.I)
    return int(m.group(1)) if m else None


async def _fetch_cinemeta(
    client: httpx.AsyncClient,
    content_type: str,
    imdb_id: str,
) -> dict[str, Any] | None:
    key = f"{content_type}:{imdb_id}"
    if key in _cinemeta_cache:
        return _cinemeta_cache[key]
    url = f"https://v3-cinemeta.strem.io/meta/{content_type}/{imdb_id}.json"
    try:
        r = await client.get(url, headers=UA, timeout=8)
        if r.status_code == 200:
            meta = r.json().get("meta")
            if isinstance(meta, dict) and meta.get("name"):
                _cinemeta_cache[key] = meta
                return meta
    except Exception:
        pass
    return None


async def _kkphim_tmdb_lookup(
    client: httpx.AsyncClient,
    kk_base: str,
    content_type: str,
    tmdb_id: str,
) -> dict[str, Any] | None:
    tmdb_type = "tv" if content_type == "series" else "movie"
    try:
        r = await client.get(f"{kk_base}/tmdb/{tmdb_type}/{tmdb_id}", headers=UA, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("movie"):
                return data["movie"]
    except Exception:
        pass
    return None


async def _search_kkphim_candidates(
    client: httpx.AsyncClient,
    kk_base: str,
    keyword: str,
) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            f"{kk_base}/v1/api/tim-kiem?keyword={quote_plus(keyword)}&limit=15",
            headers=UA,
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return (data.get("data") or {}).get("items") or []
    except Exception:
        pass
    return []


def _score_candidate(
    it: dict[str, Any],
    *,
    target_title_norm: str,
    target_year: int | None,
    target_imdb: str | None,
    target_tmdb: str | None,
    content_type: str,
    season: int | None,
) -> int:
    score = 0
    it_imdb = str((it.get("imdb") or {}).get("id") or "").strip()
    it_tmdb_obj = it.get("tmdb") or {}
    it_tmdb = str(it_tmdb_obj.get("id") or "").strip()
    it_tmdb_season = it_tmdb_obj.get("season")

    if target_imdb and it_imdb and it_imdb == target_imdb:
        score += 100
    if target_tmdb and it_tmdb and it_tmdb == target_tmdb:
        score += 100

    name_norm = normalize_title(it.get("name") or "")
    orig_norm = normalize_title(it.get("origin_name") or "")

    if target_title_norm and (target_title_norm == orig_norm or target_title_norm == name_norm):
        score += 60
    elif target_title_norm and (target_title_norm in orig_norm or target_title_norm in name_norm):
        score += 30

    it_year = _extract_year(it.get("year"))
    if target_year and it_year:
        diff = abs(it_year - target_year)
        if diff == 0:
            score += 20
        elif diff == 1:
            score += 10
        elif diff >= 3 and (not season or season == 1):
            score -= 25

    raw_type = (it.get("type") or "").lower()
    if content_type == "movie" and raw_type == "single":
        score += 15
    elif content_type == "series" and raw_type in ("series", "tvshows", "hoathinh"):
        score += 15

    if content_type == "series" and season is not None:
        cand_season = None
        if isinstance(it_tmdb_season, int):
            cand_season = it_tmdb_season
        else:
            cand_season = _extract_season_from_text(f"{it.get('name', '')} {it.get('origin_name', '')}")
        if cand_season == season:
            score += 40
        elif season == 1 and cand_season is None:
            score += 25
        elif cand_season is not None and cand_season != season:
            score -= 50

    return score


async def resolve_external_to_slug(
    client: httpx.AsyncClient,
    content_type: str,
    item_id: str,
) -> tuple[str, int | None]:
    """Chuyển đổi ID từ Cinemeta/IMDb (tt...) hoặc TMDB (tmdb:...) sang slug tiếng Việt + số tập."""
    kind, ext_id, season, episode = parse_external_id(item_id)
    if not kind or not ext_id:
        return "", None

    cache_key = f"{content_type}:{kind}:{ext_id}:{season or 0}"
    if cache_key in _resolve_cache:
        slug, _ = _resolve_cache[cache_key]
        return slug, episode

    kk_cfg = config_store.get_source("kkphim")
    kk_base = (kk_cfg.get("base_url") or "https://phimapi.com").rstrip("/")

    target_imdb: str | None = ext_id if kind == "imdb" else None
    target_tmdb: str | None = ext_id if kind == "tmdb" else None
    title = ""
    year: int | None = None

    # 1. Lấy tên + năm + tmdb_id từ Cinemeta nếu là IMDb ID
    if kind == "imdb":
        meta = await _fetch_cinemeta(client, content_type, ext_id)
        if meta:
            title = str(meta.get("name") or "").strip()
            year = _extract_year(meta.get("year") or meta.get("releaseInfo"))
            if meta.get("moviedb_id"):
                target_tmdb = str(meta["moviedb_id"])

    # 2. Thử tra cứu trực tiếp theo TMDB ID trên KKPhim (/tmdb/{movie|tv}/{id})
    if target_tmdb:
        movie = await _kkphim_tmdb_lookup(client, kk_base, content_type, target_tmdb)
        if movie and movie.get("slug"):
            if not title:
                title = str(movie.get("origin_name") or movie.get("name") or "").strip()
            tmdb_season = (movie.get("tmdb") or {}).get("season")
            # Nếu là phim lẻ, hoặc season khớp (hoặc không yêu cầu season) -> dùng luôn!
            if (
                content_type == "movie"
                or season is None
                or tmdb_season == season
                or (season == 1 and tmdb_season in (None, 1))
            ):
                slug = str(movie["slug"]).strip()
                _resolve_cache[cache_key] = (slug, episode)
                return slug, episode

    # 3. Tìm kiếm theo tên phim trên KKPhim để chọn đúng phần (season) / năm
    if title:
        queries = [title]
        if content_type == "series" and season and season > 1:
            queries.insert(0, f"{title} {season}")
        target_norm = normalize_title(title)

        best_slug = ""
        best_score = 0
        for q in queries:
            items = await _search_kkphim_candidates(client, kk_base, q)
            for it in items:
                sc = _score_candidate(
                    it,
                    target_title_norm=target_norm,
                    target_year=year,
                    target_imdb=target_imdb,
                    target_tmdb=target_tmdb,
                    content_type=content_type,
                    season=season,
                )
                if sc > best_score:
                    best_score = sc
                    best_slug = str(it.get("slug") or "").strip()
            if best_score >= 90:
                break

        if best_slug and best_score >= 45:
            _resolve_cache[cache_key] = (best_slug, episode)
            return best_slug, episode

    return "", episode


async def resolve_external_to_tmdb(
    client: httpx.AsyncClient,
    content_type: str,
    item_id: str,
) -> tuple[str, int | None, int | None]:
    """Chuyển đổi ID IMDb (tt...) hoặc TMDB (tmdb:...) sang (tmdb_id, season, episode)."""
    kind, ext_id, season, episode = parse_external_id(item_id)
    if not kind or not ext_id:
        return "", None, None
    if kind == "tmdb":
        return ext_id, season, episode
    if kind == "imdb":
        meta = await _fetch_cinemeta(client, content_type, ext_id)
        if meta and meta.get("moviedb_id"):
            return str(meta["moviedb_id"]).strip(), season, episode
    return "", season, episode
