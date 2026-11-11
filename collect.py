"""采集 X 书签（或点赞）到本地 JSON。

前提：本机有一个**已登录 x.com** 的 Chrome，并且带 --remote-debugging-port。
本项目提供 `serve.py --login`（页面上的"登录"按钮）来拉起这样的窗口。

设计要点（v2.2）：
- 翻页由 **Python 驱动**，一页一次 CDP 调用：能实时打印进度，也能被随时中止；
- 每一页失败会重试一次，浏览器真的断了会抛出明确的错误，不再卡在那儿；
- 只有在全部抓完后才写盘，中途被杀不会留下半份数据。
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
MAX_PAGES = 80

# 在页面里装一个「抓一页」的函数，之后 Python 反复调用它
INSTALL_JS = r"""
(() => {
  const H = __HEADERS__, BASE = __BASE__, FEAT = __FEAT__, V0 = __VARS__;
  const pickTL = (d) => {
    if (!d) return null;
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (v && typeof v === 'object' && v.timeline && Array.isArray(v.timeline.instructions)) return { key: k, tl: v };
    }
    return null;
  };
  window.__XBM = { seen: {}, out: [], pages: 0, tkey: '', lastErr: '' };
  window.__XBMPage = async (cursor) => {
    const S = window.__XBM;
    const vars = Object.assign({}, V0, { count: 100 }, cursor ? { cursor } : {});
    const u = BASE + '?variables=' + encodeURIComponent(JSON.stringify(vars)) + '&features=' + encodeURIComponent(FEAT);
    let j;
    try {
      const r = await fetch(u, { headers: H, credentials: 'include' });
      if (!r.ok) { S.lastErr = 'HTTP ' + r.status; return { ok: false, err: S.lastErr }; }
      j = await r.json();
    } catch (e) { S.lastErr = 'FETCH ' + e.message; return { ok: false, err: S.lastErr }; }
    const picked = pickTL(j.data);
    if (!picked) { S.lastErr = 'no timeline'; return { ok: false, err: 'no timeline ' + JSON.stringify(j.errors || {}).slice(0, 160) }; }
    S.tkey = picked.key;
    let next = null, added = 0, raw = 0;
    for (const ins of (picked.tl.timeline.instructions || [])) {
      for (const e of (ins.entries || [])) {
        raw++;
        const c = e.content || {};
        if (c.cursorType === 'Bottom') {
          if (!next || /cursor-bottom/i.test(String(e.entryId || ''))) next = c.value;
        } else if (c.cursorType === 'ShowMore' && !next) next = c.value;
        if (c.cursorType || c.entryType === 'TimelineTimelineCursor') continue;
        const k = e.entryId || ('r' + Math.random().toString(36).slice(2));
        if (S.seen[k]) continue;
        S.seen[k] = 1; added++; S.out.push(e);
      }
    }
    S.pages += 1;
    return { ok: true, raw: raw, added: added, total: S.out.length, next: next };
  };
  window.__XBMBuf = () => {
    const s = JSON.stringify(window.__XBM.out);
    const N = 200000, parts = [];
    for (let i = 0; i < s.length; i += N) parts.push(s.slice(i, i + N));
    window.__XBM.parts = parts;
    return parts.length;
  };
  return 'installed';
})()
"""

MODES = {
    "bookmark": {"tab": r"Bookmark", "key": "/Bookmarks?", "file": "raw_bookmarks.json"},
    "like": {"tab": r"Like", "key": "/Likes?", "file": "raw_likes.json"},
}


def js(v):
    return json.dumps(v, ensure_ascii=False)


def say(msg):
    """进度行：serve.py 会读 stdout 解析成 UI 上的实时进度。"""
    print(msg, flush=True)


def new_ids(ids, prev):
    """本页里真正新增的 rest_id（顺序保持不变）。

    增量同步靠它判断「已经到上次同步的位置了」：书签按收藏时间倒序，
    某页一旦混进库里已有的 id，后面的页只会更旧，可以立刻停。
    """
    prev = prev or set()
    known = set()
    out = []
    for x in ids or []:
        if x in prev or x in known:
            continue
        known.add(x)
        out.append(x)
    return out


def collect(mode="bookmark", port=9222, out=None, data_dir=None, should_stop=None):
    cfg = MODES[mode]
    data_dir = Path(data_dir or (ROOT / "data"))
    out = Path(out or (data_dir / cfg["file"]))

    try:
        tgt = cdp.pick_target(port)
        c = cdp.CDP(tgt["webSocketDebuggerUrl"])
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.send("Network.enable")
    except Exception as e:
        raise RuntimeError("连不上采集浏览器（端口 %d）：%s" % (port, e))
    say("[progress] step=attach msg=已连接浏览器")

    try:
        def read_handle():
            h = c.ev("""(() => {
  const av = (root) => {
    const el = (root || document).querySelector('[data-testid^="UserAvatar-Container-"]');
    if (!el) return '';
    const t = el.getAttribute('data-testid') || '';
    return t.replace('UserAvatar-Container-', '');
  };
  const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  if (a) return a.getAttribute('href');
  const sw = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  let h = av(sw);
  if (!h) {
    const menu = document.querySelector('[aria-label="Account menu"]');
    h = av(menu);
  }
  if (!h) h = av(document);
  return h ? '/' + h : '';
})()""") or ""
            return h.strip("/").lstrip("@")

        # 直接打开书签页。X 的侧边栏在书签页上同样存在，账号名可以在这里读，
        # 省掉「先跳 /home、再跳回来」的两次整页加载（以前光是这两下就要 12 秒）。
        c.send("Page.navigate", {"url": "https://x.com/i/history", "transitionType": "reload"})
        t_wait = time.time()

        def doc_answered():
            for m in c.events:
                if m.get("method") == "Network.responseReceived":
                    pr = m.get("params") or {}
                    if pr.get("type") == "Document":
                        return True
            return False

        while time.time() - t_wait < 15:
            try:
                if c.ev("""document.querySelectorAll('[role="tab"]').length"""):
                    break
                href_now = c.ev("location.href") or ""
                if href_now.startswith("chrome-error"):
                    raise RuntimeError("X 打不开（打开的是浏览器错误页）：检查代理/VPN 或网络，恢复后再刷新")
            except RuntimeError:
                raise
            except Exception:
                pass
            # Chrome 自己会等 30 秒才给出错误页；等 10 秒还没有任何主文档响应，
            # 基本就是网络/代理挂了，直接说清楚，别让用户干等。
            if (time.time() - t_wait) > 10 and not doc_answered():
                raise RuntimeError("X 打不开（10 秒没有任何响应）：检查代理/VPN 或网络，恢复后再刷新")
            time.sleep(0.2)

        handle = read_handle()
        if not handle:                       # 页面刚出来时侧边栏可能还没渲染
            t_h = time.time()
            while time.time() - t_h < 5 and not handle:
                time.sleep(0.4)
                handle = read_handle()
        url = c.ev("location.href") or ""
        if not handle:
            if "/i/flow/login" in url:
                raise RuntimeError("未登录：点右上角「登录 / 换账号」，在弹出的窗口里登录 X")
            # 读不到账号名 != 没登录。X 改过一次侧边栏 DOM，我们更信 cookie。
            raw_cookie = c.ev("document.cookie") or ""
            if "twid=" not in raw_cookie:
                raise RuntimeError("未登录：点右上角「登录 / 换账号」，在弹出的窗口里登录 X")
            say("[progress] step=account msg=已登录（读不到账号名，继续抓取）")
        else:
            say("[progress] step=account msg=@" + handle)

        click_js = """(() => { const t = [...document.querySelectorAll('[role="tab"]')].find(x => /%s/i.test(x.textContent||''));
            if (t) { t.click(); return 'tab-clicked'; }
            return 'tabs=' + [...document.querySelectorAll('[role="tab"]')].map(x=>x.textContent).join(','); })()""" % cfg["tab"]

        def find_req():
            for m in c.events:
                if m.get("method") == "Network.requestWillBeSent":
                    if cfg["key"] in (m["params"]["request"].get("url") or ""):
                        return m
            return None

        def wait_req(seconds):
            t0 = time.time()
            while time.time() - t0 < seconds:
                got = find_req()
                if got:
                    return got
                time.sleep(0.2)
            return None

        hit = wait_req(6)
        if not hit:
            say("[progress] step=tab msg=" + str(c.ev(click_js))[:80])
            hit = wait_req(6)
        if not hit:
            say("[progress] step=retry msg=没抓到请求，强制刷新一次")
            try:
                c.send("Page.reload", {"ignoreCache": True})
            except Exception:
                pass
            hit = wait_req(8)
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
        say("[progress] step=graphql msg=" + base_path)

        install = (INSTALL_JS.replace("__HEADERS__", js(headers)).replace("__BASE__", js(base_path))
                   .replace("__FEAT__", js(feat)).replace("__VARS__", js(vars_)))
        c.ev(install)

        # 增量：先记住库里已有的 id。书签按「收藏时间」倒序，
        # 所以一旦某页出现已有 id，后面的页只会更旧，可以立刻停。
        prev_ids = set()
        if out.exists():
            try:
                d = json.loads(out.read_text(encoding="utf-8"))
                if isinstance(d, list):
                    prev_ids = {str(t.get("rest_id")) for t in d if isinstance(t, dict)}
            except Exception:
                prev_ids = set()

        cursor, pages, total, dry, new_n, seen_len = None, 0, 0, 0, 0, 0
        err = ""
        while pages < MAX_PAGES:
            if should_stop and should_stop():
                err = "用户中止"
                break
            try:
                raw = c.ev("window.__XBMPage(%s)" % js(cursor))
                r = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except Exception as e:
                raise RuntimeError("采集浏览器的连接断了（窗口被关闭？）：%s" % e)
            if not r.get("ok"):
                err = str(r.get("err") or "抓取失败")
                break
            pages += 1
            total = r.get("total") or total

            ids = []
            try:
                chunk = c.ev("JSON.stringify(window.__XBM.out.slice(%d))" % seen_len) or "[]"
                for e in json.loads(chunk or "[]"):
                    rr = ((((e.get("content") or {}).get("itemContent") or {}).get("tweet_results") or {}).get("result") or {})
                    if rr.get("__typename") == "TweetWithVisibilityResults" and isinstance(rr.get("tweet"), dict):
                        rr = rr["tweet"]
                    if rr.get("rest_id"):
                        ids.append(str(rr["rest_id"]))
            except Exception:
                ids = []
            seen_len = total

            fresh = new_ids(ids, prev_ids)
            new_n += len(fresh)
            say("[progress] pages=%d count=%d added=%d new=%d" % (pages, total, r.get("added") or 0, new_n))
            added = r.get("added") or 0
            if added == 0:
                dry += 1
                if dry >= 2:
                    err = "连续两页没有新内容"
                    break
            else:
                dry = 0
            if ids and len(fresh) < len(ids):
                err = "增量：已到上次同步的位置"
                break
            nxt = r.get("next")
            if not nxt:
                break
            if nxt == cursor:
                err = "游标没有前进，停止翻页"
                break
            cursor = nxt
            time.sleep(0.35)

        parts = int(c.ev("window.__XBMBuf()") or 0)
        chunks = [c.ev("window.__XBM.parts[%d]" % i) for i in range(parts)]
        joined = "".join(x or "" for x in chunks)
        entries = json.loads(joined or "[]")
        tkey = c.ev("window.__XBM.tkey") or ""
    finally:
        try:
            c.close()
        except Exception:
            pass

    tweets = []
    for e in entries:
        r = (((e.get("content") or {}).get("itemContent") or {}).get("tweet_results") or {}).get("result")
        if not isinstance(r, dict):
            continue
        if r.get("__typename") == "TweetWithVisibilityResults" and isinstance(r.get("tweet"), dict):
            r = r["tweet"]
        if r.get("rest_id"):
            tweets.append(r)

    # 与上一次的 raw 求并集：X 偶尔只回一页（限流/断网），
    # 只写这一页会把库里几千条书签冲掉，所以永远只增不减。
    prev = []
    if out.exists():
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(d, list):
                prev = d
        except Exception:
            prev = []
    if prev:
        seen_ids = {str(t.get("rest_id")) for t in tweets}
        kept = [t for t in prev if isinstance(t, dict) and str(t.get("rest_id")) not in seen_ids]
        if kept:
            tweets = tweets + kept
            say("[progress] step=merge msg=并入历史 %d 条，共 %d 条" % (len(kept), len(tweets)))

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False)
    tmp.replace(out)
    try:
        mp = data_dir / "meta.json"
        meta = {}
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8-sig"))
        meta.update({"collected_at": time.strftime("%Y-%m-%d %H:%M"),
                     "count": len(tweets), "mode": mode})
        if handle:                      # 读不到账号名时不要覆盖已有值
            meta["account"] = handle
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        say("[progress] step=meta err=%s" % e)

    say("[progress] done pages=%d count=%d new=%d err=%s" % (pages, len(tweets), new_n, err))
    if err and err not in ("连续两页没有新内容", "游标没有前进，停止翻页"):
        say("[progress] step=abort msg=" + str(err)[:80])
    say("timeline: %s pages: %d entries: %d err: %s" % (tkey, pages, len(tweets), err))
    say("推文 %d -> %s (%d 字节)" % (len(tweets), out, os.path.getsize(out)))
    benign = ("连续两页没有新内容", "游标没有前进，停止翻页", "增量：已到上次同步的位置")
    return {"count": len(tweets), "handle": handle, "out": str(out), "pages": pages, "err": err, "new": new_n,
            "abort": bool(err) and err not in benign}


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
        r = collect(args.mode, args.port, Path(args.out) if args.out else None)
    except RuntimeError as e:
        print("采集失败:", e, file=sys.stderr)
        return 3
    if r.get("abort"):
        print("采集未完整：%s" % r.get("err"), file=sys.stderr)
        return 5
    return 0 if r.get("count") else 4


if __name__ == "__main__":
    sys.exit(main())
