// X 书签桥 · 页面主世界脚本（document_start 注入）
// 作用：1) 旁听页面自己发出的 /Bookmarks 请求，拿到真实 url + headers（含 bearer / csrf）
//       2) 用同一套 headers 翻页，把所有书签条目捞出来
// 不读 cookie、不存 token，headers 只在页面内存里用一次。
(() => {
  if (window.__XBM_BRIDGE && window.__XBM_BRIDGE.v === 1) return;

  const S = { url: null, headers: null, hits: 0, at: 0 };
  const isBkm = (u) => typeof u === "string" && /\/i\/api\/graphql\/[^/]+\/Bookmarks(\?|$)/.test(u);

  function headersOf(input, init) {
    let h = (init && init.headers) || (input && input.headers);
    if (!h) return null;
    const out = {};
    try {
      if (typeof h.forEach === "function") h.forEach((v, k) => (out[k] = v));
      else if (Array.isArray(h)) h.forEach(([k, v]) => (out[k] = v));
      else Object.keys(h).forEach((k) => (out[k] = h[k]));
    } catch (e) {
      return null;
    }
    return Object.keys(out).length ? out : null;
  }

  const rawFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const u = typeof input === "string" ? input : input && input.url;
      if (isBkm(u)) {
        const h = headersOf(input, init);
        if (h) { S.url = u; S.headers = h; S.hits++; S.at = Date.now(); }
      }
    } catch (e) { /* 旁听失败不影响页面 */ }
    return rawFetch.apply(this, arguments);
  };

  // XHR 兜底（X 偶尔用 XHR）
  const rawOpen = XMLHttpRequest.prototype.open;
  const rawSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__xbmUrl = u;
    return rawOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try {
      if (isBkm(this.__xbmUrl)) {
        if (!this.__xbmH) this.__xbmH = {};
        this.__xbmH[k] = v;
        if (this.__xbmH["authorization"] || this.__xbmH["Authorization"]) {
          S.url = this.__xbmUrl; S.headers = Object.assign({}, this.__xbmH); S.hits++; S.at = Date.now();
        }
      }
    } catch (e) { /* ignore */ }
    return rawSet.apply(this, arguments);
  };

  function pickTimeline(data) {
    if (!data || typeof data !== "object") return null;
    for (const k of Object.keys(data)) {
      const v = data[k];
      if (v && typeof v === "object" && v.timeline && Array.isArray(v.timeline.instructions)) {
        return { key: k, tl: v };
      }
    }
    return null;
  }

  function qsParam(url, name) {
    const q = url.indexOf("?");
    if (q < 0) return "";
    for (const part of url.slice(q + 1).split("&")) {
      const i = part.indexOf("=");
      if (i > 0 && part.slice(0, i) === name) return decodeURIComponent(part.slice(i + 1));
    }
    return "";
  }

  async function collect(maxPages) {
    if (!S.url || !S.headers) {
      return { ok: false, err: "还没抓到书签请求：先在页面里打开 x.com/i/history 的「Bookmarks」标签，等列表刷出来再点同步", pages: 0, count: 0, entries: [] };
    }
    const base = S.url.split("?")[0];
    const features = qsParam(S.url, "features") || "{}";
    const v0 = qsParam(S.url, "variables");
    let vars0 = {};
    try { vars0 = v0 ? JSON.parse(v0) : {}; } catch (e) { vars0 = {}; }
    delete vars0.cursor;

    const seen = {};
    const out = [];
    const log = [];
    let cursor = null;
    let pages = 0;
    let err = "";
    let dry = 0;

    while (pages < (maxPages || 80)) {
      const vars = Object.assign({}, vars0, { count: 100 }, cursor ? { cursor } : {});
      const u = base + "?variables=" + encodeURIComponent(JSON.stringify(vars)) +
        "&features=" + encodeURIComponent(features);
      let r;
      try {
        r = await fetch(u, { headers: S.headers, credentials: "include" });
      } catch (e) {
        err = "请求失败：" + e.message;
        break;
      }
      if (!r.ok) {
        err = "HTTP " + r.status + " " + (await r.text()).slice(0, 120);
        break;
      }
      const j = await r.json();
      const picked = pickTimeline(j.data);
      if (!picked) {
        err = "返回结构不认识：" + JSON.stringify(j.errors || j).slice(0, 160);
        break;
      }
      let next = null;
      let added = 0;
      for (const ins of picked.tl.timeline.instructions || []) {
        for (const e of ins.entries || []) {
          const c = e.content || {};
          if (c.cursorType === "Bottom") next = c.value;
          else if (c.cursorType === "ShowMore" && !next) next = c.value;
          if (c.cursorType || c.entryType === "TimelineTimelineCursor") continue;
          const k = e.entryId || Math.random().toString(36);
          if (seen[k]) continue;
          seen[k] = 1;
          added++;
          out.push(e);
        }
      }
      pages++;
      log.push({ page: pages, added, total: out.length });
      try { window.__XBM_BRIDGE.prog = { pages: pages, count: out.length }; } catch (e) { /* ignore */ }
      if (added === 0) {
        dry++;
        if (dry >= 2) { err = "连续两页没有新内容"; break; }
      } else {
        dry = 0;
      }
      if (!next) { err = "没有下一页游标"; break; }
      cursor = next;
      await new Promise((res) => setTimeout(res, 1200));
    }
    const items = [];
    for (const e of out) {
      const r = (((e.content || {}).itemContent || {}).tweet_results || {}).result;
      if (!r || typeof r !== "object") continue;
      let t = r.__typename === "TweetWithVisibilityResults" && r.tweet ? r.tweet : r;
      if (t && t.rest_id) items.push(t);
    }
    return { ok: items.length > 0, err: err, pages: pages, count: items.length,
             raw: out.length, log: log, items: items };
  }

  window.__XBM_BRIDGE = {
    v: 1,
    state: () => ({ captured: !!S.url, hits: S.hits, at: S.at }),
    collect: collect,
  };
})();
