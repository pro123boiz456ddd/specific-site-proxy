# Specific-Site Reverse Proxy Relay

A preset reverse proxy relay that opens **Instagram**, **YouTube**, and **TikTok** directly through keyword-free alias routes (`/s/alpha`, `/s/beta`, `/s/gamma`) and Base64URL-encoded subpaths (`/r/<alias>/<token>`) instead of exposing an arbitrary URL unblocker.

## Architecture & Key Features

- **Preset Target Allowlist (No Open URL Unblocker)**: Only the three configured upstream origins (`https://www.instagram.com`, `https://www.youtube.com`, and `https://www.tiktok.com`) are permitted. Requests to generic unblocker paths (`/unblock`, `/proxy`) or unlisted origins return `403 Forbidden`.
- **Keyword-Free Alias & Base64URL Path Routing**:
  - `/s/alpha` (or `/r/alpha/<base64url_path>`) opens **Instagram**
  - `/s/beta` (or `/r/beta/<base64url_path>`) opens **YouTube**
  - `/s/gamma` (or `/r/gamma/<base64url_path>`) opens **TikTok**
- **Hop-by-Hop Header Stripping & Host Rewriting**: Strips `Connection`, `Keep-Alive`, `Transfer-Encoding`, `TE`, `Trailer`, `Upgrade`, `Proxy-Authorization`, `Proxy-Authenticate`, and incoming `Host` headers before forwarding upstream, and rewrites `Host`, `Origin`, `Referer`, and `Location` redirects.
- **Streaming Forwarding, Timeouts & Retry Backoff**: Uses `httpx.AsyncClient` streaming in Python (`main.py`) and `http-proxy-middleware` in Node.js (`server.js`) with configurable request timeouts, exponential retry backoff, and structured JSON logging.

## Quick Start

### Option 1: Python (FastAPI / Uvicorn or Built-in Stdlib Fallback)

```bash
cd specific-site-proxy
cp .env.example .env

# Run syntax check
python3 -m py_compile main.py routes/proxy.py middleware/headers.py

# Optional: install FastAPI + httpx in a virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start the relay server (binds to 127.0.0.1:8080 by default)
python3 main.py
```

### Option 2: Node.js (Express + `http-proxy-middleware`)

```bash
cd specific-site-proxy
cp .env.example .env
npm install
npm run check
npm start
```

## Verification Endpoints

- **Launcher UI**: `http://127.0.0.1:8080/`
- **Health Check**: `http://127.0.0.1:8080/health`
- **Preset Targets Registry**: `http://127.0.0.1:8080/api/targets`
