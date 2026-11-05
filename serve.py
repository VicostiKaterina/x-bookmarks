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
}

SORT_KEYS = ("time_desc", "time_asc", "views", "likes", "retweets", "bookmarks", "replies")
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
    out["access_code"] = str(out.get("access_code") or "").strip()[:64]
    out["default_sort"] = str(out.get("default_sort") or "").strip()
    if out["default_sort"] not in SORT_KEYS:
        out["default_sort"] = DEFAULTS["default_sort"]
    for k in ("data_dir", "export_dir", "profile_dir"):
        v = str(out.get(k) or "").strip()
        out[k] = v or DEFAULTS[k]
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

LOGIN = {"state": "idle", "handle": "", "msg": ""}
STATE = {"running": False, "step": "", "log": [], "err": "", "count": 0, "at": "", "started": 0.0}
LOCK = threading.Lock()
UNLOCK_FAIL = {}          # ip -> (失败次数, 解禁时间)


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


VERSION = "2.1.7"
COOKIE_KEY = "xbm_key"


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


def kill_our_chrome(s=None):
    """只杀用的是本项目 profile 的 chrome，不碰用户自己的浏览器。"""
    s = s or load_settings()
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
        ok, msg = reclassify()
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


def run_pipeline(reason="手动刷新"):
    s = load_settings()
    with LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, step="准备浏览器", err="", log=[], started=time.time())
    try:
        if cdp.alive(int(s.get("debug_port") or 9222)):
            with LOCK:
                STATE["step"] = "检查登录状态"
            try:
                tgt = cdp.pick_target(int(s["debug_port"]))
                cur = tgt.get("url") or ""
                if "onboarding" in cur or "/i/flow/login" in cur:
                    raise RuntimeError("未登录：点右上角「登录」按钮，在弹出的窗口里登录 X")
            except RuntimeError:
                raise
            except Exception:
                pass
        else:
            with LOCK:
                STATE["step"] = "启动采集浏览器"
            ensure_chrome(s, visible=False, url="https://x.com/home")

        with LOCK:
            STATE["step"] = "抓取书签"
        code = subprocess.run(
            [sys.executable, str(ROOT / "collect.py"), "--port", str(int(s.get("debug_port") or 9222))],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=1800)
        for line in (code.stdout or "").splitlines()[-6:]:
            log(line)
        if code.returncode != 0:
            raise RuntimeError((code.stderr or code.stdout or "采集失败").strip().splitlines()[-1])

        with LOCK:
            STATE["step"] = "分类整理"
        code = subprocess.run(
            [sys.executable, str(ROOT / "organize.py")],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=900)
        for line in (code.stdout or "").splitlines()[:8]:
            log(line)
        if code.returncode != 0:
            raise RuntimeError((code.stderr or "分类失败").strip().splitlines()[-1])

        meta = build_meta(s)
        with LOCK:
            STATE.update(running=False, step="完成", count=meta.get("total", 0),
                         at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log(f"刷新完成：{meta.get('total')} 条")
        return True
    except Exception as e:
        with LOCK:
            STATE.update(running=False, step="失败", err=str(e), at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log(f"刷新失败：{e}")
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
    dd = data_dir(s)
    dd.mkdir(parents=True, exist_ok=True)
    raw = dd / ("raw_likes.json" if mode == "like" else "raw_bookmarks.json")
    if raw.exists():
        try:
            (dd / (raw.stem + ".bak" + raw.suffix)).write_bytes(raw.read_bytes())
        except Exception:
            pass
    merged = merge_items(_raw_list(raw), items)
    tmp = raw.with_name(raw.stem + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    tmp.replace(raw)
    try:
        mp = dd / "meta.json"
        meta = {}
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8-sig"))
        meta.update({"account": handle or meta.get("account") or "", "collected_at": time.strftime("%Y-%m-%d %H:%M"),
                     "count": len(merged), "mode": mode, "source": "extension"})
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log("写 meta.json 失败：%s" % e)
    ok, msg = reclassify(force=True)
    return ok, msg, len(merged)


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
        code = subprocess.run(
            [sys.executable, str(ROOT / "organize.py")],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=900)
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
        head = ["url", "title", "folder", "tags", "created", "notes"]
    else:
        head = ["Name", "URL", "Category", "Author", "Date", "Text"]
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for it in items:
        cat = CAT_LABEL.get(it.get("cat") or "misc", "待定")
        text = (it.get("text") or "").replace("\r", " ")
        if kind == "raindrop":
            w.writerow([it.get("url") or "", (it.get("text") or "")[:200], cat,
                        it.get("handle") or "", (it.get("created") or "")[:10],
                        ("@%s " % it.get("handle")) + text[:500]])
        else:
            w.writerow([(it.get("text") or "(无正文)")[:120], it.get("url") or "", cat,
                        "@" + (it.get("handle") or ""), (it.get("created") or "")[:10], text])
    return buf.getvalue().encode("utf-8-sig")


def load_items(s=None, limit_text=900):
    p = data_dir(s) / "items.json"
    if not p.exists():
        return []
    try:
        its = json.loads(p.read_text(encoding="utf-8")).get("items") or []
    except Exception as e:
        raise RuntimeError("data/items.json 读不出来（%s）：修好或重新刷新一次" % e)
    out = []
    for it in its:
        d = dict(it)
        t = d.get("text") or ""
        if len(t) > limit_text:
            d["text"] = t[:limit_text] + "…"
            d["trunc"] = True
        out.append(d)
    return out


# ---------------------------------------------------------------- 登录

def start_login():
    s = load_settings()
    with LOCK:
        if LOGIN["state"] == "waiting":
            return LOGIN
        LOGIN.update(state="waiting", handle="", msg="正在打开登录窗口…")
    threading.Thread(target=_login_worker, args=(s,), daemon=True).start()
    return LOGIN


def _login_worker(s):
    port = int(s.get("debug_port") or 9222)
    try:
        kill_our_chrome(s)
        launch_chrome(s, visible=True, url="https://x.com/i/flow/login")
        for _ in range(60):
            time.sleep(0.5)
            if cdp.alive(port):
                break
        else:
            raise RuntimeError("浏览器没起来")
        with LOCK:
            LOGIN["msg"] = "请在打开的窗口里登录 X（登录完不用关窗口）"
        for _ in range(600):
            time.sleep(3)
            try:
                tgt = cdp.pick_target(port)
                c = cdp.CDP(tgt["webSocketDebuggerUrl"], timeout=20)
                href = c.ev("location.href") or ""
                who = c.ev("""(() => { const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
                    return a ? a.getAttribute('href') : ''; })()""") or ""
                c.close()
                if who:
                    handle = who.strip("/").lstrip("@")
                    dd = data_dir(s)
                    mp = dd / "meta.json"
                    old = {}
                    if mp.exists():
                        try:
                            old = json.loads(mp.read_text(encoding="utf-8"))
                        except Exception:
                            old = {}
                    old["account"] = handle
                    mp.parent.mkdir(parents=True, exist_ok=True)
                    mp.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
                    with LOCK:
                        LOGIN.update(state="ok", handle=handle, msg=f"已登录 @{handle}")
                    time.sleep(1.5)
                    kill_our_chrome(s)          # 关掉登录窗口，刷新会在后台无头窗口里跑
                    threading.Thread(target=run_pipeline, kwargs={"reason": "登录后首次采集"}, daemon=True).start()
                    return
            except Exception:
                continue
        with LOCK:
            LOGIN.update(state="error", msg="等了 30 分钟还没检测到登录")
    except Exception as e:
        with LOCK:
            LOGIN.update(state="error", msg=str(e))


# ---------------------------------------------------------------- HTTP

class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True          # 必须在构造前生效，否则重启会撞 TIME_WAIT


class Handler(BaseHTTPRequestHandler):
    server_version = "XBookmarks/2.1.3"
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
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
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
        if is_local(self.client_address[0]):
            return True
        if not code:
            return False                    # 没设码就不许从局域网进
        tok = _code_token(s)
        got = self._cookies().get(COOKIE_KEY) or ""
        if got and (_const_eq(got, tok) or _const_eq(got, code)):
            return True
        hdr = self.headers.get("X-XBM-Key") or ""
        return bool(hdr) and (_const_eq(hdr, code) or _const_eq(hdr, tok))

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
                items = load_items(s)
            except Exception as e:
                return self.send_json({"error": str(e), "items": [], "meta": build_meta(s),
                                       "login": LOGIN, "settings": {}}, 500)
            pub = {k: v for k, v in s.items()
                   if k not in ("chrome_path", "llm_key", "cookie_salt")}
            pub["has_llm_key"] = bool(s.get("llm_key"))
            if not is_local(self.client_address[0]):
                pub["access_code"] = _mask_code(pub.get("access_code"))
                pub["code_masked"] = True
            return self.send_json({
                "items": items,
                "meta": build_meta(s),
                "settings": pub,
                "login": LOGIN,
                "refresh": {k: STATE[k] for k in ("running", "step", "err", "count", "at")},
                "semantic": sem_meta(s),
                "version": VERSION,
            })

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
                return self.send_json({k: STATE[k] for k in
                                       ("running", "step", "log", "err", "count", "at")})

        if p == "/api/login":
            with LOCK:
                return self.send_json(LOGIN)

        if p == "/api/export":
            kind = (q.get("kind") or ["csv"])[0]
            inc = (q.get("private") or ["0"])[0].lower() in ("1", "true", "yes", "on")
            if kind in ("md", "markdown", "obsidian", "zip"):
                try:
                    data = build_markdown_zip(inc)
                except RuntimeError as e:
                    return self.send_json({"error": str(e)}, 500)
                return self.send_bytes(data, "application/zip", "x-bookmarks-md.zip")
            if kind in ("raindrop", "notion"):
                try:
                    data = build_table_csv(kind, inc)
                except RuntimeError as e:
                    return self.send_json({"error": str(e)}, 500)
                return self.send_bytes(data, "text/csv; charset=utf-8",
                                       "x-bookmarks-%s.csv" % kind)
            f = export_dir(s) / ("bookmarks.xlsx" if kind == "xlsx" else "bookmarks.csv")
            if not f.exists():
                return self.send_json({"error": "还没有导出文件，先刷新一次"}, 404)
            ctype = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                     if kind == "xlsx" else "text/csv; charset=utf-8")
            return self.send_file(f, ctype)

        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        s = load_settings()

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
            body = self.body_json()
            if getattr(self, "_too_big", False):
                return self.send_json({"ok": False, "msg": "书签数据太大（上限 1MB），请分批同步"}, 413)
            items = (body or {}).get("items") if isinstance(body, dict) else None
            if not isinstance(items, list) or not items:
                return self.send_json({"ok": False, "msg": "没有收到书签数据"}, 400)
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
            cat = (body.get("cat") or "").strip()
            with LOCK:
                if STATE["running"]:
                    return self.send_json({"ok": False, "msg": "正在刷新中，稍后再改"}, 202)
            if cat and cat not in CATS:
                return self.send_json({"error": f"未知分类 {cat}"}, 400)
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
            if not isinstance(body, dict):
                return self.send_json({"error": "bad body"}, 400)
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
    sys.exit(main())
