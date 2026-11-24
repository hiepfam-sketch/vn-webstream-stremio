from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from providers.base import StreamServer

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
SAFARI_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# CloudBeta AES-256-CBC keys (từ player.cloudbeta.win)
CB_KEY_PLAYLIST = b"a4cxe8d2f6b703915e2a4c8d9b1f6073"
CB_KEY_TOKEN = b"7e1b9c4axf806d5e3a9c7b4d1f2e6085"
CB_IV = b"3a9f5c8e1b4d2074"


# Secret ký HMAC cho các URL đi qua HLS Proxy (chống Open Proxy / SSRF)
_PROXY_SECRET: bytes = (
    os.getenv("PROXY_SECRET", "").encode("utf-8") or secrets.token_bytes(32)
)


def b64u_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64u_decode(token: str) -> str:
    pad = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")


def sign_proxy_params(u: str, r: str = "") -> str:
    msg = f"{u}|{r}".encode("utf-8")
    digest = hmac.new(_PROXY_SECRET, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:24]


def verify_proxy_params(u: str, r: str, sig: str) -> bool:
    if not u or not sig:
        return False
    expected = sign_proxy_params(u, r or "")
    return hmac.compare_digest(expected, sig)


def is_safe_http_url(url: str) -> bool:
    """Chỉ cho phép HTTP/HTTPS tới host công khai, chặn IP nội bộ / localhost (chống SSRF)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower().strip(".")
        if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".internal"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def build_proxy_url(addon_base: str, endpoint: str, target_url: str, referer: str = "") -> str:
    u = b64u_encode(target_url)
    r = b64u_encode(referer) if referer else ""
    sig = sign_proxy_params(u, r)
    if r:
        return f"{addon_base}{endpoint}?u={u}&r={r}&sig={sig}"
    return f"{addon_base}{endpoint}?u={u}&sig={sig}"


def _aes_cbc_decrypt(b64_str: str, key: bytes, iv: bytes) -> str:
    raw = base64.b64decode(b64_str)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    padded = dec.update(raw) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def strip_ts_prefix(data: bytes) -> bytes:
    """Loại bỏ header ảnh giả (PNG/JPG...) đứng trước MPEG-TS sync byte 0x47."""
    if not data or data[0] == 0x47:
        return data
    limit = min(len(data), 4096)
    for i in range(limit):
        if data[i] != 0x47:
            continue
        if i + 376 < len(data):
            if data[i + 188] == 0x47 and data[i + 376] == 0x47:
                return data[i:]
        elif i + 188 < len(data):
            if data[i + 188] == 0x47:
                return data[i:]
    return data


def rewrite_m3u8_segments(
    m3u8_text: str,
    playlist_url: str,
    addon_base: str,
    *,
    referer: str = "",
) -> str:
    """Viết lại các URL segment/sub-playlist trong M3U8 để đi qua proxy có chữ ký HMAC."""
    out_lines: list[str] = []
    for raw_line in m3u8_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Nếu là EXT-X-KEY hoặc EXT-X-MAP có URI="..."
            if 'URI="' in line:
                def _repl(m: re.Match[str]) -> str:
                    abs_u = urljoin(playlist_url, m.group(1))
                    return f'URI="{build_proxy_url(addon_base, "/hls/seg.ts", abs_u, referer)}"'
                line = re.sub(r'URI="([^"]+)"', _repl, line)
            out_lines.append(line)
            continue

        abs_url = urljoin(playlist_url, line)
        if ".m3u8" in abs_url.lower():
            out_lines.append(build_proxy_url(addon_base, "/hls/raw.m3u8", abs_url, referer))
        else:
            out_lines.append(build_proxy_url(addon_base, "/hls/seg.ts", abs_url, referer))
    return "\n".join(out_lines) + "\n"


async def fetch_cloudbeta_m3u8(
    client: httpx.AsyncClient,
    em3u8_url: str,
    addon_base: str,
) -> str:
    """Tải và giải mã file em3u8 của CloudBeta thành playlist HLS chuẩn."""
    resp = await client.get(em3u8_url, headers={"User-Agent": DESKTOP_UA}, timeout=15)
    resp.raise_for_status()
    lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("CloudBeta em3u8 trống")

    enc_blob = lines[-1]
    b64_m3u8 = _aes_cbc_decrypt(enc_blob, CB_KEY_PLAYLIST, CB_IV)
    plain_m3u8 = base64.b64decode(b64_m3u8).decode("utf-8")

    decoded_lines: list[str] = []
    for raw_line in plain_m3u8.splitlines():
        line = raw_line.strip()
        if line.startswith("http"):
            qs = parse_qs(urlparse(line).query)
            if "token" in qs:
                try:
                    tok = unquote(qs["token"][0])
                    line = _aes_cbc_decrypt(tok, CB_KEY_TOKEN, CB_IV)
                except Exception:
                    pass
        decoded_lines.append(line)

    decrypted_text = "\n".join(decoded_lines)
    return rewrite_m3u8_segments(decrypted_text, em3u8_url, addon_base)


async def fetch_streamc_m3u8(
    client: httpx.AsyncClient,
    embed_url: str,
    site_referer: str,
    addon_base: str,
) -> str:
    """Gọi API bootstrap của StreamC để lấy playlist HLS gốc và viết lại qua proxy."""
    parsed = urlparse(embed_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    ref_parsed = urlparse(site_referer or "https://motchillv.online/")
    site_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}"

    payload = {
        "action": "bootstrap",
        "referrer": site_origin + "/",
        "frame_origins": [site_origin],
        "request_grant": True,
        "playlist_format": "hls",
        "pretty_url": True,
        "path_chunks": True,
    }
    headers = {
        "Origin": origin,
        "Referer": embed_url,
        "User-Agent": SAFARI_MOBILE_UA,
    }
    r_boot = await client.post(embed_url, json=payload, headers=headers, timeout=15)
    r_boot.raise_for_status()
    data = r_boot.json()
    preissued = data.get("preissued") or {}
    playlist_url = preissued.get("playlist")
    if not playlist_url:
        raise ValueError("StreamC không trả về preissued.playlist")

    r_pl = await client.get(
        playlist_url,
        headers={
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": SAFARI_MOBILE_UA,
        },
        timeout=15,
    )
    r_pl.raise_for_status()
    if "#EXTM3U" not in r_pl.text:
        raise ValueError("StreamC playlist không hợp lệ")
    return rewrite_m3u8_segments(r_pl.text, playlist_url, addon_base, referer=origin + "/")


@dataclass
class ResolvedStream:
    name: str
    title: str
    url: str
    not_web_ready: bool = False
    headers: dict[str, str] | None = None

    def to_stremio(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "url": self.url,
        }
        hints: dict[str, Any] = {}
        if self.not_web_ready:
            hints["notWebReady"] = True
        if self.headers:
            hints["proxyHeaders"] = {"request": self.headers}
        if hints:
            out["behaviorHints"] = hints
        return out


async def _resolve_one(
    client: httpx.AsyncClient,
    sv: StreamServer,
    addon_base: str,
) -> ResolvedStream | None:
    link = sv.link
    src = sv.source_name or "WebStream"
    host = urlparse(link).netloc.lower()

    # Nếu embed chỉ là wrapper dạng ?url=https://...m3u8 (vd: player.phimapi.com)
    qs = parse_qs(urlparse(link).query)
    if "url" in qs and ".m3u8" in qs["url"][0].lower():
        link = unquote(qs["url"][0])
        host = urlparse(link).netloc.lower()

    # 0. Direct MP4/MKV file (ví dụ: FMovies / VidZee direct file)
    if sv.server_type == "direct_file":
        req_headers = {"User-Agent": DESKTOP_UA}
        if sv.referer:
            req_headers["Referer"] = sv.referer
        if sv.headers:
            req_headers.update(sv.headers)
        if sv.skip_verify:
            return ResolvedStream(
                name=f"{src}\n{sv.label}",
                title=f"Direct Video ({host})",
                url=link,
                not_web_ready=bool(sv.headers),
                headers=req_headers if sv.headers else None,
            )
        try:
            r = await client.head(link, headers=req_headers, timeout=8)
            if r.status_code in (200, 206, 302, 405):
                return ResolvedStream(
                    name=f"{src}\n{sv.label}",
                    title=f"Direct Video ({host})",
                    url=link,
                    not_web_ready=bool(sv.headers),
                    headers=req_headers if sv.headers else None,
                )
        except Exception:
            return None
        return None

    # 1. Direct M3U8 (ví dụ: KKPhim, OPhim, cachevideo.com, motchillti TOP #1, FMovies/VidNest...)
    if sv.server_type == "m3u8" or ".m3u8" in link.lower():
        req_headers = {"User-Agent": DESKTOP_UA}
        if sv.referer:
            req_headers["Referer"] = sv.referer
        if sv.headers:
            req_headers.update(sv.headers)
        if sv.skip_verify:
            return ResolvedStream(
                name=f"{src}\n{sv.label}",
                title=f"Direct HLS ({host})",
                url=link,
                not_web_ready=bool(sv.headers),
                headers=req_headers if sv.headers else None,
            )
        try:
            r = await client.get(
                link,
                headers=req_headers,
                timeout=8,
            )
            if r.status_code == 200 and "#EXTM3U" in r.text[:200]:
                if any(ext in r.text for ext in (".png", ".jpg", ".jpeg", ".image")):
                    proxy_u = build_proxy_url(addon_base, "/hls/raw.m3u8", link, req_headers.get("Referer", sv.referer))
                    return ResolvedStream(
                        name=f"{src}\n{sv.label}",
                        title=f"HLS Proxy ({host})",
                        url=proxy_u,
                    )
                return ResolvedStream(
                    name=f"{src}\n{sv.label}",
                    title=f"Direct HLS ({host})",
                    url=link,
                    not_web_ready=bool(sv.headers),
                    headers=req_headers if sv.headers else None,
                )
        except Exception:
            return None
        return None

    # 2. StreamC embed (embed*.streamc.xyz)
    if "streamc." in host:
        try:
            parsed = urlparse(link)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            ref_parsed = urlparse(sv.referer or "https://motchillv.online/")
            site_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}"
            r_boot = await client.post(
                link,
                json={
                    "action": "bootstrap",
                    "referrer": site_origin + "/",
                    "frame_origins": [site_origin],
                    "request_grant": True,
                    "playlist_format": "hls",
                    "pretty_url": True,
                    "path_chunks": True,
                },
                headers={
                    "Origin": origin,
                    "Referer": link,
                    "User-Agent": SAFARI_MOBILE_UA,
                },
                timeout=10,
            )
            if r_boot.status_code == 200:
                data = r_boot.json()
                playlist_url = (data.get("preissued") or {}).get("playlist")
                if playlist_url:
                    return ResolvedStream(
                        name=f"{src}\n{sv.label}",
                        title=f"StreamC Direct ({host})",
                        url=playlist_url,
                        not_web_ready=True,
                        headers={
                            "Origin": origin,
                            "Referer": origin + "/",
                            "User-Agent": SAFARI_MOBILE_UA,
                        },
                    )
        except Exception:
            return None
        return None

    # 3. CloudBeta & mirrors (player.cloudbeta.win, player.cbplay.cc, ...)
    if "cloudbeta." in host or "cbplay." in host:
        try:
            r = await client.get(
                link,
                headers={"User-Agent": DESKTOP_UA, "Referer": sv.referer},
                timeout=10,
            )
            if r.status_code == 200:
                m_em3u8 = re.search(
                    r"https?://[^\s\"'<>\\]+/file/em3u8/[^\s\"'<>\\]+\.m3u8",
                    r.text,
                )
                if m_em3u8:
                    em3u8_url = m_em3u8.group(0)
                    proxy_u = build_proxy_url(addon_base, "/hls/cloudbeta.m3u8", em3u8_url)
                    return ResolvedStream(
                        name=f"{src}\n{sv.label}",
                        title=f"CloudBeta HD ({host})",
                        url=proxy_u,
                    )
        except Exception:
            return None
        return None

    # 3b. StreamVSMov embed (v*.streamvsmov.com/video/<hash>)
    if "streamvsmov." in host:
        try:
            parsed = urlparse(link)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            vhash = parts[-1] if parts else ""
            if vhash:
                master_url = f"{origin}/stream/{vhash}/master.m3u8"
                chk = await client.get(
                    master_url,
                    headers={"User-Agent": DESKTOP_UA, "Referer": origin + "/"},
                    timeout=10,
                )
                if chk.status_code == 200 and "#EXTM3U" in chk.text[:200]:
                    proxy_u = build_proxy_url(addon_base, "/hls/raw.m3u8", master_url, origin + "/")
                    return ResolvedStream(
                        name=f"{src}\n{sv.label}",
                        title=f"VSMov HD ({host})",
                        url=proxy_u,
                    )
        except Exception:
            return None
        return None

    # 4. Generic embed fallback: kiểm tra em3u8 trước, rồi mới tìm link .m3u8 trực tiếp (và xác thực #EXTM3U)
    try:
        r = await client.get(
            link,
            headers={"User-Agent": DESKTOP_UA, "Referer": sv.referer},
            timeout=8,
        )
        if r.status_code == 200:
            m_em3u8 = re.search(
                r"https?://[^\s\"'<>\\]+/file/em3u8/[^\s\"'<>\\]+\.m3u8",
                r.text,
            )
            if m_em3u8:
                em3u8_url = m_em3u8.group(0)
                proxy_u = f"{addon_base}/hls/cloudbeta.m3u8?u={b64u_encode(em3u8_url)}"
                return ResolvedStream(
                    name=f"{src}\n{sv.label}",
                    title=f"CloudBeta HD ({host})",
                    url=proxy_u,
                )
            for m_direct in re.finditer(r"https?://[^\s\"'<>\\]+\.m3u8[^\s\"'<>\\]*", r.text):
                found = m_direct.group(0)
                if "/file/play/" in found:
                    continue
                chk = await client.get(
                    found,
                    headers={"User-Agent": DESKTOP_UA, "Referer": link},
                    timeout=6,
                )
                if chk.status_code == 200 and "#EXTM3U" in chk.text[:200]:
                    return ResolvedStream(
                        name=f"{src}\n{sv.label}",
                        title=f"HLS ({host})",
                        url=found,
                    )
    except Exception:
        pass
    return None


async def resolve_servers(
    client: httpx.AsyncClient,
    servers: list[StreamServer],
    addon_base: str,
) -> list[dict[str, Any]]:
    """Giải mã song song các server phát, loại bỏ URL stream trùng lặp và sắp xếp theo độ ổn định."""
    if not servers:
        return []
    tasks = [_resolve_one(client, sv, addon_base) for sv in servers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    resolved: list[ResolvedStream] = []
    seen_urls: set[str] = set()
    for r in results:
        if isinstance(r, ResolvedStream) and r.url not in seen_urls:
            seen_urls.add(r.url)
            resolved.append(r)

    def _priority(item: ResolvedStream) -> int:
        t = item.title.lower()
        if "direct hls" in t:
            return 0
        if "cloudbeta" in t:
            return 1
        if "hls proxy" in t:
            return 2
        return 3

    resolved.sort(key=_priority)
    return [r.to_stremio() for r in resolved]
