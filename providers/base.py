from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamServer:
    label: str
    server_type: str  # "m3u8" or "embed"
    link: str
    referer: str
    source_name: str = ""


@dataclass
class CatalogItem:
    id: str
    type: str  # "movie" or "series"
    name: str
    slug: str = ""
    source_id: str = ""
    origin_name: str = ""
    year: str = ""
    poster: str | None = None
    description: str | None = None
    release_info: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_stremio(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
        }
        if self.poster:
            out["poster"] = self.poster
        if self.description:
            out["description"] = self.description
        if self.release_info:
            out["releaseInfo"] = self.release_info
        return out
