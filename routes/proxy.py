"""Route handlers and streaming upstream relay for the three preset target websites."""

import asyncio
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

from middleware.headers import (
    decode_subpath,
    encode_subpath,
    rewrite_html_payload,
    sanitize_request_headers,
    sanitize_response_headers,
)

logger = logging.getLogger("specific-site-proxy")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def log_event(event: str, **kwargs) -> None:
    """Emit structured JSON log entries for upstream proxy telemetry."""
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **kwargs,
    }
    logger.info(json.dumps(payload))


def get_target_registry() -> dict:
    """Return the fixed allowlist of target websites mapped to keyword-free route aliases."""
    ig_url = os.getenv("TARGET_ALPHA_URL", "https://www.instagram.com").rstrip("/")
    yt_url = os.getenv("TARGET_BETA_URL", "https://www.youtube.com").rstrip("/")
    tt_url = os.getenv("TARGET_GAMMA_URL", "https://www.tiktok.com").rstrip("/")

    return {
        "alpha": {
            "id": "alpha",
            "short_code": "ig",
            "display_label": "Visual Feed (Instagram)",
            "upstream": ig_url,
            "domains": ["instagram.com", "cdninstagram.com"],
            "launch_path": "/s/alpha",
            "encoded_root": f"/r/alpha/{encode_subpath('/?hl=en')}",
        },
        "beta": {
            "id": "beta",
            "short_code": "yt",
            "display_label": "Video Hub (YouTube)",
            "upstream": yt_url,
            "domains": ["youtube.com", "youtu.be", "ytimg.com", "googlevideo.com"],
            "launch_path": "/s/beta",
            "encoded_root": f"/r/beta/{encode_subpath('/?gl=US')}",
        },
        "gamma": {
            "id": "gamma",
            "short_code": "tt",
            "display_label": "Short Clips (TikTok)",
            "upstream": tt_url,
            "domains": ["tiktok.com", "tiktokcdn.com"],
            "launch_path": "/s/gamma",
            "encoded_root": f"/r/gamma/{encode_subpath('/explore')}",
        },
    }


# Map short aliases (ig, yt, tt) to canonical keyword-free route keys (alpha, beta, gamma)
SHORT_CODE_ALIASES = {
    "alpha": "alpha",
    "ig": "alpha",
    "beta": "beta",
    "yt": "beta",
    "gamma": "gamma",
    "tt": "gamma",
}


def resolve_target(key: str):
    """Resolve an alias or short code to its fixed target configuration, or None if not allowlisted."""
    registry = get_target_registry()
    canonical = SHORT_CODE_ALIASES.get((key or "").lower())
    if not canonical:
        return None, registry
    return registry.get(canonical), registry


def build_upstream_url(target_info: dict, subpath: str = "/", query_string: str = "") -> str:
    """Construct the exact upstream URL within the allowlisted target origin."""
    clean_path = subpath if subpath.startswith("/") else f"/{subpath}"
    base_url = target_info["upstream"].rstrip("/")
    full_url = f"{base_url}{clean_path}"
    if query_string:
        separator = "&" if "?" in full_url else "?"
        full_url = f"{full_url}{separator}{query_string}"
    return full_url


async def forward_with_httpx(
    method: str,
    target_key: str,
    subpath: str,
    query_string: str,
    headers: dict,
    body: bytes = b"",
):
    """Stream upstream requests asynchronously using httpx.AsyncClient with retry backoff."""
    import httpx
    from fastapi.responses import Response, StreamingResponse

    target, registry = resolve_target(target_key)
    if not target:
        return Response(
            content=json.dumps({
                "error": "Target not allowlisted",
                "message": "This proxy opens only the preset target websites (alpha, beta, gamma) and blocks arbitrary URL unblocking.",
            }),
            status_code=403,
            media_type="application/json",
        )

    upstream_url = build_upstream_url(target, subpath, query_string)
    req_headers = sanitize_request_headers(headers, upstream_url)
    timeout_sec = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "15.0"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    backoff_base = float(os.getenv("RETRY_BACKOFF_BASE_SECONDS", "0.5"))

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec),
                follow_redirects=False,
                verify=True,
            )
            req = client.build_request(
                method=method,
                url=upstream_url,
                headers=req_headers,
                content=body if body else None,
            )
            upstream_resp = await client.send(req, stream=True)
            resp_headers = sanitize_response_headers(
                dict(upstream_resp.headers),
                target["id"],
                registry,
            )
            content_type = upstream_resp.headers.get("content-type", "")

            log_event(
                "upstream_response",
                alias=target["id"],
                method=method,
                status=upstream_resp.status_code,
                attempt=attempt,
                content_type=content_type,
            )

            # Rewrite HTML payloads so embedded links stay on keyword-free proxy routes
            if "text/html" in content_type.lower():
                raw_bytes = await upstream_resp.aread()
                await upstream_resp.aclose()
                await client.aclose()
                rewritten = rewrite_html_payload(raw_bytes, target["id"], registry)
                return Response(
                    content=rewritten,
                    status_code=upstream_resp.status_code,
                    headers=resp_headers,
                    media_type="text/html; charset=utf-8",
                )

            async def _stream_chunks():
                try:
                    async for chunk in upstream_resp.aiter_bytes(chunk_size=32768):
                        yield chunk
                finally:
                    await upstream_resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                _stream_chunks(),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=content_type or "application/octet-stream",
            )
        except Exception as exc:
            last_exc = exc
            log_event(
                "upstream_retry",
                alias=target["id"],
                attempt=attempt,
                max_retries=max_retries,
                error=str(exc),
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))

    log_event("upstream_failed", alias=target["id"], error=str(last_exc))
    return Response(
        content=json.dumps({
            "error": "Upstream connection failed",
            "alias": target["id"],
            "details": str(last_exc),
        }),
        status_code=502,
        media_type="application/json",
    )


def forward_with_urllib(
    method: str,
    target_key: str,
    subpath: str,
    query_string: str,
    headers: dict,
    body: bytes = b"",
) -> tuple:
    """Stdlib fallback relay with retry backoff when httpx/fastapi are not installed."""
    target, registry = resolve_target(target_key)
    if not target:
        payload = json.dumps({
            "error": "Target not allowlisted",
            "message": "This proxy opens only the preset target websites (alpha, beta, gamma) and blocks arbitrary URL unblocking.",
        }).encode("utf-8")
        return 403, {"Content-Type": "application/json"}, payload

    upstream_url = build_upstream_url(target, subpath, query_string)
    req_headers = sanitize_request_headers(headers, upstream_url)
    timeout_sec = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "15.0"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    backoff_base = float(os.getenv("RETRY_BACKOFF_BASE_SECONDS", "0.5"))

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                url=upstream_url,
                data=body if body and method not in ("GET", "HEAD") else None,
                headers=req_headers,
                method=method,
            )
            with opener.open(req, timeout=timeout_sec) as resp:
                status = resp.getcode()
                raw_headers = dict(resp.info().items())
                raw_body = resp.read()
        except urllib.error.HTTPError as http_err:
            status = http_err.code
            raw_headers = dict(http_err.headers.items()) if http_err.headers else {}
            raw_body = http_err.read() if http_err.fp else b""
        except Exception as exc:
            last_exc = exc
            log_event(
                "upstream_retry",
                alias=target["id"],
                attempt=attempt,
                max_retries=max_retries,
                error=str(exc),
            )
            if attempt < max_retries:
                time.sleep(backoff_base * (2 ** (attempt - 1)))
            continue

        resp_headers = sanitize_response_headers(raw_headers, target["id"], registry)
        content_type = raw_headers.get("Content-Type", raw_headers.get("content-type", ""))
        if "text/html" in content_type.lower():
            raw_body = rewrite_html_payload(raw_body, target["id"], registry)
            resp_headers["Content-Type"] = "text/html; charset=utf-8"

        log_event(
            "upstream_response",
            alias=target["id"],
            method=method,
            status=status,
            attempt=attempt,
        )
        return status, resp_headers, raw_body

    log_event("upstream_failed", alias=target["id"], error=str(last_exc))
    err_payload = json.dumps({
        "error": "Upstream connection failed",
        "alias": target["id"],
        "details": str(last_exc),
    }).encode("utf-8")
    return 502, {"Content-Type": "application/json"}, err_payload
