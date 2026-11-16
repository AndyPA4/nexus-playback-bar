importScripts("bridge-config.js");

chrome.action.onClicked.addListener(async (tab) => {
  try {
    await chrome.scripting.executeScript({
      target: {tabId: tab.id, allFrames: true},
      files: ["player.js"]
    }).catch(() => chrome.scripting.executeScript({
      target: {tabId: tab.id},
      files: ["player.js"]
    }));
    await chrome.action.setBadgeText({tabId: tab.id, text: "ON"});
    await chrome.action.setTitle({tabId: tab.id, title: "NEXUS enabled on this tab"});
  } catch {
    await chrome.action.setBadgeText({tabId: tab.id, text: "!"});
    await chrome.action.setTitle({tabId: tab.id, title: "Chrome could not connect the NEXUS to this page"});
  }
});

const pageTitles = new Map();
const posters = new Map();
const primePosters = new Map();
const primeKey = value => (typeof value === "string" ? value : "").toLowerCase()
  .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
function amazonPoster(value) {
  if (typeof value !== "string" || value.length > 1800) return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port &&
      (url.hostname === "images.amazon.com" || url.hostname.endsWith(".media-amazon.com") ||
       url.hostname.endsWith(".ssl-images-amazon.com")) ? url.href : "";
  } catch { return ""; }
}
function posterData(url) {
  if (!/^https?:\/\//i.test(url || "")) return "";
  if (!posters.has(url)) {
    if (posters.size >= 16) posters.delete(posters.keys().next().value);
    const entry = {data: "", started: Date.now()};
    posters.set(url, entry);
    (async () => {
      const response = await fetch(url, {credentials: "omit", signal: AbortSignal.timeout(3500)});
      if (!response.ok || !response.headers.get("content-type")?.startsWith("image/")) return "";
      const blob = await response.blob();
      if (blob.size > 4000000) return "";
      const bitmap = await createImageBitmap(blob);
      try {
        const scale = Math.min(1, 192 / Math.max(bitmap.width, bitmap.height));
        const canvas = new OffscreenCanvas(Math.max(1, Math.round(bitmap.width * scale)),
                                           Math.max(1, Math.round(bitmap.height * scale)));
        canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        const jpeg = await canvas.convertToBlob({type: "image/jpeg", quality: 0.76});
        const bytes = new Uint8Array(await jpeg.arrayBuffer());
        return bytes.length <= 24000 ? "data:image/jpeg;base64," + btoa(String.fromCharCode(...bytes)) : "";
      } finally {
        bitmap.close();
      }
    })().then(data => { entry.data = data; }, () => {});
  }
  const entry = posters.get(url);
  if (!entry.data && Date.now() - entry.started > 30000) posters.delete(url);
  return entry.data;
}
chrome.tabs.onRemoved.addListener(id => pageTitles.delete(id));
chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.url || change.title || change.status === "loading") pageTitles.delete(id);
});

function pageTitle(tab) {
  const cached = pageTitles.get(tab.id);
  if (cached && Date.now() - cached.time < 2000) return cached.value;
  const value = chrome.tabs.sendMessage(tab.id, {type: "nexus-page-title"}, {frameId: 0})
    .catch(() => ({title: tab.title}))
    .then(page => ({poster: page?.poster, prime: page?.prime, title: (typeof page?.title === "string" ? page.title : "")
      .replace(/^\s*watch\s+/i, "")
      .replace(/\s+(?:full movie|full episode|online|free online)\b.*$/i, "")
      .replace(/\s*[|·–—-]\s*(?:myflixer|watch free|watch online).*$/i, "")
      .trim().slice(0, 160)}));
  pageTitles.set(tab.id, {time: Date.now(), value});
  return value;
}

chrome.runtime.onMessage.addListener((state, sender, reply) => {
  if (!sender.url || !state) return;
  const source = new URL(sender.url);
  if (state.type === "nexus-prime-posters") {
    if (source.protocol !== "https:" || sender.frameId !== 0 ||
        (!/(^|\.)primevideo\.com$/.test(source.hostname) &&
         !(source.hostname === "www.amazon.com" &&
           /^\/(?:gp\/video|Amazon-Video|Prime-Video|prime-video|detail\/|dp\/)/i.test(source.pathname))) ||
        !Array.isArray(state.posters)) return;
    for (const entry of state.posters.slice(0, 41)) {
      const key = primeKey(entry?.title), poster = amazonPoster(entry?.poster);
      if (!key || key.length > 160 || !poster) continue;
      primePosters.delete(key);
      if (primePosters.size >= 96) primePosters.delete(primePosters.keys().next().value);
      primePosters.set(key, poster);
    }
    return;
  }
  if (typeof state.id !== "string") return;
  if (!["https:", "http:"].includes(source.protocol) || source.hostname !== state.site) return;
  const embedded = sender.frameId > 0 && sender.tab && sender.tab.id >= 0;
  const metadata = embedded ? pageTitle(sender.tab) : Promise.resolve({});
  metadata.then(({title, poster, prime}) => {
    if (prime && state.prime && state.title &&
        (!title || state.title.toLowerCase() !== title.toLowerCase())) {
      title = "";
      poster = "";
    }
    const primePlayback = state.prime === true || prime === true;
    const primePoster = primePlayback && (
      amazonPoster(state.artwork) || amazonPoster(poster) || primePosters.get(primeKey(title || state.title)) || ""
    );
    const artwork = primePlayback ? posterData(primePoster) || primePoster
      : posterData(poster) || posterData(state.artwork) || state.artwork;
    return fetch("http://127.0.0.1:18763/media", {
    method: "POST",
    signal: AbortSignal.timeout(2200),
    headers: {"Content-Type": "application/json", "X-Nexus-Bridge": globalThis.NEXUS_BRIDGE_KEY},
    body: JSON.stringify({...state, ...(title ? {title, credit: ""} : {}), artwork})
  });
  }).then(r => r.ok ? r.json() : null).then(reply, () => reply(null));
  return true;
});


