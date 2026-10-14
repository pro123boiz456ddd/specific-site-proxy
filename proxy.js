/**
 * Express route handlers and streaming http-proxy-middleware configuration
 * for the three allowlisted specific websites (Instagram, YouTube, TikTok).
 */

const express = require("express");
const { createProxyMiddleware } = require("http-proxy-middleware");
const {
  encodeSubpath,
  decodeSubpath,
  stripHopByHopRequestHeaders,
  sanitizeUpstreamResponseHeaders,
} = require("../middleware/headers");

function getTargetRegistry() {
  return {
    alpha: {
      id: "alpha",
      short_code: "ig",
      display_label: "Visual Feed (Instagram)",
      upstream: process.env.TARGET_ALPHA_URL || "https://www.instagram.com",
      launch_path: "/s/alpha",
      encoded_root: `/r/alpha/${encodeSubpath("/?hl=en")}`,
    },
    beta: {
      id: "beta",
      short_code: "yt",
      display_label: "Video Hub (YouTube)",
      upstream: process.env.TARGET_BETA_URL || "https://www.youtube.com",
      launch_path: "/s/beta",
      encoded_root: `/r/beta/${encodeSubpath("/?gl=US")}`,
    },
    gamma: {
      id: "gamma",
      short_code: "tt",
      display_label: "Short Clips (TikTok)",
      upstream: process.env.TARGET_GAMMA_URL || "https://www.tiktok.com",
      launch_path: "/s/gamma",
      encoded_root: `/r/gamma/${encodeSubpath("/explore")}`,
    },
  };
}

const ALIAS_MAP = {
  alpha: "alpha",
  ig: "alpha",
  beta: "beta",
  yt: "beta",
  gamma: "gamma",
  tt: "gamma",
};

function inferAliasFromRequest(req) {
  const lowerPath = (req.path || "").toLowerCase();
  if (
    ["/youtubei/", "/watch", "/results", "/s/player/", "/yts/", "/generate_204", "/api/stats/"].some((p) =>
      lowerPath.startsWith(p)
    )
  ) {
    return "beta";
  }
  if (
    ["/api/v1/", "/graphql/", "/ajax/", "/rsrc.php/", "/accounts/", "/data/shared_data/"].some((p) =>
      lowerPath.startsWith(p)
    )
  ) {
    return "alpha";
  }

  const referer = req.headers.referer || "";
  const refMatch = referer.match(/\/(?:s|r)\/(alpha|beta|gamma|ig|yt|tt)\b/i);
  if (refMatch) {
    return ALIAS_MAP[refMatch[1].toLowerCase()] || "alpha";
  }

  const cookieHdr = req.headers.cookie || "";
  const cookieMatch = cookieHdr.match(/active_proxy_alias=(alpha|beta|gamma)/);
  if (cookieMatch) {
    return cookieMatch[1];
  }
  return "";
}

function createProxyRouter() {
  const router = express.Router();
  const registry = getTargetRegistry();
  const timeoutMs = Number(process.env.UPSTREAM_TIMEOUT_MS || 15000);
  const middlewares = {};

  router.get("/api/targets", (_req, res) => {
    res.json({
      mode: "specific-website-relay",
      targets: Object.values(registry),
    });
  });

  router.all(["/unblock", "/proxy"], (_req, res) => {
    res.status(403).json({
      error: "Arbitrary URL unblocking is disabled",
      message:
        "Use the preset service routes (/s/alpha, /s/beta, /s/gamma) to open the specific website directly.",
    });
  });

  for (const [aliasKey, target] of Object.entries(registry)) {
    const proxyMiddleware = createProxyMiddleware({
      target: target.upstream,
      changeOrigin: true,
      proxyTimeout: timeoutMs,
      timeout: timeoutMs,
      followRedirects: false,
      pathRewrite: (path, req) => {
        if (req.encodedSubpath) {
          return req.encodedSubpath;
        }
        return path.replace(new RegExp(`^/s/(?:${aliasKey}|${target.short_code})`), "") || "/";
      },
      onProxyReq: (proxyReq, req) => {
        stripHopByHopRequestHeaders(proxyReq, req, target.upstream, aliasKey);
      },
      onProxyRes: (proxyRes) => {
        sanitizeUpstreamResponseHeaders(proxyRes, aliasKey);
      },
      onError: (err, _req, res) => {
        console.error(
          JSON.stringify({
            timestamp: new Date().toISOString(),
            event: "upstream_failed",
            alias: aliasKey,
            error: err.message,
          })
        );
        if (!res.headersSent) {
          res.status(502).json({
            error: "Upstream connection failed",
            alias: aliasKey,
            details: err.message,
          });
        }
      },
    });

    middlewares[aliasKey] = proxyMiddleware;
    router.use(`/s/${aliasKey}`, proxyMiddleware);
    router.use(`/s/${target.short_code}`, proxyMiddleware);
    router.use(`/r/${aliasKey}/:token`, (req, res, next) => {
      req.encodedSubpath = decodeSubpath(req.params.token);
      return proxyMiddleware(req, res, next);
    });
  }

  // Catch-all for root-relative SPA API/asset calls (/youtubei/v1/*, /api/v1/*, /graphql/query, /results, /watch)
  router.use((req, res, next) => {
    const inferred = inferAliasFromRequest(req);
    if (inferred && middlewares[inferred]) {
      return middlewares[inferred](req, res, next);
    }
    return next();
  });

  return router;
}

module.exports = {
  getTargetRegistry,
  ALIAS_MAP,
  inferAliasFromRequest,
  createProxyRouter,
};
