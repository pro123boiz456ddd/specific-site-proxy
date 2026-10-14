/**
 * Express entry point for the Specific-Site Reverse Proxy Relay.
 * Binds to 127.0.0.1 locally by default (or 0.0.0.0 on Render) and serves preset routes for Instagram, YouTube, and TikTok.
 */

const path = require("path");
const express = require("express");
const cors = require("cors");
require("dotenv").config();

const { getTargetRegistry, createProxyRouter } = require("./routes/proxy");

const DEFAULT_HOST = process.env.RENDER ? "0.0.0.0" : "127.0.0.1";
const HOST = process.env.HOST || DEFAULT_HOST;
const PORT = Number(process.env.PORT || 8080);

const app = express();

app.use(
  cors({
    origin: (process.env.CORS_ALLOWED_ORIGINS || `http://${HOST}:${PORT}`).split(","),
    credentials: true,
  })
);

app.get("/health", (_req, res) => {
  const registry = getTargetRegistry();
  res.json({
    status: "ok",
    service: "specific-site-proxy",
    mode: "preset-target-relay",
    arbitrary_url_unblocking: false,
    bind: `${HOST}:${PORT}`,
    targets: Object.fromEntries(
      Object.entries(registry).map(([key, val]) => [
        key,
        {
          short_code: val.short_code,
          launch_path: val.launch_path,
          encoded_root: val.encoded_root,
        },
      ])
    ),
  });
});

app.use("/assets", express.static(path.join(__dirname, "public")));
app.use(express.static(path.join(__dirname, "public")));
app.use("/", createProxyRouter());

if (require.main === module) {
  app.listen(PORT, HOST, () => {
    console.log(
      JSON.stringify({
        timestamp: new Date().toISOString(),
        event: "server_started",
        engine: "express+http-proxy-middleware",
        bind: `http://${HOST}:${PORT}`,
      })
    );
  });
}

module.exports = app;
