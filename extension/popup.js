// X 书签桥 · popup
const $ = (s) => document.querySelector(s);
let tabId = null;
let PAGE = "—";
let CAP = null;
let timer = null;

const LABEL = {
  idle: ["空闲", ""],
  running: ["抓取中…", "run"],
  done: ["已同步", "ok"],
  error: ["出错了", "err"],
};

function render(st, page, cap) {
  const s = st || {};
  const pair = LABEL[s.state] || LABEL.idle;
  $("#state").textContent = s.step ? `${pair[0]} · ${s.step}` : pair[0];
  $("#dot").className = "dot " + (pair[1] || "");
  $("#page").textContent = page || "—";
  $("#cap").textContent = cap === null || cap === undefined ? "—" : (cap ? "已捕获 ✓" : "还没捕获");
  $("#prog").textContent = (s.pages || s.count) ? `${s.pages || 0} 页 / ${s.count || 0} 条` : "—";
  const err = $("#err");
  if (s.err) { err.hidden = false; err.textContent = s.err; } else { err.hidden = true; err.textContent = ""; }
  $("#go").disabled = s.state === "running";
}

function showErr(t) { const e = $("#err"); e.hidden = false; e.textContent = t; }
function clearErr() { const e = $("#err"); e.hidden = true; e.textContent = ""; }

// SW 未就绪 / 扩展刚重载时 sendMessage 会 reject，不能让 UI 卡在「检查中…」
async function safeSend(msg) {
  try { return await chrome.runtime.sendMessage(msg); }
  catch (e) { return { __error: String((e && e.message) || e) }; }
}

function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

function startPolling() {
  stopPolling();
  timer = setInterval(async () => {
    const st = await safeSend({ type: "status" });
    if (st && !st.__error) render(st, PAGE, CAP);
    if (!st || st.__error || st.state !== "running") stopPolling();
  }, 1500);
}

async function boot() {
  try {
    try { $("#ver").textContent = "v" + chrome.runtime.getManifest().version; } catch (e) { /* ignore */ }
    const { server } = await chrome.storage.local.get({ server: "http://127.0.0.1:8900" });
    $("#srv").value = server;

    let t = null;
    try {
      const q = await chrome.tabs.query({ active: true, currentWindow: true });
      t = q && q[0] ? q[0] : null;
    } catch (e) { t = null; }
    tabId = t ? t.id : null;
    const url = (t && t.url) || "";
    const onX = url ? /^https:\/\/(x|twitter)\.com\//.test(url) : true; // 没 tabs 权限时读不到 url，先按 x.com 处理
    PAGE = url ? (onX ? "x.com（已打开）" : "不是 x.com") : "当前标签页";

    if (onX && tabId) {
      const p = await safeSend({ type: "probe", tabId });
      CAP = p && p.state ? !!p.state.captured : null;
      if (p && p.error) CAP = null;
    }
    const st = await safeSend({ type: "status" });
    if (st && st.__error) {
      render({ state: "error", err: "扩展后台没响应：" + st.__error, step: "失败" }, PAGE, CAP);
      return;
    }
    render(st, PAGE, CAP);
    if (!onX) {
      showErr("请先切到 x.com 的标签页，再点上面的按钮。");
    } else if (CAP === false) {
      $("#hint").textContent = "还没抓到书签请求——点上面的按钮，它会自动打开书签页并抓取。";
    }
    startPolling();
  } catch (e) {
    render({ state: "error", err: "初始化失败：" + ((e && e.message) || e), step: "失败" }, PAGE, CAP);
  }
}

$("#go").addEventListener("click", async () => {
  if (!tabId) { showErr("没有可用的 x.com 标签页。"); return; }
  clearErr();
  $("#go").disabled = true;
  render({ state: "running", step: "启动中" }, PAGE, CAP);
  chrome.runtime.sendMessage({ type: "sync", tabId })
    .then(async (r) => {
      if (r && r.ok) {
        CAP = true;
      } else {
        const pb = await safeSend({ type: "probe", tabId });
        if (pb && pb.state) CAP = !!pb.state.captured;
      }
      const st = await safeSend({ type: "status" });
      render(st, PAGE, CAP);
      stopPolling();
      if (r && r.ok) {
        $("#hint").innerHTML = `已同步 <b>${r.count}</b> 条（${r.pages} 页）到本机 App。`;
      } else if (r && r.err) {
        showErr(r.err);
      } else {
        showErr("同步没有返回结果：扩展可能被浏览器回收了，重新打开弹窗再试。");
      }
    })
    .catch((e) => {
      showErr("同步中断：" + ((e && e.message) || e));
      $("#go").disabled = false;
    });
  startPolling();
});

$("#open").addEventListener("click", async () => {
  const { server } = await chrome.storage.local.get({ server: "http://127.0.0.1:8900" });
  chrome.tabs.create({ url: server });
});

$("#reload").addEventListener("click", async () => {
  try { if (tabId) await chrome.tabs.reload(tabId); } catch (e) { showErr("刷新失败：" + ((e && e.message) || e)); }
});

$("#srv").addEventListener("change", async () => {
  let v = $("#srv").value.trim() || "http://127.0.0.1:8900";
  if (!/^https?:\/\//.test(v)) v = "http://" + v;
  v = v.replace(/\/+$/, "");
  $("#srv").value = v;
  try { await chrome.storage.local.set({ server: v }); } catch (e) { showErr("保存地址失败：" + ((e && e.message) || e)); }
});

boot();
