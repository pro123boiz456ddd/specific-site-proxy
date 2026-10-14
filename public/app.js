/**
 * Client-side controller for opening the three allowlisted applications
 * via keyword-free alias routes (/s/<alias>) and Base64URL-encoded subpaths (/r/<alias>/<token>).
 */

function encodeBase64Url(input) {
  if (!input || input === "/") return "";
  const utf8Bytes = new TextEncoder().encode(input);
  let binary = "";
  for (const b of utf8Bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function buildRelayUrl(alias, subpath = "/") {
  const token = encodeBase64Url(subpath);
  return token ? `/r/${alias}/${token}` : `/s/${alias}`;
}

async function checkRelayHealth() {
  const dot = document.getElementById("health-dot");
  const meta = document.getElementById("health-meta");
  try {
    const resp = await fetch("/health");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    dot.classList.add("online");
    meta.textContent = `Relay Active (${data.bind}) • Preset Allowlist Enforced`;
  } catch (err) {
    dot.classList.remove("online");
    meta.textContent = `Relay status check failed (${err.message})`;
  }
}

function openInViewport(alias, subpath = "/") {
  const relayUrl = buildRelayUrl(alias, subpath);
  const iframe = document.getElementById("relay-viewport");
  const overlay = document.getElementById("empty-viewport-overlay");
  const routeDisplay = document.getElementById("active-route-display");
  const reloadBtn = document.getElementById("reload-viewport-btn");
  const popoutLink = document.getElementById("popout-viewport-link");

  overlay.classList.add("hidden");
  iframe.src = relayUrl;
  routeDisplay.textContent = relayUrl;
  reloadBtn.disabled = false;
  popoutLink.href = relayUrl;
  popoutLink.classList.remove("disabled-link");
}

document.addEventListener("DOMContentLoaded", () => {
  checkRelayHealth();

  document.querySelectorAll(".launch-frame-btn, .chip-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const alias = btn.getAttribute("data-alias");
      const subpath = btn.getAttribute("data-subpath") || "/";
      openInViewport(alias, subpath);
    });
  });

  const reloadBtn = document.getElementById("reload-viewport-btn");
  reloadBtn.addEventListener("click", () => {
    const iframe = document.getElementById("relay-viewport");
    if (iframe.src) {
      iframe.contentWindow.location.reload();
    }
  });
});
