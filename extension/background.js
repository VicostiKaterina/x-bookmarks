// X 书签桥 · 后台（service worker）
// popup 只负责点按钮；真正的「抓取 + POST 到本机 App」都在这里做，
// 这样即使 popup 被关掉，同步也会继续跑完。

const DEFAULT_SERVER = "http://127.0.0.1:8900";
const MAX_PAGES = 80;
const CHUNK = 200;          // 每次从页面搬多少条回 SW，避免一次 structured-clone 上百万字符
const OPEN_TRIES = 3;       // 打开书签页的尝试次数（后两次会 reload）

let BUSY = false;           // 简单并发锁：popup 被点两次也只跑一份

async function server() {
  const d = await chrome.storage.local.get({ server: DEFAULT_SERVER });
  return String(d.server || DEFAULT_SERVER).replace(/\/+$/, "");
}

async function reportExtlog(stage, msg, pageUrl) {
  // 尽力而为：失败原因回传到 App 的 /api/extlog，方便事后看卡在哪一步
  try {
    const base = await server();
    const body = JSON.stringify({
      stage: stage || "", msg: msg || "", url: pageUrl || "",
      ver: (chrome.runtime.getManifest && chrome.runtime.getManifest().version) || "",
    });
    await fetch(base + "/api/extlog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
    });
  } catch (e) { /* 静默 */ }
}

async function setStatus(st, fresh) {
  const cur = fresh ? {} : ((await chrome.storage.local.get({ status: null })).status || {});
  const next = Object.assign({}, cur, st, { at: Date.now() });
  await chrome.storage.local.set({ status: next });
  return next;
}

async function getStatus() {
  return (await chrome.storage.local.get({ status: null })).status;
}

async function inPage(tabId, func, args) {
  const out = await chrome.scripting.executeScript({
    target: { tabId: tabId },
    world: "MAIN",
    func: func,
    args: args || [],
  });
  return out && out[0] ? out[0].result : null;
}

// 带错误文本的版本：页面没注入脚本、标签页被关掉时能拿到原因，而不是静默 null
async function tryInPage(tabId, func, args) {
  try {
    return { ok: true, v: await inPage(tabId, func, args) };
  } catch (e) {
    return { ok: false, err: String((e && e.message) || e) };
  }
}

function pageCollect(n) {
  const B = window.__XBM_BRIDGE;
  if (!B) return { started: false, err: "页面里没有桥接脚本：请在 x.com 标签页按 F5 刷新后再试" };
  B.prog = { pages: 0, count: 0 };
  B.result = null;
  B.collect(n)
    .then((r) => { B.result = r; })
    .catch((e) => { B.result = { ok: false, err: "抓取异常：" + (e && e.message), items: [] }; });
  return { started: true };
}

// 让页面自己构造请求（不依赖页面有没有发过 Bookmarks 请求）
function pageDirect(n) {
  const B = window.__XBM_BRIDGE;
  if (!B) return { started: false, err: "页面里没有桥接脚本：请到 chrome://extensions 点一次 ↻ 再试" };
  if (!B.direct) return { started: false, err: "扩展的页面脚本是旧版：请到 chrome://extensions 点一次 ↻ 再试" };
  B.prog = { pages: 0, count: 0 };
  B.result = null;
  B.direct(n)
    .then((r) => { B.result = r; })
    .catch((e) => { B.result = { ok: false, err: "抓取异常：" + (e && e.message), items: [] }; });
  return { started: true };
}

function pageProgress() {
  const B = window.__XBM_BRIDGE;
  if (!B) return { alive: false };
  return { alive: true, prog: B.prog || null, done: !!B.result };
}

function pageMeta() {
  const B = window.__XBM_BRIDGE;
  if (!B || !B.result) return null;
  const r = B.result;
  return { ok: !!r.ok, count: (r.items || []).length, pages: r.pages || 0, err: r.err || "" };
}

function pageSlice(from, size) {
  const B = window.__XBM_BRIDGE;
  if (!B || !B.result) return null;
  const a = B.result.items || [];
  return { total: a.length, items: a.slice(from, from + size) };
}

function pageHandle() {
  const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  return a ? (a.getAttribute("href") || "").replace("/", "") : "";
}

// 「书签」标签：优先按链接认（不受界面语言影响），再退回多语言文案。
// force=true 时，如果书签标签已经是选中态，会先切到「Likes」再让它切回来——
// X 对已选中的标签再点一次不会重新发请求，只有切走再切回才会。
function pageClickBookmarks(force) {
  const WORD = /bookmark|saved|书签|收藏|ブックマーク|保存済み|북마크|marcador|guardad|favorit|segnalibri|gespeichert|enregistr/i;
  const LIKE = /\blikes?\b|喜欢|赞|いいね|좋아요|me gusta|mi piace|gefällt/i;
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  const label = (x) => ((x.textContent || "") + " " + (x.getAttribute("aria-label") || "")).trim();
  const bm = tabs.find((x) => WORD.test(label(x)));
  const lk = tabs.find((x) => LIKE.test(label(x)));
  const sel = (el) => !!el && el.getAttribute("aria-selected") === "true";

  if (bm && force && sel(bm) && lk) { lk.click(); return { ok: true, label: "switched to likes", need: true }; }
  if (bm) { bm.click(); return { ok: true, label: (bm.textContent || "").trim() }; }

  const byHref = document.querySelector('[role="tab"][href*="/bookmarks"], a[href*="/i/history/bookmarks"]');
  if (byHref) { byHref.click(); return { ok: true, label: "href" }; }
  return { ok: false, tabs: tabs.map((x) => (x.textContent || "").trim()).slice(0, 10) };
}

// 往下滚到底部：列表已经渲染出来（来自缓存）时，滚动会触发「加载更多/翻页」，
// 那个请求同样是 /Bookmarks，照样能旁听到它的 url 和签名头。
function pageScrollBottom() {
  try {
    const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
    window.scrollTo(0, h);
    window.dispatchEvent(new Event("scroll"));
    document.dispatchEvent(new Event("scroll"));
    const st = window.__XBM_BRIDGE && window.__XBM_BRIDGE.state ? window.__XBM_BRIDGE.state() : null;
    return { h: h, y: window.scrollY || 0, captured: !!st && !!st.captured };
  } catch (e) { return { err: String(e) }; }
}

function pageVisibility() {
  try {
    return {
      vis: document.visibilityState || "",
      hasFocus: !!document.hasFocus(),
      y: window.scrollY || 0,
      h: Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement ? document.documentElement.scrollHeight : 0),
      bridge: !!window.__XBM_BRIDGE,
    };
  } catch (e) { return { err: String(e) }; }
}

function pageBridgeState() {
  return window.__XBM_BRIDGE ? window.__XBM_BRIDGE.state() : { captured: false, hits: 0 };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 等标签页真正加载完（tabs.update 立刻返回，不等导航）
function waitTabComplete(tabId, ms) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      try { chrome.tabs.onUpdated.removeListener(onUpd); } catch (e) { /* ignore */ }
      resolve(ok);
    };
    const onUpd = (id, info) => { if (id === tabId && info && info.status === "complete") finish(true); };
    try { chrome.tabs.onUpdated.addListener(onUpd); } catch (e) { /* ignore */ }
    chrome.tabs.get(tabId)
      .then((t) => { if (t && t.status === "complete") finish(true); })
      .catch(() => { /* ignore */ });
    setTimeout(() => finish(false), ms);
  });
}

// 导航到某个地址，并真正等到新文档加载完（tabs.update 会立刻返回，不等导航）
async function navigate(tabId, url) {
  let seenLoading = false;
  const wait = new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      try { chrome.tabs.onUpdated.removeListener(onUpd); } catch (e) { /* ignore */ }
      resolve();
    };
    const onUpd = (id, info) => {
      if (id !== tabId || !info) return;
      if (info.status === "loading") seenLoading = true;
      if (info.status === "complete" && seenLoading) finish();
    };
    try { chrome.tabs.onUpdated.addListener(onUpd); } catch (e) { /* ignore */ }
    setTimeout(finish, 30000);
  });
  try {
    await chrome.tabs.update(tabId, { url: url });
  } catch (e) {
    throw new Error("打不开标签页：" + ((e && e.message) || e));
  }
  await wait;
  await sleep(1200);            // 给页面自己的首屏 GraphQL 请求一点时间
}

// 页面里有没有桥接脚本（没有 = 这个标签页是在装扩展之前打开的，必须 reload 才会注入）
async function bridgeState(tabId) {
  const r = await tryInPage(tabId, pageBridgeState);
  if (!r.ok) return { alive: false, err: r.err };
  if (!r.v) return { alive: false, err: "" };
  return { alive: true, captured: !!r.v.captured, hits: r.v.hits || 0, lastErr: r.v.lastErr || "" };
}

async function reloadTab(tabId, bypass) {
  try { await chrome.tabs.reload(tabId, { bypassCache: !!bypass }); } catch (e) { throw new Error("刷新失败：" + ((e && e.message) || e)); }
  await waitTabComplete(tabId, 25000);
  await sleep(1200);
}

// 打开书签页并等页面自己发出 Bookmarks 请求（脚本靠它学到签名头）。
// 顺序：先开 /i/history（X 现在的正式路由，/i/history/bookmarks 会被重定向回来）→
// 切一次标签强制重新请求 → 还不行就绕缓存硬刷新，最多 OPEN_TRIES 轮。
async function openBookmarksPage(tabId) {
  await setStatus({ state: "running", step: "打开书签页", count: 0, pages: 0, err: "", msg: "" });
  const notes = [];

  // clickEvery > 0 时，轮询期间反复点标签：书签标签已选中时点它会切到 Likes，
  // 下一轮再点会切回书签——「切走再切回」才会让 X 重新发请求。
  const waitCapture = async (ms, clickEvery) => {
    const t0 = Date.now();
    let lastClick = 0;
    while (Date.now() - t0 < ms) {
      const st = await bridgeState(tabId);
      if (!st.alive) { notes.push(st.err || "页面里没有桥接脚本"); return false; }
      if (st.captured) return true;
      if (st.lastErr) notes.push(st.lastErr);
      if (clickEvery && Date.now() - lastClick >= clickEvery) {
        lastClick = Date.now();
        const c = await clickTab(true);
        // 点标签之后顺手滚到底：缓存页不点标签也会因滚动触发翻页请求
        await tryInPage(tabId, pageScrollBottom);
        if (c && c.ok && c.v && c.v.label) notes.push("点了「" + c.v.label + "」");
      }
      await sleep(800);
    }
    return false;
  };

  const clickTab = async (force) => {
    const c = await tryInPage(tabId, pageClickBookmarks, [!!force]);
    if (c.ok && c.v && !c.v.ok && c.v.tabs && c.v.tabs.length) {
      notes.push("页面上没找到「书签」标签（看到：" + c.v.tabs.join(" / ") + "）");
    }
    return c;
  };

  // 1) 打开 /i/history（书签标签默认选中，冷加载会直接请求）
  await setStatus({ step: "打开书签页" });
  await navigate(tabId, "https://x.com/i/history");
  await reportExtlog("step", "已打开 /i/history", "https://x.com/i/history");
  let st = await bridgeState(tabId);
  if (!st.alive) {                       // 装扩展前打开的标签页里没有桥接脚本
    await setStatus({ step: "刷新页面以注入脚本" });
    await reloadTab(tabId, true);
  }
  if (await waitCapture(7000)) return true;

  // 2) 反复切标签（书签 ↔ Likes），切回时 X 会重新请求
  await setStatus({ step: "点「书签」标签" });
  await clickTab(true);
  if (await waitCapture(10000, 2500)) return true;

  // 3) 硬刷新（绕缓存），每轮再点一次标签
  for (let i = 1; i <= OPEN_TRIES; i++) {
    await setStatus({ step: "硬刷新页面（第 " + i + " 次）" });
    await reloadTab(tabId, true);
    if (await waitCapture(9000, 2500)) return true;
    await clickTab(true);
    if (await waitCapture(8000, 2500)) return true;
    const vis = await tryInPage(tabId, pageVisibility);
    const vd = (vis.ok && vis.v) ? vis.v : {};
    await reportExtlog("step", "第 " + i + " 轮仍无书签请求（可见性=" + (vd.vis || "?") +
      "，焦点=" + (vd.hasFocus ? "有" : "无") + "，高度=" + (vd.h || 0) + "）",
      "https://x.com" + ((await bridgeState(tabId)).alive ? "" : " (无桥接)"));
  }

  throw new Error("没抓到书签请求，原因：" + (notes.length ? notes.slice(-2).join("；") : "页面一直没发出 Bookmarks 请求（可能这个账号还没登录，或书签页被限流）"));
}

async function sync(tabId) {
  if (BUSY) return { ok: false, err: "已经有一次同步在跑，等它结束再点" };
  BUSY = true;
  try {
    await setStatus({ state: "running", step: "检查页面", count: 0, pages: 0, err: "", msg: "" }, true);
    let tab = null;
    try { tab = await chrome.tabs.get(tabId); } catch (e) { tab = null; }
    // 后台标签页拿不到渲染机会，X 会推迟发请求：先把它激活、窗口置顶
    try { await chrome.tabs.update(tabId, { active: true }); } catch (e) { /* ignore */ }
    try {
      if (tab && tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
    } catch (e) { /* 没有 windows 权限时忽略 */ }
    const u = (tab && tab.url) || "";
    // 没有 tabs 权限时 url 可能是空串：那就信任 popup 传来的标签页
    if (u && !/^https:\/\/(x|twitter)\.com\//.test(u)) {
      throw new Error("当前标签页不是 x.com：请先在这个窗口打开 x.com，再点同步");
    }

    // 1) 页面里没有桥接脚本 = 这个标签页在装/重载扩展之前就开着 → 刷新一次把脚本注进去
    let started = null;
    let st = await bridgeState(tabId);
    if (!st.alive) {
      await setStatus({ step: "刷新页面注入脚本" });
      await reloadTab(tabId, true);
      for (let i = 0; i < 15; i++) {
        st = await bridgeState(tabId);
        if (st.alive) break;
        await sleep(1000);
      }
    }

    // 2) 首选：页面自己发过 Bookmarks 请求 → 直接沿用它的签名和翻页（X 自己的翻页最完整）
    if (st.alive && st.captured) {
      await setStatus({ step: "已捕获书签请求，开始翻页" });
      started = await inPage(tabId, pageCollect, [MAX_PAGES]);
      if (started && started.started) await reportExtlog("step", "用页面自己的请求翻页", "");
    }

    // 2b) 页面一个请求都没发（缓存渲染）→ 自己构造请求
    if ((!started || !started.started) && st.alive) {
      await setStatus({ step: "自己构造请求抓取" });
      started = await inPage(tabId, pageDirect, [MAX_PAGES]);
      if (started && started.started) await reportExtlog("step", "自己构造请求，开始翻页", "");
    }

    // 3) 退路：旁听页面自己的 Bookmarks 请求（老方案）
    if (!started || !started.started) {
      await setStatus({ step: "旁听页面请求" });
      st = await bridgeState(tabId);
      if (!st.captured) await openBookmarksPage(tabId);
      started = await inPage(tabId, pageCollect, [MAX_PAGES]);
      if (!started || !started.started) throw new Error((started && started.err) || "启动抓取失败");
    }

    let last = { pages: 0, count: 0 };
    const t0 = Date.now();
    for (;;) {
      await sleep(1500);
      const p = await tryInPage(tabId, pageProgress);
      if (!p.ok) throw new Error("抓取过程中页面失联：" + p.err);
      if (p.v && p.v.prog) last = p.v.prog;
      await setStatus({ step: "抓取书签", pages: last.pages, count: last.count });
      if (p.v && p.v.done) break;
      if (Date.now() - t0 > 1000 * 60 * 8) throw new Error("抓取超时（8 分钟）");
    }

    // 分块搬回 SW：200 条一次，避免一次性 clone 上百万字符把页面/SW 卡死
    let meta = await tryInPage(tabId, pageMeta);
    if (!meta.ok || !meta.v) throw new Error("读取抓取结果失败：" + (meta.err || "页面没给出结果"));
    if (!meta.v.ok && /还没抓到书签请求/.test(meta.v.err || "")) {
      // 页面在导航后把桥接状态重置了（请求还没发出）：重开书签页再抓一次
      await setStatus({ step: "重试：重新打开书签页" });
      await openBookmarksPage(tabId);
      const again = await inPage(tabId, pageCollect, [MAX_PAGES]);
      if (again && again.started) {
        const t1 = Date.now();
        for (;;) {
          await sleep(1500);
          const q = await tryInPage(tabId, pageProgress);
          if (q.ok && q.v && q.v.prog) last = q.v.prog;
          await setStatus({ step: "抓取书签", pages: last.pages, count: last.count });
          if (q.v && q.v.done) break;
          if (Date.now() - t1 > 1000 * 60 * 5) break;
        }
        meta = await tryInPage(tabId, pageMeta);
        if (!meta.ok || !meta.v) throw new Error("读取抓取结果失败：" + (meta.err || ""));
      }
    }
    if (!meta.v || !meta.v.ok) throw new Error((meta.v && meta.v.err) || "没抓到内容");
    const items = [];
    for (let i = 0; i < meta.v.count; i += CHUNK) {
      const part = await tryInPage(tabId, pageSlice, [i, CHUNK]);
      if (!part.ok || !part.v) throw new Error("读取抓取结果失败：" + (part.err || ""));
      items.push.apply(items, part.v.items);
      await setStatus({ step: "读取抓取结果", count: items.length, pages: meta.v.pages });
    }
    if (!items.length) throw new Error("抓到 0 条：可能是这个账号没有书签，或请求被限流");

    await setStatus({ step: "写入 App", count: items.length, pages: meta.v.pages });
    const handle = (await tryInPage(tabId, pageHandle)).v || "";
    const url = (await server()) + "/api/ingest";
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: items, handle: handle, mode: "bookmark" }),
    });
    const txt = await resp.text();
    let j = {};
    try { j = JSON.parse(txt); } catch (e) { j = {}; }
    if (resp.status === 202) throw new Error("App 正在刷新/采集，稍等一会儿再点同步" + (j.msg ? "：" + j.msg : ""));
    if (!resp.ok) throw new Error("写入 App 失败（HTTP " + resp.status + "）：" + (j.msg || txt.slice(0, 160)));
    if (j.ok !== true) throw new Error("App 没接受这次写入：" + (j.msg || txt.slice(0, 160)));

    await setStatus({
      state: "done", step: "完成", count: items.length, pages: meta.v.pages,
      account: j.account || handle, err: "", msg: meta.v.err || "",
    });
    await reportExtlog("done", "同步成功：" + items.length + " 条 / " + meta.v.pages + " 页", "");
    return { ok: true, count: items.length, pages: meta.v.pages, err: meta.v.err || "", account: j.account || handle };
  } catch (e) {
    let diag = "";
    try {
      const d = await bridgeState(tabId);
      let u = "";
      try { u = (await chrome.tabs.get(tabId)).url || ""; } catch (e2) { u = ""; }
      diag = "［诊断：桥接" + (d.alive ? "在" : "不在") + "，捕获" + (d.captured ? "有" : "无") +
        "，hits=" + (d.hits || 0) + "，页面=" + (u ? u.replace(/^https:\/\/x\.com/, "") : "未知") + "］";
    } catch (e3) { diag = ""; }
    let pageUrl = "";
    try { pageUrl = (await chrome.tabs.get(tabId)).url || ""; } catch (e4) { pageUrl = ""; }
    await setStatus({ state: "error", err: ((e && e.message) || String(e)) + " " + diag, step: "失败" });
    await reportExtlog("error", (((e && e.message) || String(e)) + " " + diag).slice(0, 600), pageUrl);
    return { ok: false, err: (e && e.message) || String(e) };
  } finally {
    BUSY = false;
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return;
  if (msg.type === "sync") {
    sync(msg.tabId).then(sendResponse);
    return true;
  }
  if (msg.type === "status") {
    getStatus().then(sendResponse);
    return true;
  }
  if (msg.type === "probe") {
    tryInPage(msg.tabId, pageBridgeState).then((r) => {
      sendResponse(r.ok ? { state: r.v } : { error: r.err });
    });
    return true;
  }
});
