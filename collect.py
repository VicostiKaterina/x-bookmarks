"""采集 X 书签（或点赞）到本地 JSON。

前提：本机有一个**已登录 x.com** 的 Chrome，并且带 --remote-debugging-port。
本项目提供 `serve.py --login`（页面上的"登录"按钮）来拉起这样的窗口。

用法：
    python collect.py                    # 书签 -> data/raw_bookmarks.json
    python collect.py --mode like        # 点赞 -> data/raw_likes.json
    python collect.py --port 9222 --out data/raw.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cdp

ROOT = Path(__file__).resolve().parent

JOB = r"""
(async () => {
  const H = __HEADERS__, BASE = __BASE__, FEAT = __FEAT__, V0 = __VARS__;
  const seen = {}, out = [], log = [];
  let cursor = null, pages = 0, err = '', dry = 0, tkey = '';
  const pickTL = (d) => {
    if (!d) return null;
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (v && typeof v === 'object' && v.timeline && Array.isArray(v.timeline.instructions)) return { key: k, tl: v };
    }
    return null;
  };
  try {
    while (pages < 80) {
      const vars = Object.assign({}, V0, { count: 100 }, cursor ? { cursor } : {});
      const u = BASE + '?variables=' + encodeURIComponent(JSON.stringify(vars)) + '&features=' + encodeURIComponent(FEAT);
      const r = await fetch(u, { headers: H, credentials: 'include' });
      if (!r.ok) { err = 'HTTP ' + r.status + ' ' + (await r.text()).slice(0, 120); break; }
      const j = await r.json();
      const picked = pickTL(j.data);
      if (!picked) { err = 'no timeline ' + JSON.stringify(j.errors || j).slice(0, 160); break; }
      tkey = picked.key;
      let next = null, added = 0, raw = 0;
      for (const ins of (picked.tl.timeline.instructions || [])) {
        for (const e of (ins.entries || [])) {
          raw++;
          const c = e.content || {};
          if (c.cursorType === 'Bottom') next = c.value;
          else if (c.cursorType === 'ShowMore' && !next) next = c.value;
          if (c.cursorType || c.entryType === 'TimelineTimelineCursor') continue;
          const k = e.entryId || Math.random().toString(36);
          if (seen[k]) continue;
          seen[k] = 1; added++; out.push(e);
        }
      }
      pages++;
      log.push({ p: pages, raw, added, total: out.length });
      window.__XBM2 = { pages, count: out.length, entries: out, log, err, tkey };
      if (added === 0) { dry++; if (dry >= 2) { err = '连续2页无新增'; break; } } else { dry = 0; }
      if (!next) { err = 'no cursor'; break; }
      cursor = next;
      await new Promise((r) => setTimeout(r, 1200));
    }
  } catch (e) { err = 'EXC ' + e.message; }
  window.__XBM2 = { pages, count: out.length, entries: out, log, err, tkey };
  return JSON.stringify({ pages, count: out.length, err, log, tkey });
})()
"""

MODES = {
    "bookmark": {"tab": r"Bookmark", "key": "/Bookmarks?", "file": "raw_bookmarks.json"},
    "like": {"tab": r"Like", "key": "/Likes?", "file": "raw_likes.json"},
}


def js(v):
    return json.dumps(v, ensure_ascii=False)


def collect(mode="bookmark", port=9222, out=None, data_dir=None, quiet=False):
    cfg = MODES[mode]
    data_dir = Path(data_dir or (ROOT / "data"))
    out = Path(out or (data_dir / cfg["file"]))
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))

    tgt = cdp.pick_target(port)
    c = cdp.CDP(tgt["webSocketDebuggerUrl"])
    c.send("Page.enable")
    c.send("Runtime.enable")
    c.send("Network.enable")
    say("目标标签页:", (tgt.get("url") or "")[:70])

    url = c.ev("location.href") or ""
    if "x.com" not in url:
        c.send("Page.navigate", {"url": "https://x.com/home"})
        time.sleep(6)
        url = c.ev("location.href") or ""
    if "onboarding" in url or "/i/flow/login" in url:
        raise RuntimeError("未登录：请先在浏览器窗口里登录 x.com")

    handle = c.ev("""(() => { const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
        return a ? a.getAttribute('href') : ''; })()""") or ""
    handle = handle.strip("/").lstrip("@")
    say("账号:", "@" + handle if handle else "(未识别)")

    c.send("Page.navigate", {"url": "https://x.com/i/history"})
    time.sleep(7)
    for _ in range(15):
        if c.ev("document.querySelectorAll('[role=\\\"tab\\\"]').length"):
            break
        time.sleep(2)

    click_js = """(() => { const t = [...document.querySelectorAll('[role="tab"]')].find(x => /%s/i.test(x.textContent||''));
        if (t) { t.click(); return 'tab-clicked'; }
        return 'tabs=' + [...document.querySelectorAll('[role="tab"]')].map(x=>x.textContent).join(','); })()""" % cfg["tab"]
    say("点击 tab:", c.ev(click_js))

    def find_req():
        for m in c.events:
            if m.get("method") == "Network.requestWillBeSent":
                if cfg["key"] in (m["params"]["request"].get("url") or ""):
                    return m
        return None

    hit = None
    for attempt in range(3):
        time.sleep(4)
        hit = find_req()
        if hit:
            break
        say("  第 %d 次没抓到请求，刷新重试" % (attempt + 1))
        c.send("Page.reload", {"ignoreCache": True})
        time.sleep(9)
        c.ev(click_js)
    if not hit:
        raise RuntimeError("没抓到 %s 请求（页面结构可能变了）" % cfg["key"])

    req = hit["params"]["request"]
    full = req["url"]
    headers = dict(req.get("headers") or {})
    for h in ("content-length", "Content-Length", "accept-encoding", "Accept-Encoding", "host", "Host"):
        headers.pop(h, None)
    q = parse_qs(urlparse(full).query)
    base_path = urlparse(full).path
    feat = q.get("features", [""])[0]
    vars_ = json.loads(q.get("variables", ["{}"])[0])
    vars_.pop("cursor", None)
    say("graphql:", base_path)

    job = (JOB.replace("__HEADERS__", js(headers)).replace("__BASE__", js(base_path))
              .replace("__FEAT__", js(feat)).replace("__VARS__", js(vars_)))
    say("翻页中…")
    say(c.ev(job))

    info = c.ev("""(() => { const s = JSON.stringify(window.__XBM2);
        window.__XBM3 = []; const N = 150000;
        for (let i = 0; i < s.length; i += N) window.__XBM3.push(s.slice(i, i + N));
        return JSON.stringify({parts: window.__XBM3.length}); })()""")
    parts = [c.ev("window.__XBM3[%d]" % i) for i in range(json.loads(info)["parts"])]
    obj = json.loads("".join(parts))
    say("timeline:", obj.get("tkey"), "pages:", obj["pages"], "entries:", obj["count"], "err:", obj.get("err"))

    tweets = []
    for e in obj.get("entries") or []:
        r = (((e.get("content") or {}).get("itemContent") or {}).get("tweet_results") or {}).get("result")
        if not isinstance(r, dict):
            continue
        if r.get("__typename") == "TweetWithVisibilityResults" and isinstance(r.get("tweet"), dict):
            r = r["tweet"]
        if r.get("rest_id"):
            tweets.append(r)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False)
    try:
        mp = data_dir / "meta.json"
        meta = {}
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8"))
        meta.update({"account": handle, "collected_at": time.strftime("%Y-%m-%d %H:%M"),
                     "count": len(tweets), "mode": mode})
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        say("写 meta.json 失败:", e)
    say("推文 %d -> %s (%d 字节)" % (len(tweets), out, os.path.getsize(out)))
    c.close()
    return {"count": len(tweets), "handle": handle, "out": str(out)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="采集 X 书签/点赞")
    ap.add_argument("--mode", choices=list(MODES), default="bookmark")
    ap.add_argument("--port", type=int, default=int(os.environ.get("XBM_PORT") or 9222))
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    if not cdp.alive(args.port):
        print("端口 %d 没有可用的浏览器，请先在 App 里点「登录 / 换账号」" % args.port, file=sys.stderr)
        return 2
    try:
        collect(args.mode, args.port, Path(args.out) if args.out else None)
    except RuntimeError as e:
        print("采集失败:", e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
