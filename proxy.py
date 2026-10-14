"""Route handlers, streaming upstream relay, CDN proxy, and YouTube InnerTube search/player engine."""

import asyncio
import html
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from middleware.headers import (
    SESSION_COOKIE_JAR,
    decode_subpath,
    encode_subpath,
    is_allowed_cdn_host,
    rewrite_html_payload,
    sanitize_request_headers,
    sanitize_response_headers_multi,
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


def infer_alias_from_request(path: str, headers: dict) -> str:
    """Infer the active target alias (alpha, beta, gamma) for root-relative SPA requests."""
    lower_path = (path or "").lower()
    if any(
        lower_path.startswith(p)
        for p in ("/youtubei/", "/watch", "/results", "/s/player/", "/yts/", "/generate_204", "/api/stats/")
    ):
        return "beta"
    if any(
        lower_path.startswith(p)
        for p in ("/api/v1/", "/graphql/", "/ajax/", "/rsrc.php/", "/accounts/", "/data/shared_data/")
    ):
        return "alpha"

    # Check Referer header for /s/<alias> or /r/<alias>
    referer = headers.get("referer") or headers.get("Referer") or ""
    match = re.search(r"/(?:s|r)/(alpha|beta|gamma|ig|yt|tt)\b", referer, re.IGNORECASE)
    if match:
        return SHORT_CODE_ALIASES.get(match.group(1).lower(), "alpha")

    # Check active_proxy_alias cookie
    cookie_hdr = headers.get("cookie") or headers.get("Cookie") or ""
    cookie_match = re.search(r"active_proxy_alias=(alpha|beta|gamma)", cookie_hdr)
    if cookie_match:
        return cookie_match.group(1)

    return ""


def build_upstream_url(target_info: dict, subpath: str = "/", query_string: str = "") -> str:
    """Construct the exact upstream URL within the allowlisted target origin."""
    clean_path = subpath if subpath.startswith("/") else f"/{subpath}"
    base_url = target_info["upstream"].rstrip("/")
    full_url = f"{base_url}{clean_path}"
    if query_string:
        separator = "&" if "?" in full_url else "?"
        full_url = f"{full_url}{separator}{query_string}"
    return full_url


# ---------------------------------------------------------------------------
# YouTube InnerTube Search & Direct Video Stream Engine (/s/beta)
# ---------------------------------------------------------------------------

def _extract_runs_text(obj) -> str:
    if not obj or not isinstance(obj, dict):
        return ""
    if "simpleText" in obj:
        return str(obj["simpleText"])
    runs = obj.get("runs")
    if isinstance(runs, list):
        return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    return ""


def _collect_video_renderers(node, results: list) -> None:
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            vr = node["videoRenderer"]
            vid = vr.get("videoId")
            title = _extract_runs_text(vr.get("title"))
            if vid and title and not any(x["videoId"] == vid for x in results):
                results.append({
                    "videoId": vid,
                    "title": title,
                    "channel": _extract_runs_text(vr.get("ownerText") or vr.get("longBylineText")) or "YouTube Channel",
                    "duration": _extract_runs_text(vr.get("lengthText")) or "LIVE",
                    "views": _extract_runs_text(vr.get("shortViewCountText") or vr.get("viewCountText")) or "",
                    "published": _extract_runs_text(vr.get("publishedTimeText")) or "",
                    "thumbnail": f"/cdn/beta/i.ytimg.com/vi/{vid}/hqdefault.jpg",
                })
        for val in node.values():
            _collect_video_renderers(val, results)
    elif isinstance(node, list):
        for item in node:
            _collect_video_renderers(item, results)


def fetch_youtube_search_results(query: str) -> list:
    """Query YouTube's upstream InnerTube /youtubei/v1/search API directly from the proxy."""
    effective_query = (query or "").strip() or "trending music tech gaming"
    url = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
    payload = json.dumps({
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240725.01.00",
                "hl": "en",
                "gl": "US",
            }
        },
        "query": effective_query,
    }).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
            "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+999; SOCS=CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
        },
    )
    videos = []
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=12.0, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            _collect_video_renderers(data, videos)
    except Exception as exc:
        log_event("youtube_innertube_search_error", query=effective_query, error=str(exc))
    return videos[:24]


def resolve_youtube_stream_url(video_id: str) -> dict:
    """Resolve a playable MP4/WebM stream URL and metadata for a YouTube video ID."""
    meta = {
        "videoId": video_id,
        "title": f"Video {video_id}",
        "channel": "",
        "stream_url": "",
    }
    ctx = ssl.create_default_context()

    # 1. Try YouTube InnerTube ANDROID / IOS / TVHTML5_SIMPLY_EMBEDDED_PLAYER clients
    clients = [
        {
            "clientName": "ANDROID",
            "clientVersion": "19.09.37",
            "androidSdkVersion": 30,
            "userAgent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip",
        },
        {
            "clientName": "IOS",
            "clientVersion": "19.09.3",
            "deviceMake": "Apple",
            "deviceModel": "iPhone16,2",
            "userAgent": "com.google.ios.youtube/19.09.3 (iPhone16,2; U; CPU iOS 17_4 like Mac OS X)",
        },
    ]

    for c_profile in clients:
        try:
            player_req = urllib.request.Request(
                url="https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                data=json.dumps({
                    "videoId": video_id,
                    "context": {
                        "client": {
                            "clientName": c_profile["clientName"],
                            "clientVersion": c_profile["clientVersion"],
                            "hl": "en",
                            "gl": "US",
                            **(
                                {"androidSdkVersion": c_profile["androidSdkVersion"]}
                                if "androidSdkVersion" in c_profile
                                else {}
                            ),
                        }
                    },
                    "contentCheckOk": True,
                    "racyCheckOk": True,
                }).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": c_profile["userAgent"],
                },
            )
            with urllib.request.urlopen(player_req, timeout=10.0, context=ctx) as resp:
                pdata = json.loads(resp.read().decode("utf-8", errors="replace"))
                vdetails = pdata.get("videoDetails") or {}
                if vdetails.get("title"):
                    meta["title"] = vdetails["title"]
                if vdetails.get("author"):
                    meta["channel"] = vdetails["author"]
                formats = (pdata.get("streamingData") or {}).get("formats") or []
                for fmt in reversed(formats):
                    if fmt.get("url"):
                        meta["stream_url"] = fmt["url"]
                        return meta
        except Exception as exc:
            log_event("youtube_player_profile_failed", client=c_profile["clientName"], error=str(exc))

    # 2. Fallback to public Piped stream API if datacenter IP is rate-limited by InnerTube
    piped_instances = [
        "https://pipedapi.kavin.rocks",
        "https://api.piped.private.coffee",
    ]
    for base in piped_instances:
        try:
            req = urllib.request.Request(
                url=f"{base}/streams/{urllib.parse.quote(video_id)}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=8.0, context=ctx) as resp:
                sdata = json.loads(resp.read().decode("utf-8", errors="replace"))
                if sdata.get("title"):
                    meta["title"] = sdata["title"]
                if sdata.get("uploader"):
                    meta["channel"] = sdata["uploader"]
                for vstream in sdata.get("videoStreams") or []:
                    if not vstream.get("videoOnly") and vstream.get("url"):
                        meta["stream_url"] = vstream["url"]
                        return meta
        except Exception:
            continue

    return meta


def render_youtube_portal_html(query: str = "", watch_id: str = "") -> bytes:
    """Render an interactive, self-contained YouTube Search & Video Watch experience routed through /s/beta."""
    videos = fetch_youtube_search_results(query or ("related videos" if watch_id else "trending"))
    escaped_q = html.escape(query or "")

    player_section = ""
    if watch_id:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", watch_id)
        meta = resolve_youtube_stream_url(safe_id)
        safe_title = html.escape(meta.get("title") or f"Video {safe_id}")
        safe_channel = html.escape(meta.get("channel") or "YouTube")
        has_direct_stream = bool(meta.get("stream_url"))

        video_element = (
            f'<video id="yt-proxy-video" controls autoplay playsinline '
            f'poster="/cdn/beta/i.ytimg.com/vi/{safe_id}/hqdefault.jpg" '
            f'style="width:100%;max-height:520px;background:#000;border-radius:12px;">'
            f'<source src="/s/beta/stream/{safe_id}" type="video/mp4" />'
            f"Your browser does not support HTML5 video."
            f"</video>"
            if has_direct_stream
            else f'<iframe src="https://www.youtube-nocookie.com/embed/{safe_id}?autoplay=1" '
            f'style="width:100%;height:480px;border:none;border-radius:12px;background:#000;" '
            f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" '
            f"allowfullscreen></iframe>"
        )

        player_section = f"""
        <section class="player-box">
          <div class="video-container" id="video-stage">
            {video_element}
          </div>
          <div class="video-info-bar">
            <div>
              <h2 class="video-main-title">{safe_title}</h2>
              <p class="video-channel-name">{safe_channel}</p>
            </div>
            <div class="player-mode-buttons">
              <button type="button" class="mode-btn" onclick="switchPlayer('stream', '{safe_id}')">Direct Proxy Stream</button>
              <button type="button" class="mode-btn" onclick="switchPlayer('embed', '{safe_id}')">Embed Player</button>
            </div>
          </div>
        </section>
        """

    cards_html = []
    for v in videos:
        vid = html.escape(v["videoId"])
        vtitle = html.escape(v["title"])
        vchan = html.escape(v["channel"])
        vdur = html.escape(v["duration"])
        vviews = html.escape(v["views"])
        vpub = html.escape(v["published"])
        vthumb = html.escape(v["thumbnail"])
        watch_href = f"/s/beta/watch?v={vid}&q={urllib.parse.quote(query or '')}"
        cards_html.append(f"""
        <a class="yt-card" href="{watch_href}">
          <div class="thumb-wrap">
            <img src="{vthumb}" alt="{vtitle}" loading="lazy" />
            <span class="duration-badge">{vdur}</span>
          </div>
          <div class="yt-card-body">
            <h3 class="yt-card-title">{vtitle}</h3>
            <p class="yt-card-meta">{vchan}</p>
            <p class="yt-card-meta">{vviews} {('• ' + vpub) if vpub else ''}</p>
          </div>
        </a>
        """)

    cards_joined = "\n".join(cards_html) or (
        '<div class="empty-msg">No videos returned. Try searching another topic above.</div>'
    )

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{escaped_q + ' - ' if escaped_q else ''}Video Hub Relay (/s/beta)</title>
  <style>
    :root {{
      --bg: #0f0f0f;
      --surface: #212121;
      --surface-hover: #2f2f2f;
      --border: #383838;
      --text: #f1f1f1;
      --muted: #aaaaaa;
      --accent: #ff0033;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }}
    .yt-header {{
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.75rem 1.5rem;
      background: rgba(15, 15, 15, 0.96);
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(8px);
    }}
    .yt-logo {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      color: var(--text);
      text-decoration: none;
      font-weight: 700;
      font-size: 1.15rem;
      white-space: nowrap;
    }}
    .yt-logo-badge {{
      background: var(--accent);
      color: #fff;
      padding: 0.15rem 0.5rem;
      border-radius: 6px;
      font-size: 0.85rem;
    }}
    .search-form {{
      display: flex;
      flex: 1;
      max-width: 640px;
    }}
    .search-input {{
      flex: 1;
      padding: 0.6rem 1rem;
      border-radius: 999px 0 0 999px;
      border: 1px solid var(--border);
      background: #121212;
      color: var(--text);
      font-size: 0.95rem;
      outline: none;
    }}
    .search-input:focus {{
      border-color: #3ea6ff;
    }}
    .search-btn {{
      padding: 0.6rem 1.25rem;
      border-radius: 0 999px 999px 0;
      border: 1px solid var(--border);
      border-left: none;
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      font-weight: 500;
    }}
    .search-btn:hover {{
      background: var(--surface-hover);
    }}
    .category-bar {{
      display: flex;
      gap: 0.55rem;
      padding: 0.75rem 1.5rem;
      overflow-x: auto;
      border-bottom: 1px solid #1f1f1f;
    }}
    .cat-pill {{
      background: var(--surface);
      color: var(--text);
      text-decoration: none;
      padding: 0.38rem 0.85rem;
      border-radius: 8px;
      font-size: 0.85rem;
      white-space: nowrap;
    }}
    .cat-pill:hover {{
      background: var(--surface-hover);
    }}
    .main-wrap {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 1.25rem 1.5rem 2.5rem;
    }}
    .player-box {{
      margin-bottom: 1.75rem;
      background: #181818;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1rem;
    }}
    .video-info-bar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 1rem;
      margin-top: 0.85rem;
    }}
    .video-main-title {{
      font-size: 1.2rem;
      margin-bottom: 0.25rem;
    }}
    .video-channel-name {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .player-mode-buttons {{
      display: flex;
      gap: 0.5rem;
    }}
    .mode-btn {{
      background: var(--surface);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 0.4rem 0.75rem;
      border-radius: 8px;
      font-size: 0.8rem;
      cursor: pointer;
    }}
    .mode-btn:hover {{
      background: var(--surface-hover);
    }}
    .section-heading {{
      font-size: 1rem;
      color: var(--muted);
      margin-bottom: 1rem;
      font-weight: 500;
    }}
    .video-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 1.25rem;
    }}
    .yt-card {{
      background: #181818;
      border: 1px solid #262626;
      border-radius: 12px;
      overflow: hidden;
      text-decoration: none;
      color: var(--text);
      display: flex;
      flex-direction: column;
      transition: transform 0.15s ease, border-color 0.15s ease;
    }}
    .yt-card:hover {{
      transform: translateY(-2px);
      border-color: #444;
    }}
    .thumb-wrap {{
      position: relative;
      width: 100%;
      aspect-ratio: 16 / 9;
      background: #000;
    }}
    .thumb-wrap img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .duration-badge {{
      position: absolute;
      right: 8px;
      bottom: 8px;
      background: rgba(0, 0, 0, 0.85);
      color: #fff;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .yt-card-body {{
      padding: 0.85rem;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
    }}
    .yt-card-title {{
      font-size: 0.95rem;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .yt-card-meta {{
      font-size: 0.8rem;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <header class="yt-header">
    <a class="yt-logo" href="/s/beta">
      <span class="yt-logo-badge">▶</span>
      <span>YouTube Relay</span>
    </a>
    <form class="search-form" action="/s/beta/results" method="GET">
      <input
        class="search-input"
        type="text"
        name="search_query"
        value="{escaped_q}"
        placeholder="Search YouTube videos..."
        autocomplete="off"
      />
      <button class="search-btn" type="submit">Search</button>
    </form>
    <a class="cat-pill" href="/">Hub</a>
  </header>

  <nav class="category-bar">
    <a class="cat-pill" href="/s/beta/results?search_query=trending">Trending</a>
    <a class="cat-pill" href="/s/beta/results?search_query=music+hits">Music</a>
    <a class="cat-pill" href="/s/beta/results?search_query=gaming">Gaming</a>
    <a class="cat-pill" href="/s/beta/results?search_query=technology+reviews">Tech</a>
    <a class="cat-pill" href="/s/beta/results?search_query=world+news">News</a>
    <a class="cat-pill" href="/s/beta/results?search_query=coding+tutorials">Coding</a>
    <a class="cat-pill" href="/s/beta/results?search_query=lofi+beats">Lo-Fi</a>
  </nav>

  <main class="main-wrap">
    {player_section}
    <h2 class="section-heading">
      {('Search results for "' + escaped_q + '"') if escaped_q else 'Recommended Videos'}
    </h2>
    <div class="video-grid">
      {cards_joined}
    </div>
  </main>

  <script>
    function switchPlayer(mode, videoId) {{
      const stage = document.getElementById("video-stage");
      if (!stage) return;
      if (mode === "embed") {{
        stage.innerHTML = '<iframe src="https://www.youtube-nocookie.com/embed/' + videoId + '?autoplay=1" style="width:100%;height:480px;border:none;border-radius:12px;background:#000;" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>';
      }} else {{
        stage.innerHTML = '<video controls autoplay playsinline style="width:100%;max-height:520px;background:#000;border-radius:12px;"><source src="/s/beta/stream/' + videoId + '" type="video/mp4" /></video>';
      }}
    }}
  </script>
</body>
</html>"""
    return page_html.encode("utf-8")


# ---------------------------------------------------------------------------
# FastAPI Async Streaming Relay (httpx)
# ---------------------------------------------------------------------------

async def stream_youtube_video_httpx(video_id: str, request_headers: dict):
    """Stream raw MP4/WebM video bytes for a YouTube video ID through the proxy."""
    import httpx
    from fastapi.responses import Response, StreamingResponse

    meta = await asyncio.to_thread(resolve_youtube_stream_url, video_id)
    stream_url = meta.get("stream_url")
    if not stream_url:
        return Response(
            content=json.dumps({"error": "Unable to resolve playable video stream", "videoId": video_id}),
            status_code=502,
            media_type="application/json",
        )

    fwd_headers = {
        "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip",
    }
    range_hdr = request_headers.get("range") or request_headers.get("Range")
    if range_hdr:
        fwd_headers["Range"] = range_hdr

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True, verify=True)
    req = client.build_request("GET", stream_url, headers=fwd_headers)
    upstream_resp = await client.send(req, stream=True)

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
    }
    for k in ("content-type", "content-length", "content-range"):
        if k in upstream_resp.headers:
            resp_headers[k.title()] = upstream_resp.headers[k]

    async def _iter_video():
        try:
            async for chunk in upstream_resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _iter_video(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type", "video/mp4"),
    )


async def forward_cdn_with_httpx(alias: str, cdn_host: str, subpath: str, query_string: str, headers: dict):
    """Proxy static scripts, stylesheets, and images from allowlisted CDN hosts."""
    import httpx
    from fastapi.responses import Response, StreamingResponse

    if not is_allowed_cdn_host(cdn_host):
        return Response(
            content=json.dumps({"error": "CDN host not allowlisted", "host": cdn_host}),
            status_code=403,
            media_type="application/json",
        )

    clean_path = subpath if subpath.startswith("/") else f"/{subpath}"
    upstream_url = f"https://{cdn_host}{clean_path}"
    if query_string:
        upstream_url = f"{upstream_url}?{query_string}"

    req_headers = sanitize_request_headers(headers, upstream_url, alias=alias)
    client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True, verify=True)
    req = client.build_request("GET", upstream_url, headers=req_headers)
    upstream_resp = await client.send(req, stream=True)

    content_type = upstream_resp.headers.get("content-type", "application/octet-stream")
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
    }

    async def _stream_cdn():
        try:
            async for chunk in upstream_resp.aiter_bytes(chunk_size=32768):
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream_cdn(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type,
    )


async def forward_with_httpx(
    method: str,
    target_key: str,
    subpath: str,
    query_string: str,
    headers: dict,
    body: bytes = b"",
):
    """Stream upstream requests asynchronously using httpx.AsyncClient with session cookies and multi-Set-Cookie headers."""
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

    # Use interactive YouTube Search & Video Portal on /s/beta GET navigation routes
    if target["id"] == "beta" and method == "GET":
        clean_sub = subpath.rstrip("/") or "/"
        qs_params = urllib.parse.parse_qs(query_string)
        if clean_sub in ("/", "/results", "/watch", "/feed/trending", "/feed/subscriptions"):
            q_val = (qs_params.get("search_query") or qs_params.get("q") or [""])[0]
            if clean_sub == "/feed/trending" and not q_val:
                q_val = "trending"
            elif clean_sub == "/feed/subscriptions" and not q_val:
                q_val = "popular channels"
            v_val = (qs_params.get("v") or [""])[0]
            portal_bytes = await asyncio.to_thread(render_youtube_portal_html, q_val, v_val)
            resp = Response(content=portal_bytes, status_code=200, media_type="text/html; charset=utf-8")
            resp.headers.append("Set-Cookie", "active_proxy_alias=beta; Path=/; SameSite=Lax")
            return resp
        if clean_sub.startswith("/stream/"):
            vid = clean_sub.split("/stream/", 1)[1].split("/")[0]
            return await stream_youtube_video_httpx(vid, headers)

    upstream_url = build_upstream_url(target, subpath, query_string)
    req_headers = sanitize_request_headers(headers, upstream_url, alias=target["id"])
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
            multi_headers = sanitize_response_headers_multi(
                list(upstream_resp.headers.multi_items()),
                target["id"],
                registry,
            )
            content_type = upstream_resp.headers.get("content-type", "")

            log_event(
                "upstream_response",
                alias=target["id"],
                method=method,
                subpath=subpath,
                status=upstream_resp.status_code,
                attempt=attempt,
                content_type=content_type,
            )

            if "text/html" in content_type.lower():
                raw_bytes = await upstream_resp.aread()
                await upstream_resp.aclose()
                await client.aclose()
                rewritten = rewrite_html_payload(raw_bytes, target["id"], registry)
                resp = Response(
                    content=rewritten,
                    status_code=upstream_resp.status_code,
                    media_type="text/html; charset=utf-8",
                )
                for k, v in multi_headers:
                    if k.lower() != "content-type":
                        resp.headers.append(k, v)
                return resp

            async def _stream_chunks():
                try:
                    async for chunk in upstream_resp.aiter_bytes(chunk_size=32768):
                        yield chunk
                finally:
                    await upstream_resp.aclose()
                    await client.aclose()

            resp = StreamingResponse(
                _stream_chunks(),
                status_code=upstream_resp.status_code,
                media_type=content_type or "application/octet-stream",
            )
            for k, v in multi_headers:
                if k.lower() != "content-type":
                    resp.headers.append(k, v)
            return resp
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


# ---------------------------------------------------------------------------
# Stdlib Fallback Relay (urllib)
# ---------------------------------------------------------------------------

def forward_with_urllib(
    method: str,
    target_key: str,
    subpath: str,
    query_string: str,
    headers: dict,
    body: bytes = b"",
) -> tuple:
    """Stdlib fallback relay returning (status, list_of_header_tuples, body_bytes)."""
    target, registry = resolve_target(target_key)
    if not target:
        payload = json.dumps({
            "error": "Target not allowlisted",
            "message": "This proxy opens only the preset target websites (alpha, beta, gamma) and blocks arbitrary URL unblocking.",
        }).encode("utf-8")
        return 403, [("Content-Type", "application/json")], payload

    # Interactive YouTube Search & Video Portal on /s/beta GET routes
    if target["id"] == "beta" and method == "GET":
        clean_sub = subpath.rstrip("/") or "/"
        qs_params = urllib.parse.parse_qs(query_string)
        if clean_sub in ("/", "/results", "/watch", "/feed/trending", "/feed/subscriptions"):
            q_val = (qs_params.get("search_query") or qs_params.get("q") or [""])[0]
            if clean_sub == "/feed/trending" and not q_val:
                q_val = "trending"
            elif clean_sub == "/feed/subscriptions" and not q_val:
                q_val = "popular channels"
            v_val = (qs_params.get("v") or [""])[0]
            portal_bytes = render_youtube_portal_html(q_val, v_val)
            return (
                200,
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Set-Cookie", "active_proxy_alias=beta; Path=/; SameSite=Lax"),
                ],
                portal_bytes,
            )
        if clean_sub.startswith("/stream/"):
            vid = clean_sub.split("/stream/", 1)[1].split("/")[0]
            meta = resolve_youtube_stream_url(vid)
            stream_url = meta.get("stream_url")
            if stream_url:
                return 302, [("Location", stream_url)], b""

    upstream_url = build_upstream_url(target, subpath, query_string)
    req_headers = sanitize_request_headers(headers, upstream_url, alias=target["id"])
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
                raw_header_items = list(resp.info().items())
                raw_body = resp.read()
        except urllib.error.HTTPError as http_err:
            status = http_err.code
            raw_header_items = list(http_err.headers.items()) if http_err.headers else []
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

        multi_headers = sanitize_response_headers_multi(raw_header_items, target["id"], registry)
        content_type = ""
        for k, v in raw_header_items:
            if k.lower() == "content-type":
                content_type = v
                break
        if "text/html" in content_type.lower():
            raw_body = rewrite_html_payload(raw_body, target["id"], registry)
            multi_headers = [(k, v) for k, v in multi_headers if k.lower() != "content-type"]
            multi_headers.append(("Content-Type", "text/html; charset=utf-8"))

        log_event(
            "upstream_response",
            alias=target["id"],
            method=method,
            subpath=subpath,
            status=status,
            attempt=attempt,
        )
        return status, multi_headers, raw_body

    log_event("upstream_failed", alias=target["id"], error=str(last_exc))
    err_payload = json.dumps({
        "error": "Upstream connection failed",
        "alias": target["id"],
        "details": str(last_exc),
    }).encode("utf-8")
    return 502, [("Content-Type", "application/json")], err_payload
