from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any

import httpx

from config_store import config_store
from providers.base import CatalogItem, StreamServer
from providers.kkphim import KKPhimApiProvider
from providers.motchill import MotchillProvider


def normalize_title(text: str) -> str:
    """Chuẩn hóa tên phim để phát hiện và loại trừ phim trùng lặp giữa các nguồn."""
    if not text:
        return ""
    s = text.strip().lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    # Bỏ các hậu tố phổ biến như (vietsub), (thuyet minh), (2026), [hd]...
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
    s = re.sub(r"\b(vietsub|thuyet\s*minh|long\s*tieng|full|hd|fhd|4k|cam)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_ep_num(ep_slug: str | None) -> int | None:
    if not ep_slug:
        return None
    if "full" in ep_slug.lower():
        return None
    m = re.search(r"(\d+)", ep_slug)
    return int(m.group(1)) if m else None


class SourceAggregator:
    """Điều phối đa nguồn (KKPhim, OPhim, MotchillV, MotchillTi), lọc trùng lặp & gộp stream."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.providers: dict[str, KKPhimApiProvider | MotchillProvider] = {}
        self._sync_providers()

    def _sync_providers(self) -> None:
        sources = config_store.get_sources()
        for sid, scfg in sources.items():
            ptype = scfg.get("type", "motchill")
            existing = self.providers.get(sid)
            if ptype == "kkphim_api":
                if not isinstance(existing, KKPhimApiProvider):
                    self.providers[sid] = KKPhimApiProvider(sid, self.client)
            else:
                if not isinstance(existing, MotchillProvider):
                    self.providers[sid] = MotchillProvider(sid, self.client)
        # Bỏ provider không còn trong config
        for sid in list(self.providers.keys()):
            if sid not in sources:
                del self.providers[sid]

    def clear_cache(self) -> None:
        self._sync_providers()
        for p in self.providers.values():
            p.clear_cache()

    def active_providers(self) -> list[KKPhimApiProvider | MotchillProvider]:
        self._sync_providers()
        return [p for p in self.providers.values() if p.enabled]

    async def check_source(self, source_id: str, url: str) -> dict[str, Any]:
        self._sync_providers()
        prov = self.providers.get(source_id)
        if not prov:
            # Mặc định thử theo kiểu motchill
            prov = MotchillProvider(source_id, self.client)
        return await prov.check_connection(url)

    async def get_catalog(
        self,
        catalog_id: str,
        content_type: str,
        *,
        search: str | None = None,
        genre: str | None = None,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """Lấy danh mục/tìm kiếm song song từ tất cả các nguồn đang bật và loại trừ phim trùng lặp."""
        provs = self.active_providers()
        if not provs:
            return []

        tasks = [
            p.get_catalog_items(catalog_id, content_type, search=search, genre=genre, skip=skip)
            for p in provs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Gộp xen kẽ (round-robin) để mỗi nguồn đều xuất hiện cân bằng, đồng thời loại trùng lặp
        lists: list[list[CatalogItem]] = [r for r in results if isinstance(r, list) and r]
        if not lists:
            return []

        dedup_map: dict[str, CatalogItem] = {}
        dedup_sources: dict[str, list[str]] = {}
        ordered_keys: list[str] = []

        max_len = max(len(lst) for lst in lists)
        for idx in range(max_len):
            for lst in lists:
                if idx >= len(lst):
                    continue
                item = lst[idx]
                key = normalize_title(item.name) or normalize_title(item.origin_name) or item.slug
                if not key:
                    continue

                if key not in dedup_map:
                    dedup_map[key] = item
                    dedup_sources[key] = [item.source_id]
                    ordered_keys.append(key)
                else:
                    # Đã tồn tại -> ghi nhận thêm nguồn
                    if item.source_id not in dedup_sources[key]:
                        dedup_sources[key].append(item.source_id)
                    cur = dedup_map[key]
                    cur_info = f"{cur.release_info or ''} {cur.description or ''}".lower()
                    new_info = f"{item.release_info or ''} {item.description or ''}".lower()
                    # Nếu bản cũ là "Sắp chiếu/Trailer" còn bản mới đã có tập/Full -> ưu tiên bản mới làm nguồn chính!
                    if any(k in cur_info for k in ("sắp chiếu", "sap chieu", "trailer")) and not any(
                        k in new_info for k in ("sắp chiếu", "sap chieu", "trailer")
                    ):
                        dedup_map[key] = item
                    else:
                        if not cur.poster and item.poster:
                            cur.poster = item.poster
                        if not cur.year and item.year:
                            cur.year = item.year
                        if item.publish_ts > cur.publish_ts:
                            cur.publish_ts = item.publish_ts

        def _item_year(it: CatalogItem) -> int:
            for txt in (it.year, it.release_info or "", it.description or ""):
                m = re.search(r"\b(19|20)\d{2}\b", txt)
                if m:
                    return int(m.group(0))
            return 0

        # Sắp xếp theo năm phát hành giảm dần; trong cùng năm giữ đúng thứ tự đăng phim mới nhất (publish_ts)
        ranked_keys = sorted(
            enumerate(ordered_keys),
            key=lambda pair: (
                -_item_year(dedup_map[pair[1]]),
                -dedup_map[pair[1]].publish_ts,
                pair[0],
            ),
        )

        metas: list[dict[str, Any]] = []
        sources_cfg = config_store.get_sources()
        for _, key in ranked_keys:
            item = dedup_map[key]
            item.type = content_type
            src_names = [
                sources_cfg.get(sid, {}).get("name", sid) for sid in dedup_sources[key]
            ]
            src_badge = f"Nguồn: {', '.join(src_names)}"
            if item.description:
                item.description = f"{item.description} • {src_badge}"
            else:
                item.description = src_badge
            metas.append(item.to_stremio())

        return metas

    def _parse_id(self, item_id: str) -> tuple[str, str, str | None]:
        """Phân tách ID dạng vn:<source>:<slug>[:<ep_slug>] hoặc motchill:<slug>[:<ep_slug>]."""
        if item_id.startswith("vn:"):
            parts = item_id.split(":")
            source_id = parts[1] if len(parts) > 1 else "kkphim"
            slug = parts[2] if len(parts) > 2 else ""
            ep_slug = parts[3] if len(parts) > 3 else None
            return source_id, slug, ep_slug
        if item_id.startswith("motchill:"):
            parts = item_id.split(":")
            slug = parts[1] if len(parts) > 1 else ""
            ep_slug = parts[2] if len(parts) > 2 else None
            return "motchillv", slug, ep_slug
        return "", "", None

    async def get_meta(self, content_type: str, item_id: str) -> dict[str, Any] | None:
        self._sync_providers()
        source_id, slug, _ = self._parse_id(item_id)
        if not slug:
            return None

        primary_meta: dict[str, Any] | None = None
        primary = self.providers.get(source_id)
        if primary and primary.enabled:
            primary_meta = await primary.get_meta(content_type, slug)

        # Nếu là phim bộ mà nguồn chính chưa có tập (vd: "Sắp chiếu") hoặc bị lỗi -> lấy bổ sung từ các nguồn khác!
        need_fallback = primary_meta is None or (
            content_type == "series" and not primary_meta.get("videos")
        )
        if need_fallback:
            other_provs = [p for sid, p in self.providers.items() if sid != source_id and p.enabled]
            if other_provs:
                other_results = await asyncio.gather(
                    *(p.get_meta(content_type, slug) for p in other_provs),
                    return_exceptions=True,
                )
                best_other: dict[str, Any] | None = None
                for res in other_results:
                    if not isinstance(res, dict):
                        continue
                    if best_other is None or len(res.get("videos") or []) > len(best_other.get("videos") or []):
                        best_other = res
                if best_other:
                    if primary_meta is None:
                        primary_meta = best_other
                    elif best_other.get("videos"):
                        primary_meta["videos"] = best_other["videos"]
                        if not primary_meta.get("poster") and best_other.get("poster"):
                            primary_meta["poster"] = best_other["poster"]

        if primary_meta:
            primary_meta["id"] = item_id
        return primary_meta

    async def get_all_watch_servers(
        self,
        item_id: str,
        *,
        content_type: str = "series",
    ) -> list[StreamServer]:
        """Tổng hợp song song server phát từ TẤT CẢ các nguồn đang bật cho cùng 1 phim/tập và loại bỏ link trùng."""
        self._sync_providers()

        if item_id.startswith(("tt", "tmdb:")):
            from providers.external_id import resolve_external_to_slug

            slug, ep_num = await resolve_external_to_slug(self.client, content_type, item_id)
            source_id = ""
            ep_slug = None
        else:
            source_id, slug, ep_slug = self._parse_id(item_id)
            ep_num = _extract_ep_num(ep_slug)

        if not slug:
            return []

        provs = self.active_providers()
        if not provs:
            return []

        tasks = []
        for prov in provs:
            # Nguồn chính dùng đúng ep_slug; các nguồn phụ khớp theo cùng slug + số tập ep_num
            use_ep_slug = ep_slug if prov.source_id == source_id else None
            tasks.append(prov.get_streams_for_slug(slug, ep_slug=use_ep_slug, ep_num=ep_num))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[StreamServer] = []
        seen_links: set[str] = set()
        for res in results:
            if not isinstance(res, list):
                continue
            for sv in res:
                norm_link = sv.link.strip()
                if not norm_link or norm_link in seen_links:
                    continue
                seen_links.add(norm_link)
                merged.append(sv)
        return merged
