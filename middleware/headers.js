/**
 * Middleware for hop-by-hop header stripping, Host header rewriting,
 * CORS headers, and Base64URL path encoding for keyword-free routing.
 */

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "accept-encoding",
]);

const STRIP_RESPONSE_HEADERS = new Set([
  "x-frame-options",
  "content-security-policy",
  "content-security-policy-report-only",
  "cross-origin-opener-policy",
  "cross-origin-embedder-policy",
  "cross-origin-resource-policy",
  "strict-transport-security",
]);

function encodeSubpath(rawPath = "/") {
  if (!rawPath || rawPath === "/") return "";
  return Buffer.from(rawPath, "utf8")
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function decodeSubpath(token = "") {
  if (!token) return "/";
  try {
    const base64 = token.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const decoded = Buffer.from(padded, "base64").toString("utf8");
    return decoded.startsWith("/") ? decoded : `/${decoded}`;
  } catch {
    return `/${token.replace(/^\/+/, "")}`;
  }
}

function stripHopByHopRequestHeaders(proxyReq, req, targetOrigin) {
  const parsedTarget = new URL(targetOrigin);
  for (const headerName of Object.keys(req.headers)) {
    if (HOP_BY_HOP_HEADERS.has(headerName.toLowerCase())) {
      proxyReq.removeHeader(headerName);
    }
  }
  proxyReq.setHeader("Host", parsedTarget.host);
  proxyReq.setHeader("Origin", parsedTarget.origin);
  proxyReq.setHeader("Referer", `${parsedTarget.origin}/`);
  proxyReq.setHeader("Accept-Encoding", "identity");
}

function sanitizeUpstreamResponseHeaders(proxyRes, aliasKey) {
  for (const headerName of Object.keys(proxyRes.headers)) {
    const lower = headerName.toLowerCase();
    if (HOP_BY_HOP_HEADERS.has(lower) || STRIP_RESPONSE_HEADERS.has(lower)) {
      delete proxyRes.headers[headerName];
    }
  }

  if (proxyRes.headers.location) {
    const loc = String(proxyRes.headers.location);
    if (loc.startsWith("/")) {
      const encoded = encodeSubpath(loc);
      proxyRes.headers.location = encoded ? `/r/${aliasKey}/${encoded}` : `/s/${aliasKey}`;
    } else {
      try {
        const parsed = new URL(loc);
        const pathAndQuery = `${parsed.pathname || "/"}${parsed.search || ""}`;
        const encoded = encodeSubpath(pathAndQuery);
        proxyRes.headers.location = encoded ? `/r/${aliasKey}/${encoded}` : `/s/${aliasKey}`;
      } catch {
        proxyRes.headers.location = `/s/${aliasKey}`;
      }
    }
  }

  proxyRes.headers["access-control-allow-origin"] =
    process.env.CORS_ALLOWED_ORIGINS?.split(",")[0] || "http://127.0.0.1:8080";
  proxyRes.headers["access-control-allow-methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS";
  proxyRes.headers["access-control-allow-headers"] =
    "Content-Type, Authorization, X-Requested-With, X-Proxy-Token";
}

module.exports = {
  HOP_BY_HOP_HEADERS,
  STRIP_RESPONSE_HEADERS,
  encodeSubpath,
  decodeSubpath,
  stripHopByHopRequestHeaders,
  sanitizeUpstreamResponseHeaders,
};
