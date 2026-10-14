/**
 * Injected Client-Side Runtime Shim (__proxy_client_shim)
 *
 * Solves SPA interactivity inside proxied sessions:
 * 1. Rewrites fetch() and XMLHttpRequest URLs so API calls (/youtubei/v1/*, /api/v1/*,
 *    /graphql/query, /ajax/*) and CDN assets route through /s/<alias> or /cdn/<alias>/<host>.
 * 2. Rewrites History pushState/replaceState and <form>/<a> navigation so YouTube search
 *    (/results?search_query=...) and video watch (/watch?v=...) stay inside /s/<alias>.
 * 3. Propagates Instagram CSRF tokens and dismisses the sticky cookie consent overlay
 *    when the user clicks Accept or Decline non-essential cookies so login works cleanly.
 */
(function () {
  if (window.__PROXY_SHIM_INITIALIZED__) return;
  window.__PROXY_SHIM_INITIALIZED__ = true;

  const ALIAS = window.__PROXY_ALIAS__ || "alpha";
  const PREFIX = `/s/${ALIAS}`;

  const UPSTREAM_HOST_TO_ALIAS = {
    "www.instagram.com": "alpha",
    "instagram.com": "alpha",
    "i.instagram.com": "alpha",
    "graph.instagram.com": "alpha",
    "www.youtube.com": "beta",
    "m.youtube.com": "beta",
    "youtube.com": "beta",
    "music.youtube.com": "beta",
    "www.youtube-nocookie.com": "beta",
    "www.tiktok.com": "gamma",
    "m.tiktok.com": "gamma",
    "tiktok.com": "gamma",
  };

  const CDN_HOST_SUFFIXES = [
    "cdninstagram.com",
    "fbcdn.net",
    "ytimg.com",
    "ggpht.com",
    " tiktokcdn.com",
    "tiktokcdn.com",
    "tiktokv.com",
  ];

  function getCookieValue(name) {
    const match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[2]) : "";
  }

  function rewriteUrl(rawUrl) {
    if (!rawUrl || typeof rawUrl !== "string") return rawUrl;
    const trimmed = rawUrl.trim();
    if (
      trimmed.startsWith("data:") ||
      trimmed.startsWith("blob:") ||
      trimmed.startsWith("javascript:") ||
      trimmed.startsWith("mailto:") ||
      trimmed.startsWith("#")
    ) {
      return rawUrl;
    }

    // Already routed through our proxy prefixes
    if (
      trimmed.startsWith("/s/") ||
      trimmed.startsWith("/r/") ||
      trimmed.startsWith("/cdn/") ||
      trimmed.startsWith("/assets/") ||
      trimmed.startsWith("/api/")
    ) {
      return rawUrl;
    }

    // Protocol-relative URL (//example.com/...)
    let normalized = trimmed;
    if (normalized.startsWith("//")) {
      normalized = "https:" + normalized;
    }

    // Absolute URL (http:// or https://)
    if (/^https?:\/\//i.test(normalized)) {
      try {
        const parsed = new URL(normalized);
        const host = parsed.hostname.toLowerCase();

        // Same proxy host
        if (host === window.location.hostname) {
          if (
            !parsed.pathname.startsWith("/s/") &&
            !parsed.pathname.startsWith("/r/") &&
            !parsed.pathname.startsWith("/cdn/") &&
            !parsed.pathname.startsWith("/assets/")
          ) {
            return `${PREFIX}${parsed.pathname}${parsed.search}${parsed.hash}`;
          }
          return `${parsed.pathname}${parsed.search}${parsed.hash}`;
        }

        // Primary allowlisted target host
        if (UPSTREAM_HOST_TO_ALIAS[host]) {
          const targetAlias = UPSTREAM_HOST_TO_ALIAS[host];
          return `/s/${targetAlias}${parsed.pathname}${parsed.search}${parsed.hash}`;
        }

        // Known CDN host for Instagram, YouTube, or TikTok
        if (CDN_HOST_SUFFIXES.some((suffix) => host === suffix.trim() || host.endsWith("." + suffix.trim()))) {
          return `/cdn/${ALIAS}/${host}${parsed.pathname}${parsed.search}`;
        }
      } catch (_e) {
        return rawUrl;
      }
      return rawUrl;
    }

    // Root-relative URL (/youtubei/v1/..., /api/v1/..., /results?..., /watch?...)
    if (trimmed.startsWith("/")) {
      return `${PREFIX}${trimmed}`;
    }

    return rawUrl;
  }

  // 1. Intercept window.fetch
  const originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = function (input, init) {
      let nextInput = input;
      let nextInit = init ? Object.assign({}, init) : {};

      if (typeof input === "string") {
        nextInput = rewriteUrl(input);
      } else if (input instanceof URL) {
        nextInput = rewriteUrl(input.toString());
      } else if (input && typeof input === "object" && input.url) {
        const rewritten = rewriteUrl(input.url);
        if (rewritten !== input.url) {
          nextInput = new Request(rewritten, input);
        }
      }

      // Attach Instagram CSRF token automatically on state-changing requests
      if (ALIAS === "alpha") {
        const csrf = getCookieValue("csrftoken") || window.__IG_CSRF_TOKEN__ || "missing_csrf_fallback";
        const headers = new Headers(nextInit.headers || (input instanceof Request ? input.headers : undefined) || {});
        if (!headers.has("X-CSRFToken") && csrf) {
          headers.set("X-CSRFToken", csrf);
        }
        if (!headers.has("X-Requested-With")) {
          headers.set("X-Requested-With", "XMLHttpRequest");
        }
        nextInit.headers = headers;
        nextInit.credentials = "include";
      }

      return originalFetch.call(this, nextInput, nextInit);
    };
  }

  // 2. Intercept XMLHttpRequest
  const originalXhrOpen = XMLHttpRequest.prototype.open;
  const originalXhrSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__proxyMethod = (method || "GET").toUpperCase();
    const rewritten = rewriteUrl(typeof url === "string" ? url : String(url));
    return originalXhrOpen.call(this, method, rewritten, ...rest);
  };

  XMLHttpRequest.prototype.send = function (body) {
    if (ALIAS === "alpha") {
      try {
        const csrf = getCookieValue("csrftoken") || window.__IG_CSRF_TOKEN__;
        if (csrf) {
          this.setRequestHeader("X-CSRFToken", csrf);
        }
        this.setRequestHeader("X-Requested-With", "XMLHttpRequest");
        this.withCredentials = true;
      } catch (_e) {}
    }
    return originalXhrSend.call(this, body);
  };

  // 3. Intercept History API so SPA route transitions stay under /s/<alias>
  const originalPushState = History.prototype.pushState;
  const originalReplaceState = History.prototype.replaceState;

  History.prototype.pushState = function (state, title, url) {
    const nextUrl = url ? rewriteUrl(String(url)) : url;
    return originalPushState.call(this, state, title, nextUrl);
  };

  History.prototype.replaceState = function (state, title, url) {
    const nextUrl = url ? rewriteUrl(String(url)) : url;
    return originalReplaceState.call(this, state, title, nextUrl);
  };

  // 4. Intercept Form submissions (e.g., YouTube search box, Instagram login form)
  document.addEventListener(
    "submit",
    function (event) {
      const form = event.target;
      if (!form || form.tagName !== "FORM") return;
      const rawAction = form.getAttribute("action") || window.location.pathname;
      const rewritten = rewriteUrl(rawAction);
      if (rewritten !== rawAction) {
        form.setAttribute("action", rewritten);
      }
    },
    true
  );

  // 5. Intercept link clicks (<a href="...">) so navigation stays inside the proxy
  document.addEventListener(
    "click",
    function (event) {
      const anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
      if (!anchor) return;
      const rawHref = anchor.getAttribute("href");
      if (!rawHref) return;
      const rewritten = rewriteUrl(rawHref);
      if (rewritten !== rawHref) {
        anchor.setAttribute("href", rewritten);
      }
    },
    true
  );

  // 6. Instagram Cookie Banner Consent & Login Unlocker
  if (ALIAS === "alpha") {
    // Set Instagram cookie consent cookies immediately in browser jar
    document.cookie = "ig_nrcb=1; path=/; max-age=31536000; SameSite=Lax";
    document.cookie = "ig_cb=1; path=/; max-age=31536000; SameSite=Lax";
    try {
      localStorage.setItem("ig_nrcb", "1");
      localStorage.setItem("cookie_banner_dismissed", "true");
    } catch (_e) {}

    function dismissInstagramCookieDialog(forceRemove) {
      const dialogs = document.querySelectorAll('div[role="dialog"], div._ab8w, div[class*="x1n2onr6"]');
      dialogs.forEach((dialog) => {
        const text = (dialog.innerText || "").toLowerCase();
        const isCookieDialog =
          text.includes("allow the use of cookies") ||
          text.includes("allow all cookies") ||
          text.includes("decline optional cookies") ||
          text.includes("non-essential cookies") ||
          text.includes("cookies and similar technologies");

        if (isCookieDialog && forceRemove) {
          // Remove the dialog and its backdrop container
          const wrapper = dialog.closest('div[role="presentation"]') || dialog.parentElement || dialog;
          wrapper.style.setProperty("display", "none", "important");
          dialog.style.setProperty("display", "none", "important");
          document.body.style.overflow = "auto";
          document.documentElement.style.overflow = "auto";
        }
      });
    }

    // Listen for clicks on Accept / Allow / Decline cookie buttons
    document.addEventListener(
      "click",
      function (event) {
        const btn = event.target && event.target.closest ? event.target.closest('button, div[role="button"]') : null;
        if (!btn) return;
        const label = (btn.innerText || btn.textContent || "").toLowerCase();
        if (
          label.includes("allow all") ||
          label.includes("accept") ||
          label.includes("decline") ||
          label.includes("essential") ||
          label.includes("cookie")
        ) {
          document.cookie = "ig_nrcb=1; path=/; max-age=31536000; SameSite=Lax";
          document.cookie = "ig_cb=1; path=/; max-age=31536000; SameSite=Lax";
          setTimeout(() => dismissInstagramCookieDialog(true), 80);
        }
      },
      true
    );

    // Also auto-dismiss if localStorage/cookie already recorded consent
    window.addEventListener("DOMContentLoaded", () => {
      if (getCookieValue("ig_nrcb") === "1") {
        setTimeout(() => dismissInstagramCookieDialog(true), 500);
      }
    });
  }
})();
