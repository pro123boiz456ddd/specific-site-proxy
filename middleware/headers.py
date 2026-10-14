"""Middleware for hop-by-hop header stripping, Host rewriting, CORS, and keyword-free path encoding."""

import base64
import re
from urllib.parse import urlparse, urljoin

# RFC 2616 / RFC 7230 hop-by-hop headers that must not be forwarded blindly by a proxy
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

# Upstream response headers removed so pages render cleanly through the local relay
STRIP_RESPONSE_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
    "strict-transport-security",
    "content-encoding",
}


def encode_subpath(raw_path: str) -> str:
    """Encode an upstream path + query into a URL-safe Base64 token free of blocked keywords."""
    if not raw_path or raw_path == "/":
        return ""
    encoded = base64.urlsafe_b64encode(raw_path.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def decode_subpath(token: str) -> str:
    """Decode a Base64URL token back into the upstream path + query string."""
    if not token:
        return "/"
    padding = "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
        return decoded if decoded.startswith("/") else f"/{decoded}"
    except Exception:
        return f"/{token.lstrip('/')}"


def sanitize_request_headers(
    incoming_headers: dict,
    upstream_url: str,
) -> dict:
    """Strip hop-by-hop headers and rewrite Host, Origin, and Referer for the target site."""
    parsed = urlparse(upstream_url)
    upstream_host = parsed.netloc
    upstream_origin = f"{parsed.scheme}://{parsed.netloc}"

    cleaned = {}
    connection_tokens = set()
    conn_val = incoming_headers.get("connection") or incoming_headers.get("Connection")
    if conn_val:
        for token in conn_val.split(","):
            connection_tokens.add(token.strip().lower())

    for key, value in incoming_headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS or lower_key in connection_tokens:
            continue
        if lower_key in ("origin", "referer"):
            continue
        cleaned[key] = value

    # Explicitly rewrite Host, Origin, Referer, and request uncompressed payload for safe link rewriting
    cleaned["Host"] = upstream_host
    cleaned["Origin"] = upstream_origin
    cleaned["Referer"] = f"{upstream_origin}/"
    cleaned["Accept-Encoding"] = "identity"
    cleaned.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    return cleaned


def sanitize_response_headers(
    upstream_headers: dict,
    alias: str,
    target_map: dict,
    allowed_origin: str = "http://127.0.0.1:8080",
) -> dict:
    """Strip hop-by-hop and framing headers, rewrite Location redirects, and add CORS headers."""
    cleaned = {}
    for key, value in upstream_headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS or lower_key in STRIP_RESPONSE_HEADERS:
            continue
        if lower_key == "location":
            cleaned[key] = rewrite_redirect_location(value, alias, target_map)
        elif lower_key == "set-cookie":
            # Strip Domain and Secure flags so cookies bind to the local relay session
            cookie_val = re.sub(r";\s*Domain=[^;]+", "", value, flags=re.IGNORECASE)
            cookie_val = re.sub(r";\s*Secure", "", cookie_val, flags=re.IGNORECASE)
            cookie_val = re.sub(r";\s*SameSite=None", "; SameSite=Lax", cookie_val, flags=re.IGNORECASE)
            cleaned[key] = cookie_val
        else:
            cleaned[key] = value

    cleaned["Access-Control-Allow-Origin"] = allowed_origin
    cleaned["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    cleaned["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, X-Proxy-Token"
    cleaned["Access-Control-Allow-Credentials"] = "true"
    return cleaned


def rewrite_redirect_location(location: str, current_alias: str, target_map: dict) -> str:
    """Rewrite upstream Location headers into keyword-free proxy routes (/r/<alias>/<encoded_path>)."""
    if not location:
        return location
    if location.startswith("/"):
        encoded = encode_subpath(location)
        return f"/r/{current_alias}/{encoded}" if encoded else f"/s/{current_alias}"

    parsed = urlparse(location)
    host = (parsed.netloc or "").lower()
    path_and_query = parsed.path or "/"
    if parsed.query:
        path_and_query = f"{path_and_query}?{parsed.query}"

    for alias_key, info in target_map.items():
        for domain in info["domains"]:
            if host == domain or host.endswith(f".{domain}"):
                encoded = encode_subpath(path_and_query)
                return f"/r/{alias_key}/{encoded}" if encoded else f"/s/{alias_key}"

    # Block redirects to non-allowlisted external URLs by returning to the current target root
    return f"/s/{current_alias}"


def rewrite_html_payload(body_bytes: bytes, alias: str, target_map: dict) -> bytes:
    """Rewrite upstream URLs and blocked keywords inside HTML/JS/CSS into encoded proxy paths."""
    try:
        text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        return body_bytes

    # Rewrite full URLs for the three allowlisted platforms into /s/<alias> base routes
    for alias_key, info in target_map.items():
        for domain in info["domains"]:
            pattern = re.compile(rf"https?://(?:www\.|m\.)?{re.escape(domain)}(/[^\s\"'<>)]*)?", re.IGNORECASE)

            def _replace_url(match, a=alias_key):
                subpath = match.group(1) or "/"
                encoded = encode_subpath(subpath)
                return f"/r/{a}/{encoded}" if encoded else f"/s/{a}"

            text = pattern.sub(_replace_url, text)

    # Inject base navigation shim so root-relative links stay routed under /s/<alias>/
    shim = (
        f'<base href="/s/{alias}/">'
        f'<script data-proxy-shim="{alias}">'
        f'window.__PROXY_ALIAS__="{alias}";'
        f"</script>"
    )
    if "<head>" in text:
        text = text.replace("<head>", f"<head>{shim}", 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", f"<HEAD>{shim}", 1)

    return text.encode("utf-8")
