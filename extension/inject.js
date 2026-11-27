// X 书签桥 · 页面主世界脚本（document_start 注入）
// 作用：1) 旁听页面自己发出的 /Bookmarks 请求，拿到真实 url + headers（含 bearer / csrf）
//       2) 用同一套 headers 翻页，把所有书签条目捞出来
// 不读 cookie、不存 token，headers 只在页面内存里用一次。
(() => {
  if (window.__XBM_BRIDGE && window.__XBM_BRIDGE.v === 2) return;

  const S = { url: null, headers: null, hits: 0, at: 0, lastErr: "" };
  const isBkm = (u) => typeof u === "string" && /\/i\/api\/graphql\/[^/]+\/Bookmarks(\?|$)/.test(u);

  // 重放时不能带这些：由浏览器自己算/会和安全校验冲突
  const DROP = { "content-length": 1, "accept-encoding": 1, host: 1, connection: 1, cookie: 1 };

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

  function cleanHeaders(h) {
    const out = {};
    Object.keys(h).forEach((k) => { if (!DROP[k.toLowerCase()]) out[k] = h[k]; });
    return out;
  }

  function hash(s) {
    let x = 2166136261;
    for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); }
    return (x >>> 0).toString(36);
  }

  const rawFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const u = typeof input === "string" ? input : (input && input.url);
      if (isBkm(u)) {
        S.url = u;
        S.at = Date.now();
        const h = headersOf(input, init);
        if (h) { S.headers = cleanHeaders(h); S.hits++; S.lastErr = ""; }
        else { S.lastErr = "抓到了书签请求，但读不到它的请求头（X 可能换了调用方式）"; }
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
        const lk = String(k).toLowerCase();
        if (lk === "authorization" || lk === "x-csrf-token") {
          S.url = this.__xbmUrl;
          S.headers = cleanHeaders(this.__xbmH);
          S.hits++;
          S.at = Date.now();
          S.lastErr = "";
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

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  async function collect(maxPages) {
    if (!S.url) {
      return { ok: false, err: "还没抓到书签请求：先在页面里打开 x.com/i/history 的「书签」标签，等列表刷出来再点同步", pages: 0, count: 0, items: [] };
    }
    if (!S.headers) {
      return { ok: false, err: S.lastErr || "抓到了书签请求但没读到请求头，请刷新页面后重试", pages: 0, count: 0, items: [] };
    }
    const base = S.url.split("?")[0];
    const features = qsParam(S.url, "features");
    if (!features) {
      return { ok: false, err: "书签请求里没有 features 参数，没法照它的签名翻页：请刷新 x.com 后再试", pages: 0, count: 0, items: [] };
    }
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
      if (r.status === 429) {
        await wait(60000);            // 限流了就等一分钟，再给一次机会
        try { r = await fetch(u, { headers: S.headers, credentials: "include" }); } catch (e) { err = "请求失败：" + e.message; break; }
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
          // entryId 缺失时用内容哈希当 key，保证同一页重放不会重复计入
          const k = e.entryId || ("h" + hash(JSON.stringify(e).slice(0, 400)));
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
      if (!next) { err = ""; break; }   // 到底了，不是错误
      cursor = next;
      await wait(1200);
    }
    const items = [];
    for (const e of out) {
      const r = (((e.content || {}).itemContent || {}).tweet_results || {}).result;
      if (!r || typeof r !== "object") continue;
      const t = r.__typename === "TweetWithVisibilityResults" && r.tweet ? r.tweet : r;
      if (t && t.rest_id) items.push(t);
    }
    return { ok: items.length > 0, err: items.length ? err : (err || "一页都没抓到条目"), pages: pages, count: items.length, raw: out.length, log: log, items: items };
  }

  // 直接调用（不依赖页面自己发请求）：X 的 Bookmarks 接口用「公开 web bearer + ct0 头」
  // 就能调通，features 传 {} 服务端也接受（2026-11 实测 200）。
  // 这样即使页面从缓存直接渲染、一个请求都不发，也能把书签抓下来。
  let QUERY_ID = "jbqsfdzI0EMFHAtpEC6vyA";

  function queryId() {
    try {
      const m = /\/i\/api\/graphql\/([^/]+)\/Bookmarks/.exec(S.url || "");
      if (m) { QUERY_ID = m[1]; return m[1]; }
    } catch (e) { /* ignore */ }
    return QUERY_ID;
  }

  async function direct(maxPages) {
    const ct0 = (document.cookie.match(/(?:^|; )ct0=([^;]+)/) || [])[1] || "";
    if (!ct0) return { ok: false, err: "这个标签页里没有 ct0 cookie（没登录，或不是 x.com 页面）", pages: 0, count: 0, raw: 0, items: [] };
    const headers = {
      authorization: "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
      "x-csrf-token": ct0,
      "x-twitter-auth-type": "OAuth2Session",
      "x-twitter-active-user": "yes",
      "content-type": "application/json",
    };
    const base = "https://x.com/i/api/graphql/" + queryId() + "/Bookmarks";
    S.headers = headers;
    S.url = base + "?variables=" + encodeURIComponent(JSON.stringify({ count: 100 })) + "&features=" + encodeURIComponent("{}");

    const seen = {};
    const out = [];
    const log = [];
    let cursor = null, pages = 0, err = "", dry = 0;

    while (pages < (maxPages || 80)) {
      const vars = { count: 100, includePromotedContent: true };
      if (cursor) vars.cursor = cursor;
      const u = base + "?variables=" + encodeURIComponent(JSON.stringify(vars)) + "&features=" + encodeURIComponent("{}");
      let r;
      try {
        r = await fetch(u, { headers: headers, credentials: "include" });
      } catch (e) {
        err = "请求失败：" + e.message;
        break;
      }
      if (r.status === 429) { await wait(60000); try { r = await fetch(u, { headers: headers, credentials: "include" }); } catch (e) { err = "请求失败：" + e.message; break; } }
      if (!r.ok) { err = "HTTP " + r.status + " " + (await r.text()).slice(0, 140); break; }
      const j = await r.json();
      const picked = pickTimeline(j.data);
      if (!picked) { err = "返回结构不认识：" + JSON.stringify(j.errors || j).slice(0, 160); break; }
      let next = null, added = 0;
      for (const ins of picked.tl.timeline.instructions || []) {
        for (const e of ins.entries || []) {
          const c = e.content || {};
          if (c.cursorType === "Bottom") {
            // 优先取 entryId 里带 cursor-bottom 的那个，避免拿到别的游标
            if (!next || /cursor-bottom/i.test(String(e.entryId || ""))) next = c.value;
          } else if (c.cursorType === "ShowMore" && !next) next = c.value;
          if (c.cursorType || c.entryType === "TimelineTimelineCursor") continue;
          const k = e.entryId || ("h" + hash(JSON.stringify(e).slice(0, 400)));
          if (seen[k]) continue;
          seen[k] = 1; added++; out.push(e);
        }
      }
      pages++;
      log.push({ page: pages, added, total: out.length });
      try { window.__XBM_BRIDGE.prog = { pages: pages, count: out.length }; } catch (e) { /* ignore */ }
      if (added === 0) { dry++; if (dry >= 3) { err = "连续三页没有新内容"; break; } } else { dry = 0; }
      if (!next) { err = ""; break; }
      if (next === cursor) { err = "游标没有前进，停止翻页"; break; }
      cursor = next;
      await wait(1100);
    }

    const items = [];
    for (const e of out) {
      const r2 = (((e.content || {}).itemContent || {}).tweet_results || {}).result;
      if (!r2 || typeof r2 !== "object") continue;
      const t = r2.__typename === "TweetWithVisibilityResults" && r2.tweet ? r2.tweet : r2;
      if (t && t.rest_id) items.push(t);
    }
    return { ok: items.length > 0, err: items.length ? err : (err || "一页都没抓到条目"), pages: pages, count: items.length, raw: out.length, log: log, items: items };
  }

  window.__XBM_BRIDGE = {
    v: 2,
    state: () => ({ captured: !!S.url, hits: S.hits, at: S.at, lastErr: S.lastErr }),
    collect: collect,
    direct: direct,
  };
})();
