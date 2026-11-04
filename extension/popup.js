// X 书签桥 · popup
const $ = (s) => document.querySelector(s);
let tabId = null;

const LABEL = {
  idle: ["空闲", ""],
  running: ["抓取中…", "run"],
  done: ["已同步", "ok"],
  error: ["出错了", "err"],
};

function render(st, page, cap) {
  const s = st || {};
  const [text, cls] = LABEL[s.state] || [...LABEL.idle];
  $("#state").textContent = s.step ? `${text} · ${s.step}` : text;
  $("#dot").className = "dot " + (cls || "");
  $("#page").textContent = page || "—";
  $("#cap").textContent = cap === null || cap === undefined ? "—" : (cap ? "已捕获 ✓" : "还没捕获");
  if (s.pages || s.count) {
    $("#prog").textContent = `${s.pages || 0} 页 / ${s.count || 0} 条`;
  } else {
    $("#prog").textContent = "—";
  }
  const err = $("#err");
  if (s.err) { err.hidden = false; err.textContent = s.err; } else { err.hidden = true; err.textContent = ""; }
  $("#go").disabled = s.state === "running";
}

async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t || null;
}

async function boot() {
  const { server } = await chrome.storage.local.get({ server: "http://127.0.0.1:8900" });
  $("#srv").value = server;

  const t = await activeTab();
  tabId = t ? t.id : null;
  const url = (t && t.url) || "";
  const onX = /^https:\/\/(x|twitter)\.com\//.test(url);
  $("#page").textContent = onX ? "x.com（已打开）" : "不是 x.com";

  let cap = null;
  if (onX && tabId) {
    try {
      const p = await chrome.runtime.sendMessage({ type: "probe", tabId });
      cap = p && p.state ? !!p.state.captured : null;
    } catch (e) { cap = null; }
  }
  const st = await chrome.runtime.sendMessage({ type: "status" });
  render(st, $("#page").textContent, cap);
  if (!onX) {
    $("#err").hidden = false;
    $("#err").textContent = "请先切到 x.com 的标签页，再点上面的按钮。";
  } else if (cap === false) {
    $("#hint").textContent = "还没抓到书签请求——点上面的按钮，它会自动打开书签页并抓取。";
  }
}

$("#go").addEventListener("click", async () => {
  if (!tabId) return;
  $("#go").disabled = true;
  $("#err").hidden = true;
  const url = ((await chrome.tabs.get(tabId)).url) || "";
  if (!/^https:\/\/(x|twitter)\.com\//.test(url)) {
    $("#err").hidden = false;
    $("#err").textContent = "当前标签页不是 x.com。";
    $("#go").disabled = false;
    return;
  }
  const r = await chrome.runtime.sendMessage({ type: "sync", tabId });
  const st = await chrome.runtime.sendMessage({ type: "status" });
  render(st, $("#page").textContent, true);
  if (r && r.ok) {
    $("#hint").innerHTML = `已同步 <b>${r.count}</b> 条（${r.pages} 页）到本机 App。`;
  } else if (r && r.err) {
    $("#err").hidden = false;
    $("#err").textContent = r.err;
  }
});

$("#open").addEventListener("click", async () => {
  const { server } = await chrome.storage.local.get({ server: "http://127.0.0.1:8900" });
  chrome.tabs.create({ url: server });
});

$("#reload").addEventListener("click", async () => {
  if (tabId) await chrome.tabs.reload(tabId);
});

$("#srv").addEventListener("change", async () => {
  let v = $("#srv").value.trim() || "http://127.0.0.1:8900";
  if (!/^https?:\/\//.test(v)) v = "http://" + v;
  v = v.replace(/\/+$/, "");
  $("#srv").value = v;
  await chrome.storage.local.set({ server: v });
});

boot();
