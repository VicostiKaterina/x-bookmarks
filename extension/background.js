// X 书签桥 · 后台（service worker）
// popup 只负责点按钮；真正的「抓取 + POST 到本机 App」都在这里做，
// 这样即使 popup 被关掉，同步也会继续跑完。

const DEFAULT_SERVER = "http://127.0.0.1:8900";
const MAX_PAGES = 80;

async function server() {
  const d = await chrome.storage.local.get({ server: DEFAULT_SERVER });
  return String(d.server || DEFAULT_SERVER).replace(/\/+$/, "");
}

async function setStatus(st) {
  const cur = (await chrome.storage.local.get({ status: null })).status || {};
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

function pageCollect(n) {
  const B = window.__XBM_BRIDGE;
  if (!B) return { started: false, err: "页面里没有桥接脚本，请刷新这个标签页（F5）后重试" };
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

function pageResult() {
  const B = window.__XBM_BRIDGE;
  return B ? B.result : null;
}

function pageHandle() {
  const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  return a ? (a.getAttribute("href") || "").replace("/", "") : "";
}

function pageClickBookmarks() {
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  const t = tabs.find((x) => /bookmark|书签/i.test(x.textContent || ""));
  if (t) { t.click(); return { ok: true, label: (t.textContent || "").trim() }; }
  const links = Array.from(document.querySelectorAll('a[href="/i/history/bookmarks"], a[href*="bookmarks"]'));
  if (links[0]) { links[0].click(); return { ok: true, label: "link" }; }
  return { ok: false, tabs: tabs.map((x) => (x.textContent || "").trim()).slice(0, 8) };
}

function pageBridgeState() {
  return window.__XBM_BRIDGE ? window.__XBM_BRIDGE.state() : { captured: false, hits: 0 };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 自动打开书签页并点中「书签」标签，直到页面自己发出 Bookmarks 请求（脚本据此学到签名头）
async function openBookmarksPage(tabId) {
  await setStatus({ state: "running", step: "打开书签页", count: 0, err: "", msg: "" });
  await chrome.tabs.update(tabId, { url: "https://x.com/i/history" });
  const t0 = Date.now();
  while (Date.now() - t0 < 45000) {
    await sleep(1500);
    const st = await inPage(tabId, pageBridgeState).catch(() => null);
    if (st && st.captured) return true;
    if (st) {
      await inPage(tabId, pageClickBookmarks).catch(() => null);
      await setStatus({ step: "等书签列表加载" });
    }
  }
  return false;
}

async function sync(tabId) {
  await setStatus({ state: "running", step: "检查页面", count: 0, err: "", msg: "" });
  try {
    let tab = null;
    try { tab = await chrome.tabs.get(tabId); } catch (e) { tab = null; }
    const u = (tab && tab.url) || "";
    // 没有 tabs 权限时 url 可能是空串：那就信任 popup 传来的标签页
    if (u && !/^https:\/\/(x|twitter)\.com\//.test(u)) {
      throw new Error("当前标签页不是 x.com：请先在这个窗口打开 x.com，再点同步");
    }
    let st = await inPage(tabId, pageBridgeState).catch(() => null);
    if (!st || !st.captured) {
      const ok = await openBookmarksPage(tabId);
      if (!ok) {
        throw new Error("没能自动打开书签页：请手动打开 https://x.com/i/history 并点「书签」标签，等列表出来再点同步");
      }
      st = await inPage(tabId, pageBridgeState).catch(() => null);
      if (!st || !st.captured) throw new Error("还是没抓到书签请求，请刷新 x.com 后重试");
    }
    await setStatus({ step: "已捕获书签请求" });

    await setStatus({ step: "抓取书签" });
    const started = await inPage(tabId, pageCollect, [MAX_PAGES]);
    if (!started || !started.started) throw new Error((started && started.err) || "启动抓取失败");

    let last = { pages: 0, count: 0 };
    const t0 = Date.now();
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const p = await inPage(tabId, pageProgress);
      if (p && p.prog) last = p.prog;
      await setStatus({ step: "抓取书签", pages: last.pages, count: last.count });
      if (p && p.done) break;
      if (Date.now() - t0 > 1000 * 60 * 8) throw new Error("抓取超时（8 分钟）");
    }

    const res = await inPage(tabId, pageResult);
    if (!res || !res.ok) {
      throw new Error((res && res.err) || "没抓到内容");
    }

    await setStatus({ step: "写入 App", count: res.count, pages: res.pages });
    const handle = (await inPage(tabId, pageHandle)) || "";
    const url = (await server()) + "/api/ingest";
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: res.items, handle: handle, mode: "bookmark" }),
    });
    const txt = await resp.text();
    if (!resp.ok) throw new Error("写入 App 失败：" + txt.slice(0, 160));
    let j = {};
    try { j = JSON.parse(txt); } catch (e) { j = {}; }
    await setStatus({
      state: "done", step: "完成", count: res.count, pages: res.pages,
      account: (j && j.account) || handle, err: "", msg: (res.err || ""),
    });
    return { ok: true, count: res.count, pages: res.pages, err: res.err || "", account: (j && j.account) || handle };
  } catch (e) {
    await setStatus({ state: "error", err: (e && e.message) || String(e), step: "失败" });
    return { ok: false, err: (e && e.message) || String(e) };
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
    inPage(msg.tabId, pageBridgeState).then((s) => sendResponse({ state: s })).catch((e) =>
      sendResponse({ error: String(e && e.message) }));
    return true;
  }
});

