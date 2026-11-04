"""极简 Chrome DevTools Protocol 客户端（只用 websocket-client，无第三方浏览器库）。

用途：复用本机一个已登录 x.com 的 Chrome（带 --remote-debugging-port），
在页面里发 fetch / 取数据，不需要 X API key。
"""
import json
import time
import urllib.request

import websocket


def http_json(port, path, timeout=15, method=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method or "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def alive(port, timeout=2):
    try:
        return bool(http_json(port, "/json/version", timeout))
    except Exception:
        return False


class CDP:
    def __init__(self, ws_url, timeout=900):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True, max_size=None)
        self.i = 0
        self.events = []

    def send(self, method, params=None, timeout=900):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(method)

    def ev(self, expr, timeout=900):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400])
        return r.get("result", {}).get("value")

    def drain(self, seconds, predicate):
        end = time.time() + seconds
        old_to = self.ws.gettimeout()
        self.ws.settimeout(2)
        try:
            while time.time() < end:
                try:
                    msg = json.loads(self.ws.recv())
                except Exception:
                    continue
                if "method" in msg:
                    self.events.append(msg)
                    if len(self.events) > 500:
                        del self.events[:-200]
                    if predicate(msg):
                        return msg
            return None
        finally:
            self.ws.settimeout(old_to)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def pick_target(port, prefer="x.com"):
    pages = [t for t in http_json(port, "/json/list") if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("没有可用的标签页")
    hit = [t for t in pages if prefer in (t.get("url") or "")]
    return (hit or pages)[0]


def page_url(port):
    try:
        return pick_target(port).get("url") or ""
    except Exception:
        return ""
