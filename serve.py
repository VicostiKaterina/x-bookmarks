#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""X Bookmarks · 本地服务

只监听 127.0.0.1，负责：
  - 把 data/items.json 渲染成一个书签 App 页面（web/app.html）
  - 一键刷新：调用 collect.py 抓取 → organize.py 分类
  - 登录：拉起一个带调试端口的 Chrome 窗口，让用户自己登录 X（登录态留在本地 profile）
  - 设置：保存到 settings.json

用法:
    python serve.py            # 启动（默认 8900）
    python serve.py --port 8900
"""
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cdp

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SETTINGS_PATH = ROOT / "settings.json"
SETTINGS_BAK = ROOT / "settings.json.bak"
CATS = ("ai", "tech", "life", "private", "misc")

DEFAULTS = {
    "port": 8900,
    "data_dir": "data",
    "export_dir": "exports",
    "profile_dir": ".profile",
    "proxy": "",
    "debug_port": 9222,
    "default_sort": "time_desc",
    "translate_provider": "auto",
    "translate_target": "zh-CN",
    "llm_base": "https://api.deepseek.com/v1",
    "llm_key": "",
    "llm_model": "deepseek-chat",
    "blur_private": True,
    "blur_misc_media": True,
    "auto_refresh_hours": 0,
    "bind": "127.0.0.1",
    "access_code": "",
    "cookie_salt": "",
    "semantic_enabled": True,
    "semantic_threshold": 0.6,
    "switch_account_policy": "archive",
}

SWITCH_POLICIES = ("archive", "delete")

SORT_KEYS = ("added_desc", "time_desc", "time_asc", "views", "likes", "retweets", "bookmarks", "replies")
BINDS = ("127.0.0.1", "0.0.0.0", "::1", "::")


def _as_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


def sanitize_settings(d):
    """把任意来源的设置洗成「一定能用」的形状：类型错、超范围、非法值一律回退。"""
    src = d if isinstance(d, dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in src:
            out[k] = src[k]

    def as_int(key, lo, hi):
        try:
            v = int(str(out.get(key)).strip())
        except Exception:
            v = DEFAULTS[key]
        out[key] = max(lo, min(hi, v))

    as_int("port", 1, 65535)
    as_int("debug_port", 1, 65535)
    as_int("auto_refresh_hours", 0, 24 * 30)

    try:
        thr = float(out.get("semantic_threshold"))
    except Exception:
        thr = DEFAULTS["semantic_threshold"]
    out["semantic_threshold"] = round(max(0.4, min(0.95, thr)), 2)

    for k in ("blur_private", "blur_misc_media", "semantic_enabled"):
        out[k] = _as_bool(out.get(k), DEFAULTS[k])

    bind = str(out.get("bind") or "").strip() or DEFAULTS["bind"]
    out["bind"] = bind if bind in BINDS else DEFAULTS["bind"]
    code = str(out.get("access_code") or "").strip()[:64]
    # 开了局域网还要 6 位口令能挡住邻居；只有 1~3 位等于没锁，直接换新的
    if code and len(code) < 4:
        out["access_code"] = new_code()
        log("访问码太短（%s），已换成 %s" % (code, out["access_code"]))
    else:
        out["access_code"] = code
    out["default_sort"] = str(out.get("default_sort") or "").strip()
    if out["default_sort"] not in SORT_KEYS:
        out["default_sort"] = DEFAULTS["default_sort"]
    for k in ("data_dir", "export_dir", "profile_dir"):
        v = str(out.get(k) or "").strip()
        out[k] = v or DEFAULTS[k]
    pol = str(out.get("switch_account_policy") or "").strip().lower()
    out["switch_account_policy"] = pol if pol in SWITCH_POLICIES else "archive"
    for k in ("translate_provider", "translate_target", "llm_model"):
        out[k] = str(out.get(k) or "").strip()
    # 代理会被拼进 chrome 命令行：只接受干净的 scheme://host:port，挡参数注入
    px = str(out.get("proxy") or "").strip()
    out["proxy"] = px if re.match(r"^(https?|socks5)://[A-Za-z0-9._:\-]+$", px) else ""
    lb = str(out.get("llm_base") or "").strip()
    out["llm_base"] = lb if re.match(r"^https?://[A-Za-z0-9._:\-]+/?$", lb.rstrip("/")) or re.match(r"^https?://[A-Za-z0-9._:\-]+(/[A-Za-z0-9._:\-/]*)?$", lb) else DEFAULTS["llm_base"]
    out["llm_key"] = str(out.get("llm_key") or "").strip()[:200]
    out["cookie_salt"] = re.sub(r"[^0-9a-f]", "", str(out.get("cookie_salt") or ""))[:64]
    return out


CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

LOGIN = {"state": "idle", "handle": "", "msg": "", "at": 0.0}
STATE = {"running": False, "step": "", "log": [], "err": "", "count": 0, "at": "", "started": 0.0,
         "cancel": False, "pages": 0}
LOCK = threading.Lock()
UNLOCK_FAIL = {}          # ip -> (失败次数, 解禁时间)

BROWSER_LOCK = threading.Lock()   # 串行化「开窗口 / 关窗口 / 清 cookie」，避免两个线程互相杀窗口
LOGIN_WORKER = {"t": None}
UI_VISIBLE = {"on": False}        # 采集浏览器当前是不是开着给用户看的窗口


def _serve_log(msg):
    """登录/浏览器这类「事后说不清」的事写到 data/serve.log，出问题能回看。"""
    try:
        p = data_dir(load_settings()) / "serve.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------- 配置 / 路径

def load_settings():
    d = dict(DEFAULTS)
    loaded = None
    if SETTINGS_PATH.exists():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        except Exception as e:
            log("settings.json 读不出来（%s），改用备份" % e)
            if SETTINGS_BAK.exists():
                try:
                    loaded = json.loads(SETTINGS_BAK.read_text(encoding="utf-8-sig"))
                except Exception as e2:
                    log("备份也读不出来：%s（先按默认值跑）" % e2)
    if isinstance(loaded, dict):
        d.update(loaded)
    out = sanitize_settings(d)

    dirty = False
    # 清洗结果（例如太短的访问码被换成新码、非法 bind 被回退）要落盘，
    # 否则内存里一套、磁盘上另一套，每次重启访问码都不一样。
    if isinstance(loaded, dict) and any(loaded.get(k) != out.get(k) for k in out):
        dirty = True
    if not out["cookie_salt"]:
        out["cookie_salt"] = secrets.token_hex(8)
        dirty = True
    # 对外监听却没访问码 = 局域网裸奔：自动补一个
    if not out["access_code"] and out["bind"] not in ("127.0.0.1", "::1"):
        out["access_code"] = new_code()
        dirty = True
        log("没设访问码又开着局域网访问，已自动生成访问码：%s" % out["access_code"])
    if dirty:
        try:
            save_settings(out)
        except Exception as e:
            log("写回 settings.json 失败：%s" % e)
    return out


def save_settings(d):
    """原子写：先写 .tmp 再 os.replace，并保留上一版 .bak（避免写一半变默认值）。"""
    d = sanitize_settings(d)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        try:
            SETTINGS_BAK.write_bytes(SETTINGS_PATH.read_bytes())
        except Exception:
            pass
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)
    return d


def _const_eq(a, b):
    """定长比较，别让访问码能被时间侧信道猜。"""
    try:
        return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))
    except Exception:
        return False


def _code_token(s):
    """cookie 里不放明文访问码，放盐化 HMAC。"""
    code = str(s.get("access_code") or "")
    salt = str(s.get("cookie_salt") or "")
    if not code or not salt:
        return ""
    return hmac.new(salt.encode("utf-8"), code.encode("utf-8"), "sha256").hexdigest()[:40]


def _child_env():
    """子进程统一 UTF-8：Windows 默认 cp936 会让中文输出变 U+FFFD。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


VERSION = "2.5.0"
COOKIE_KEY = "xbm_key"
# 扩展一次同步会把整份书签 JSON POST 过来（1000+ 条约 7~10MB），单独给大一点的配额
INGEST_LIMIT = 64 << 20


def _mask_code(v):
    v = str(v or "")
    if not v:
        return ""
    return "\u2022" * max(1, len(v) - 2) + v[-2:]


def is_local(ip):
    return ip in ("127.0.0.1", "::1") or str(ip).startswith("::ffff:127.")


def lan_ip():
    """拿本机在局域网里的地址（手机要访问的那个）。"""
    try:
        sck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sck.connect(("8.8.8.8", 80))
        ip = sck.getsockname()[0]
        sck.close()
        return ip
    except Exception:
        return ""


def new_code():
    return "%06d" % secrets.randbelow(1000000)


def unlock_html(err=""):
    tip = '<p class="err">%s</p>' % err if err else ""
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>X 书签 · 输入访问码</title>
<style>
 :root{--bg:#0e1117;--card:#151a23;--line:#242c39;--txt:#e6ebf5;--dim:#8b96a8;--pri:#3b82f6}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:var(--bg);color:var(--txt);font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
 .card{width:min(92vw,360px);background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}
 h1{margin:0 0 4px;font-size:17px}
 p.sub{margin:0 0 18px;color:var(--dim);font-size:12.5px}
 input{width:100%;font:inherit;letter-spacing:.32em;text-align:center;font-size:20px;
       background:#0f141c;border:1px solid var(--line);border-radius:9px;color:var(--txt);padding:10px}
 button{width:100%;margin-top:12px;font:inherit;font-weight:600;border:0;border-radius:9px;
        background:var(--pri);color:#fff;padding:11px;cursor:pointer}
 .err{color:#fca5a5;font-size:12.5px;margin:10px 0 0}
</style></head><body>
<form class="card" method="post" action="/api/unlock">
  <h1>X 书签</h1>
  <p class="sub">这台设备第一次访问，输入访问码（在电脑端「设置 → 局域网访问」里查看）</p>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="000000" maxlength="12" autofocus>
  <button type="submit">解锁</button>
  %TIP%
</form></body></html>""" .replace("%TIP%", tip)



def path_of(s, key):
    p = Path(s.get(key) or DEFAULTS[key])
    return p if p.is_absolute() else (ROOT / p)


def data_dir(s=None):
    return path_of(s or load_settings(), "data_dir")


def export_dir(s=None):
    return path_of(s or load_settings(), "export_dir")


def profile_dir(s=None):
    return path_of(s or load_settings(), "profile_dir")


def chrome_path(s=None):
    s = s or load_settings()
    p = s.get("chrome_path") or ""
    if p and Path(p).exists():
        return p
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    raise RuntimeError("找不到 Chrome，请在设置里填 chrome_path")


def log(msg):
    with LOCK:
        STATE["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        STATE["log"] = STATE["log"][-80:]
    try:
        print(msg, flush=True)
    except Exception:
        pass          # pythonw 没有控制台、cp936 管道也会编码失败：日志留在 STATE 里


# ---------------------------------------------------------------- Chrome

def chrome_args(s, visible, url):
    prof = profile_dir(s)
    prof.mkdir(parents=True, exist_ok=True)
    a = [
        chrome_path(s),
        f"--user-data-dir={prof}",
        f"--remote-debugging-port={int(s.get('debug_port') or 9222)}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--lang=zh-CN",
    ]
    if s.get("proxy"):
        a.append(f"--proxy-server={s['proxy']}")
    if visible:
        a += ["--window-size=1000,760", "--window-position=120,80"]
    else:
        a += ["--window-size=1100,900", "--window-position=-32000,-32000"]
    a.append(url)
    return a


def _browser_ws(port):
    """拿浏览器级 websocket 地址（/json/version 里的那个），用来优雅关闭。"""
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port,
                                              timeout=3).read().decode("utf-8"))
        return d.get("webSocketDebuggerUrl") or ""
    except Exception:
        return ""


def close_browser_gracefully(s=None, wait=7.0):
    """让 Chrome 自己退出（Browser.close），把 cookie/登录态正常落盘。

    以前一律 taskkill /F：登录刚成功就被强杀，cookie 还没写进 Cookies 库，
    用户看到的就是「明明登上了，一刷新又变回未登录」。
    """
    s = s or load_settings()
    port = int(s.get("debug_port") or 9222)
    if not cdp.alive(port):
        return True
    ws = _browser_ws(port)
    if not ws:
        return False
    try:
        c = cdp.CDP(ws, timeout=6)
        c.send("Browser.close")
        try:
            c.close()
        except Exception:
            pass
    except Exception as e:
        log("Browser.close 失败：%s" % e)
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(0.4)
        if not cdp.alive(port):
            _serve_log("browser closed gracefully in %.1fs" % (time.time() - t0))
            return True
    _serve_log("graceful close timed out after %.1fs" % wait)
    return False


def kill_our_chrome(s=None, graceful=True):
    """只杀用的是本项目 profile 的 chrome，不碰用户自己的浏览器。"""
    s = s or load_settings()
    if graceful:
        try:
            if close_browser_gracefully(s):
                return
        except Exception as e:
            log("优雅关闭失败：%s" % e)
    prof = str(profile_dir(s))          # 别转义反斜杠：转了 PowerShell -like 永远匹配不到
    if os.name != "nt":
        subprocess.run(["pkill", "-f", f"--user-data-dir={profile_dir(s)}"], capture_output=True)
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*--user-data-dir=" + prof + "*' } | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=25)
        pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        if pids:
            time.sleep(1.2)
    except Exception as e:
        log(f"清理旧浏览器失败: {e}")


def launch_chrome(s, visible=False, url="https://x.com/home"):
    proc = subprocess.Popen(chrome_args(s, visible, url),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc


def ensure_chrome(s=None, visible=False, url="https://x.com/home"):
    s = s or load_settings()
    port = int(s.get("debug_port") or 9222)
    if cdp.alive(port):
        return "reused"
    launch_chrome(s, visible, url)
    for _ in range(40):
        time.sleep(0.5)
        if cdp.alive(port):
            return "started"
    raise RuntimeError("浏览器没起来（端口 %d 无响应）" % port)


# ---------------------------------------------------------------- 采集 + 分类

def run_semantic():
    """本地小模型跑一遍语义分类，然后重跑 organize.py。"""
    s = load_settings()
    with LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, step="语义分类", err="", log=[], started=time.time())
    try:
        log("载入本地 embedding 模型并计算向量…")
        code = subprocess.run(
            [sys.executable, str(ROOT / "semantic.py")],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=3600)
        for line in (code.stdout or "").splitlines()[-10:]:
            log(line)
        if code.returncode != 0:
            raise RuntimeError((code.stderr or code.stdout or "语义分类失败").strip().splitlines()[-1])
        with LOCK:
            STATE["step"] = "重新分类"
        # 这里自己已经持有 running 标记，必须 force，否则会被当成「别的任务在跑」
        ok, msg = reclassify(force=True)
        if not ok:
            raise RuntimeError(msg)
        meta = build_meta(s)
        with LOCK:
            STATE.update(running=False, step="完成", count=meta.get("total", 0),
                         at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log("语义分类完成：%d 条" % meta.get("total", 0))
        return True
    except Exception as e:
        with LOCK:
            STATE.update(running=False, step="失败", err=str(e), at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log("语义分类失败：%s" % e)
        return False


def run_collect_stream(s):
    """跑 collect.py 并把 [progress] 行实时写进 STATE；支持中止。返回 (ok, err)。"""
    port = int(s.get("debug_port") or 9222)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "collect.py"), "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=_child_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out_lines = []

    def reader(stream, keep):
        for line in stream:
            line = (line or "").rstrip()
            if not line:
                continue
            keep.append(line)
            if line.startswith("[progress]"):
                parts = dict(x.split("=", 1) for x in line[10:].strip().split(" msg=")[0].split() if "=" in x)
                with LOCK:
                    st = parts.get("step")
                    if st:
                        STATE["step"] = {
                            "attach": "连接采集浏览器",
                            "account": "识别账号",
                            "tab": "打开书签页",
                            "graphql": "拿到接口签名",
                            "restart": "重启采集浏览器",
                        }.get(st, STATE["step"])
                    if parts.get("pages"):
                        STATE["pages"] = int(parts["pages"])
                    if parts.get("count"):
                        STATE["count"] = int(parts["count"])
                    if parts.get("new"):
                        STATE["new"] = int(parts["new"])
                if parts.get("pages"):
                    with LOCK:
                        STATE["step"] = "抓取书签"
            else:
                log(line)

    t1 = threading.Thread(target=reader, args=(proc.stdout, out_lines), daemon=True)
    t2 = threading.Thread(target=reader, args=(proc.stderr, out_lines), daemon=True)
    t1.start(); t2.start()
    t0 = time.time()
    last_progress = time.time()
    seen_progress = 0
    while proc.poll() is None:
        time.sleep(0.6)
        if len(out_lines) > seen_progress:      # 任何一行输出都算「还在动」
            seen_progress = len(out_lines)
            last_progress = time.time()
        elif time.time() - last_progress > 20:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return False, "采集卡住了（20 秒没有任何进展）：多半是网络/代理断了，恢复后再刷新"
        with LOCK:
            cancelled = STATE.get("cancel")
        if cancelled:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
            return False, "用户中止"
        if time.time() - t0 > 900:
            proc.terminate()
            return False, "采集超时（15 分钟）"
    t1.join(timeout=3); t2.join(timeout=3)
    if proc.returncode == 0:
        return True, ""
    tail = [x for x in out_lines if x.strip()][-6:]
    for line in reversed(tail):
        if "失败" in line or "未完整" in line or "中止" in line:
            return False, line.split(":", 1)[-1].strip()
    return False, (tail[-1] if tail else "采集失败")


def run_pipeline(reason="手动刷新"):
    s = load_settings()
    who = ""
    with LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, step="准备浏览器", err="", log=[], started=time.time(),
                     cancel=False, pages=0, new=0)
    try:
        if cdp.alive(int(s.get("debug_port") or 9222)):
            with LOCK:
                STATE["step"] = "检查登录状态"
            try:
                tgt = cdp.pick_target(int(s["debug_port"]))
                cur = tgt.get("url") or ""
                who = ""
                try:
                    cc = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=20)
                    who = cc.ev("""(() => {
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
                    raw = cc.ev("document.cookie") or ""
                    cc.close()
                except Exception:
                    pass
                # 已登录也可能停在 onboarding 页，所以「有主页链接」就不算未登录。
                # 拿不到主页链接时再看 cookie：twid 在就是登着的（X 改 DOM 不再误判）。
                if (not who.strip("/").lstrip("@")
                        and "twid=" not in (raw or "")
                        and ("onboarding" in cur or "/i/flow/login" in cur)):
                    raise RuntimeError("未登录：点右上角「登录 / 换账号」，在弹出的窗口里登录 X")
            except RuntimeError:
                raise
            except Exception:
                pass
        else:
            with LOCK:
                STATE["step"] = "启动采集浏览器"
            ensure_chrome(s, visible=UI_VISIBLE["on"], url="https://x.com/home")

        handle_now = (who or "").strip("/").strip().lstrip("@")
        if handle_now:
            switch_account_if_needed(s, handle_now)

        # 采集浏览器如果卡在错误页、或者根本不回话，先重启它。
        # 否则后面每一步都要等到超时，用户看到的就是「刷新要几分钟」。
        try:
            _port = int(s.get("debug_port") or 9222)
            _tgt = cdp.pick_target(_port)
            _href = None
            try:
                _cc = cdp.CDP(_tgt["webSocketDebuggerUrl"], timeout=4)
                _href = _cc.ev("location.href") or ""
                _cc.close()
            except Exception:
                _href = None
            if (not _href) or str(_href).startswith("chrome-error"):
                log("采集浏览器状态不对（%s），先重启它…" % (_href or "没响应"))
                _serve_log("collect: preflight restart, href=%r" % _href)
                with BROWSER_LOCK:
                    kill_our_chrome(s)
                    ensure_chrome(s, visible=UI_VISIBLE["on"], url="https://x.com/home")
        except Exception as _e:
            _serve_log("collect: preflight failed: %s" % _e)

        with LOCK:
            STATE["step"] = "抓取书签"
            STATE["pages"] = 0
        last_err = ""
        for attempt in (1, 2):
            ok, err = run_collect_stream(s)
            if ok:
                last_err = ""
                break
            last_err = err or "采集失败"
            if "中止" in last_err:
                raise RuntimeError("已停止")
            if attempt != 1:
                break
            port = int(s.get("debug_port") or 9222)
            if "断" in last_err:
                # 浏览器真的断了：只有端口确实死了才重启。
                # 登录窗口是用户看得见的：强杀会丢 cookie（登录态跟着没），所以先优雅关，
                # 而且重启后窗口仍然可见，不能偷偷挪到屏幕外。
                log("采集浏览器的连接断了，重试一次…")
                with LOCK:
                    STATE["step"] = "重启采集浏览器"
                with BROWSER_LOCK:
                    if not cdp.alive(port):
                        kill_our_chrome(s)
                        ensure_chrome(s, visible=UI_VISIBLE["on"], url="https://x.com/home")
                        _serve_log("collect: browser was dead, relaunched (visible=%s)" % UI_VISIBLE["on"])
                    time.sleep(3)
                continue
            if any(k in last_err for k in ("未登录", "登录", "已停止", "打不开", "错误页")):
                # 需要人动手的错误，重试只会白等。
                break
            # 其它偶发失败：等几秒，把页面带回书签页，再抓一次。
            log("刷新失败（%s），5 秒后自动重试一次…" % last_err)
            _serve_log("collect: retry once after error: %s" % last_err)
            with LOCK:
                STATE["step"] = "自动重试"
            time.sleep(5)
            with BROWSER_LOCK:
                try:
                    if cdp.alive(port):
                        tgt = cdp.pick_target(port)
                        cc = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=20)
                        cc.send("Page.navigate", {"url": "https://x.com/i/history"})
                        cc.close()
                        time.sleep(3)
                    else:
                        kill_our_chrome(s)
                        ensure_chrome(s, visible=UI_VISIBLE["on"], url="https://x.com/home")
                except Exception as e:
                    _serve_log("collect: retry prep failed: %s" % e)
            continue
        if last_err:
            raise RuntimeError(last_err)

        with LOCK:
            new_n = int(STATE.get("new") or 0)
        if new_n == 0:
            # 没有新书签：不用重跑分类整理（省两秒多），直接收工。
            meta = build_meta(s)
            with LOCK:
                STATE.update(running=False, step="完成", count=meta.get("total", 0),
                             at=time.strftime("%Y-%m-%d %H:%M:%S"))
            log("没有新书签，已是最新（%s 条）" % meta.get("total"))
            return True

        with LOCK:
            STATE["step"] = "分类整理"
        added_map = _read_added_map(s)
        code = subprocess.run(
            [sys.executable, str(ROOT / "organize.py")],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=900)
        for line in (code.stdout or "").splitlines()[:8]:
            log(line)
        if code.returncode != 0:
            raise RuntimeError((code.stderr or "分类失败").strip().splitlines()[-1])
        _apply_added_map(s, added_map)

        meta = build_meta(s)
        with LOCK:
            STATE.update(running=False, step="完成", count=meta.get("total", 0),
                         at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log(f"刷新完成：{meta.get('total')} 条")
        return True
    except Exception as e:
        stopped = "已停止" in str(e)
        with LOCK:
            STATE.update(running=False, step="已停止" if stopped else "失败",
                         err="" if stopped else str(e), at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log("刷新已停止（数据没有改动）" if stopped else f"刷新失败：{e}")
        return False


def sem_meta(s=None):
    s = s or load_settings()
    p = data_dir(s) / "semantic.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"generated": d.get("generated"), "model": d.get("model"),
                "count": d.get("count"), "checked": len(d.get("items") or {})}
    except Exception:
        return {}


def build_meta(s=None):
    s = s or load_settings()
    dd = data_dir(s)
    items_path = dd / "items.json"
    meta_path = dd / "meta.json"
    meta = {"total": 0, "by_cat": {}, "updated_at": "", "account": "", "items_file": ""}
    if items_path.exists():
        try:
            d = json.loads(items_path.read_text(encoding="utf-8"))
            its = d.get("items") or []
            meta["total"] = len(its)
            meta["updated_at"] = d.get("generated") or ""
            c = {}
            for it in its:
                c[it.get("cat") or "misc"] = c.get(it.get("cat") or "misc", 0) + 1
            meta["by_cat"] = c
            meta["items_file"] = str(items_path)
        except Exception as e:
            meta["error"] = str(e)
    if meta_path.exists():
        try:
            meta.update({k: v for k, v in json.loads(meta_path.read_text(encoding="utf-8")).items()
                         if k in ("account", "collected_at")})
        except Exception:
            pass
    return meta


def overrides_path(s=None):
    return data_dir(s) / "overrides.json"


def load_overrides(s=None):
    p = overrides_path(s)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_overrides(s, ov):
    p = overrides_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ov, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _raw_list(path):
    """读一份原始抓取结果（列表），坏文件当空处理。"""
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return d if isinstance(d, list) else []


def _item_key(it):
    """尽量从各种形状的原始数据里取一个稳定 id，取不到返回空串。"""
    if not isinstance(it, dict):
        return ""
    for k in ("id", "rest_id", "tweet_id", "id_str"):
        v = it.get(k)
        if v:
            return str(v)
    nested = it.get("legacy") if isinstance(it.get("legacy"), dict) else None
    if nested and nested.get("id_str"):
        return str(nested["id_str"])
    if it.get("url"):
        return "@" + str(it["url"])
    return ""


def _stamp_added(items, when=None):
    """给没有 added_at 的条目补上「首次收录时间」，已有的保持不动。"""
    when = when or time.strftime("%Y-%m-%d %H:%M")
    n = 0
    for it in items or []:
        if isinstance(it, dict) and not it.get("added_at"):
            it["added_at"] = when
            n += 1
    return n


def merge_items(old, new):
    """按 id 合并：老的在前，新的覆盖同 id，新增的追加在后面。扩展只抓到一部分时不会丢数据。"""
    merged = {}
    order = []
    for it in list(old) + list(new):
        k = _item_key(it)
        if not k:
            continue
        if k not in merged:
            order.append(k)
        merged[k] = it
    return [merged[k] for k in order]


def ingest_items(items, handle="", mode="bookmark"):
    """浏览器扩展桥把整批书签 POST 进来：与本地已有数据合并 -> 落盘 -> 重跑分类 -> 更新 meta。"""
    s = load_settings()
    if handle:
        switch_account_if_needed(s, handle)
    dd = data_dir(s)
    dd.mkdir(parents=True, exist_ok=True)
    raw = dd / ("raw_likes.json" if mode == "like" else "raw_bookmarks.json")
    if raw.exists():
        try:
            (dd / (raw.stem + ".bak" + raw.suffix)).write_bytes(raw.read_bytes())
        except Exception:
            pass
    before = {str(x.get("rest_id") or x.get("id") or ""): x.get("added_at")
              for x in _raw_list(raw) if isinstance(x, dict)}
    merged = merge_items(_raw_list(raw), items)
    _stamp_added(merged)
    for x in merged:                      # 老条目的 added_at 不要被新的覆盖
        if isinstance(x, dict):
            k = str(x.get("rest_id") or x.get("id") or "")
            if before.get(k):
                x["added_at"] = before[k]
    tmp = raw.with_name(raw.stem + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    tmp.replace(raw)
    try:
        mp = dd / "meta.json"
        meta = {}
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8-sig"))
        meta.update({"account": handle or meta.get("account") or "",
                     "owner": handle or meta.get("owner") or meta.get("account") or "",
                     "collected_at": time.strftime("%Y-%m-%d %H:%M"),
                     "count": len(merged), "mode": mode, "source": "extension"})
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log("写 meta.json 失败：%s" % e)
    ok, msg = reclassify(force=True)
    return ok, msg, len(merged)


def _read_added_map(s=None):
    """items.json 里 id -> added_at，用于分类重建后保留首次收录时间。"""
    s = s or load_settings()
    pth = data_dir(s) / "items.json"
    out = {}
    if not pth.exists():
        return out
    try:
        d = json.loads(pth.read_text(encoding="utf-8"))
        for it in d.get("items") or []:
            if it.get("id") and it.get("added_at"):
                out[str(it["id"])] = it["added_at"]
    except Exception:
        pass
    return out


def _apply_added_map(s, old):
    """分类重建后把 added_at 写回；老条目保持原值，新条目补当前采集时间。"""
    old = old or {}
    pth = data_dir(s) / "items.json"
    if not pth.exists():
        return 0
    try:
        d = json.loads(pth.read_text(encoding="utf-8"))
    except Exception:
        return 0
    meta = {}
    try:
        meta = json.loads((data_dir(s) / "meta.json").read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    when = meta.get("collected_at") or time.strftime("%Y-%m-%d %H:%M")
    n = 0
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "")
        if old.get(iid):
            it["added_at"] = old[iid]
            n += 1
        elif not it.get("added_at"):
            it["added_at"] = when
            n += 1
    tmp = pth.with_name(pth.stem + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(pth)
    return n


def invalidate_state_cache():
    try:
        invalidate_items_cache()
    except Exception:
        pass


def reclassify(force=False):
    """改了手动分类后重跑 organize.py（只重算分类/导出，不抓取）。

    force=True 用于「调用方自己已经持有 running 标记」的场景（比如扩展桥的 /api/ingest），
    否则这里会把自己刚设的 running 当成别的任务，永远返回「App 正在刷新」。
    """
    s = load_settings()
    with LOCK:
        if STATE["running"] and not force:
            return False, "App 正在刷新，等它跑完再改分类"
        STATE.update(running=True, step="重新分类", err="")
    try:
        added_map = _read_added_map(s)
        code = subprocess.run(
            [sys.executable, str(ROOT / "organize.py")],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=900)
        _apply_added_map(s, added_map)
        if code.returncode != 0:
            msg = (code.stderr or code.stdout or "重新分类失败").strip().splitlines()[-1]
            with LOCK:
                STATE.update(running=False, step="失败", err=msg)
            return False, msg
        with LOCK:
            STATE.update(running=False, step="完成", err="", count=build_meta(s).get("total", 0))
        return True, "ok"
    except Exception as e:
        with LOCK:
            STATE.update(running=False, step="失败", err=str(e))
        return False, str(e)


def trans_path(s=None):
    return data_dir(s) / "trans.json"


def load_trans(s=None):
    p = trans_path(s)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_trans(s, d):
    p = trans_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _open_json(url, s, data=None, headers=None, timeout=30):
    """先直连、再走代理（代理节点常被翻译服务限流）。"""
    proxy = (s.get("proxy") or "").strip()
    last = None
    for use_proxy in (False, True):
        if use_proxy and not proxy:
            break
        try:
            handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if use_proxy \
                else [urllib.request.ProxyHandler({})]
            req = urllib.request.Request(url, data=data,
                                         headers=headers or {"User-Agent": "Mozilla/5.0"})
            with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError("no route")


def _tr_llm(text, s):
    key = (s.get("llm_key") or "").strip()
    if not key:
        return ""
    base = (s.get("llm_base") or "").rstrip("/")
    body = json.dumps({
        "model": s.get("llm_model") or "deepseek-chat",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content":
             "Translate the user's social-media post into Simplified Chinese. "
             "Keep @mentions, hashtags, URLs and emoji as-is. Output only the translation."},
            {"role": "user", "content": text[:6000]},
        ],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key,
               "User-Agent": "Mozilla/5.0"}
    d = _open_json(base + "/chat/completions", s, data=body, headers=headers, timeout=60)
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def _tr_google(text, s):
    tl = s.get("translate_target") or "zh-CN"
    q = urllib.parse.quote(text[:4500])
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=%s&dt=t&q=%s"
           % (tl, q))
    d = _open_json(url, s, timeout=10)
    out = "".join(seg[0] for seg in (d[0] or []) if isinstance(seg, list) and seg and seg[0])
    out = out.strip()
    src = d[2] if len(d) > 2 and isinstance(d[2], str) else ""
    return (out, src) if out else ("", "")


def _norm_cmp(t):
    return re.sub(r"[\s\W_]+", "", (t or "")).lower()


def _chunks(text, size=450):
    """MyMemory 单次上限 500 字符，按句/空格切开再翻。"""
    parts, buf = [], ""
    for piece in re.split(r"(?<=[.!?。！？\n])\s*", text):
        while len(piece) > size:
            cut = piece.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            parts.append(piece[:cut])
            piece = piece[cut:]
        if len(buf) + len(piece) + 1 > size:
            parts.append(buf)
            buf = piece
        else:
            buf = (buf + " " + piece).strip() if buf else piece
    if buf:
        parts.append(buf)
    return [p for p in parts if p.strip()]


def _tr_mymemory(text, s):
    tl = s.get("translate_target") or "zh-CN"
    chunks = _chunks(text)

    def one(ch):
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=%s"
               % (urllib.parse.quote(ch), urllib.parse.quote("Autodetect|" + tl)))
        d = _open_json(url, s, timeout=25)
        t = ((d.get("responseData") or {}).get("translatedText") or "").strip()
        if not t or "INVALID" in t.upper() or "QUERY LENGTH" in t.upper():
            raise RuntimeError((d.get("responseDetails") or "MyMemory 没有返回")[:120])
        return t

    if len(chunks) <= 1:
        return " ".join(one(c) for c in chunks).strip()
    # 并发：MyMemory 单次 500 字符限制，长帖要切很多段，串行会很慢
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        outs = list(ex.map(one, chunks))
    return " ".join(outs).strip()


def translate_text(text, s):
    """按 provider 依次尝试，失败自动降级。"""
    text = (text or "").strip()
    if not text:
        return None, "没有正文可翻译"
    prov = (s.get("translate_provider") or "auto").lower()
    if prov == "off":
        return None, "翻译已关闭"
    chain = []
    if prov == "llm":
        chain = [("llm", _tr_llm), ("google", _tr_google), ("mymemory", _tr_mymemory)]
    elif prov == "google":
        chain = [("google", _tr_google), ("mymemory", _tr_mymemory)]
    elif prov == "mymemory":
        chain = [("mymemory", _tr_mymemory), ("google", _tr_google)]
    else:
        chain = [("mymemory", _tr_mymemory), ("google", _tr_google), ("llm", _tr_llm)]
    def same(a, b):
        return _norm_cmp(a) == _norm_cmp(b)

    errs = []
    for name, fn in chain:
        try:
            src = ""
            if name == "google":
                out, src = fn(text, s)
            else:
                out = fn(text, s)
            if not out:
                errs.append("%s: 空结果" % name)
                continue
            if same(out, text):
                errs.append("%s: 原样返回" % name)
                continue
            return {"text": out, "src": src, "provider": name}, None
        except Exception as e:  # noqa: BLE001
            errs.append("%s: %s" % (name, str(e)[:120]))
    if errs and all("原样返回" in e for e in errs):
        return None, ("翻译服务这次没给出译文（短句、本来就是目标语言、或服务被限流时都这样），"
                      "换一段长一点的内容再试")
    return None, "翻译失败（%s）" % "；".join(errs)


CAT_LABEL = {"ai": "AI", "tech": "技术", "life": "生活", "private": "私密", "misc": "待定"}


def _slug(text, n=48):
    t = re.sub(r'[\\/:*?"<>|\s]+', "-", str(text or "")).strip("-")
    return (t[:n] or "untitled")


def _yaml_str(v):
    return json.dumps(str(v or ""), ensure_ascii=False)


def export_items(include_private=False):
    """导出用：不截断正文；默认不含私密（要显式 include_private=1）。"""
    items = load_items(limit_text=10 ** 9)
    if not include_private:
        items = [x for x in items if x.get("cat") != "private"]
    return items


def build_markdown_zip(include_private=False):
    """Obsidian / Logseq 友好：每个分类一个文件夹，一条书签一个 md，带 YAML frontmatter。"""
    import io
    import zipfile

    items = export_items(include_private)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        seen = {}
        for it in items:
            cat = it.get("cat") or "misc"
            folder = CAT_LABEL.get(cat, cat)
            base = _slug((it.get("text") or "").strip().splitlines()[0] if it.get("text") else it.get("id"))
            name = "%s/%s-%s.md" % (folder, base, str(it.get("id"))[-8:])
            n = seen.get(name, 0)
            seen[name] = n + 1
            if n:
                name = name[:-3] + "-%d.md" % n
            lines = [
                "---",
                "url: %s" % _yaml_str(it.get("url")),
                "author: %s" % _yaml_str(it.get("handle")),
                "name: %s" % _yaml_str(it.get("name")),
                "date: %s" % _yaml_str(it.get("created")),
                "added_at: %s" % _yaml_str(it.get("added_at")),
                "category: %s" % _yaml_str(CAT_LABEL.get(cat, cat)),
                "media: %d" % len(it.get("media") or []),
                "sensitive: %s" % ("true" if it.get("sensitive") else "false"),
                "---",
                "",
                "# %s" % ((it.get("text") or "(无正文)").splitlines()[0][:120]),
                "",
                it.get("text") or "",
                "",
                "来源：%s" % (it.get("url") or ""),
            ]
            for m in (it.get("media") or [])[:12]:
                lines.append("![](%s)" % (m.get("url") or ""))
            z.writestr(name, "\n".join(lines).rstrip() + "\n")
    return buf.getvalue()


def build_table_csv(kind, include_private=False):
    """kind=raindrop 给 Raindrop.io 导入用；kind=notion 给 Notion 的 CSV 导入用。"""
    import csv
    import io

    items = export_items(include_private)
    buf = io.StringIO()
    if kind == "raindrop":
        head = ["url", "title", "folder", "tags", "created", "added", "notes"]
    else:
        head = ["Name", "URL", "Category", "Author", "Date", "Added", "Text"]
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for it in items:
        cat = CAT_LABEL.get(it.get("cat") or "misc", "待定")
        text = (it.get("text") or "").replace("\r", " ")
        if kind == "raindrop":
            w.writerow([it.get("url") or "", (it.get("text") or "")[:200], cat,
                        it.get("handle") or "", (it.get("created") or "")[:10],
                        it.get("added_at") or "",
                        ("@%s " % it.get("handle")) + text[:500]])
        else:
            w.writerow([(it.get("text") or "(无正文)")[:120], it.get("url") or "", cat,
                        "@" + (it.get("handle") or ""), (it.get("created") or "")[:10],
                        it.get("added_at") or "", text])
    return buf.getvalue().encode("utf-8-sig")


def _safe_url(u):
    """只让 http(s) 进 img/链接，避免 javascript: 之类被渲染成可点的东西。"""
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def load_items(s=None, limit_text=900):
    p = data_dir(s) / "items.json"
    if not p.exists():
        return []
    raw = None
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError("data/items.json 读不出来（%s）：修好或重新刷新一次" % e)
    try:
        doc = json.loads(raw)
    except Exception as e:
        # 自愈：items.json 是从 raw 重新生成的中间产物，坏掉时先退回上一次的备份
        bak = p.with_name("items.json.bak")
        if bak.exists():
            try:
                doc = json.loads(bak.read_text(encoding="utf-8"))
                p.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
                log("[自愈] items.json 坏了（%s），已用 items.json.bak 顶上" % e)
            except Exception:
                raise RuntimeError("data/items.json 读不出来（%s），备份也坏了：点「刷新」重新生成" % e)
        else:
            raise RuntimeError("data/items.json 读不出来（%s）：点「刷新」重新生成" % e)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise RuntimeError("data/items.json 结构不对（顶层该是 {\"items\": [...]}）：点「刷新」重新生成")
    its = doc.get("items") or []
    out = []
    for it in its:
        d = dict(it)
        t = d.get("text") or ""
        if len(t) > limit_text:
            d["text"] = t[:limit_text] + "…"
            d["trunc"] = True
        d["url"] = _safe_url(d.get("url"))
        med = []
        for m in (d.get("media") or []):
            if not isinstance(m, dict):
                continue
            mm = dict(m)
            mm["url"] = _safe_url(mm.get("url"))
            mm["thumb"] = _safe_url(mm.get("thumb")) or mm["url"]
            if mm["url"] or mm["thumb"]:
                med.append(mm)
        d["media"] = med
        out.append(d)
    return out


_ITEMS_JSON_CACHE = {"key": None, "text": "[]"}
_GZ_CACHE = {"sha": None, "data": b""}


def items_json_cached(s=None, limit_text=900):
    """把 items 的 JSON 文本缓存起来：文件没变就不重复读盘/加工/序列化。

    只缓存「文本」不缓存对象，避免调用方拿到共享 dict 后改坏缓存。
    """
    p = data_dir(s or load_settings()) / "items.json"
    try:
        st = p.stat()
    except OSError:
        return "[]"
    key = (str(p), st.st_mtime_ns, st.st_size, limit_text)
    if _ITEMS_JSON_CACHE.get("key") == key:
        return _ITEMS_JSON_CACHE["text"]
    items = load_items(s, limit_text=limit_text)
    text = json.dumps(items, ensure_ascii=False)
    _ITEMS_JSON_CACHE.update(key=key, text=text)
    return text


def invalidate_items_cache():
    _ITEMS_JSON_CACHE.update(key=None, text="[]")


# ---------------------------------------------------------------- 登录

ARCHIVE_FILES = ("items.json", "items.json.bak", "raw_bookmarks.json",
                 "raw_bookmarks.bak.json", "raw_likes.json", "raw_likes.bak.json",
                 "overrides.json")


def _acct_slug(h):
    h = str(h or "").strip().lstrip("@").strip("/")
    keep = "".join(ch for ch in h if ch.isalnum() or ch in "._-")
    return (keep or "unknown")[:60].lower()


def _read_meta(s=None):
    try:
        mp = data_dir(s or load_settings()) / "meta.json"
        if mp.exists():
            d = json.loads(mp.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _write_meta(s, meta):
    mp = data_dir(s) / "meta.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def data_owner(s=None):
    """data/ 里这批书签是谁的。老数据没有 owner 时退回 account。"""
    m = _read_meta(s)
    return str(m.get("owner") or m.get("account") or "").strip().lstrip("@")


def _move_path(src, dst):
    try:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return True
    except Exception as e:
        log("[账号] 归档 %s 失败：%s" % (getattr(src, "name", src), e))
    return False


def archive_account_data(s=None, handle="", reason=""):
    """把 data/ 里属于某个账号的数据整体挪到 data/archive/<handle>-<时间>/。只挪不删。"""
    s = s or load_settings()
    d = data_dir(s)
    if not d.exists():
        return ""
    dest = d / "archive" / ("%s-%s" % (_acct_slug(handle), time.strftime("%Y%m%d-%H%M%S")))
    moved = 0
    for name in ARCHIVE_FILES:
        if _move_path(d / name, dest / name):
            moved += 1
    if _move_path(d / "exports", dest / "exports"):
        moved += 1
    if not moved:
        return ""
    try:
        (dest / "README.txt").write_text(
            "这里存的是 @%s 的书签数据（%s）。\n"
            "归档时间：%s\n\n"
            "换账号时这些文件被整体挪过来，一条都没删。\n"
            "想找回：把它们拷回 data/ 覆盖同名文件，再重启 App 即可。\n"
            % (handle or "未知账号", reason or "切换账号", time.strftime("%Y-%m-%d %H:%M:%S")),
            encoding="utf-8")
    except Exception:
        pass
    log("[账号] @%s 的数据（%d 项）已归档到 data/archive/%s" % (handle or "上一个账号", moved, dest.name))
    _serve_log("account: archived %d files of %s -> %s" % (moved, handle or "?", dest))
    return str(dest)


def delete_account_data(s=None, handle="", reason=""):
    """换账号时直接删掉旧数据（用户在设置里选了「删除」）。删前不备份，但会记日志。"""
    s = s or load_settings()
    d = data_dir(s)
    if not d.exists():
        return []
    gone = []
    for name in ARCHIVE_FILES:
        target = d / name
        try:
            if target.exists():
                target.unlink()
                gone.append(name)
        except Exception as e:
            log("[账号] 删除 %s 失败：%s" % (name, e))
    try:
        shutil.rmtree(d / "exports", ignore_errors=True)
    except Exception:
        pass
    log("[账号] @%s 的数据已删除（%d 个文件，设置里选的是「删除」）"
        % (handle or "上一个账号", len(gone)))
    _serve_log("account: deleted %d files of %s (%s)" % (len(gone), handle or "?", reason))
    return gone


def switch_account_if_needed(s, handle):
    """换了 X 账号：上一个账号的数据整体归档，新账号的书签从零开始抓。

    只在「明确知道是谁」时才动数据：handle 为空（读不到账号名）绝不清库。
    """
    handle = str(handle or "").strip().lstrip("@").strip("/")
    if not handle:
        return ""
    owner = data_owner(s)
    if owner and owner.lower() != handle.lower():
        policy = str((s or {}).get("switch_account_policy") or "archive").lower()
        if policy == "delete":
            delete_account_data(s, owner, reason="换成了 @%s" % handle)
            dest = "deleted"
        else:
            dest = archive_account_data(s, owner, reason="换成了 @%s" % handle)
        meta = _read_meta(s)
        meta.update({"account": handle, "owner": handle, "total": 0,
                     "collected_at": "", "switched_from": owner,
                     "switched_policy": policy,
                     "switched_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        meta.pop("count", None)
        _write_meta(s, meta)
        with LOCK:
            STATE.update(count=0, err="")
        if policy == "delete":
            log("换了 X 账号：@%s → @%s。旧数据已按设置删除，新账号的书签重新抓一遍" % (owner, handle))
        else:
            log("换了 X 账号：@%s → @%s。旧数据已归档到 data/archive/（不是删除），新账号的书签重新抓一遍"
                % (owner, handle))
        return dest
    meta = _read_meta(s)
    if str(meta.get("owner") or "") != handle:
        meta["owner"] = handle
        if not meta.get("account"):
            meta["account"] = handle
        _write_meta(s, meta)
    return ""


def _meta_account(s=None):
    try:
        p = data_dir(s or load_settings()) / "meta.json"
        if p.exists():
            return str((json.loads(p.read_text(encoding="utf-8-sig")) or {}).get("account") or "")
    except Exception:
        pass
    return ""


def start_login(navigate=True):
    """开始登录：**只允许一个登录线程**，并且复用已经开着的窗口。

    以前是「先 kill_our_chrome 再 launch」：换账号时 /api/logout 刚开的窗口会被
    /api/login 的线程杀掉，用户正在里面输账号密码，窗口就没了。
    """
    s = load_settings()
    port = int(s.get("debug_port") or 9222)
    with LOCK:
        t = LOGIN_WORKER.get("t")
        if LOGIN.get("state") == "waiting" and t and t.is_alive():
            return LOGIN
        switch = bool(_meta_account(s))
        LOGIN.update(state="waiting", handle="", msg="正在打开登录窗口…", at=time.time())

    def run():
        try:
            _login_worker(s, switch, navigate)
        finally:
            LOGIN_WORKER["t"] = None

    th = threading.Thread(target=run, daemon=True)
    LOGIN_WORKER["t"] = th
    th.start()
    _serve_log("login: start (switch=%s, navigate=%s, browser_alive=%s)"
               % (switch, navigate, cdp.alive(port)))
    return LOGIN


def clear_x_cookies(s=None):
    """只清掉本机采集浏览器里 x.com 的登录态，不动用户的日常浏览器。"""
    s = s or load_settings()
    port = int(s.get("debug_port") or 9222)
    if not cdp.alive(port):
        return False
    try:
        tgt = cdp.pick_target(port)
        c = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=30)
        c.send("Network.enable")
        c.send("Network.clearBrowserCookies")
        try:
            c.send("Page.navigate", {"url": "https://x.com/i/flow/login"})
        except Exception:
            pass
        c.close()
        return True
    except Exception as e:
        log("清 cookie 失败：%s" % e)
        return False


def start_logout():
    s = load_settings()
    with LOCK:
        LOGIN.update(state="idle", handle="", msg="正在退出当前账号…")
    threading.Thread(target=_logout_worker, args=(s,), daemon=True).start()
    return {"state": "switching", "msg": "正在退出当前账号…"}


def _logout_worker(s):
    try:
        with LOCK:
            if STATE["running"]:
                LOGIN.update(state="error", msg="有任务在跑，先点「停止」")
                return
        # 这里**不再自己开窗口**：以前它开一个、/api/login 的线程紧接着又杀掉它，
        # 用户正在输密码的窗口就是这么消失的。现在只清已有窗口里的 cookie。
        port = int(s.get("debug_port") or 9222)
        with BROWSER_LOCK:
            if cdp.alive(port):
                clear_x_cookies(s)
                _serve_log("logout: cookies cleared in open window")
            else:
                _serve_log("logout: no browser open, nothing to clear")
        dd = data_dir(s)
        mp = dd / "meta.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8-sig"))
                meta.pop("account", None)
                mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        with LOCK:
            LOGIN.update(state="idle", handle="", msg="已退出，点「登录」重新登录")
        log("已退出当前 X 账号")
    except Exception as e:
        with LOCK:
            LOGIN.update(state="error", msg=str(e))
        _serve_log("logout: error %s" % e)


def _login_worker(s, switch=False, navigate=True):
    """打开（或复用）一个可见窗口，等用户在里面登录。

    三条铁律：
      1. 窗口活着就只导航，绝不「先杀再开」——用户可能正在里面打字；
      2. 登录成功后窗口留着，采集复用同一个窗口；
      3. 窗口被人关掉时立刻报错，不要傻等 30 分钟。
    """
    port = int(s.get("debug_port") or 9222)
    try:
        with BROWSER_LOCK:
            if cdp.alive(port):
                try:
                    tgt = cdp.pick_target(port)
                    c = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=20)
                    if switch:
                        c.send("Network.enable")
                        c.send("Network.clearBrowserCookies")
                        _serve_log("login: cleared cookies (switching account)")
                    if navigate:
                        # 服务重启后「继续盯着已开的窗口」时不要跳转，否则会冲掉用户正在输入的账号
                        c.send("Page.navigate", {"url": "https://x.com/i/flow/login"})
                    c.close()
                    _serve_log("login: reused open window (navigate=%s)" % navigate)
                except Exception as e:
                    _serve_log("login: reuse failed %s" % e)
            else:
                launch_chrome(s, visible=True, url="https://x.com/i/flow/login")
                _serve_log("login: launched visible window")
            for _ in range(60):
                time.sleep(0.5)
                if cdp.alive(port):
                    break
            else:
                raise RuntimeError("浏览器没起来（端口 %d 无响应）" % port)
            UI_VISIBLE["on"] = True
        with LOCK:
            LOGIN["msg"] = "请在打开的窗口里登录 X（登录完不用关窗口，我会自动接着抓）"
            LOGIN["at"] = time.time()

        dead = 0
        last_beat = 0.0
        nudged = 0.0
        for _ in range(900):              # 2s × 900 = 30 分钟
            time.sleep(2)
            if not cdp.alive(port):
                dead += 1
                if dead >= 3:
                    raise RuntimeError("登录窗口被关掉了（或 Chrome 崩了）—— 点「登录」重新打开，"
                                       "密码浏览器帮你记着")
                continue
            dead = 0
            try:
                tgt = cdp.pick_target(port)
                c = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=20)
                href = c.ev("location.href") or ""
                who = c.ev("""(() => {
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
                raw = c.ev("document.cookie") or ""
                c.close()
            except Exception:
                continue
            logged = ("twid=" in raw)
            if time.time() - last_beat > 8:
                last_beat = time.time()
                _serve_log("[login] url=%s who=%s cookie=%s" % (href[:80], who or "-", "1" if logged else "0"))
            if not who and logged and "/onboarding" in href and time.time() - nudged > 30:
                # 登录成功但停在 onboarding：主动去首页，个人主页链接才会出现
                nudged = time.time()
                try:
                    tgt = cdp.pick_target(port)
                    c2 = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=20)
                    c2.send("Page.navigate", {"url": "https://x.com/home"})
                    c2.close()
                    _serve_log("login: onboarding → /home")
                except Exception:
                    pass
            if who or logged:
                handle = who.strip("/").lstrip("@")
                if handle:
                    switch_account_if_needed(s, handle)
                with LOCK:
                    LOGIN.update(state="ok", handle=handle,
                                 msg=("已登录 @%s" % handle) if handle
                                 else "已登录（读不到账号名，但不影响抓取）",
                                 at=time.time())
                _serve_log("login: ok @%s" % (handle or "-"))
                time.sleep(2)
                # 窗口留着：采集要用同一个已登录的浏览器
                threading.Thread(target=run_pipeline, kwargs={"reason": "登录后首次采集"},
                                 daemon=True).start()
                return
        with LOCK:
            LOGIN.update(state="error", msg="等了 30 分钟还没检测到登录")
        _serve_log("login: timeout 30min")
    except Exception as e:
        with LOCK:
            LOGIN.update(state="error", msg=str(e))
        _serve_log("login: error %s" % e)


# ---------------------------------------------------------------- HTTP

class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True          # 必须在构造前生效，否则重启会撞 TIME_WAIT


class Handler(BaseHTTPRequestHandler):
    server_version = "XBookmarks/" + VERSION
    timeout = 15                        # 慢连接别把线程池拖死

    def log_message(self, fmt, *args):
        pass

    # -------- helpers
    @staticmethod
    def _origin_ok(o):
        """允许的来源：浏览器扩展、本机、局域网（手机用局域网地址直连时也会带 Origin）。"""
        if not o:
            return True
        if o.startswith("chrome-extension://") or o.startswith("http://127.0.0.1") \
                or o.startswith("http://localhost") or o == "null":
            return True
        for pre in ("http://192.168.", "http://10.", "http://172."):
            if o.startswith(pre):
                return True
        return False

    def _cors(self):
        """只放行浏览器扩展与本地/局域网页面，方便「桥」直接 POST 进来。"""
        o = self.headers.get("Origin") or ""
        allow = bool(o) and self._origin_ok(o)
        if allow:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if (self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _not_allowed(self):
        return self.send_json({"error": "%s 不支持这个方法" % self.path}, 501)

    def do_DELETE(self):
        return self._not_allowed()

    def do_PUT(self):
        return self._not_allowed()

    def do_PATCH(self):
        return self._not_allowed()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json_raw(self, body, code=200):
        """已经是 JSON 文本的响应体：大体积且非本机访问时 gzip。"""
        try:
            local = is_local(self.client_address[0])
        except Exception:
            local = True
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if (not local) and "gzip" in ae and len(body) > 16384:
            sha = hashlib.sha1(body).digest()
            if _GZ_CACHE.get("sha") == sha:
                body = _GZ_CACHE["data"]
            else:
                body = gzip.compress(body, 4)
                _GZ_CACHE.update(sha=sha, data=body)
            enc = "gzip"
        else:
            enc = ""
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, filename=""):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self, limit=1 << 20):
        self._too_big = False
        self._bad_json = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        if n > limit:
            self._too_big = True
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data
        except Exception:
            self._bad_json = True
            return {}

    # -------- 访问控制（手机/局域网访问时用访问码）
    def _cookies(self):
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = urllib.parse.unquote(v.strip())
        return out

    def _authed(self):
        s = load_settings()
        code = str(s.get("access_code") or "")
        ip = self.client_address[0]
        if is_local(ip):
            return True
        if not code:
            return False                    # 没设码就不许从局域网进
        fails, until = UNLOCK_FAIL.get(ip, (0, 0.0))
        if until > time.time():
            return False                    # 试错太多次：这段时间一律拒绝
        tok = _code_token(s)
        got = self._cookies().get(COOKIE_KEY) or ""
        if got and (_const_eq(got, tok) or _const_eq(got, code)):
            UNLOCK_FAIL.pop(ip, None)
            return True
        hdr = self.headers.get("X-XBM-Key") or ""
        if hdr and (_const_eq(hdr, code) or _const_eq(hdr, tok)):
            UNLOCK_FAIL.pop(ip, None)
            return True
        if got or hdr:
            # 带了码但是错的：跟 /api/unlock 共用一套退避，别让 header 成为无限试错通道
            fails += 1
            if fails >= 3:
                until = time.time() + min(300.0, 5.0 * (2 ** min(fails - 3, 6)))
            UNLOCK_FAIL[ip] = (fails, until)
            time.sleep(min(1.2, 0.15 * fails))
        return False

    def _need_key(self):
        if self.path.startswith("/api/"):
            return self.send_json({"ok": False, "error": "需要访问码", "need_key": True}, 401)
        return self.send_html(unlock_html())

    # -------- routes
    def log_message(self, fmt, *args):
        # 访问日志：只写 data/access.log（最近 500 条），方便看外部请求有没有真的到达
        try:
            line = "%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                    self.client_address[0], fmt % args)
            path = Path(load_settings().get("data_dir") or "data") / "access.log"
            old = []
            if path.exists():
                old = path.read_text(encoding="utf-8", errors="replace").splitlines()[-499:]
            old.append(line.rstrip("\n"))
            path.write_text("\n".join(old) + "\n", encoding="utf-8")
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        s = load_settings()

        # PWA 外壳：不需要访问码（里面没有数据）
        if p == "/sw.js":
            return self.send_file(WEB / "sw.js", "application/javascript; charset=utf-8")
        if p == "/manifest.webmanifest":
            return self.send_file(WEB / "manifest.webmanifest", "application/manifest+json; charset=utf-8")
        if p.startswith("/icon-") and p.endswith(".png"):
            f = (WEB / p.lstrip("/")).resolve()
            try:
                inside = f.is_relative_to(WEB.resolve())
            except AttributeError:
                inside = str(f).startswith(str(WEB.resolve()) + os.sep)
            if inside and f.is_file():
                return self.send_file(f, "image/png")
        if p == "/unlock":
            return self.send_html(unlock_html())
        if not self._authed():
            return self._need_key()

        if p in ("/", "/index.html"):
            f = WEB / "app.html"
            if not f.exists():
                return self.send_json({"error": "web/app.html 缺失"}, 500)
            return self.send_html(f.read_text(encoding="utf-8"))

        if p == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if p == "/api/extlog":
            path = Path(s.get("data_dir") or ".") / "extlog.json"
            try:
                logs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            except Exception:
                logs = []
            if not isinstance(logs, list):
                logs = []
            return self.send_json({"ok": True, "logs": logs[-20:]})

        if p == "/api/state":
            try:
                items_text = items_json_cached(s)
            except Exception as e:
                return self.send_json({"error": str(e), "items": [], "meta": build_meta(s),
                                       "login": LOGIN, "settings": {}}, 500)
            pub = {k: v for k, v in s.items()
                   if k not in ("chrome_path", "llm_key", "cookie_salt")}
            pub["has_llm_key"] = bool(s.get("llm_key"))
            if not is_local(self.client_address[0]):
                pub["access_code"] = _mask_code(pub.get("access_code"))
                pub["code_masked"] = True
            body = ('{"items":%s,"meta":%s,"settings":%s,"login":%s,"refresh":%s,'
                    '"semantic":%s,"version":%s}') % (
                items_text,
                json.dumps(build_meta(s), ensure_ascii=False),
                json.dumps(pub, ensure_ascii=False),
                json.dumps(LOGIN, ensure_ascii=False),
                json.dumps({k: STATE[k] for k in ("running", "step", "err", "count", "at")},
                           ensure_ascii=False),
                json.dumps(sem_meta(s), ensure_ascii=False),
                json.dumps(VERSION, ensure_ascii=False),
            )
            return self.send_json_raw(body.encode("utf-8"))

        if p == "/api/item":
            iid = (q.get("id") or [""])[0]
            for it in load_items(s, limit_text=10 ** 9):
                if it.get("id") == iid:
                    return self.send_json(it)
            return self.send_json({"error": "not found"}, 404)

        if p == "/api/net":
            ip = lan_ip()
            port = int(s.get("port") or 8900)
            code = str(s.get("access_code") or "")
            local = is_local(self.client_address[0])
            return self.send_json({
                "bind": str(s.get("bind") or "127.0.0.1"),
                "lan_ip": ip, "port": port,
                "access_code": code if local else _mask_code(code),
                "code_masked": not local,
                "lan_url": ("http://%s:%d/" % (ip, port)) if ip else "",
            })

        if p in ("/api/status", "/status"):
            with LOCK:
                return self.send_json({k: STATE.get(k) for k in
                                       ("running", "step", "log", "err", "count", "at", "pages", "new")})

        if p == "/api/login":
            with LOCK:
                return self.send_json(LOGIN)

        if p == "/api/export":
            kind = (q.get("kind") or ["csv"])[0]
            inc = (q.get("private") or ["0"])[0].lower() in ("1", "true", "yes", "on")
            if kind in ("md", "markdown", "obsidian", "zip", "json", "items", "raindrop",
                        "notion", "csv", "xlsx") and not build_meta(s).get("total"):
                return self.send_json({"error": "还没有书签，先点「刷新」抓一次再导出"}, 400)
            if kind in ("md", "markdown", "obsidian", "zip"):
                try:
                    data = build_markdown_zip(inc)
                except RuntimeError as e:
                    return self.send_json({"error": str(e)}, 500)
                return self.send_bytes(data, "application/zip", "x-bookmarks-md.zip")
            if kind in ("json", "items"):
                items = export_items(inc)
                data = json.dumps({"count": len(items), "items": items},
                                  ensure_ascii=False, indent=1).encode("utf-8")
                return self.send_bytes(data, "application/json; charset=utf-8",
                                       "x-bookmarks.json")
            if kind in ("raindrop", "notion"):
                try:
                    data = build_table_csv(kind, inc)
                except RuntimeError as e:
                    return self.send_json({"error": str(e)}, 500)
                return self.send_bytes(data, "text/csv; charset=utf-8",
                                       "x-bookmarks-%s.csv" % kind)
            if kind not in ("csv", "xlsx"):
                return self.send_json({"error": "未知导出格式 %s" % kind}, 400)
            f = export_dir(s) / ("bookmarks.xlsx" if kind == "xlsx" else "bookmarks.csv")
            if not f.exists():
                return self.send_json({"error": "还没有导出文件，先刷新一次"}, 404)
            ctype = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                     if kind == "xlsx" else "text/csv; charset=utf-8")
            return self.send_file(f, ctype)

        if p == "/api/settings":
            cur = dict(load_settings())
            if not is_local(self.client_address[0]):
                cur["access_code"] = _mask_code(str(cur.get("access_code") or ""))
            return self.send_json({"ok": True, "settings": cur})

        if p == "/api/rules":
            try:
                rules = json.loads((ROOT / "rules.json").read_text(encoding="utf-8"))
            except Exception as e:
                return self.send_json({"ok": False, "error": "rules.json 读不出来：%s" % e}, 500)
            return self.send_json({"ok": True, "rules": rules})

        if p == "/api/semantic":
            return self.send_json({"ok": True, "semantic": sem_meta(load_settings())})

        return self.send_json({"error": "not found"}, 404)

    def _reject_cross_site(self):
        """CORS 只挡「读响应」，挡不住写请求的副作用：带 Origin 且不是自家页面的，直接 403。"""
        o = self.headers.get("Origin") or ""
        if o and not self._origin_ok(o):
            self.send_json({"ok": False, "error": "拒绝了来自 %s 的跨站请求" % o}, 403)
            return True
        return False

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        s = load_settings()
        if self._reject_cross_site():
            return

        if p == "/api/unlock":
            ip = self.client_address[0]
            fails, until = UNLOCK_FAIL.get(ip, (0, 0.0))
            now = time.time()
            if until > now:
                return self.send_html(unlock_html("试错太多了，请 %d 秒后再试" % int(until - now)), 429)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except Exception:
                n = 0
            if n > 4096:
                return self.send_html(unlock_html("请求太大"), 413)
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            ctype = (self.headers.get("Content-Type") or "").lower()
            form = {}
            if "json" in ctype:
                try:
                    form = json.loads(raw or "{}")
                except Exception:
                    form = {}
            else:
                form = {k: v[0] for k, v in parse_qs(raw).items()}
            code = str((form or {}).get("code") or "").strip()
            want = str(s.get("access_code") or "")
            if code and want and _const_eq(code, want):
                UNLOCK_FAIL.pop(ip, None)
                self.send_response(303)
                self._cors()
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", "%s=%s; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly"
                                 % (COOKIE_KEY, _code_token(s) or code))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            fails += 1
            wait = min(300, 2 ** min(fails, 8))
            UNLOCK_FAIL[ip] = (fails, now + wait)
            log("访问码错误：%s 第 %d 次，%d 秒内不再受理" % (ip, fails, wait))
            return self.send_html(unlock_html("访问码不对"), 401)

        if not self._authed():
            return self._need_key()

        if p in ("/api/refresh", "/refresh"):
            if STATE["running"]:
                return self.send_json({"ok": False, "msg": "正在刷新中"}, 202)
            threading.Thread(target=run_pipeline, daemon=True).start()
            return self.send_json({"ok": True, "msg": "已开始刷新"})

        if p == "/api/extlog":
            if self.command == "GET":
                path = Path(s.get("data_dir") or ".") / "extlog.json"
                try:
                    logs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                except Exception:
                    logs = []
                return self.send_json({"ok": True, "logs": logs[-20:]})
            body = self.body_json()
            if not isinstance(body, dict):
                return self.send_json({"ok": False, "msg": "bad body"}, 400)
            entry = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stage": str(body.get("stage") or "")[:40],
                "msg": str(body.get("msg") or "")[:600],
                "url": str(body.get("url") or "")[:300],
                "ver": str(body.get("ver") or "")[:20],
            }
            path = Path(s.get("data_dir") or ".") / "extlog.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                logs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            except Exception:
                logs = []
            if not isinstance(logs, list):
                logs = []
            logs.append(entry)
            logs = logs[-50:]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(logs, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
            log("[扩展] %s · %s" % (entry["stage"], entry["msg"][:120]))
            return self.send_json({"ok": True, "saved": len(logs)})

        if p == "/api/ingest":
            body = self.body_json(limit=INGEST_LIMIT)
            if getattr(self, "_too_big", False):
                return self.send_json({
                    "ok": False,
                    "msg": "书签数据太大（上限 %d MB），请分批同步" % (INGEST_LIMIT >> 20),
                }, 413)
            items = (body or {}).get("items") if isinstance(body, dict) else None
            if not isinstance(items, list) or not items:
                return self.send_json({"ok": False, "msg": "没有收到书签数据"}, 400)
            def _known(rec):
                if not isinstance(rec, dict):
                    return False
                return bool(rec.get("legacy") or rec.get("full_text") or rec.get("note_tweet")
                            or rec.get("note_tweet_results") or rec.get("rest_id") or rec.get("id_str"))
            if sum(1 for x in items if _known(x)) < len(items) * 0.5:
                return self.send_json({
                    "ok": False,
                    "msg": "这批数据里一半以上没有 full_text/legacy 字段，不是 X 的原始结构，写进来只会是一堆空卡片；"
                           "请用扩展（1.2.x）重新抓取后再同步",
                }, 400)
            with LOCK:
                if STATE["running"]:
                    return self.send_json({"ok": False, "msg": "App 正在刷新，稍后再试"}, 202)
                STATE.update(running=True, step="插件同步", err="", log=[], started=time.time())
            try:
                ok, msg, total_raw = ingest_items(items, str((body or {}).get("handle") or ""),
                                                 str((body or {}).get("mode") or "bookmark"))
                meta = build_meta(s)
                with LOCK:
                    STATE.update(running=False, step="完成" if ok else "失败",
                                 count=meta.get("total", 0),
                                 at=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 err="" if ok else msg)
                if not ok:
                    return self.send_json({"ok": False, "msg": msg}, 500)
                log("插件同步完成：收到 %d 条，合并后共 %d 条" % (len(items), total_raw))
                return self.send_json({"ok": True, "count": total_raw, "received": len(items),
                                       "total": meta.get("total", 0),
                                       "account": meta.get("account") or (body or {}).get("handle") or ""})
            except Exception as e:
                with LOCK:
                    STATE.update(running=False, step="失败", err=str(e))
                return self.send_json({"ok": False, "msg": str(e)}, 500)

        if p == "/api/login":
            return self.send_json(start_login())

        if p == "/api/cancel":
            with LOCK:
                if not STATE["running"]:
                    return self.send_json({"ok": False, "msg": "现在没有在跑的任务"})
                STATE["cancel"] = True
            log("收到「停止」请求")
            return self.send_json({"ok": True, "msg": "正在停止"})

        if p == "/api/logout":
            return self.send_json(start_logout())

        if p == "/api/semantic":
            if STATE["running"]:
                return self.send_json({"ok": False, "msg": "正在跑别的任务"}, 202)
            threading.Thread(target=run_semantic, daemon=True).start()
            return self.send_json({"ok": True, "msg": "已开始语义分类"})

        if p == "/api/translate":
            body = self.body_json()
            if not isinstance(body, dict):
                return self.send_json({"error": "bad body"}, 400)
            iid = str(body.get("id") or "")
            text = body.get("text") or ""
            if iid:
                for it in load_items(s, limit_text=10 ** 9):
                    if it.get("id") == iid:
                        text = it.get("text") or ""
                        break
            cache = load_trans(s)
            key = iid or ("t:" + text[:60])
            hit = cache.get(key)
            if (body.get("fresh") is not True and hit and hit.get("text")
                    and _norm_cmp(hit["text"]) != _norm_cmp(text)):
                return self.send_json({"ok": True, "cached": True, "text": hit["text"],
                                       "src": hit.get("lang") or "", "provider": hit.get("provider") or ""})
            try:
                res, err = translate_text(text, s)
            except Exception as e:
                return self.send_json({"ok": False, "msg": "翻译失败：%s" % e}, 502)
            if not res:
                code = 400 if (err and "没有正文" in err) else 502
                return self.send_json({"ok": False, "msg": err or "翻译失败"}, code)
            cache[key] = {"text": res["text"], "lang": res.get("src") or "",
                          "provider": res.get("provider") or "", "at": time.strftime("%Y-%m-%d %H:%M")}
            save_trans(s, cache)
            return self.send_json({"ok": True, "cached": False, "text": res["text"],
                                   "src": res.get("src") or "", "provider": res.get("provider") or ""})

        if p == "/api/item":
            body = self.body_json()
            if not isinstance(body, dict) or not body.get("id"):
                return self.send_json({"error": "bad body"}, 400)
            iid = str(body["id"])
            cat = str(body.get("cat") or "").strip()
            with LOCK:
                if STATE["running"]:
                    return self.send_json({"ok": False, "msg": "正在刷新中，稍后再改"}, 202)
            if cat and cat not in CATS:
                return self.send_json({"error": f"未知分类 {cat}"}, 400)
            if not any(str(it.get("id")) == iid for it in load_items(s)):
                return self.send_json({"error": "没有这条书签：%s" % iid}, 404)
            ov = load_overrides(s)
            if cat:
                ov[iid] = cat
            else:
                ov.pop(iid, None)
            save_overrides(s, ov)
            ok, msg = reclassify()
            return self.send_json({"ok": ok, "cat": cat or "auto", "msg": msg,
                                   "meta": build_meta(s)}, 200 if ok else 500)

        if p == "/api/item_batch":
            body = self.body_json()
            if getattr(self, "_too_big", False):
                return self.send_json({"ok": False, "msg": "请求太大"}, 413)
            ids = (body or {}).get("ids") if isinstance(body, dict) else None
            cat = str(((body or {}).get("cat") or "")).strip() if isinstance(body, dict) else ""
            if not isinstance(ids, list) or not ids:
                return self.send_json({"error": "ids 不能为空"}, 400)
            if len(ids) > 5000:
                return self.send_json({"error": "一次最多 5000 条"}, 400)
            if any(not isinstance(x, (str, int)) for x in ids):
                return self.send_json({"error": "ids 里只能是字符串或数字"}, 400)
            if cat and cat not in CATS:
                return self.send_json({"error": "未知分类 %s" % cat}, 400)
            with LOCK:
                if STATE["running"]:
                    return self.send_json({"ok": False, "msg": "正在刷新中，稍后再改"}, 202)
            ov = load_overrides(s)
            for iid in ids:
                iid = str(iid)
                if cat:
                    ov[iid] = cat
                else:
                    ov.pop(iid, None)
            save_overrides(s, ov)
            ok, msg = reclassify()
            return self.send_json({"ok": ok, "count": len(ids), "cat": cat or "auto",
                                   "msg": msg, "meta": build_meta(s)}, 200 if ok else 500)

        if p == "/api/settings":
            body = self.body_json()
            if getattr(self, "_too_big", False):
                return self.send_json({"ok": False, "msg": "请求太大"}, 413)
            if getattr(self, "_bad_json", False):
                return self.send_json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "bad body"}, 400)
            for key in ("port", "debug_port"):
                if key in body:
                    try:
                        v = int(str(body[key]).strip())
                    except Exception:
                        return self.send_json({"ok": False, "error": "%s 必须是数字（收到 %r）" % (key, body[key])}, 400)
                    if not 1 <= v <= 65535:
                        return self.send_json({"ok": False, "error": "%s 超出范围（1-65535）" % key}, 400)
                    body[key] = v
            if "bind" in body:
                b = str(body["bind"] or "").strip()
                if b and b not in ("0.0.0.0", "127.0.0.1", "::1", "::") \
                        and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", b):
                    return self.send_json({"ok": False, "error": "bind 只能填 0.0.0.0 / 127.0.0.1 或一个 IP"}, 400)
            # 能改代理/LLM 地址就等于能把 llm_key 和翻译流量导走：非本机必须重输访问码
            risky = [k for k in ("llm_base", "llm_key", "proxy")
                     if k in body and str(body.get(k)) != str(s.get(k))]
            if risky and not is_local(self.client_address[0]):
                if not _const_eq(str(body.get("confirm_code") or ""), str(s.get("access_code") or "")):
                    return self.send_json({"ok": False, "need_code": True, "keys": risky,
                                           "msg": "改代理 / 翻译地址 / 密钥需要重新输入访问码"}, 403)
            merged = dict(s)
            for k in DEFAULTS:
                if k in body:
                    merged[k] = body[k]
            clean = save_settings(merged)
            fixed = sorted(k for k in merged if k in DEFAULTS and merged[k] != clean[k])
            return self.send_json({"ok": True, "settings": clean,
                                   "warn": ("有 %d 项取值不合法，已回退到允许范围：%s"
                                            % (len(fixed), "、".join(fixed))) if fixed else ""})

        return self.send_json({"error": "not found"}, 404)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="serve.py",
        description="X 书签 · 本地自动分类书签 App（默认 http://127.0.0.1:8900）",
        epilog="示例：python serve.py --open    # 启动并打开 App 窗口",
    )
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    ap.add_argument("--port", type=int, default=0, help="监听端口（默认取 settings.json 的 port，或 8900）")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器窗口（Chrome --app 模式）")
    a = ap.parse_args(argv)
    s = load_settings()
    port = a.port or int(s.get("port") or 8900)

    if not SETTINGS_PATH.exists():
        save_settings(s)

    # 单实例：端口已被占用就直接退出
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if probe.connect_ex(("127.0.0.1", port)) == 0:
        print(f"端口 {port} 已在监听，视为已在运行")
        if a.open:
            open_app_window(port)
        return 0
    probe.close()

    bind = str(s.get("bind") or "127.0.0.1")
    if bind not in ("127.0.0.1", "0.0.0.0", "::"):
        bind = "127.0.0.1"
    if bind != "127.0.0.1" and not str(s.get("access_code") or ""):
        s["access_code"] = new_code()
        save_settings(s)
        print("已生成访问码：%s" % s["access_code"])

    srv = LocalServer((bind, port), Handler)

    def auto_loop():
        while True:
            time.sleep(60)
            s2 = load_settings()
            hrs = float(s2.get("auto_refresh_hours") or 0)
            if hrs <= 0 or STATE["running"]:
                continue
            last = 0.0
            try:
                last = datetime_from(build_meta(s2).get("updated_at"))
            except Exception:
                last = 0.0
            if time.time() - last >= hrs * 3600:
                run_pipeline("自动刷新")

    threading.Thread(target=auto_loop, daemon=True).start()

    # 服务重启时如果登录窗口还开着、又还没登进去，接着盯着它（不跳转，免得冲掉正在输入的账号）
    def resume_login_watch():
        time.sleep(1.5)
        s2 = load_settings()
        port2 = int(s2.get("debug_port") or 9222)
        if _meta_account(s2) or not cdp.alive(port2):
            return
        try:
            url = (cdp.pick_target(port2).get("url") or "").lower()
        except Exception:
            return
        if "flow/login" in url or "onboarding" in url or "/i/jf/" in url:
            _serve_log("startup: resuming login watch on open window")
            start_login(navigate=False)

    threading.Thread(target=resume_login_watch, daemon=True).start()
    print(f"X Bookmarks 已启动: http://127.0.0.1:{port}/")
    if bind != "127.0.0.1":
        ip = lan_ip()
        print("局域网访问: http://%s:%d/  访问码: %s" % (ip, port, s.get("access_code") or "(未设)"))
    if a.open:
        threading.Thread(target=lambda: (time.sleep(1.2), open_app_window(port)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def datetime_from(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        try:
            return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))
        except Exception:
            return 0.0


def open_app_window(port):
    s = load_settings()
    app_profile = profile_dir(s) / "app"
    app_profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen([chrome_path(s), f"--app=http://127.0.0.1:{port}/",
                          f"--user-data-dir={app_profile}",
                          "--no-first-run", "--no-default-browser-check",
                          "--disable-features=Translate,MediaRouter",
                          "--window-size=1240,940", "--window-position=90,60"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print("打开窗口失败:", e)


if __name__ == "__main__":
    # 进程以前挂了什么都不留（用户只看到"窗口没了"），这里兜住所有未捕获异常。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        try:
            p = data_dir(load_settings()) / "crash.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write("=" * 70 + "\n%s\n%s\n" %
                        (time.strftime("%Y-%m-%d %H:%M:%S"), traceback.format_exc()))
        except Exception:
            pass
        raise
