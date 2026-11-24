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


def _build_manifest() -> dict[str, Any]:
    cfg = config_store.get()
    addon_name = cfg.get("addon_name") or "VN WebStream (Multi-Source)"
    enabled_names = [
        s.get("name", sid)
        for sid, s in cfg.get("sources", {}).items()
        if s.get("enabled")
    ]
    genre_options = [g for g in GENRE_MAP.keys() if g != "Hoạt Hình"]
    return {
        "id": "vn.webstream.multisource",
        "version": "2.5.0",
        "name": addon_name,
        "description": (
            f"Phim Trung Quốc & Hàn Quốc phát trực tiếp HTTP HLS trên Stremio/Nuvio từ {len(enabled_names)} nguồn "
            f"({', '.join(enabled_names)}) — lọc trùng, bỏ hoạt hình, hỗ trợ khớp IMDb (tt) & TMDB."
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
        "catalogs": [
            {
                "type": "series",
                "id": "vn-trung-quoc",
                "name": "VN Stream • Phim Trung Quốc",
                "extra": [
                    {"name": "search", "isRequired": False},
                    {"name": "genre", "isRequired": False, "options": genre_options},
                    {"name": "skip", "isRequired": False},
                ],
            },
            {
                "type": "series",
                "id": "vn-han-quoc",
                "name": "VN Stream • Phim Hàn Quốc",
                "extra": [
                    {"name": "search", "isRequired": False},
                    {"name": "genre", "isRequired": False, "options": genre_options},
                    {"name": "skip", "isRequired": False},
                ],
            },
        ],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False,
        },
    }


# ---------- Stremio Protocol Routes ----------

async def health_endpoint(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "2.0.0"})


async def manifest_endpoint(_request: Request) -> JSONResponse:
    assert http_client is not None
    await config_store.sync_remote_if_due(http_client)
    return JSONResponse(_build_manifest())


async def catalog_endpoint(request: Request) -> JSONResponse:
    assert aggregator is not None and http_client is not None
    await config_store.sync_remote_if_due(http_client)
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
    content_type = request.path_params["type"]
    item_id = unquote(request.path_params["id"].removesuffix(".json"))

    if not item_id.startswith(("vn:", "motchill:", "tt", "tmdb:")):
        return JSONResponse({"streams": []})

    addon_base = _get_addon_base(request)
    try:
        servers = await aggregator.get_all_watch_servers(item_id, content_type=content_type)
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


CONFIG_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>VN WebStream — Stremio Multi-Source Addon</title>
  <style>
    :root {
      --bg: #0f172a; --card: #1e293b; --border: #334155;
      --text: #f1f5f9; --muted: #94a3b8; --accent: #6366f1;
      --accent-hover: #4f46e5; --ok: #10b981; --err: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg); color: var(--text); padding: 24px;
    }
    .wrap { max-width: 820px; margin: 0 auto; }
    .card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 14px; padding: 22px; margin-bottom: 18px;
    }
    h1 { margin: 0 0 6px; font-size: 21px; }
    h2 { margin: 0 0 12px; font-size: 16px; color: #e2e8f0; }
    p.sub { margin: 0 0 16px; color: var(--muted); font-size: 14px; }
    label { display: block; font-size: 13px; font-weight: 600; margin: 10px 0 4px; color: #cbd5e1; }
    input[type="url"], input[type="text"], textarea {
      width: 100%; background: #0f172a; color: var(--text);
      border: 1px solid var(--border); border-radius: 8px;
      padding: 9px 12px; font-size: 14px; font-family: inherit;
    }
    .source-box {
      border: 1px solid var(--border); border-radius: 10px;
      padding: 14px; margin-bottom: 12px; background: rgba(15,23,42,0.45);
    }
    .source-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .source-title { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 15px; }
    .badge {
      font-size: 11px; padding: 2px 8px; border-radius: 999px;
      background: #334155; color: #cbd5e1; font-weight: 600;
    }
    .row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    button, a.btn {
      display: inline-flex; align-items: center; justify-content: center;
      padding: 9px 15px; border-radius: 8px; font-size: 13px; font-weight: 600;
      border: none; cursor: pointer; text-decoration: none;
      background: var(--accent); color: #fff;
    }
    button:hover, a.btn:hover { background: var(--accent-hover); }
    button.sec { background: #334155; }
    button.sec:hover { background: #475569; }
    .status {
      margin-top: 12px; padding: 10px 12px; border-radius: 8px;
      font-size: 13px; display: none; white-space: pre-wrap;
    }
    .status.ok { display: block; background: rgba(16,185,129,0.15); border: 1px solid var(--ok); color: #6ee7b7; }
    .status.err { display: block; background: rgba(239,68,68,0.15); border: 1px solid var(--err); color: #fca5a5; }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      background: #0f172a; padding: 10px 12px; border-radius: 8px;
      border: 1px solid var(--border); word-break: break-all; font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🎬 VN WebStream — Stremio Multi-Source Addon (v2.5)</h1>
      <p class="sub">Tổng hợp nhiều nguồn phim (KKPhim, OPhim, NguonC, MotchillV, MotchillTi), tự động lọc phim trùng lặp và gộp nhiều server phát trực tiếp trên Stremio.</p>
      <div id="sourcesContainer"></div>
      <div id="adminPwBox" style="display:__ADMIN_PW_DISPLAY__; margin-top:12px;">
        <label for="adminPw">🔐 Mật khẩu quản trị (ADMIN_PASSWORD)</label>
        <input id="adminPw" type="password" placeholder="Nhập ADMIN_PASSWORD để lưu hoặc kiểm tra kết nối" />
      </div>
      <div class="row">
        <button id="btnSave" type="button">💾 Lưu tất cả cấu hình nguồn</button>
      </div>
      <div id="statusBox" class="status"></div>
    </div>

    <div class="card">
      <h2>☁️ Đồng bộ tên miền tự động từ GitHub (khi chạy trên Cloud)</h2>
      <p class="sub">Dán link Raw của file <code>domains.json</code> trên GitHub để đổi tên miền định kỳ từ xa mà không cần deploy lại.</p>
      <input id="remoteUrl" type="url" placeholder="https://raw.githubusercontent.com/username/repo/main/domains.json" value="__REMOTE_URL__" />
      <div class="row">
        <button id="btnSync" class="sec" type="button">🔄 Đồng bộ ngay từ GitHub</button>
      </div>
    </div>

    <div class="card">
      <h2>📺 Cài đặt vào Stremio</h2>
      <p class="sub">Dán URL Manifest vào <b>Addons → Add Addon</b> trong Stremio hoặc bấm nút cài nhanh:</p>
      <div class="mono" id="manifestUrl">__MANIFEST_URL__</div>
      <div class="row">
        <a class="btn" href="__STREMIO_URL__">Cài đặt vào Stremio</a>
        <button class="sec" id="btnCopy" type="button">Sao chép URL Manifest</button>
      </div>
    </div>
  </div>

  <script>
    const INITIAL_SOURCES = __SOURCES_JSON__;
    const statusBox = document.getElementById('statusBox');
    function showStatus(msg, isOk) {
      statusBox.textContent = msg;
      statusBox.className = 'status ' + (isOk ? 'ok' : 'err');
    }

    function renderSources() {
      const container = document.getElementById('sourcesContainer');
      container.innerHTML = '';
      for (const [sid, s] of Object.entries(INITIAL_SOURCES)) {
        const div = document.createElement('div');
        div.className = 'source-box';
        div.innerHTML = `
          <div class="source-head">
            <div class="source-title">
              <input type="checkbox" id="en_${sid}" ${s.enabled ? 'checked' : ''} />
              <span>${s.name || sid}</span>
              <span class="badge">${s.type}</span>
            </div>
            <button type="button" class="sec" onclick="checkOne('${sid}')">Kiểm tra kết nối</button>
          </div>
          <label>Base URL</label>
          <input type="url" id="url_${sid}" value="${s.base_url || ''}" />
          <label>Domain dự phòng (cách nhau bằng dấu phẩy)</label>
          <input type="text" id="fb_${sid}" value="${(s.fallback_urls || []).join(', ')}" />
        `;
        container.appendChild(div);
      }
    }

    function adminHeaders() {
      const h = {'Content-Type': 'application/json'};
      const pwEl = document.getElementById('adminPw');
      if (pwEl && pwEl.value.trim()) h['X-Admin-Password'] = pwEl.value.trim();
      return h;
    }

    async function checkOne(sid) {
      const url = document.getElementById('url_' + sid).value.trim();
      showStatus(`Đang kiểm tra ${sid} (${url}) ...`, true);
      try {
        const res = await fetch('/api/check', {
          method: 'POST', headers: adminHeaders(),
          body: JSON.stringify({ source_id: sid, url })
        });
        const d = await res.json();
        if (d.ok) {
          showStatus(`[${sid}] Thành công! HTTP ${d.status_code} • ${d.movie_cards} phim • ${d.title || ''}`, true);
        } else {
          showStatus(`[${sid}] Lỗi kết nối: ${d.error || ('HTTP ' + d.status_code)}`, false);
        }
      } catch (e) {
        showStatus('Lỗi: ' + e.message, false);
      }
    }

    document.getElementById('btnSave').onclick = async () => {
      const updated = {};
      for (const [sid, s] of Object.entries(INITIAL_SOURCES)) {
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
          showStatus('Đã lưu cấu hình đa nguồn thành công! Hiệu lực ngay trên Stremio.', true);
        } else {
          showStatus('Lỗi lưu: ' + d.error, false);
        }
      } catch (e) {
        showStatus('Lỗi: ' + e.message, false);
      }
    };

    document.getElementById('btnSync').onclick = async () => {
      showStatus('Đang đồng bộ cấu hình từ GitHub...', true);
      const res = await fetch('/api/sync-remote', { method: 'POST' });
      const d = await res.json();
      if (d.synced) {
        showStatus('Đồng bộ từ GitHub thành công! Đang tải lại trang...', true);
        setTimeout(() => location.reload(), 900);
      } else {
        showStatus('Không đồng bộ được: ' + (d.error || d.reason), false);
      }
    };

    document.getElementById('btnCopy').onclick = async () => {
      await navigator.clipboard.writeText(document.getElementById('manifestUrl').textContent.trim());
      showStatus('Đã sao chép URL Manifest!', true);
    };

    renderSources();
  </script>
</body>
</html>
"""


async def configure_page(request: Request) -> HTMLResponse:
    cfg = config_store.get()
    addon_base = _get_addon_base(request)
    manifest_url = f"{addon_base}/manifest.json"
    stremio_url = manifest_url.replace("https://", "stremio://").replace("http://", "stremio://")
    html = (
        CONFIG_HTML
        .replace("__SOURCES_JSON__", json.dumps(cfg.get("sources", {}), ensure_ascii=False))
        .replace("__REMOTE_URL__", cfg.get("remote_config_url", ""))
        .replace("__MANIFEST_URL__", manifest_url)
        .replace("__STREMIO_URL__", stremio_url)
        .replace("__ADMIN_PW_DISPLAY__", "block" if os.getenv("ADMIN_PASSWORD", "").strip() else "none")
    )
    return HTMLResponse(html)


routes = [
    Route("/", configure_page),
    Route("/configure", configure_page),
    Route("/health", health_endpoint),
    # Stremio routes
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
