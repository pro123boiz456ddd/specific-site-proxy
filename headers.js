/**
 * Middleware for hop-by-hop header stripping, session cookie jar,
 * CSRF propagation, and Base64URL path encoding for keyword-free routing.
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
  "content-length",
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

const SESSION_COOKIE_JAR = {
  alpha: {
    ig_nrcb: "1",
    ig_cb: "1",
  },
  beta: {
    CONSENT: "YES+cb.20210328-17-p0.en+FX+999",
    SOCS: "CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
  },
  gamma: {},
};

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

function parseCookieHeader(cookieHeader = "") {
  const out = {};
  for (const part of cookieHeader.split(";")) {
    const idx = part.indexOf("=");
    if (idx > 0) {
      out[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
    }
  }
  return out;
}

function harvestAndRewriteSetCookies(setCookieArray = [], aliasKey = "alpha") {
  const rewritten = [];
  const jar = SESSION_COOKIE_JAR[aliasKey] || (SESSION_COOKIE_JAR[aliasKey] = {});

  for (const raw of setCookieArray) {
    const firstPart = String(raw).split(";")[0];
    const eqIdx = firstPart.indexOf("=");
    if (eqIdx > 0) {
      const k = firstPart.slice(0, eqIdx).trim();
      const v = firstPart.slice(eqIdx + 1).trim();
      if (k && v) jar[k] = v;
    }
    let cleaned = String(raw)
      .replace(/;\s*Domain=[^;]+/gi, "")
      .replace(/;\s*Secure/gi, "")
      .replace(/;\s*HttpOnly/gi, "")
      .replace(/;\s*SameSite=None/gi, "; SameSite=Lax")
      .replace(/;\s*Path=[^;]+/gi, "; Path=/");
    if (!/;\s*Path=/i.test(cleaned)) {
      cleaned += "; Path=/";
    }
    rewritten.push(cleaned);
  }

  rewritten.push(`active_proxy_alias=${aliasKey}; Path=/; SameSite=Lax`);
  if (aliasKey === "alpha") {
    rewritten.push("ig_nrcb=1; Path=/; Max-Age=31536000; SameSite=Lax");
  }
  return rewritten;
}

function stripHopByHopRequestHeaders(proxyReq, req, targetOrigin, aliasKey = "alpha") {
  const parsedTarget = new URL(targetOrigin);
  for (const headerName of Object.keys(req.headers)) {
    if (HOP_BY_HOP_HEADERS.has(headerName.toLowerCase())) {
      proxyReq.removeHeader(headerName);
    }
  }

  const mergedCookies = {
    ...(SESSION_COOKIE_JAR[aliasKey] || {}),
    ...parseCookieHeader(req.headers.cookie || ""),
  };
  if (aliasKey === "alpha") {
    mergedCookies.ig_nrcb = "1";
    mergedCookies.ig_cb = "1";
    if (mergedCookies.csrftoken && !req.headers["x-csrftoken"]) {
      proxyReq.setHeader("X-CSRFToken", mergedCookies.csrftoken);
    }
    proxyReq.setHeader("X-IG-App-ID", "936619743392459");
    proxyReq.setHeader("X-ASBD-ID", "129477");
  }

  const cookieStr = Object.entries(mergedCookies)
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");
  if (cookieStr) {
    proxyReq.setHeader("Cookie", cookieStr);
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

  if (proxyRes.headers["set-cookie"]) {
    const rawCookies = Array.isArray(proxyRes.headers["set-cookie"])
      ? proxyRes.headers["set-cookie"]
      : [proxyRes.headers["set-cookie"]];
    proxyRes.headers["set-cookie"] = harvestAndRewriteSetCookies(rawCookies, aliasKey);
  } else {
    proxyRes.headers["set-cookie"] = harvestAndRewriteSetCookies([], aliasKey);
  }

  if (proxyRes.headers.location) {
    const loc = String(proxyRes.headers.location);
    if (loc.startsWith("/")) {
      proxyRes.headers.location = `/s/${aliasKey}${loc}`;
    } else {
      try {
        const parsed = new URL(loc);
        const pathAndQuery = `${parsed.pathname || "/"}${parsed.search || ""}`;
        proxyRes.headers.location = `/s/${aliasKey}${pathAndQuery}`;
      } catch {
        proxyRes.headers.location = `/s/${aliasKey}`;
      }
    }
  }

  proxyRes.headers["access-control-allow-origin"] = "*";
  proxyRes.headers["access-control-allow-methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS";
  proxyRes.headers["access-control-allow-headers"] =
    "Content-Type, Authorization, X-Requested-With, X-CSRFToken, X-IG-App-ID, X-ASBD-ID, X-Proxy-Token";
}

module.exports = {
  HOP_BY_HOP_HEADERS,
  STRIP_RESPONSE_HEADERS,
  SESSION_COOKIE_JAR,
  encodeSubpath,
  decodeSubpath,
  stripHopByHopRequestHeaders,
  sanitizeUpstreamResponseHeaders,
};
