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

// 「书签」标签：优先按链接认（不受界面语言影响），再退回多语言文案
function pageClickBookmarks() {
  const byHref = document.querySelector('[role="tab"][href*="/bookmarks"], a[href="/i/history/bookmarks"], a[href*="/i/history/bookmarks"]');
  if (byHref) { byHref.click(); return { ok: true, label: "href" }; }

  const WORD = /bookmark|saved|书签|收藏|ブックマーク|保存済み|북마크|marcador|guardad|favorit|segnalibri|gespeichert|enregistr/i;
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  const t = tabs.find((x) => WORD.test((x.textContent || "") + " " + (x.getAttribute("aria-label") || "")));
  if (t) { t.click(); return { ok: true, label: (t.textContent || "").trim() }; }
  return { ok: false, tabs: tabs.map((x) => (x.textContent || "").trim()).slice(0, 8) };
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

async function reloadTab(tabId) {
  try { await chrome.tabs.reload(tabId); } catch (e) { throw new Error("刷新失败：" + ((e && e.message) || e)); }
  await waitTabComplete(tabId, 25000);
  await sleep(1200);
}

// 打开书签页并等页面自己发出 Bookmarks 请求（脚本靠它学到签名头）。
// 关键点：直接进 /i/history/bookmarks —— 书签标签是预选中的，页面加载时就会请求，
// 不依赖「点标签」这一步（那一步在非中文/日文界面、或标签还没渲染时最容易失败）。
async function openBookmarksPage(tabId) {
  await setStatus({ state: "running", step: "打开书签页", count: 0, pages: 0, err: "", msg: "" });
  const notes = [];

  const waitCapture = async (ms) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const st = await bridgeState(tabId);
      if (!st.alive) { notes.push(st.err || "页面里没有桥接脚本"); return false; }
      if (st.captured) return true;
      if (st.lastErr) notes.push(st.lastErr);
      const c = await tryInPage(tabId, pageClickBookmarks);
      if (c.ok && c.v && !c.v.ok && c.v.tabs && c.v.tabs.length) {
        notes.push("页面上没找到「书签」标签（看到：" + c.v.tabs.join(" / ") + "）");
      }
      await sleep(1200);
    }
    return false;
  };

  // 第 1 次：直接导航到书签页
  await navigate(tabId, "https://x.com/i/history/bookmarks");
  let st = await bridgeState(tabId);
  if (!st.alive) {                       // 装扩展前打开的标签页里没有桥接脚本
    await setStatus({ step: "刷新页面以注入脚本" });
    await reloadTab(tabId);
  }
  if (await waitCapture(14000)) return true;

  // 第 2 次：reload（有些情况下首屏请求在脚本注入前就发出去了）
  for (let attempt = 2; attempt <= OPEN_TRIES; attempt++) {
    await setStatus({ step: "重新加载页面（第 " + attempt + " 次）" });
    await reloadTab(tabId);
    if (await waitCapture(16000)) return true;
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
    const u = (tab && tab.url) || "";
    // 没有 tabs 权限时 url 可能是空串：那就信任 popup 传来的标签页
    if (u && !/^https:\/\/(x|twitter)\.com\//.test(u)) {
      throw new Error("当前标签页不是 x.com：请先在这个窗口打开 x.com，再点同步");
    }

    let st = await bridgeState(tabId);
    if (!st.alive && !/^https:\/\/(x|twitter)\.com\//.test(u || "https://x.com/")) {
      st = { alive: true, captured: false };   // 读不到 url 又没有桥：交给 openBookmarksPage 去导航
    }
    if (!st.alive || !st.captured) {
      await openBookmarksPage(tabId);
      st = await bridgeState(tabId);
      if (!st.captured) throw new Error("还是没抓到书签请求：请刷新 x.com 后重试");
    }
    await setStatus({ step: "已捕获书签请求" });

    await setStatus({ step: "抓取书签" });
    let started = await inPage(tabId, pageCollect, [MAX_PAGES]);
    if (!started || !started.started) {
      // 页面里没有桥接脚本 = 这个标签页在装扩展/重载扩展之前就开着，注入不进去。
      // 重新走一遍「打开书签页」，它会 reload 页面把脚本注进去。
      await setStatus({ step: "重新注入脚本" });
      await openBookmarksPage(tabId);
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
    await setStatus({ state: "error", err: ((e && e.message) || String(e)) + " " + diag, step: "失败" });
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
