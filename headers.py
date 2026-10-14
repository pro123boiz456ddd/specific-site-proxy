"""Middleware for hop-by-hop header stripping, session cookie jar, CSRF propagation, and SPA HTML/URL rewriting."""

import base64
import re
from http.cookies import SimpleCookie
from urllib.parse import urlparse

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

# Upstream response headers removed so pages and scripts run cleanly through the proxy
STRIP_RESPONSE_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
    "strict-transport-security",
    "content-encoding",
    "nel",
    "report-to",
}

# Persistent server-side cookie jar per alias, pre-seeded with cookie-consent tokens
SESSION_COOKIE_JAR = {
    "alpha": {
        "ig_nrcb": "1",
        "ig_cb": "1",
    },
    "beta": {
        "CONSENT": "YES+cb.20210328-17-p0.en+FX+999",
        "SOCS": "CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
        "PREF": "hl=en&gl=US",
    },
    "gamma": {},
}

CDN_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
    "ytimg.com",
    "ggpht.com",
    "tiktokcdn.com",
    "tiktokv.com",
)


def is_allowed_cdn_host(host: str) -> bool:
    """Return True if the host belongs to an allowlisted static/media CDN for the 3 platforms."""
    clean = (host or "").lower().split(":")[0]
    return any(clean == suffix or clean.endswith(f".{suffix}") for suffix in CDN_SUFFIXES)


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


def parse_cookie_header(cookie_header: str) -> dict:
    """Parse a raw Cookie request header into a key-value dict."""
    cookies = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def harvest_set_cookie(raw_set_cookie: str, alias: str) -> str:
    """Store upstream Set-Cookie values in SESSION_COOKIE_JAR and rewrite attributes for the proxy."""
    if not raw_set_cookie:
        return ""
    first_semi = raw_set_cookie.split(";", 1)[0]
    if "=" in first_semi:
        k, v = first_semi.split("=", 1)
        k_clean = k.strip()
        v_clean = v.strip()
        if k_clean and v_clean:
            SESSION_COOKIE_JAR.setdefault(alias, {})[k_clean] = v_clean

    # Strip Domain, Secure, HttpOnly (so client-shim can read csrftoken), and force Path=/
    rewritten = re.sub(r";\s*Domain=[^;]+", "", raw_set_cookie, flags=re.IGNORECASE)
    rewritten = re.sub(r";\s*Secure", "", rewritten, flags=re.IGNORECASE)
    rewritten = re.sub(r";\s*HttpOnly", "", rewritten, flags=re.IGNORECASE)
    rewritten = re.sub(r";\s*SameSite=None", "; SameSite=Lax", rewritten, flags=re.IGNORECASE)
    rewritten = re.sub(r";\s*Path=[^;]+", "; Path=/", rewritten, flags=re.IGNORECASE)
    if "path=" not in rewritten.lower():
        rewritten = f"{rewritten}; Path=/"
    return rewritten


def sanitize_request_headers(
    incoming_headers: dict,
    upstream_url: str,
    alias: str = "alpha",
) -> dict:
    """Strip hop-by-hop headers, merge session cookies, and inject CSRF/Origin headers for the target site."""
    parsed = urlparse(upstream_url)
    upstream_host = parsed.netloc
    upstream_origin = f"{parsed.scheme}://{parsed.netloc}"

    cleaned = {}
    connection_tokens = set()
    conn_val = incoming_headers.get("connection") or incoming_headers.get("Connection")
    if conn_val:
        for token in conn_val.split(","):
            connection_tokens.add(token.strip().lower())

    incoming_cookie_str = ""
    for key, value in incoming_headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS or lower_key in connection_tokens:
            continue
        if lower_key in ("origin", "referer"):
            continue
        if lower_key == "cookie":
            incoming_cookie_str = value
            continue
        cleaned[key] = value

    # Merge server-side session cookies (including consent & csrftoken) with browser cookies
    merged_cookies = dict(SESSION_COOKIE_JAR.get(alias, {}))
    merged_cookies.update(parse_cookie_header(incoming_cookie_str))
    # Always enforce consent cookies
    if alias == "alpha":
        merged_cookies["ig_nrcb"] = "1"
        merged_cookies["ig_cb"] = "1"
    elif alias == "beta":
        merged_cookies.setdefault("CONSENT", "YES+cb.20210328-17-p0.en+FX+999")
        merged_cookies.setdefault("SOCS", "CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg")

    if merged_cookies:
        cleaned["Cookie"] = "; ".join(f"{k}={v}" for k, v in merged_cookies.items())

    # Inject Instagram CSRF & AJAX headers so login and cookie-consent mutations succeed
    if alias == "alpha":
        csrf_token = merged_cookies.get("csrftoken", "")
        if csrf_token and not any(k.lower() == "x-csrftoken" for k in cleaned):
            cleaned["X-CSRFToken"] = csrf_token
        cleaned.setdefault("X-IG-App-ID", "936619743392459")
        cleaned.setdefault("X-ASBD-ID", "129477")
        cleaned.setdefault("X-IG-WWW-Claim", "0")

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


def sanitize_response_headers_multi(
    upstream_headers_items: list,
    alias: str,
    target_map: dict,
    allowed_origin: str = "*",
) -> list:
    """Return a list of (header_name, header_value) preserving multiple Set-Cookie headers."""
    header_list = []
    for key, value in upstream_headers_items:
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS or lower_key in STRIP_RESPONSE_HEADERS:
            continue
        if lower_key == "location":
            header_list.append((key, rewrite_redirect_location(value, alias, target_map)))
        elif lower_key == "set-cookie":
            rewritten_cookie = harvest_set_cookie(value, alias)
            if rewritten_cookie:
                header_list.append(("Set-Cookie", rewritten_cookie))
        else:
            header_list.append((key, value))

    # Track the active alias in a browser cookie so root-relative AJAX/sub-resource requests resolve properly
    header_list.append(("Set-Cookie", f"active_proxy_alias={alias}; Path=/; SameSite=Lax"))
    if alias == "alpha":
        header_list.append(("Set-Cookie", "ig_nrcb=1; Path=/; Max-Age=31536000; SameSite=Lax"))

    header_list.append(("Access-Control-Allow-Origin", allowed_origin))
    header_list.append(("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"))
    header_list.append((
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-Requested-With, X-CSRFToken, X-IG-App-ID, X-ASBD-ID, X-Proxy-Token",
    ))
    header_list.append(("Access-Control-Allow-Credentials", "true"))
    return header_list


def sanitize_response_headers(
    upstream_headers: dict,
    alias: str,
    target_map: dict,
    allowed_origin: str = "http://127.0.0.1:8080",
) -> dict:
    """Dict-compatible wrapper around sanitize_response_headers_multi."""
    items = list(upstream_headers.items()) if isinstance(upstream_headers, dict) else list(upstream_headers)
    multi = sanitize_response_headers_multi(items, alias, target_map, allowed_origin)
    out = {}
    for k, v in multi:
        out[k] = v
    return out


def rewrite_redirect_location(location: str, current_alias: str, target_map: dict) -> str:
    """Rewrite upstream Location headers into proxy routes."""
    if not location:
        return location
    if location.startswith("/"):
        return f"/s/{current_alias}{location}"

    parsed = urlparse(location)
    host = (parsed.netloc or "").lower()
    path_and_query = parsed.path or "/"
    if parsed.query:
        path_and_query = f"{path_and_query}?{parsed.query}"

    for alias_key, info in target_map.items():
        for domain in info["domains"]:
            if host == domain or host.endswith(f".{domain}"):
                return f"/s/{alias_key}{path_and_query}"

    return f"/s/{current_alias}"


def rewrite_html_payload(body_bytes: bytes, alias: str, target_map: dict) -> bytes:
    """Rewrite upstream URLs, CDN domains, and root-relative links in HTML/JS/CSS and inject client-shim.js."""
    try:
        text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        return body_bytes

    # 1. Strip Subresource Integrity (integrity="...") and crossorigin="..." so rewritten scripts execute
    text = re.sub(r'\s+integrity=["\'][^"\']*["\']', "", text, flags=re.IGNORECASE)
    text = re.sub(r'\s+crossorigin=["\'][^"\']*["\']', "", text, flags=re.IGNORECASE)

    # 2. Rewrite CDN URLs (static.cdninstagram.com, i.ytimg.com, etc.) into /cdn/<alias>/<host>/...
    for cdn_suffix in CDN_SUFFIXES:
        cdn_pattern = re.compile(
            rf"https?://([a-z0-9.-]*{re.escape(cdn_suffix)})(/[^\s\"'<>\\)]*)?",
            re.IGNORECASE,
        )

        def _replace_cdn(match):
            cdn_host = match.group(1)
            subpath = match.group(2) or "/"
            return f"/cdn/{alias}/{cdn_host}{subpath}"

        text = cdn_pattern.sub(_replace_cdn, text)

    # 3. Rewrite primary platform URLs into /r/<alias>/<encoded> and /s/<alias>/...
    for alias_key, info in target_map.items():
        for domain in info["domains"]:
            pattern = re.compile(
                rf"https?://(?:www\.|m\.)?{re.escape(domain)}(/[^\s\"'<>\\)]*)?",
                re.IGNORECASE,
            )

            def _replace_url(match, a=alias_key):
                subpath = match.group(1) or "/"
                encoded = encode_subpath(subpath)
                return f"/r/{a}/{encoded}" if encoded else f"/s/{a}"

            text = pattern.sub(_replace_url, text)

    # 4. Rewrite root-relative href="/...", src="/...", action="/..." attributes into /s/<alias>/...
    attr_pattern = re.compile(
        r'(\b(?:href|src|action)=["\'])/(?!/|s/|r/|cdn/|assets/)([^"\']*)(["\'])',
        re.IGNORECASE,
    )
    text = attr_pattern.sub(rf"\1/s/{alias}/\2\3", text)

    # 5. Inject client-side runtime shim & Instagram cookie-modal helper
    csrf_token = SESSION_COOKIE_JAR.get(alias, {}).get("csrftoken", "")
    shim = (
        f'<base href="/s/{alias}/">'
        f'<script data-proxy-shim="{alias}">'
        f'window.__PROXY_ALIAS__="{alias}";'
        f'window.__IG_CSRF_TOKEN__="{csrf_token}";'
        f"</script>"
        f'<script src="/assets/client-shim.js"></script>'
    )
    if "<head>" in text:
        text = text.replace("<head>", f"<head>{shim}", 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", f"<HEAD>{shim}", 1)
    else:
        text = f"{shim}{text}"

    return text.encode("utf-8")
