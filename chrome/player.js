(() => {
  if (globalThis.nexusPlaybackStarted) return;
  globalThis.nexusPlaybackStarted = true;
  const prime = /(^|\.)primevideo\.com$/i.test(location.hostname) ||
      (location.hostname === "www.amazon.com" &&
       /^\/(?:gp\/video|Amazon-Video|Prime-Video|prime-video|detail\/|dp\/)/i.test(location.pathname));

  function artworkUrl(...candidates) {
    for (const candidate of candidates.flat(2)) {
      try {
        const raw = typeof candidate === "string" ? candidate : candidate?.url || candidate?.contentUrl;
        if (!raw) continue;
        const url = new URL(raw, document.baseURI);
        if (["http:", "https:"].includes(url.protocol)) return url.href;
      } catch {}
    }
    return "";
  }

  function pageMetadata(video) {
    const meta = key => document.querySelector(`meta[property="${key}"], meta[name="${key}"]`)?.content;
    const structured = [];
    function collect(value) {
      if (!value || typeof value !== "object") return;
      if (Array.isArray(value)) { value.forEach(collect); return; }
      structured.push(value);
      collect(value["@graph"]);
      collect(value.mainEntity);
    }
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try { collect(JSON.parse(node.textContent)); } catch {}
    }
    const entries = structured.filter(value => [value["@type"]].flat().some(
      type => ["Movie", "TVEpisode", "TVSeries", "VideoObject", "MusicRecording"].includes(type)));
    const entry = entries[0];
    if (prime) {
      const clean = value => (typeof value === "string" ? value : "").trim()
        .replace(/^(?:Prime Video|Amazon(?:\.com)?)\s*[:|–—-]\s*/i, "")
        .replace(/\s*[:|–—-]\s*(?:Prime Video|Amazon(?:\.com)?)$/i, "")
        .replace(/^Watch\s+/i, "").trim();
      const key = value => clean(value).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
      const text = selector => document.querySelector(selector)?.textContent?.trim();
      const session = navigator.mediaSession?.metadata;
      const hero = text('[data-testid="hero-title"]');
      const social = meta("og:title") || meta("twitter:title");
      const detail = entries.find(value => [value["@type"]].flat().some(
        type => ["Movie", "TVEpisode", "TVSeries"].includes(type)));
      const title = [text(".atvwebplayersdk-title-text"), hero, detail?.name,
        social, session?.title, document.title].map(clean).find(value =>
          value && !/^(?:amazon(?:\.com)?|prime video|home|watch now)$/i.test(value)) || "";
      const matches = value => !!title && key(value) === key(title);
      const matching = entries.filter(value => matches(value.name));
      const heroImage = matches(hero) && document.querySelector(
        'img[data-testid="hero-image"], [data-testid="hero-image"] img, img[data-testid="hero-background-image"]');
      const poster = artworkUrl(
        matching.flatMap(value => [value.image, value.thumbnailUrl].flat()),
        heroImage?.currentSrc, heroImage?.src,
        matches(social) ? [meta("og:image"), meta("twitter:image")] : [],
        matches(session?.title) ? (session?.artwork || []).map(image => image.src).reverse() : []
      );
      return {title, poster, prime: true};
    }
    const poster = document.querySelector(".detail_page-watch .film-poster img") ||
        document.querySelector(".movie-poster img, img[itemprop='image'], .film-poster img");
    return {
      title: entry?.name || meta("og:title") || document.title,
      poster: artworkUrl(poster?.dataset.src, poster?.currentSrc, poster?.src, video?.poster,
                         entry?.thumbnailUrl, entry?.image, meta("og:image"), meta("twitter:image"))
    };
  }
  if (window === window.top) {
    chrome.runtime.onMessage.addListener((message, _sender, reply) => {
      if (message?.type !== "nexus-page-title") return;
      reply(pageMetadata());
    });
  }
  if (location.hostname === "www.amazon.com" &&
      !/^\/(?:gp\/video|Amazon-Video|Prime-Video|prime-video|detail\/|dp\/)/i.test(location.pathname)) return;
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
                        byte => byte.toString(16).padStart(2, "0")).join("");
  let media, source, generation = 0, played = 0, busy = false;
  let page = {}, pageKey, pageRead = -Infinity;
  const playedMedia = new WeakSet();
  function notePlay(event) {
    if (event.target instanceof HTMLMediaElement) {
      playedMedia.add(event.target);
      media = event.target;
      played = Date.now();
    }
  }
  document.addEventListener("play", notePlay, true);
  const spotify = location.hostname === "open.spotify.com";
  const soundCloud = ["soundcloud.com", "www.soundcloud.com"].includes(location.hostname);
  const appleMusic = location.hostname === "music.apple.com";
  let clockSample, lastWebDuration;
  let webTrack, webPlaying = false, webPlayed = 0, webGeneration = 0;

  function seconds(text) {
    const match = text?.match(/\b\d+(?::\d{2}){1,2}\b/);
    return match ? match[0].split(":").reduce((total, part) => total * 60 + Number(part), 0) : NaN;
  }

  function smoothWebTime(key, position, playing, now) {
    const previous = clockSample;
    let estimate = position;
    if (previous?.key === key) {
      const predicted = previous.position + (previous.playing ? (now - previous.time) / 1000 : 0);
      if (Math.abs(predicted - position) <= 1.1) {
        estimate = Math.max(position, Math.min(position + 0.99, predicted));
      }
    }
    clockSample = {key, position: estimate, playing, time: now};
    return estimate;
  }

  function webPlayback(native) {
    if (window !== window.top || (!spotify && !soundCloud)) return;
    const find = selector => document.querySelector(selector);
    const text = selector => find(selector)?.textContent.trim();
    const timelineTime = className => seconds(text(className + ' [aria-hidden="true"]') || text(className));
    const metadata = navigator.mediaSession?.metadata;
    const track = find(spotify
      ? '[data-testid="now-playing-widget"] a[href*="/track/"], [data-testid="now-playing-widget"] a[href*="/episode/"], [data-testid="context-item-link"]'
      : '.playbackSoundBadge__titleLink');
    let title = (track?.getAttribute("title") || track?.textContent || metadata?.title || "").trim();
    if (soundCloud) title = title.replace(/^current track:\s*/i, "");
    const credit = text(spotify
      ? '[data-testid="context-item-info-artist"], [data-testid="now-playing-widget"] a[href*="/artist/"]'
      : '.playbackSoundBadge__lightLink') || metadata?.artist;
    const position = spotify ? seconds(text('[data-testid="playback-position"]'))
      : timelineTime(".playbackTimeline__timePassed");
    const durationLabel = spotify ? text('[data-testid="playback-duration"]') : "";
    let duration = spotify ? seconds(durationLabel) : timelineTime(".playbackTimeline__duration");
    if (spotify && /^[-\u2212]/.test(durationLabel || "")) duration += position;
    const toggle = find(spotify ? '[data-testid="control-button-playpause"]' : '.playControls__play');
    if (!title || !toggle || !Number.isFinite(position) || !Number.isFinite(duration) || duration <= 0) return;
    const label = toggle.getAttribute("aria-label") || toggle.title || "";
    const playing = spotify ? (/^pause\b/i.test(label) ||
      (!/^play\b/i.test(label) && navigator.mediaSession?.playbackState === "playing"))
      : toggle.classList.contains("playing") || /^pause\b/i.test(toggle.title);
    const slider = find(spotify ? '[data-testid="playback-progressbar"] input[type="range"]'
      : '.playbackTimeline__progressWrapper');
    const seekable = !!slider && !slider.disabled && slider.getAttribute("aria-disabled") !== "true";
    const art = find(spotify ? '[data-testid="now-playing-widget"] img'
      : '.playbackSoundBadge__avatar .sc-artwork, .playbackSoundBadge__avatar img');
    const artwork = artworkUrl(art?.currentSrc, art?.src, art?.getAttribute?.("data-src"),
      ...(metadata?.title?.trim() === title ? [...(metadata.artwork || [])].reverse().map(image => image.src) : []),
      art?.style?.backgroundImage?.match(/url\(["']?([^"')]+)["']?\)/)?.[1]);
    const trackKey = track?.href || JSON.stringify([title, credit]);
    if (trackKey !== webTrack) {
      lastWebDuration = undefined;
      webTrack = trackKey;
      webGeneration++;
      webPlayed = Date.now();
    } else if (playing && !webPlaying) webPlayed = Date.now();
    webPlaying = playing;
    const key = id + ":web:" + webGeneration;
    if (lastWebDuration !== undefined && Math.abs(duration - lastWebDuration) <= 1 &&
        position <= lastWebDuration) duration = lastWebDuration;
    lastWebDuration = duration;
    const nativeMatches = native && Math.abs(native.duration - duration) < 2 &&
      Math.abs(native.currentTime - position) < 2;
    const progress = nativeMatches ? native.currentTime
      : smoothWebTime(key, position, playing, performance.now());
    if (nativeMatches) duration = native.duration;
    return {state: {id: key, position: Math.min(progress, duration),
      duration, playing, seekable, played: webPlayed, site: location.hostname,
      title: title.slice(0, 160), credit: (credit || "").slice(0, 100), rate: 1,
      artwork: artwork.slice(0, 800)}, slider};
  }

  function seekWeb(slider, fraction) {
    if (spotify) {
      const min = slider.min === "" ? 0 : Number(slider.min);
      const max = slider.max === "" ? 100 : Number(slider.max);
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(slider, min + fraction * (max - min));
      slider.dispatchEvent(new Event("input", {bubbles: true}));
      slider.dispatchEvent(new Event("change", {bubbles: true}));
      return;
    }
    const rect = slider.getBoundingClientRect();
    if (!rect.width) return;
    const mouse = {bubbles: true, clientX: rect.left + fraction * rect.width,
      clientY: rect.top + rect.height / 2, button: 0};
    slider.dispatchEvent(new MouseEvent("mousedown", {...mouse, buttons: 1}));
    slider.dispatchEvent(new MouseEvent("mouseup", mouse));
    slider.dispatchEvent(new MouseEvent("click", mouse));
  }

  async function send(state) {
    let timeout;
    try {
      return await Promise.race([chrome.runtime.sendMessage(state),
        new Promise(resolve => { timeout = setTimeout(() => resolve(null), 3000); })]);
    } finally {
      clearTimeout(timeout);
    }
  }

  async function update() {
    if (busy) return;
    const nodes = [...document.querySelectorAll("video,audio")];
    const valid = el => el && el.isConnected && Number.isFinite(el.duration) && el.duration > 0;
    const pick = nodes.find(el => valid(el) && !el.paused && !el.ended && el.playbackRate > 0) ||
                 (valid(media) && playedMedia.has(media) ? media : null);
    if (spotify || soundCloud) {
      const web = webPlayback(pick);
      if (web) {
        busy = true;
        try {
          const next = await send(web.state);
          if (web.state.seekable && Number.isFinite(next?.seek)) {
            const current = webPlayback(pick);
            if (current?.state.id === web.state.id && current.state.seekable)
              seekWeb(current.slider, Math.max(0, Math.min(1, next.seek / current.state.duration)));
          }
        } catch {
          if (!chrome.runtime?.id) clearInterval(timer);
        } finally {
          busy = false;
        }
        return;
      }
    }
    // Spotify may expose a seekable preview or ad with its own short timeline.
    if (!pick || spotify) return;
    playedMedia.add(pick);
    if (pick !== media || pick.currentSrc !== source) {
      media = pick;
      source = pick.currentSrc;
      generation++;
      if (!pick.paused) played = Date.now();
    }
    const key = id + ":" + generation;
    const ad = location.hostname === "www.youtube.com" &&
               !!pick.closest(".ad-showing,.ad-interrupting");
    const seekable = !ad && pick.seekable.length > 0;
    const metadata = navigator.mediaSession?.metadata;
    if (pageKey !== location.href || performance.now() - pageRead >= 2000) {
      page = pageMetadata(pick);
      pageKey = location.href;
      pageRead = performance.now();
    }
    const title = prime ? page.title : metadata?.title || (location.hostname === "www.youtube.com"
        ? document.title.replace(/\s*-\s*YouTube$/, "") : page.title);
    const site = location.hostname;
    const credit = site === "www.youtube.com"
        ? document.querySelector("ytd-watch-metadata #owner ytd-channel-name a")?.textContent?.trim() || metadata?.artist
        : (site === "music.amazon.com" || appleMusic) ? metadata?.artist : metadata?.album || metadata?.artist;
    const videoId = location.hostname === "www.youtube.com" &&
        (new URL(location.href).searchParams.get("v") || location.pathname.match(/^\/shorts\/([^/]+)/)?.[1]);
    const sessionArt = [...(metadata?.artwork || [])].reverse().map(image => image.src);
    const artwork = videoId && /^[\w-]{11}$/.test(videoId)
        ? `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`
        : prime ? page.poster : artworkUrl(sessionArt, appleMusic ? "" : page.poster);
    busy = true;
    try {
      const next = await send({
        id: key, position: pick.currentTime, duration: pick.duration,
        playing: !pick.paused && !pick.ended, seekable, played, site: location.hostname,
        title: title.trim().slice(0, 160), credit: (credit || "").trim().slice(0, 100), rate: pick.playbackRate,
        artwork: artwork.slice(0, 800), prime
      });
      if (!next || !Number.isFinite(next.seek) || !seekable ||
          media !== pick || source !== pick.currentSrc || !pick.isConnected) return;
      if (Math.abs(pick.currentTime - next.seek) < 0.05) return;
      for (let i = 0; i < pick.seekable.length; i++) {
        const start = pick.seekable.start(i), end = pick.seekable.end(i);
        if (next.seek >= start - 0.25 && next.seek <= end + 0.25) {
          pick.currentTime = Math.max(start, Math.min(end, next.seek));
          break;
        }
      }
    } catch (error) {
      if (!chrome.runtime?.id) {
        clearInterval(timer);
        for (const name of events) document.removeEventListener(name, update, true);
      }
    } finally {
      busy = false;
    }
  }
  const timer = setInterval(update, 50);
  const events = ["timeupdate", "playing", "pause", "seeked", "loadedmetadata"];
  for (const name of events) {
    document.addEventListener(name, update, true);
  }
  update();
})();


