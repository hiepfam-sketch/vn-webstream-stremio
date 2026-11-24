from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
    "kkphim": {
        "enabled": True,
        "type": "kkphim_api",
        "name": "KKPhim",
        "base_url": "https://phimapi.com",
        "fallback_urls": [],
    },
    "ophim": {
        "enabled": True,
        "type": "kkphim_api",
        "name": "OPhim",
        "base_url": "https://ophim1.com",
        "fallback_urls": [],
    },
    "motchillv": {
        "enabled": True,
        "type": "motchill",
        "name": "MotchillV",
        "base_url": "https://motchillv.online",
        "fallback_urls": ["https://motchillv.org", "https://motchillv.you"],
    },
    "motchillti": {
        "enabled": True,
        "type": "motchill",
        "name": "MotchillTi",
        "base_url": "https://motchillti.mx",
        "fallback_urls": [],
    },
    "fmovies": {
        "enabled": True,
        "type": "fmovies",
        "name": "FMovies",
        "base_url": "https://www.f-moviess.org",
        "fallback_urls": [],
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "addon_name": "VN WebStream (Multi-Source)",
    "host": "0.0.0.0",
    "port": 7000,
    "public_base_url": "",
    "remote_config_url": "",
    "request_timeout": 18,
    "cache_ttl_seconds": 300,
    "sources": DEFAULT_SOURCES,
}


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"URL không hợp lệ: {url}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


class ConfigStore:
    """Quản lý cấu hình đa nguồn từ config.json + hỗ trợ đồng bộ từ GitHub Raw JSON."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._mtime: float = 0.0
        self._data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._last_remote_sync: float = 0.0
        self._load(force=True)

    def _merge_sources(self, raw_sources: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        merged = copy.deepcopy(DEFAULT_SOURCES)
        if isinstance(raw_sources, dict):
            for sid, scfg in raw_sources.items():
                if not isinstance(scfg, dict):
                    continue
                base = merged.get(sid, {"enabled": True, "type": "motchill", "name": sid, "base_url": "", "fallback_urls": []})
                item = dict(base)
                item.update(scfg)
                if item.get("base_url"):
                    try:
                        item["base_url"] = normalize_url(str(item["base_url"]))
                    except ValueError:
                        pass
                fbs = []
                for u in item.get("fallback_urls", []):
                    if u and str(u).strip():
                        try:
                            fbs.append(normalize_url(str(u)))
                        except ValueError:
                            pass
                item["fallback_urls"] = fbs
                item["enabled"] = bool(item.get("enabled", True))
                merged[sid] = item
        return merged

    def _load(self, force: bool = False) -> None:
        if not self.path.exists():
            self._save_raw(self._data)
            return
        try:
            mtime = self.path.stat().st_mtime
            if force or mtime != self._mtime:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                merged = copy.deepcopy(DEFAULT_CONFIG)
                merged.update({k: v for k, v in raw.items() if k != "sources"})
                merged["sources"] = self._merge_sources(raw.get("sources"))

                # Cho phép override qua biến môi trường khi deploy cloud (Render / HF Spaces)
                if os.getenv("PUBLIC_BASE_URL"):
                    merged["public_base_url"] = os.environ["PUBLIC_BASE_URL"].rstrip("/")
                if os.getenv("REMOTE_CONFIG_URL"):
                    merged["remote_config_url"] = os.environ["REMOTE_CONFIG_URL"].strip()
                if os.getenv("PORT"):
                    merged["port"] = int(os.environ["PORT"])
                if os.getenv("HOST"):
                    merged["host"] = os.environ["HOST"]

                self._data = merged
                self._mtime = mtime
        except Exception:
            pass

    def _save_raw(self, data: dict[str, Any]) -> None:
        try:
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._mtime = self.path.stat().st_mtime
        except Exception:
            # Trên một số cloud read-only FS, vẫn giữ cấu hình trong RAM
            pass

    def get(self) -> dict[str, Any]:
        self._load()
        return copy.deepcopy(self._data)

    def get_source(self, source_id: str) -> dict[str, Any]:
        self._load()
        return copy.deepcopy(self._data["sources"].get(source_id, {}))

    def get_sources(self) -> dict[str, dict[str, Any]]:
        self._load()
        return copy.deepcopy(self._data["sources"])

    @property
    def timeout(self) -> float:
        self._load()
        return float(self._data.get("request_timeout", 18))

    @property
    def remote_config_url(self) -> str:
        self._load()
        return str(self._data.get("remote_config_url", "")).strip()

    def update(
        self,
        *,
        sources: dict[str, Any] | None = None,
        addon_name: str | None = None,
        public_base_url: str | None = None,
        remote_config_url: str | None = None,
    ) -> dict[str, Any]:
        self._load(force=True)
        data = copy.deepcopy(self._data)
        if addon_name is not None and addon_name.strip():
            data["addon_name"] = addon_name.strip()
        if public_base_url is not None:
            data["public_base_url"] = public_base_url.strip().rstrip("/")
        if remote_config_url is not None:
            data["remote_config_url"] = remote_config_url.strip()
        if sources is not None:
            data["sources"] = self._merge_sources(sources)
        self._data = data
        self._save_raw(data)
        return copy.deepcopy(self._data)

    async def sync_remote_if_due(
        self,
        client: httpx.AsyncClient,
        *,
        force: bool = False,
        min_interval_sec: int = 300,
    ) -> dict[str, Any]:
        """Tự động kéo cấu hình tên miền mới nhất từ file JSON trên GitHub (Raw URL)."""
        url = self.remote_config_url
        if not url:
            return {"synced": False, "reason": "no_remote_url"}
        now = time.time()
        if not force and (now - self._last_remote_sync) < min_interval_sec:
            return {"synced": False, "reason": "fresh"}
        self._last_remote_sync = now
        try:
            resp = await client.get(url, timeout=10)
            resp.raise_for_status()
            remote = resp.json()
            if not isinstance(remote, dict):
                return {"synced": False, "reason": "invalid_json"}
            sources_patch = remote.get("sources") if isinstance(remote.get("sources"), dict) else remote
            updated = self.update(sources=sources_patch)
            return {"synced": True, "sources": updated["sources"]}
        except Exception as exc:
            return {"synced": False, "error": str(exc)}


config_store = ConfigStore()
