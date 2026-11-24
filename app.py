from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from config_store import config_store
from providers.aggregator import SourceAggregator
from providers.motchill import GENRE_MAP
from resolvers.hls import (
    DESKTOP_UA,
    b64u_decode,
    fetch_cloudbeta_m3u8,
    fetch_streamc_m3u8,
    is_safe_http_url,
    resolve_servers,
    rewrite_m3u8_segments,
    strip_ts_prefix,
    verify_proxy_params,
)

http_client: httpx.AsyncClient | None = None
aggregator: SourceAggregator | None = None


@asynccontextmanager
async def lifespan(_app: Starlette):
    global http_client, aggregator
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(25.0, connect=10.0),
        limits=httpx.Limits(max_connections=120, max_keepalive_connections=40),
    )
    aggregator = SourceAggregator(http_client)
    # Kéo cấu hình từ GitHub Raw JSON ngay khi khởi động nếu có REMOTE_CONFIG_URL
    await config_store.sync_remote_if_due(http_client, force=True)
    aggregator.clear_cache()
    try:
        yield
    finally:
        await http_client.aclose()


def _get_addon_base(request: Request) -> str:
    cfg = config_store.get()
    if cfg.get("public_base_url"):
        return cfg["public_base_url"].rstrip("/")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def _parse_extra(extra_str: str | None) -> dict[str, str]:
    if not extra_str:
        return {}
    cleaned = extra_str.removesuffix(".json")
    parsed = parse_qs(cleaned, keep_blank_values=False)
    return {k: unquote(v[0]) for k, v in parsed.items() if v}


CATALOG_DEFS: list[dict[str, Any]] = [
    # Danh mục chính (Main)
    {
        "id": "vn-trung-quoc",
        "type": "series",
        "name": "VN Stream • Phim Trung Quốc",
        "group": "main",
        "default": True,
        "desc": "Phim bộ & cổ trang Trung Quốc mới nhất",
    },
    {
        "id": "vn-han-quoc",
        "type": "series",
        "name": "VN Stream • Phim Hàn Quốc",
        "group": "main",
        "default": True,
        "desc": "Phim bộ Hàn Quốc Vietsub & Thuyết minh",
    },
    {
        "id": "vn-bo",
        "type": "series",
        "name": "VN Stream • Phim Bộ Tổng Hợp",
        "group": "main",
        "default": False,
        "desc": "Tổng hợp phim bộ nhiều quốc gia",
    },
    {
        "id": "vn-le",
        "type": "movie",
        "name": "VN Stream • Phim Lẻ Tổng Hợp",
        "group": "main",
        "default": False,
        "desc": "Phim lẻ HD/Full Vietsub & Thuyết minh",
    },
    # Hàng riêng trên màn hình chính (Special Shelves)
    {
        "id": "vn-au-my",
        "type": "movie",
        "name": "VN Stream • Phim Âu Mỹ",
        "group": "shelf",
        "default": False,
        "desc": "Điện ảnh Hollywood & Âu Mỹ",
    },
    {
        "id": "vn-chieu-rap",
        "type": "movie",
        "name": "VN Stream • Phim Chiếu Rạp",
        "group": "shelf",
        "default": False,
        "desc": "Phim chiếu rạp tuyển chọn",
    },
    {
        "id": "vn-thuyet-minh",
        "type": "series",
        "name": "VN Stream • Phim Thuyết Minh",
        "group": "shelf",
        "default": False,
        "desc": "Phim có bản Thuyết minh / Lồng tiếng",
    },
    {
        "id": "vn-moi",
        "type": "series",
        "name": "VN Stream • Mới Cập Nhật",
        "group": "shelf",
        "default": False,
        "desc": "Phim mới lên sóng gần đây",
    },
]

CATALOG_DEF_BY_ID: dict[str, dict[str, Any]] = {c["id"]: c for c in CATALOG_DEFS}
DEFAULT_CATALOG_IDS: list[str] = [c["id"] for c in CATALOG_DEFS if c["default"]]


def _decode_user_config(cfg_token: str | None) -> dict[str, Any]:
    """Giải mã cấu hình cá nhân từ URL dạng /<base64url>/manifest.json."""
    if not cfg_token:
        return {}
    try:
        raw = b64u_decode(unquote(cfg_token).strip())
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _extract_allowed_sources(user_cfg: dict[str, Any]) -> set[str] | None:
    raw = user_cfg.get("sources")
    if isinstance(raw, list) and raw:
        cleaned = {str(s).strip() for s in raw if str(s).strip()}
        if cleaned:
            return cleaned
    return None


def _build_manifest(user_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config_store.get()
    user_cfg = user_cfg or {}
    addon_name = cfg.get("addon_name") or "VN WebStream (Multi-Source)"
    allowed_sources = _extract_allowed_sources(user_cfg)

    enabled_names = [
        s.get("name", sid)
        for sid, s in cfg.get("sources", {}).items()
        if s.get("enabled") and (allowed_sources is None or sid in allowed_sources)
    ]

    genre_options = [g for g in GENRE_MAP.keys() if g != "Hoạt Hình"]
    raw_cat_ids = user_cfg.get("catalogs")
    if isinstance(raw_cat_ids, list) and raw_cat_ids:
        selected_ids = [cid for cid in raw_cat_ids if cid in CATALOG_DEF_BY_ID]
    else:
        selected_ids = list(DEFAULT_CATALOG_IDS)
    if not selected_ids:
        selected_ids = [DEFAULT_CATALOG_IDS[0]]

    catalogs: list[dict[str, Any]] = []
    for cid in selected_ids:
        cdef = CATALOG_DEF_BY_ID[cid]
        catalogs.append(
            {
                "type": cdef["type"],
                "id": cdef["id"],
                "name": cdef["name"],
                "extra": [
                    {"name": "search", "isRequired": False},
                    {"name": "genre", "isRequired": False, "options": genre_options},
                    {"name": "skip", "isRequired": False},
                ],
            }
        )

    return {
        "id": "vn.webstream.multisource",
        "version": "2.6.0",
        "name": addon_name,
        "description": (
            f"Phim Vietsub & Thuyết minh phát trực tiếp HTTP HLS trên Stremio/Nuvio từ {len(enabled_names)} nguồn "
            f"({', '.join(enabled_names)}) — lọc trùng, hỗ trợ khớp IMDb (tt) & TMDB."
        ),
        "types": ["movie", "series"],
        "resources": [
            "catalog",
            {
                "name": "meta",
                "types": ["movie", "series"],
                "idPrefixes": ["vn:", "motchill:"],
            },
            {
                "name": "stream",
                "types": ["movie", "series"],
                "idPrefixes": ["vn:", "motchill:", "tt", "tmdb:"],
            },
        ],
        "idPrefixes": ["vn:", "motchill:", "tt", "tmdb:"],
        "catalogs": catalogs,
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False,
        },
    }


# ---------- Stremio Protocol Routes ----------

async def health_endpoint(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "2.6.0"})


async def manifest_endpoint(request: Request) -> JSONResponse:
    assert http_client is not None
    await config_store.sync_remote_if_due(http_client)
    user_cfg = _decode_user_config(request.path_params.get("cfg"))
    return JSONResponse(_build_manifest(user_cfg))


async def catalog_endpoint(request: Request) -> JSONResponse:
    assert aggregator is not None and http_client is not None
    await config_store.sync_remote_if_due(http_client)
    user_cfg = _decode_user_config(request.path_params.get("cfg"))
    allowed_sources = _extract_allowed_sources(user_cfg)

    content_type = request.path_params["type"]
    catalog_id = request.path_params["id"].removesuffix(".json")
    extra = _parse_extra(request.path_params.get("extra"))

    search = extra.get("search")
    genre = extra.get("genre")
    try:
        skip = int(extra.get("skip", "0"))
    except ValueError:
        skip = 0

    try:
        metas = await aggregator.get_catalog(
            catalog_id,
            content_type,
            search=search,
            genre=genre,
            skip=skip,
            allowed_sources=allowed_sources,
        )
        return JSONResponse({"metas": metas})
    except Exception as exc:
        return JSONResponse({"metas": [], "error": str(exc)}, status_code=200)


async def meta_endpoint(request: Request) -> JSONResponse:
    assert aggregator is not None and http_client is not None
    await config_store.sync_remote_if_due(http_client)
    content_type = request.path_params["type"]
    item_id = unquote(request.path_params["id"].removesuffix(".json"))

    if not item_id.startswith(("vn:", "motchill:")):
        return JSONResponse({"meta": None})

    try:
        meta = await aggregator.get_meta(content_type, item_id)
        return JSONResponse({"meta": meta})
    except Exception as exc:
        return JSONResponse({"meta": None, "error": str(exc)}, status_code=200)


async def stream_endpoint(request: Request) -> JSONResponse:
    assert aggregator is not None and http_client is not None
    await config_store.sync_remote_if_due(http_client)
    user_cfg = _decode_user_config(request.path_params.get("cfg"))
    allowed_sources = _extract_allowed_sources(user_cfg)

    content_type = request.path_params["type"]
    item_id = unquote(request.path_params["id"].removesuffix(".json"))

    if not item_id.startswith(("vn:", "motchill:", "tt", "tmdb:")):
        return JSONResponse({"streams": []})

    addon_base = _get_addon_base(request)
    try:
        servers = await aggregator.get_all_watch_servers(
            item_id,
            content_type=content_type,
            allowed_sources=allowed_sources,
        )
        streams = await resolve_servers(http_client, servers, addon_base)
        return JSONResponse({"streams": streams})
    except Exception as exc:
        return JSONResponse({"streams": [], "error": str(exc)}, status_code=200)


# ---------- HLS Proxy Routes (bảo vệ bằng HMAC-SHA256 & chống SSRF) ----------

def _validate_proxy_request(request: Request) -> tuple[str, str] | PlainTextResponse:
    u = request.query_params.get("u", "")
    r = request.query_params.get("r", "")
    sig = request.query_params.get("sig", "")
    if not verify_proxy_params(u, r, sig):
        return PlainTextResponse("Forbidden: invalid proxy signature", status_code=403)
    try:
        target = b64u_decode(u)
        ref = b64u_decode(r) if r else ""
    except Exception:
        return PlainTextResponse("Bad Request: invalid encoding", status_code=400)
    if not is_safe_http_url(target):
        return PlainTextResponse("Forbidden: unsafe target URL", status_code=403)
    return target, ref


async def hls_cloudbeta_endpoint(request: Request) -> PlainTextResponse:
    assert http_client is not None
    check = _validate_proxy_request(request)
    if isinstance(check, PlainTextResponse):
        return check
    em3u8_url, _ = check
    addon_base = _get_addon_base(request)
    try:
        playlist = await fetch_cloudbeta_m3u8(http_client, em3u8_url, addon_base)
        return PlainTextResponse(
            playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    except Exception as exc:
        return PlainTextResponse(f"CloudBeta error: {exc}", status_code=502)


async def hls_streamc_endpoint(request: Request) -> PlainTextResponse:
    assert http_client is not None
    check = _validate_proxy_request(request)
    if isinstance(check, PlainTextResponse):
        return check
    embed_url, site_ref = check
    site_ref = site_ref or "https://motchillv.online/"
    addon_base = _get_addon_base(request)
    try:
        playlist = await fetch_streamc_m3u8(http_client, embed_url, site_ref, addon_base)
        return PlainTextResponse(
            playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    except Exception as exc:
        return PlainTextResponse(f"StreamC error: {exc}", status_code=502)


async def hls_raw_m3u8_endpoint(request: Request) -> PlainTextResponse:
    assert http_client is not None
    check = _validate_proxy_request(request)
    if isinstance(check, PlainTextResponse):
        return check
    target, ref = check
    addon_base = _get_addon_base(request)
    headers = {"User-Agent": DESKTOP_UA}
    if ref:
        headers["Referer"] = ref
    try:
        resp = await http_client.get(target, headers=headers, timeout=15)
        resp.raise_for_status()
        rewritten = rewrite_m3u8_segments(resp.text, str(resp.url), addon_base, referer=ref)
        return PlainTextResponse(
            rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    except Exception as exc:
        return PlainTextResponse(f"M3U8 proxy error: {exc}", status_code=502)


async def hls_segment_endpoint(request: Request) -> StreamingResponse | PlainTextResponse:
    assert http_client is not None
    check = _validate_proxy_request(request)
    if isinstance(check, PlainTextResponse):
        return check
    seg_url, ref = check
    headers = {"User-Agent": DESKTOP_UA}
    if ref:
        headers["Referer"] = ref

    async def _stream_gen():
        async with http_client.stream("GET", seg_url, headers=headers, timeout=25) as resp:
            resp.raise_for_status()
            buf = bytearray()
            stripped = False
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                if not stripped:
                    buf.extend(chunk)
                    if len(buf) >= 2048:
                        yield strip_ts_prefix(bytes(buf))
                        buf.clear()
                        stripped = True
                else:
                    yield chunk
            if buf:
                yield strip_ts_prefix(bytes(buf)) if not stripped else bytes(buf)

    return StreamingResponse(
        _stream_gen(),
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------- Admin / Configure API & UI ----------

def _check_admin_auth(request: Request) -> JSONResponse | None:
    admin_pw = os.getenv("ADMIN_PASSWORD", "").strip()
    if admin_pw:
        provided = (request.headers.get("x-admin-password") or "").strip()
        if not hmac.compare_digest(admin_pw, provided):
            return JSONResponse(
                {"ok": False, "error": "Cần nhập đúng Mật khẩu quản trị (ADMIN_PASSWORD)."},
                status_code=403,
            )
        return None
    # Nếu chạy Cloud với REMOTE_CONFIG_URL mà không đặt ADMIN_PASSWORD -> khóa sửa qua web (chỉ sửa qua GitHub)
    if config_store.remote_config_url:
        return JSONResponse(
            {
                "ok": False,
                "error": "Chế độ đồng bộ GitHub đang bật (chỉ đọc trên web). Hãy sửa file domains.json trên GitHub hoặc đặt biến môi trường ADMIN_PASSWORD.",
            },
            status_code=403,
        )
    return None


async def api_get_config(_request: Request) -> JSONResponse:
    return JSONResponse(config_store.get())


async def api_update_config(request: Request) -> JSONResponse:
    assert aggregator is not None
    denied = _check_admin_auth(request)
    if denied:
        return denied
    try:
        body = await request.json()
        updated = config_store.update(
            sources=body.get("sources"),
            addon_name=body.get("addon_name"),
            public_base_url=body.get("public_base_url"),
            remote_config_url=body.get("remote_config_url"),
        )
        aggregator.clear_cache()
        return JSONResponse({"ok": True, "config": updated})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def api_check_url(request: Request) -> JSONResponse:
    assert aggregator is not None
    denied = _check_admin_auth(request)
    if denied:
        return denied
    try:
        body = await request.json()
        source_id = body.get("source_id", "motchillv")
        url = body.get("url", "")
        if not is_safe_http_url(url):
            return JSONResponse({"ok": False, "error": "URL không hợp lệ hoặc bị chặn (IP nội bộ)."}, status_code=400)
        result = await aggregator.check_source(source_id, url)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def api_sync_remote(_request: Request) -> JSONResponse:
    assert aggregator is not None and http_client is not None
    res = await config_store.sync_remote_if_due(http_client, force=True)
    if res.get("synced"):
        aggregator.clear_cache()
    return JSONResponse(res)


CONFIG_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>VN WebStream — Stremio Multi-Source Addon</title>
  <style>
    :root {
      --bg: #0b0d14;
      --surface: rgba(20, 23, 35, 0.82);
      --surface-2: rgba(28, 32, 48, 0.72);
      --border: rgba(255, 255, 255, 0.08);
      --border-hi: rgba(255, 255, 255, 0.16);
      --text: #f3f5fb;
      --muted: #98a2b8;
      --coral: #e14155;
      --coral-hover: #c93346;
      --blue: #4c9be8;
      --blue-hover: #3888d6;
      --ok: #10b981;
      --err: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background:
        radial-gradient(600px 420px at 15% 10%, rgba(225, 65, 85, 0.16), transparent 65%),
        radial-gradient(620px 460px at 88% 18%, rgba(76, 155, 232, 0.16), transparent 65%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding-bottom: 64px;
    }
    /* Sticky Topbar */
    .topbar {
      position: sticky; top: 0; z-index: 40;
      backdrop-filter: blur(14px);
      background: rgba(11, 13, 20, 0.82);
      border-bottom: 1px solid var(--border);
      padding: 12px 20px;
      display: flex; align-items: center; justify-content: space-between;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 16px; text-decoration: none; color: var(--text); }
    .brand-icon {
      width: 32px; height: 32px; border-radius: 9px;
      background: linear-gradient(135deg, #271522, #121d2f);
      border: 1px solid rgba(225, 65, 85, 0.4);
      display: grid; place-items: center; font-size: 16px;
    }
    .grad-text {
      background: linear-gradient(90deg, #f472b6, #e14155 45%, #60a5fa);
      -webkit-background-clip: text; background-clip: text; color: transparent;
    }
    .status-pill {
      display: inline-flex; align-items: center; gap: 7px;
      padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
      background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.35); color: #6ee7b7;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); }

    .wrap { max-width: 920px; margin: 22px auto 0; padding: 0 16px; }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 24px;
      margin-bottom: 18px;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.35);
    }

    /* Hero */
    .hero { text-align: center; padding: 32px 24px; }
    .ver-pill {
      display: inline-block; padding: 5px 14px; border-radius: 999px;
      font-size: 12px; font-weight: 700; letter-spacing: 0.5px;
      background: rgba(225, 65, 85, 0.14); border: 1px solid rgba(225, 65, 85, 0.38); color: #fda4af;
      margin-bottom: 14px;
    }
    .hero-logo {
      width: 64px; height: 64px; border-radius: 18px; margin: 0 auto 14px;
      background: linear-gradient(135deg, #2a1424, #101b2e);
      border: 1px solid rgba(255, 255, 255, 0.14);
      display: grid; place-items: center; font-size: 28px;
      box-shadow: 0 10px 28px rgba(225, 65, 85, 0.25);
    }
    .hero h1 { margin: 0 0 10px; font-size: 30px; font-weight: 800; }
    .hero p.sub { margin: 0 auto 18px; max-width: 620px; color: var(--muted); font-size: 14.5px; line-height: 1.55; }
    .pills { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-bottom: 22px; }
    .pill {
      padding: 6px 12px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
      background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border); color: #dbe2f0;
    }
    .cta-row { display: flex; flex-wrap: wrap; justify-content: center; gap: 12px; margin-bottom: 12px; }
    button, a.btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 11px 18px; border-radius: 12px; font-size: 14px; font-weight: 700;
      border: 1px solid transparent; cursor: pointer; text-decoration: none; color: #fff;
      transition: transform 0.12s ease, background 0.15s ease, box-shadow 0.15s ease;
    }
    button:active, a.btn:active { transform: scale(0.98); }
    .btn-coral { background: var(--coral); box-shadow: 0 6px 22px rgba(225, 65, 85, 0.35); }
    .btn-coral:hover { background: var(--coral-hover); }
    .btn-blue { background: var(--blue); box-shadow: 0 6px 22px rgba(76, 155, 232, 0.32); }
    .btn-blue:hover { background: var(--blue-hover); }
    .btn-ghost { background: rgba(255, 255, 255, 0.06); border-color: var(--border-hi); color: #e2e8f0; }
    .btn-ghost:hover { background: rgba(255, 255, 255, 0.12); }
    .btn-sm { padding: 7px 12px; font-size: 12.5px; border-radius: 9px; }

    /* Presets */
    .section-title { font-size: 14px; font-weight: 700; color: #cbd5e1; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; }
    .preset-row { display: flex; flex-wrap: wrap; gap: 8px; }
    .preset-btn {
      padding: 8px 13px; border-radius: 10px; font-size: 13px; font-weight: 600;
      background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-hi); color: #e2e8f0;
    }
    .preset-btn:hover { background: rgba(225, 65, 85, 0.18); border-color: rgba(225, 65, 85, 0.5); }

    /* Tabs */
    .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }
    .tab-btn {
      padding: 9px 14px; border-radius: 10px; font-size: 13.5px; font-weight: 700;
      background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border); color: var(--muted);
    }
    .tab-btn.active {
      background: linear-gradient(135deg, rgba(225, 65, 85, 0.22), rgba(76, 155, 232, 0.22));
      border-color: rgba(255, 255, 255, 0.24); color: #fff;
    }
    .tab-badge {
      padding: 2px 7px; border-radius: 999px; font-size: 11px;
      background: rgba(255, 255, 255, 0.12); color: #fff;
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* Checkboxes & Cards Grid */
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .check-card {
      display: flex; align-items: flex-start; gap: 11px;
      padding: 12px 14px; border-radius: 12px; cursor: pointer;
      background: var(--surface-2); border: 1px solid var(--border);
      transition: border-color 0.15s, background 0.15s;
    }
    .check-card:hover { border-color: var(--border-hi); }
    .check-card.checked { border-color: rgba(76, 155, 232, 0.55); background: rgba(76, 155, 232, 0.1); }
    .check-card input[type="checkbox"] { width: 18px; height: 18px; margin-top: 2px; accent-color: var(--blue); cursor: pointer; flex-shrink: 0; }
    .check-meta { min-width: 0; }
    .check-name { font-size: 14px; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .check-desc { font-size: 12px; color: var(--muted); margin-top: 3px; }
    .tag { font-size: 10.5px; padding: 2px 7px; border-radius: 6px; background: rgba(255, 255, 255, 0.09); color: #cbd5e1; font-weight: 600; }
    .tag-movie { background: rgba(225, 65, 85, 0.2); color: #fda4af; }
    .tag-series { background: rgba(76, 155, 232, 0.2); color: #93c5fd; }

    /* Summary & Preview */
    .selected-pills { display: flex; flex-wrap: wrap; gap: 7px; margin: 10px 0 16px; }
    .sel-pill {
      font-size: 12px; font-weight: 600; padding: 5px 10px; border-radius: 999px;
      background: rgba(76, 155, 232, 0.14); border: 1px solid rgba(76, 155, 232, 0.35); color: #bfdbfe;
    }
    .url-box { display: flex; gap: 8px; align-items: stretch; margin-bottom: 14px; }
    .mono {
      flex: 1; font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      background: #090b11; padding: 11px 13px; border-radius: 10px;
      border: 1px solid var(--border-hi); word-break: break-all; font-size: 12.5px; color: #cbd5e1;
    }
    details.preview {
      margin-top: 14px; border: 1px solid var(--border); border-radius: 12px;
      background: #090b11; overflow: hidden;
    }
    details.preview summary {
      padding: 11px 14px; cursor: pointer; font-size: 13px; font-weight: 700; color: #cbd5e1;
      user-select: none;
    }
    details.preview pre {
      margin: 0; padding: 12px 14px; border-top: 1px solid var(--border);
      max-height: 320px; overflow: auto; font-size: 12px; color: #93c5fd;
    }

    /* Admin inputs */
    label.fld { display: block; font-size: 12.5px; font-weight: 600; margin: 10px 0 4px; color: #cbd5e1; }
    input[type="url"], input[type="text"], input[type="password"] {
      width: 100%; background: #090b11; color: var(--text);
      border: 1px solid var(--border-hi); border-radius: 9px;
      padding: 9px 12px; font-size: 13.5px; font-family: inherit;
    }
    .admin-src {
      border: 1px solid var(--border); border-radius: 12px;
      padding: 14px; margin-bottom: 12px; background: var(--surface-2);
    }
    .admin-src-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
    .status-box {
      margin-top: 12px; padding: 10px 13px; border-radius: 10px;
      font-size: 13px; display: none; white-space: pre-wrap;
    }
    .status-box.ok { display: block; background: rgba(16, 185, 129, 0.14); border: 1px solid var(--ok); color: #6ee7b7; }
    .status-box.err { display: block; background: rgba(239, 68, 68, 0.14); border: 1px solid var(--err); color: #fca5a5; }

    /* Toast */
    .toast {
      position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%) translateY(20px);
      background: rgba(16, 185, 129, 0.95); color: #052e16; font-weight: 700; font-size: 13.5px;
      padding: 10px 18px; border-radius: 999px; box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
      opacity: 0; pointer-events: none; transition: opacity 0.2s, transform 0.2s; z-index: 80;
    }
    .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

    /* QR Modal */
    .modal-bg {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.72); backdrop-filter: blur(6px);
      display: none; align-items: center; justify-content: center; z-index: 90; padding: 16px;
    }
    .modal-bg.open { display: flex; }
    .modal {
      background: #141826; border: 1px solid var(--border-hi); border-radius: 18px;
      max-width: 380px; width: 100%; padding: 22px; text-align: center;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6);
    }
    .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .modal-head h3 { margin: 0; font-size: 16px; }
    .qr-wrap { background: #fff; padding: 12px; border-radius: 14px; display: inline-block; margin: 10px 0 14px; }
    .qr-wrap img { display: block; width: 220px; height: 220px; }
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/">
      <span class="brand-icon">🎬</span>
      <span>VN <span class="grad-text">WebStream</span></span>
    </a>
    <span class="status-pill"><span class="dot"></span><span id="topStatus">Sẵn sàng</span></span>
  </header>

  <div class="wrap">
    <!-- Hero Card -->
    <section class="card hero">
      <div class="ver-pill">⚡ PHIÊN BẢN GỘP V2.6.0</div>
      <div class="hero-logo">🎬</div>
      <h1>VN <span class="grad-text">WebStream Tổng Hợp</span></h1>
      <p class="sub">Tập trung nguồn Motchill (MotchillV, MotchillTi) &amp; OPhim Vietsub / Thuyết minh, tự động lọc trùng lặp, tùy chọn danh mục theo ý thích và khớp trực tiếp IMDb / TMDB trên Stremio.</p>
      <div class="pills">
        <span class="pill" id="heroSourcePill">🎬 5 Nguồn Phim</span>
        <span class="pill">🔄 Lọc Trùng Thông Minh</span>
        <span class="pill">⚡ Khớp IMDb &amp; TMDB</span>
        <span class="pill">🎙️ Vietsub &amp; Thuyết Minh</span>
      </div>
      <div class="cta-row">
        <a class="btn btn-coral" id="heroStremioApp" href="#">⬇️ Cài vào Stremio App</a>
        <a class="btn btn-blue" id="heroStremioWeb" href="#" target="_blank" rel="noopener">🌐 Cài vào Stremio Web</a>
      </div>
      <div class="cta-row" style="margin-bottom:0;">
        <button type="button" class="btn-ghost btn-sm" onclick="copyManifest()">📋 Sao chép Link</button>
        <button type="button" class="btn-ghost btn-sm" onclick="openQr()">📱 Quét QR TV</button>
      </div>
    </section>

    <!-- Quick Presets -->
    <section class="card">
      <div class="section-title">⚡ Cấu hình nhanh (Presets):</div>
      <div class="preset-row">
        <button type="button" class="preset-btn" onclick="applyPreset('default')">🌟 Mặc định (Trung + Hàn)</button>
        <button type="button" class="preset-btn" onclick="applyPreset('full')">🔥 Đầy đủ (Tất cả danh mục)</button>
        <button type="button" class="preset-btn" onclick="applyPreset('movies')">🎬 Chỉ Phim Lẻ</button>
        <button type="button" class="preset-btn" onclick="applyPreset('series')">📺 Chỉ Phim Bộ</button>
        <button type="button" class="preset-btn" onclick="applyPreset('lite')">⚡ Chỉ Motchill (V + Ti)</button>
      </div>
    </section>

    <!-- Main Configuration Tabs -->
    <section class="card">
      <div class="tabs">
        <button type="button" class="tab-btn active" data-tab="catalogs" onclick="switchTab('catalogs')">
          📺 Danh mục &amp; Trang chủ <span class="tab-badge" id="badgeCatalogs">0/0</span>
        </button>
        <button type="button" class="tab-btn" data-tab="sources" onclick="switchTab('sources')">
          🎬 Chọn nguồn phim <span class="tab-badge" id="badgeSources">0/0</span>
        </button>
        <button type="button" class="tab-btn" data-tab="admin" onclick="switchTab('admin')">
          ⚙️ Quản trị Tên miền &amp; GitHub
        </button>
      </div>

      <!-- Tab 1: Catalogs -->
      <div class="tab-panel active" id="panel_catalogs">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; gap:8px; flex-wrap:wrap;">
          <div class="section-title" style="margin:0;">📂 Danh mục chính (Main Catalogs)</div>
          <button type="button" class="btn-ghost btn-sm" onclick="toggleAllCatalogs()">Bật / Tắt tất cả</button>
        </div>
        <div class="grid" id="mainCatalogsGrid"></div>

        <div class="section-title" style="margin-top:8px;">🌟 Hàng riêng trên màn hình chính (Special Shelves)</div>
        <div class="grid" id="shelfCatalogsGrid"></div>
      </div>

      <!-- Tab 2: Per-user Sources -->
      <div class="tab-panel" id="panel_sources">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; gap:8px; flex-wrap:wrap;">
          <div class="section-title" style="margin:0;">🎬 Nguồn phim áp dụng cho link cài đặt của bạn</div>
          <button type="button" class="btn-ghost btn-sm" onclick="toggleAllUserSources()">Bật / Tắt tất cả</button>
        </div>
        <p style="margin:0 0 14px; font-size:13px; color:var(--muted);">Tùy chọn này được mã hóa trực tiếp vào URL Manifest của riêng bạn (không ảnh hưởng cấu hình server).</p>
        <div class="grid" id="userSourcesGrid"></div>
      </div>

      <!-- Tab 3: Admin Domain & GitHub Sync -->
      <div class="tab-panel" id="panel_admin">
        <div class="section-title">🔧 Quản lý tên miền nguồn phim (Server-wide)</div>
        <div id="adminSourcesContainer"></div>
        <div id="adminPwBox" style="display:__ADMIN_PW_DISPLAY__; margin-top:12px;">
          <label class="fld" for="adminPw">🔐 Mật khẩu quản trị (ADMIN_PASSWORD)</label>
          <input id="adminPw" type="password" placeholder="Nhập ADMIN_PASSWORD để lưu hoặc kiểm tra kết nối" />
        </div>
        <label class="fld" for="remoteUrl">☁️ GitHub Raw JSON đồng bộ tên miền (REMOTE_CONFIG_URL)</label>
        <input id="remoteUrl" type="url" placeholder="https://raw.githubusercontent.com/username/repo/main/domains.json" value="__REMOTE_URL__" />
        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:14px;">
          <button id="btnSave" type="button" class="btn-coral btn-sm">💾 Lưu cấu hình Server</button>
          <button id="btnSync" type="button" class="btn-ghost btn-sm">🔄 Đồng bộ ngay từ GitHub</button>
        </div>
        <div id="statusBox" class="status-box"></div>
      </div>
    </section>

    <!-- Bottom Summary & Install Card -->
    <section class="card">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
        <div class="section-title" style="margin:0;">📺 Cấu hình bạn đã chọn</div>
        <span class="tab-badge" id="summaryCount" style="font-size:12px; padding:4px 10px;">0 danh mục</span>
      </div>
      <div class="selected-pills" id="selectedPills"></div>

      <div class="url-box">
        <div class="mono" id="manifestUrl">__MANIFEST_URL__</div>
        <button type="button" class="btn-ghost btn-sm" onclick="copyManifest()">📋 Sao chép</button>
      </div>

      <div class="cta-row" style="justify-content:flex-start; margin-bottom:0;">
        <a class="btn btn-coral" id="botStremioApp" href="#">⬇️ Cài vào Stremio (App)</a>
        <a class="btn btn-blue" id="botStremioWeb" href="#" target="_blank" rel="noopener">🌐 Cài vào Stremio Web</a>
        <button type="button" class="btn-ghost" onclick="copyManifest()">📋 Sao chép URL</button>
        <button type="button" class="btn-ghost" onclick="openQr()">📱 Mã QR TV / ĐT</button>
      </div>

      <details class="preview">
        <summary>🔍 Xem trước Manifest JSON (Realtime)</summary>
        <pre id="manifestPreview"></pre>
      </details>
    </section>
  </div>

  <!-- QR Modal -->
  <div class="modal-bg" id="qrModal" onclick="if(event.target===this) closeQr()">
    <div class="modal">
      <div class="modal-head">
        <h3>📱 Quét Mã QR Cài Đặt</h3>
        <button type="button" class="btn-ghost btn-sm" onclick="closeQr()">✕</button>
      </div>
      <p style="margin:0 0 8px; font-size:13px; color:var(--muted);">Quét bằng điện thoại hoặc Android TV để mở link Manifest Stremio:</p>
      <div class="qr-wrap"><img id="qrImg" alt="QR Code" src="" /></div>
      <div class="mono" id="qrUrlText" style="font-size:11px; margin-bottom:12px;"></div>
      <button type="button" class="btn-blue btn-sm" style="width:100%;" onclick="copyManifest()">📋 Sao chép URL Manifest</button>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const ADDON_BASE = __ADDON_BASE_JSON__;
    const ADDON_NAME = __ADDON_NAME_JSON__;
    const SERVER_SOURCES = __SOURCES_JSON__;
    const CATALOG_DEFS = __CATALOG_DEFS_JSON__;
    const DEFAULT_CATALOG_IDS = __DEFAULT_CATALOG_IDS_JSON__;
    const INITIAL_USER_CFG = __USER_CFG_JSON__;

    const ENABLED_SERVER_SOURCE_IDS = Object.entries(SERVER_SOURCES)
      .filter(([_, s]) => s.enabled)
      .map(([sid]) => sid);

    let selectedCatalogs = new Set(
      (Array.isArray(INITIAL_USER_CFG.catalogs) && INITIAL_USER_CFG.catalogs.length)
        ? INITIAL_USER_CFG.catalogs.filter(id => CATALOG_DEFS.some(c => c.id === id))
        : DEFAULT_CATALOG_IDS
    );
    if (!selectedCatalogs.size) selectedCatalogs.add(DEFAULT_CATALOG_IDS[0]);

    let selectedSources = new Set(
      (Array.isArray(INITIAL_USER_CFG.sources) && INITIAL_USER_CFG.sources.length)
        ? INITIAL_USER_CFG.sources.filter(id => ENABLED_SERVER_SOURCE_IDS.includes(id))
        : ENABLED_SERVER_SOURCE_IDS
    );
    if (!selectedSources.size && ENABLED_SERVER_SOURCE_IDS.length) {
      ENABLED_SERVER_SOURCE_IDS.forEach(id => selectedSources.add(id));
    }

    let toastTimer = null;
    function showToast(msg) {
      const el = document.getElementById('toast');
      el.textContent = msg;
      el.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
    }

    function switchTab(name) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel_' + name));
    }

    function b64uEncode(str) {
      const bytes = new TextEncoder().encode(str);
      let bin = '';
      bytes.forEach(b => bin += String.fromCharCode(b));
      return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }

    function isDefaultConfig() {
      const catArr = CATALOG_DEFS.map(c => c.id).filter(id => selectedCatalogs.has(id));
      const sameCats = catArr.length === DEFAULT_CATALOG_IDS.length && catArr.every((v, i) => v === DEFAULT_CATALOG_IDS[i]);
      const sameSrcs = selectedSources.size === ENABLED_SERVER_SOURCE_IDS.length && ENABLED_SERVER_SOURCE_IDS.every(id => selectedSources.has(id));
      return sameCats && sameSrcs;
    }

    function currentManifestUrl() {
      if (isDefaultConfig()) return ADDON_BASE + '/manifest.json';
      const payload = {
        catalogs: CATALOG_DEFS.map(c => c.id).filter(id => selectedCatalogs.has(id)),
        sources: ENABLED_SERVER_SOURCE_IDS.filter(id => selectedSources.has(id)),
      };
      return ADDON_BASE + '/' + b64uEncode(JSON.stringify(payload)) + '/manifest.json';
    }

    function buildPreviewManifest() {
      const cats = CATALOG_DEFS.filter(c => selectedCatalogs.has(c.id)).map(c => ({
        type: c.type,
        id: c.id,
        name: c.name,
        extra: [
          { name: 'search', isRequired: false },
          { name: 'genre', isRequired: false },
          { name: 'skip', isRequired: false }
        ]
      }));
      const srcNames = ENABLED_SERVER_SOURCE_IDS
        .filter(id => selectedSources.has(id))
        .map(id => (SERVER_SOURCES[id] && SERVER_SOURCES[id].name) || id);
      return {
        id: 'vn.webstream.multisource',
        version: '2.6.0',
        name: ADDON_NAME,
        description: `Phim Vietsub & Thuyết minh từ ${srcNames.length} nguồn (${srcNames.join(', ')}) — lọc trùng, hỗ trợ IMDb & TMDB.`,
        types: ['movie', 'series'],
        resources: ['catalog', 'meta', 'stream'],
        idPrefixes: ['vn:', 'motchill:', 'tt', 'tmdb:'],
        catalogs: cats,
        behaviorHints: { configurable: true, configurationRequired: false }
      };
    }

    function renderAll() {
      // Top & Hero status
      const nEnabled = ENABLED_SERVER_SOURCE_IDS.length;
      document.getElementById('topStatus').textContent = `Sẵn sàng (${nEnabled} Nguồn)`;
      document.getElementById('heroSourcePill').textContent = `🎬 ${nEnabled} Nguồn Phim`;

      // Main & Shelf Catalogs
      const mainGrid = document.getElementById('mainCatalogsGrid');
      const shelfGrid = document.getElementById('shelfCatalogsGrid');
      mainGrid.innerHTML = '';
      shelfGrid.innerHTML = '';

      for (const c of CATALOG_DEFS) {
        const checked = selectedCatalogs.has(c.id);
        const card = document.createElement('label');
        card.className = 'check-card' + (checked ? ' checked' : '');
        const tagCls = c.type === 'movie' ? 'tag tag-movie' : 'tag tag-series';
        const tagTxt = c.type === 'movie' ? 'Phim lẻ' : 'Phim bộ';
        card.innerHTML = `
          <input type="checkbox" ${checked ? 'checked' : ''} />
          <div class="check-meta">
            <div class="check-name">${c.name} <span class="${tagCls}">${tagTxt}</span></div>
            <div class="check-desc">${c.desc}</div>
          </div>
        `;
        card.querySelector('input').onchange = (e) => {
          if (e.target.checked) {
            selectedCatalogs.add(c.id);
          } else {
            if (selectedCatalogs.size <= 1) {
              e.target.checked = true;
              showToast('⚠️ Cần giữ lại ít nhất 1 danh mục!');
              return;
            }
            selectedCatalogs.delete(c.id);
          }
          renderAll();
        };
        (c.group === 'main' ? mainGrid : shelfGrid).appendChild(card);
      }

      // Per-user Sources
      const srcGrid = document.getElementById('userSourcesGrid');
      srcGrid.innerHTML = '';
      for (const sid of ENABLED_SERVER_SOURCE_IDS) {
        const s = SERVER_SOURCES[sid];
        const checked = selectedSources.has(sid);
        const card = document.createElement('label');
        card.className = 'check-card' + (checked ? ' checked' : '');
        card.innerHTML = `
          <input type="checkbox" ${checked ? 'checked' : ''} />
          <div class="check-meta">
            <div class="check-name">${s.name || sid} <span class="tag">${s.type}</span></div>
            <div class="check-desc">${s.base_url || ''}</div>
          </div>
        `;
        card.querySelector('input').onchange = (e) => {
          if (e.target.checked) {
            selectedSources.add(sid);
          } else {
            if (selectedSources.size <= 1) {
              e.target.checked = true;
              showToast('⚠️ Cần giữ lại ít nhất 1 nguồn phim!');
              return;
            }
            selectedSources.delete(sid);
          }
          renderAll();
        };
        srcGrid.appendChild(card);
      }

      // Badges & Summary
      document.getElementById('badgeCatalogs').textContent = `${selectedCatalogs.size}/${CATALOG_DEFS.length}`;
      document.getElementById('badgeSources').textContent = `${selectedSources.size}/${ENABLED_SERVER_SOURCE_IDS.length}`;
      document.getElementById('summaryCount').textContent = `${selectedCatalogs.size} danh mục • ${selectedSources.size} nguồn`;

      const pillsBox = document.getElementById('selectedPills');
      pillsBox.innerHTML = '';
      for (const c of CATALOG_DEFS) {
        if (!selectedCatalogs.has(c.id)) continue;
        const sp = document.createElement('span');
        sp.className = 'sel-pill';
        sp.textContent = c.name.replace('VN Stream • ', '');
        pillsBox.appendChild(sp);
      }

      // Links & Preview
      const mUrl = currentManifestUrl();
      const sUrl = mUrl.replace(/^https?:\/\//, 'stremio://');
      const webUrl = 'https://web.stremio.com/#/addons?addon=' + encodeURIComponent(mUrl);

      document.getElementById('manifestUrl').textContent = mUrl;
      document.getElementById('heroStremioApp').href = sUrl;
      document.getElementById('botStremioApp').href = sUrl;
      document.getElementById('heroStremioWeb').href = webUrl;
      document.getElementById('botStremioWeb').href = webUrl;
      document.getElementById('manifestPreview').textContent = JSON.stringify(buildPreviewManifest(), null, 2);
    }

    function applyPreset(kind) {
      if (kind === 'default') {
        selectedCatalogs = new Set(DEFAULT_CATALOG_IDS);
        selectedSources = new Set(ENABLED_SERVER_SOURCE_IDS);
        showToast('🌟 Đã chọn Mặc định (Trung + Hàn, tất cả nguồn)');
      } else if (kind === 'full') {
        selectedCatalogs = new Set(CATALOG_DEFS.map(c => c.id));
        selectedSources = new Set(ENABLED_SERVER_SOURCE_IDS);
        showToast('🔥 Đã bật tất cả danh mục & hàng trang chủ');
      } else if (kind === 'movies') {
        selectedCatalogs = new Set(CATALOG_DEFS.filter(c => c.type === 'movie').map(c => c.id));
        showToast('🎬 Đã chọn Chỉ Phim Lẻ');
      } else if (kind === 'series') {
        selectedCatalogs = new Set(['vn-trung-quoc', 'vn-han-quoc', 'vn-bo', 'vn-thuyet-minh']);
        showToast('📺 Đã chọn Chỉ Phim Bộ');
      } else if (kind === 'lite') {
        selectedCatalogs = new Set(DEFAULT_CATALOG_IDS);
        const lite = ['motchillv', 'motchillti'].filter(id => ENABLED_SERVER_SOURCE_IDS.includes(id));
        selectedSources = new Set(lite.length ? lite : ENABLED_SERVER_SOURCE_IDS.slice(0, 2));
        showToast('⚡ Đã chọn Chỉ Motchill (MotchillV + MotchillTi)');
      }
      renderAll();
    }

    function toggleAllCatalogs() {
      if (selectedCatalogs.size === CATALOG_DEFS.length) {
        selectedCatalogs = new Set([DEFAULT_CATALOG_IDS[0]]);
        showToast('Đã thu gọn về 1 danh mục');
      } else {
        selectedCatalogs = new Set(CATALOG_DEFS.map(c => c.id));
        showToast('Đã bật tất cả danh mục');
      }
      renderAll();
    }

    function toggleAllUserSources() {
      if (selectedSources.size === ENABLED_SERVER_SOURCE_IDS.length) {
        selectedSources = new Set([ENABLED_SERVER_SOURCE_IDS[0]]);
      } else {
        selectedSources = new Set(ENABLED_SERVER_SOURCE_IDS);
      }
      renderAll();
    }

    async function copyManifest() {
      const mUrl = currentManifestUrl();
      try {
        await navigator.clipboard.writeText(mUrl);
        showToast('📋 Đã sao chép URL Manifest!');
      } catch {
        prompt('Sao chép URL Manifest:', mUrl);
      }
    }

    function openQr() {
      const mUrl = currentManifestUrl();
      document.getElementById('qrImg').src = 'https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=' + encodeURIComponent(mUrl);
      document.getElementById('qrUrlText').textContent = mUrl;
      document.getElementById('qrModal').classList.add('open');
    }
    function closeQr() {
      document.getElementById('qrModal').classList.remove('open');
    }

    // ---------- Admin Tab ----------
    const statusBox = document.getElementById('statusBox');
    function showAdminStatus(msg, isOk) {
      statusBox.textContent = msg;
      statusBox.className = 'status-box ' + (isOk ? 'ok' : 'err');
    }
    function adminHeaders() {
      const h = { 'Content-Type': 'application/json' };
      const pwEl = document.getElementById('adminPw');
      if (pwEl && pwEl.value.trim()) h['X-Admin-Password'] = pwEl.value.trim();
      return h;
    }
    function renderAdminSources() {
      const container = document.getElementById('adminSourcesContainer');
      container.innerHTML = '';
      for (const [sid, s] of Object.entries(SERVER_SOURCES)) {
        const div = document.createElement('div');
        div.className = 'admin-src';
        div.innerHTML = `
          <div class="admin-src-head">
            <label style="display:flex; align-items:center; gap:8px; font-weight:700; cursor:pointer;">
              <input type="checkbox" id="en_${sid}" ${s.enabled ? 'checked' : ''} />
              <span>${s.name || sid}</span>
              <span class="tag">${s.type}</span>
            </label>
            <button type="button" class="btn-ghost btn-sm" onclick="checkOne('${sid}')">Kiểm tra kết nối</button>
          </div>
          <label class="fld">Base URL</label>
          <input type="url" id="url_${sid}" value="${s.base_url || ''}" />
          <label class="fld">Domain dự phòng (cách nhau bằng dấu phẩy)</label>
          <input type="text" id="fb_${sid}" value="${(s.fallback_urls || []).join(', ')}" />
        `;
        container.appendChild(div);
      }
    }
    async function checkOne(sid) {
      const url = document.getElementById('url_' + sid).value.trim();
      showAdminStatus(`Đang kiểm tra ${sid} (${url}) ...`, true);
      try {
        const res = await fetch('/api/check', {
          method: 'POST', headers: adminHeaders(),
          body: JSON.stringify({ source_id: sid, url })
        });
        const d = await res.json();
        if (d.ok) {
          showAdminStatus(`[${sid}] Thành công! HTTP ${d.status_code} • ${d.movie_cards} phim • ${d.title || ''}`, true);
        } else {
          showAdminStatus(`[${sid}] Lỗi kết nối: ${d.error || ('HTTP ' + d.status_code)}`, false);
        }
      } catch (e) {
        showAdminStatus('Lỗi: ' + e.message, false);
      }
    }
    document.getElementById('btnSave').onclick = async () => {
      const updated = {};
      for (const [sid, s] of Object.entries(SERVER_SOURCES)) {
        const fbRaw = document.getElementById('fb_' + sid).value;
        updated[sid] = {
          ...s,
          enabled: document.getElementById('en_' + sid).checked,
          base_url: document.getElementById('url_' + sid).value.trim(),
          fallback_urls: fbRaw.split(',').map(x => x.trim()).filter(Boolean)
        };
      }
      const remote_config_url = document.getElementById('remoteUrl').value.trim();
      try {
        const res = await fetch('/api/config', {
          method: 'POST', headers: adminHeaders(),
          body: JSON.stringify({ sources: updated, remote_config_url })
        });
        const d = await res.json();
        if (d.ok) {
          showAdminStatus('Đã lưu cấu hình Server thành công! Đang tải lại...', true);
          setTimeout(() => location.reload(), 800);
        } else {
          showAdminStatus('Lỗi lưu: ' + d.error, false);
        }
      } catch (e) {
        showAdminStatus('Lỗi: ' + e.message, false);
      }
    };
    document.getElementById('btnSync').onclick = async () => {
      showAdminStatus('Đang đồng bộ cấu hình từ GitHub...', true);
      const res = await fetch('/api/sync-remote', { method: 'POST' });
      const d = await res.json();
      if (d.synced) {
        showAdminStatus('Đồng bộ từ GitHub thành công! Đang tải lại trang...', true);
        setTimeout(() => location.reload(), 900);
      } else {
        showAdminStatus('Không đồng bộ được: ' + (d.error || d.reason), false);
      }
    };

    renderAdminSources();
    renderAll();
  </script>
</body>
</html>
"""


async def configure_page(request: Request) -> HTMLResponse:
    cfg = config_store.get()
    addon_base = _get_addon_base(request)
    manifest_url = f"{addon_base}/manifest.json"
    user_cfg = _decode_user_config(request.path_params.get("cfg"))
    html = (
        CONFIG_HTML
        .replace("__ADDON_BASE_JSON__", json.dumps(addon_base, ensure_ascii=False))
        .replace("__ADDON_NAME_JSON__", json.dumps(cfg.get("addon_name") or "VN WebStream (Multi-Source)", ensure_ascii=False))
        .replace("__SOURCES_JSON__", json.dumps(cfg.get("sources", {}), ensure_ascii=False))
        .replace("__CATALOG_DEFS_JSON__", json.dumps(CATALOG_DEFS, ensure_ascii=False))
        .replace("__DEFAULT_CATALOG_IDS_JSON__", json.dumps(DEFAULT_CATALOG_IDS, ensure_ascii=False))
        .replace("__USER_CFG_JSON__", json.dumps(user_cfg, ensure_ascii=False))
        .replace("__REMOTE_URL__", cfg.get("remote_config_url", ""))
        .replace("__MANIFEST_URL__", manifest_url)
        .replace("__ADMIN_PW_DISPLAY__", "block" if os.getenv("ADMIN_PASSWORD", "").strip() else "none")
    )
    return HTMLResponse(html)


routes = [
    Route("/", configure_page),
    Route("/configure", configure_page),
    Route("/health", health_endpoint),
    # Default Stremio routes
    Route("/manifest.json", manifest_endpoint),
    Route("/catalog/{type}/{id}.json", catalog_endpoint),
    Route("/catalog/{type}/{id}/{extra:path}", catalog_endpoint),
    Route("/meta/{type}/{id:path}", meta_endpoint),
    Route("/stream/{type}/{id:path}", stream_endpoint),
    # HLS Proxy routes
    Route("/hls/cloudbeta.m3u8", hls_cloudbeta_endpoint),
    Route("/hls/streamc.m3u8", hls_streamc_endpoint),
    Route("/hls/raw.m3u8", hls_raw_m3u8_endpoint),
    Route("/hls/seg.ts", hls_segment_endpoint),
    # Admin API
    Route("/api/config", api_get_config, methods=["GET"]),
    Route("/api/config", api_update_config, methods=["POST"]),
    Route("/api/check", api_check_url, methods=["POST"]),
    Route("/api/sync-remote", api_sync_remote, methods=["POST"]),
    # Per-user encoded Stremio routes (/<base64url>/...)
    Route("/{cfg}/configure", configure_page),
    Route("/{cfg}/manifest.json", manifest_endpoint),
    Route("/{cfg}/catalog/{type}/{id}.json", catalog_endpoint),
    Route("/{cfg}/catalog/{type}/{id}/{extra:path}", catalog_endpoint),
    Route("/{cfg}/meta/{type}/{id:path}", meta_endpoint),
    Route("/{cfg}/stream/{type}/{id:path}", stream_endpoint),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    cfg = config_store.get()
    host = os.getenv("HOST", cfg.get("host", "0.0.0.0"))
    port = int(os.getenv("PORT", cfg.get("port", 7000)))
    print(f"VN WebStream Stremio Addon v2.0 dang chay tai: http://127.0.0.1:{port}")
    print(f"Trang cau hinh da nguon:                      http://127.0.0.1:{port}/configure")
    print(f"Manifest URL cho Stremio:                     http://127.0.0.1:{port}/manifest.json")
    uvicorn.run(app, host=host, port=port, log_level="info")
