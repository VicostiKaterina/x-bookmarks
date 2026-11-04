#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""X 书签整理器

用法:  python organize.py <导出的.json 或 目录>
产物:  out/bookmarks.csv, out/bookmarks.xlsx, out/index.html, out/summary.txt
"""
import json, re, csv, sys, time, html as _html
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
EXPORT_DIR = BASE / "exports"
OUT = EXPORT_DIR
RULES = json.loads((BASE / "rules.json").read_text(encoding="utf-8"))
CATS = RULES["categories"]
LABELS = {k: v["label"] for k, v in CATS.items()}
ORDER = ["ai", "tech", "life", "private", "misc"]


def _dig(o, *keys, default=None):
    for k in keys:
        if not isinstance(o, dict) or k not in o:
            return default
        o = o[k]
    return o if o is not None else default


def unwrap(o):
    for _ in range(6):
        if not isinstance(o, dict):
            return {}
        t = o.get("__typename")
        if "tweet_results" in o:
            o = o["tweet_results"].get("result") or {}
        elif t == "TweetWithVisibilityResults" and isinstance(o.get("tweet"), dict):
            o = o["tweet"]
        elif isinstance(o.get("result"), dict) and t is None:
            o = o["result"]
        else:
            return o
    return o


def parse_dt(v):
    if not v:
        return ""
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        try:
            return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(v)
    s = str(v).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def clean(s):
    if not isinstance(s, str):
        return ""
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()
def norm_media(rec, r):
    out = []
    flat = rec.get("media")
    if isinstance(flat, list) and flat:
        for m in flat:
            if isinstance(m, dict):
                out.append({"type": m.get("type") or "photo",
                            "url": m.get("original") or m.get("url") or "",
                            "thumb": m.get("thumbnail") or "",
                            "alt": clean(m.get("ext_alt_text") or "")})
        return [m for m in out if m["url"]]
    raws = (_dig(r, "legacy", "extended_entities", "media")
            or _dig(r, "legacy", "entities", "media")
            or _dig(r, "media", "media") or [])
    for m in raws:
        if not isinstance(m, dict):
            continue
        t = m.get("type") or "photo"
        u = m.get("media_url_https") or m.get("media_url") or ""
        base = m.get("media_url_https") or m.get("media_url") or ""
        if t in ("video", "animated_gif"):
            vs = _dig(m, "video_info", "variants") or []
            mp4 = [v for v in vs if isinstance(v, dict) and v.get("content_type") == "video/mp4"]
            if mp4:
                u = max(mp4, key=lambda v: v.get("bitrate") or 0).get("url") or u
        elif base:
            u = base + ("&" if "?" in base else "?") + "name=orig"
        if not u:
            continue
        out.append({"type": t, "url": u,
                    "thumb": (base + "?name=thumb") if (t == "photo" and base) else base,
                    "alt": clean(m.get("ext_alt_text") or "")})
    return out


def norm(rec):
    if not isinstance(rec, dict):
        return None
    r = unwrap(rec)
    if not isinstance(r, dict) or not r:
        return None
    legacy = r.get("legacy") or {}
    meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
    user = _dig(r, "core", "user_results", "result", default={}) or {}
    tid = str(r.get("rest_id") or r.get("id_str") or rec.get("id") or legacy.get("id_str") or "").strip()
    if not tid:
        return None
    handle = clean(_dig(user, "core", "screen_name") or rec.get("screen_name") or r.get("screen_name") or "")
    name = clean(_dig(user, "core", "name") or rec.get("name") or r.get("name") or "")
    text = (_dig(r, "note_tweet", "note_tweet_results", "result", "text")
            or legacy.get("full_text") or rec.get("full_text") or r.get("full_text") or "")
    sens = bool(rec.get("possibly_sensitive") or legacy.get("possibly_sensitive")
                or _dig(meta, "legacy", "possibly_sensitive") or meta.get("possibly_sensitive"))
    lang = legacy.get("lang") or _dig(meta, "legacy", "lang") or rec.get("lang") or ""
    url = rec.get("url") or (f"https://x.com/{handle}/status/{tid}" if handle else f"https://x.com/i/status/{tid}")

    def num(*vals):
        for v in vals:
            try:
                if v is not None and v != "":
                    return int(v)
            except (TypeError, ValueError):
                continue
        return 0

    views = num(_dig(r, "views", "count"), _dig(legacy, "ext_views", "count"),
                _dig(meta, "legacy", "ext_views", "count"), rec.get("views_count"))
    return {"id": tid, "url": url, "text": clean(text), "handle": handle, "name": name,
            "created": parse_dt(rec.get("created_at") or legacy.get("created_at")),
            "media": norm_media(rec, r), "sensitive": sens, "lang": lang, "raw": rec,
            "views": views,
            "likes": num(legacy.get("favorite_count"), rec.get("favorite_count")),
            "retweets": num(legacy.get("retweet_count"), rec.get("retweet_count")),
            "replies": num(legacy.get("reply_count"), rec.get("reply_count")),
            "quotes": num(legacy.get("quote_count"), rec.get("quote_count")),
            "bookmarks": num(legacy.get("bookmark_count"), rec.get("bookmark_count"))}


_ASCII_KW = re.compile(r"^[\x00-\x7f]+$")


def _kw_hit(blob, kw, cs=False):
    """英文/符号关键词按词边界匹配，避免 ai 命中 waiting、git 命中 digital；中文按子串。
    cs=True 时大小写敏感（用于 AI/GPT 这类会被法语 ai 之类误命中的短词）。"""
    k = str(kw).strip()
    if not k:
        return False
    if _ASCII_KW.match(k.lower()):
        pat = r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])"
        return re.search(pat, blob, 0 if cs else re.I) is not None
    return (k if cs else k.lower()) in blob


def classify(rec):
    raw = rec.get("raw") or {}
    legacy = raw.get("legacy") or {}
    extras = []
    for u in ((legacy.get("entities") or {}).get("urls") or []):
        if isinstance(u, dict):
            extras.append(u.get("expanded_url") or "")
            extras.append(u.get("display_url") or "")
    for h in ((legacy.get("entities") or {}).get("hashtags") or []):
        if isinstance(h, dict):
            extras.append(h.get("text") or "")
    blob_raw = " ".join([rec["text"], rec["handle"], rec["name"], " ".join(extras),
                         " ".join(m["url"] for m in rec["media"])])
    blob = blob_raw.lower()
    scores, hits = {}, {}
    for cat, cfg in CATS.items():
        s, h = 0, []
        for kw in cfg.get("cs_keywords", []):
            if _kw_hit(blob_raw, kw, cs=True):
                s += 1
                h.append(kw)
        for kw in cfg.get("keywords", []):
            if _kw_hit(blob, kw):
                s += 1
                h.append(kw)
        for pat in cfg.get("regex", []):
            if re.search(pat, blob, re.I):
                s += 2
                h.append("/" + pat + "/")
        scores[cat] = s
        hits[cat] = h
    if rec["sensitive"] and RULES["thresholds"].get("sensitive_always_private", True):
        scores["private"] += 5
        hits["private"].append("possibly_sensitive")
    best = max(scores, key=lambda c: (scores[c], -ORDER.index(c)))
    if scores.get("private", 0) >= 2:
        best = "private"
    if scores.get(best, 0) < RULES["thresholds"].get("min_score", 1):
        best = "misc"
    conf = 0.0 if best == "misc" else min(1.0, scores[best] / 4.0)
    why = [f'{LABELS[c]}:{",".join(dict.fromkeys(map(str, hits[c])))}'
           for c in ORDER if c in hits and hits[c]][:3]
    rec.update(cat=best, conf=conf, why=" ; ".join(why), scores=scores)
    return rec
CSV_COLS = ["分类", "置信度", "时间", "作者", "ID", "链接", "正文", "媒体数", "媒体URL", "语言", "敏感", "命中"]


def write_csv(rows, path):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in rows:
            w.writerow([LABELS[r["cat"]], f'{r["conf"]:.2f}', r["created"], "@" + r["handle"], r["id"],
                        r["url"], r["text"].replace("\n", " ⏎ "), len(r["media"]),
                        " | ".join(m["url"] for m in r["media"]), r["lang"],
                        "Y" if r["sensitive"] else "", r["why"]])


def write_xlsx(rows, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    head = ["分类", "时间", "作者", "正文", "链接", "媒体数", "置信度", "命中"]
    widths = [8, 16, 18, 90, 46, 7, 7, 28]

    def sheet(title, data):
        ws = wb.create_sheet(title[:31])
        ws.append(head)
        ws.freeze_panes = "A2"
        for c in range(1, len(head) + 1):
            ws.cell(row=1, column=c).font = Font(bold=True)
            ws.cell(row=1, column=c).alignment = Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
        for r in data:
            ws.append([LABELS[r["cat"]], r["created"], "@" + r["handle"], r["text"][:3000],
                       r["url"], len(r["media"]), round(r["conf"], 2), r["why"]])
        for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")

    sheet("总览(不含私密)", [r for r in rows if r["cat"] != "private"])
    for cat in ORDER:
        d = [r for r in rows if r["cat"] == cat]
        if d:
            sheet(LABELS[cat], d)
    top = Counter("@" + r["handle"] for r in rows if r["handle"])
    if top:
        ws = wb.create_sheet("作者TOP")
        ws.append(["作者", "收藏数"])
        ws.column_dimensions["A"].width = 24
        for h, n in top.most_common(100):
            ws.append([h, n])
    wb.save(path)
HTML_HEAD = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>X 书签整理</title><style>
:root{--bg:#0f1115;--fg:#e6e8ee;--mut:#8b93a7;--card:#171a21;--line:#242935;--acc:#4f8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;background:#0f1115ee;backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:10px 16px;z-index:9}
h1{font-size:15px;margin:0 0 8px;font-weight:600}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:13px}
button.on{background:var(--acc);border-color:var(--acc);color:#fff}
input[type=search]{flex:1;min-width:200px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:6px 10px}
main{padding:14px 16px 60px;max-width:980px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.meta{color:var(--mut);font-size:12px;margin-bottom:6px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.meta a{color:var(--acc);text-decoration:none}
.txt{white-space:pre-wrap;word-break:break-word}
.media{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.media img,.media video{max-height:150px;max-width:150px;border-radius:8px;background:#000}
.tag{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:0 8px;color:var(--mut)}
.blur .txt,.blur .media{filter:blur(9px)}
.mblur .media{filter:blur(11px) saturate(.5)}
.mblur:hover .media{filter:none}
.blur:hover .txt,.blur:hover .media{filter:none}
.empty{color:var(--mut);text-align:center;padding:40px}
#gate{display:none;padding:6px 0 0;color:#ffd166;font-size:12px}
</style></head>
<body>
<header><h1>X 书签 · <span id="cnt"></span><span id="gen" style="color:#8b93a7;font-size:11px;font-weight:400;margin-left:8px">__GEN__</span></h1>
<div class="bar" id="tabs"></div>
<div class="bar" style="margin-top:8px"><input id="q" type="search" placeholder="搜索正文 / 作者 / 链接…"><button id="gateBtn">显示私密</button><button id="rfBtn" title="用你登录的 X 账号重新抓一遍书签">刷新书签</button><span id="rfMsg" style="color:var(--mut);font-size:12px"></span></div>
<div id="gate">私密内容默认模糊，鼠标悬停可看单条。数据只存在这台电脑上，不上传任何服务器。</div>
</header><main id="list"></main>
<script>const DATA=__DATA__;</script>
<script>
"""
HTML_JS = """
const LAB={ai:'AI',tech:'技术',life:'生活',private:'私密',misc:'待定'};
const ORDER=['all','ai','tech','life','private','misc'];
let cat='all',q='',showPrivate=false;
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function current(){return DATA.filter(d=>(cat==='all'||d.c===cat)&&(!q||(d.t+' '+d.a+' '+d.u).toLowerCase().includes(q)));}
function render(){
  const tabs=document.getElementById('tabs'),list=document.getElementById('list');
  tabs.innerHTML=ORDER.map(c=>{const n=c==='all'?DATA.length:DATA.filter(d=>d.c===c).length;
    const dis=(c==='private'&&!showPrivate)?' style="opacity:.45"':'';
    return '<button data-c="'+c+'" class="'+(cat===c?'on':'')+'"'+dis+'>'+(c==='all'?'全部':LAB[c])+' '+n+'</button>';}).join('');
  tabs.querySelectorAll('button').forEach(b=>b.onclick=()=>{cat=b.dataset.c;render();});
  const items=current();
  document.getElementById('cnt').textContent=DATA.length+' 条 · 当前 '+items.length;
  if(!items.length){list.innerHTML='<p class="empty">没有匹配</p>';return;}
  list.innerHTML=items.map(d=>{
    const hide=d.c==='private'&&!showPrivate;
    const mblur=!hide&&d.c==='misc'&&(d.m||[]).length>0;
    const media=(d.m||[]).map(m=>m.k==='video'
      ?'<a href="'+esc(m.u)+'" target="_blank"><video src="'+esc(m.u)+'" preload="metadata"></video></a>'
      :'<a href="'+esc(m.u)+'" target="_blank"><img loading="lazy" src="'+esc(m.h||m.u)+'" alt="'+esc(m.a||'')+'"></a>').join('');
    return '<article class="card'+(hide?' blur':'')+(mblur?' mblur':'')+'"><div class="meta"><span class="tag">'+(LAB[d.c]||d.c)+'</span><span>@'+esc(d.a)+'</span><span>'+esc(d.d)+'</span><span>'+((d.m||[]).length?(d.m||[]).length+' 媒体':'')+'</span><a href="'+esc(d.u)+'" target="_blank" rel="noopener">原帖</a>'+(d.s?'<span class="tag">敏感</span>':'')+'</div><div class="txt">'+esc(d.t)+'</div>'+(media?'<div class="media">'+media+'</div>':'')+'</article>';}).join('');
}
document.getElementById('q').oninput=e=>{q=e.target.value.trim().toLowerCase();render();};
document.getElementById('gateBtn').onclick=()=>{showPrivate=!showPrivate;document.getElementById('gate').style.display=showPrivate?'block':'none';document.getElementById('gateBtn').textContent=showPrivate?'隐藏私密':'显示私密';if(!showPrivate&&cat==='private')cat='all';render();};
render();

/* —— 刷新按钮：只有在 http://127.0.0.1:8900 打开时才可用 —— */
(function(){
  const b=document.getElementById('rfBtn'), m=document.getElementById('rfMsg');
  if(!b) return;
  if(location.protocol==='file:'){
    b.disabled=true; b.style.opacity=.5;
    m.textContent='从桌面「X 书签」图标打开才能刷新';
    return;
  }
  let poll=null;
  b.onclick=async()=>{
    b.disabled=true; m.textContent='启动中…';
    try{
      const r=await fetch('/refresh',{method:'POST'});
      if(!r.ok) throw new Error((await r.text()).slice(0,80));
      poll=setInterval(async()=>{
        try{
          const s=await (await fetch('/status')).json();
          if(s.running){m.textContent=(s.step||'处理中')+'…';}
          else{
            clearInterval(poll);
            if(s.err){m.textContent='失败：'+s.err; b.disabled=false;}
            else{m.textContent='已更新'+(s.count?(' '+s.count+' 条'):'')+'，正在重新载入'; setTimeout(()=>location.reload(),500);}
          }
        }catch(e){}
      },2000);
    }catch(e){ m.textContent='刷新不可用：'+e.message; b.disabled=false; }
  };
})();
</script></body></html>
"""


def write_html(rows, path):
    data = []
    for r in rows:
        data.append({"c": r["cat"], "t": r["text"][:4000], "a": r["handle"], "d": r["created"],
                     "u": r["url"], "s": 1 if r["sensitive"] else 0,
                     "m": [{"k": m["type"], "u": m["url"], "h": m["thumb"], "a": m["alt"][:200]}
                           for m in r["media"]][:8]})
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    gen = "更新于 " + time.strftime("%Y-%m-%d %H:%M")
    path.write_text(HTML_HEAD.replace("__DATA__", blob).replace("__GEN__", gen) + HTML_JS, encoding="utf-8")
def load_records(path):
    p = Path(path)
    if p.is_dir():
        cand = sorted(p.glob("*.json")) + sorted((p / "in").glob("*.json"))
        files = [f for f in cand if f.name != "sample.json"]
    else:
        files = [p]
    recs, used = [], []
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"  跳过 {f.name}: {e}")
            continue
        if isinstance(obj, dict):
            picked = False
            for k in ("data", "bookmarks", "tweets", "results", "items"):
                if isinstance(obj.get(k), list):
                    obj, picked = obj[k], True
                    break
            if not picked:
                obj = [obj]
        if isinstance(obj, list):
            recs += [x for x in obj if isinstance(x, dict)]
            used.append(f.name)
    return recs, used


def author_smooth(rows, ratio=0.6, min_items=3):
    """作者画像平滑：
    1) 同一作者只要有一条被 X 标记敏感，该作者其余「待定」条目也归私密；
    2) 同一作者多数条目已判私密时，其余「待定」条目也归私密。
    """
    by = {}
    for r in rows:
        by.setdefault((r.get("handle") or "").lower(), []).append(r)
    moved = 0
    for h, items in by.items():
        if not h:
            continue
        n_sens = sum(1 for r in items if r.get("sensitive"))
        priv = sum(1 for r in items if r["cat"] == "private")
        sens_author = n_sens > 0
        priv_author = len(items) >= min_items and priv / len(items) >= ratio
        if not (sens_author or priv_author):
            continue
        for r in items:
            if r["cat"] == "misc":
                r["cat"] = "private"
                r["conf"] = 0.5
                tag = f"作者敏感:{n_sens}" if sens_author else f"作者画像:{priv}/{len(items)}"
                r["why"] = (r["why"] + " ; " + tag) if r.get("why") else tag
                moved += 1
    return moved


def media_only_smooth(rows, min_sensitive_rate=0.3):
    """纯媒体帖（正文去掉链接/@提及/#话题后为空，且带媒体）：仅在"敏感帖占比高"的账号里按私密处理，
    避免把普通账号的纯图帖（表情包、截图）误判成私密。"""
    if not rows:
        return 0
    rate = sum(1 for r in rows if r.get("sensitive")) / len(rows)
    if rate < min_sensitive_rate:
        return 0
    moved = 0
    for r in rows:
        if r["cat"] != "misc" or not r["media"]:
            continue
        t = re.sub(r"https?://\S+|@\w+|#\S+", " ", r["text"] or "")
        t = re.sub(r"[\s·|,.，。!！?？~～\-–—:：;；'\"“”‘’()（）\[\]]+", "", t)
        if len(t) <= 2:
            r["cat"] = "private"
            r["conf"] = 0.4
            tag = "纯媒体帖"
            r["why"] = (r["why"] + " ; " + tag) if r.get("why") else tag
            moved += 1
    return moved


def write_items(rows, path):
    """给前端 App 用的数据（items.json）：分类 + 互动数据 + 媒体，不含 raw。"""
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "cat": r["cat"], "conf": round(r["conf"], 2), "why": r["why"],
            "created": r["created"], "handle": r["handle"], "name": r["name"],
            "text": r["text"], "url": r["url"], "sensitive": bool(r["sensitive"]), "lang": r["lang"],
            "media": [{"type": m["type"], "thumb": m["thumb"], "url": m["url"], "alt": m["alt"]}
                      for m in r["media"]],
            "views": r.get("views", 0), "likes": r.get("likes", 0), "retweets": r.get("retweets", 0),
            "replies": r.get("replies", 0), "quotes": r.get("quotes", 0),
            "bookmarks": r.get("bookmarks", 0), "manual": bool(r.get("manual")),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({"generated": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "items": items},
                  f, ensure_ascii=False)


def load_runtime_settings():
    p = BASE / "settings.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def apply_semantic(rows):
    """语义模型的建议：只影响「待定」，且要求概率够高、领先够多。"""
    s = load_runtime_settings()
    if not s.get("semantic_enabled", True):
        return 0
    thr = float(s.get("semantic_threshold") or 0.6)
    margin_min = 0.15
    p = BASE / "data" / "semantic.json"
    if not p.exists():
        return 0
    try:
        sem = (json.loads(p.read_text(encoding="utf-8")).get("items") or {})
    except Exception:
        return 0
    n = 0
    for r in rows:
        if r.get("cat") != "misc":
            continue
        hit = sem.get(r.get("id"))
        if not hit:
            continue
        cat = hit.get("cat")
        if not cat or cat == "misc" or cat not in CATS:
            continue
        if float(hit.get("p") or 0) < thr or float(hit.get("margin") or 0) < margin_min:
            continue
        r["cat"] = cat
        r["conf"] = round(float(hit.get("p") or 0), 3)
        r["why"] = "语义模型 p=%.2f" % float(hit.get("p") or 0)
        n += 1
    return n


def apply_overrides(rows, path):
    """手动改过的分类（data/overrides.json：{id: "ai"|"tech"|"life"|"private"|"misc"}）优先。"""
    if not path.exists():
        return 0
    try:
        ov = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return 0
    if not isinstance(ov, dict):
        return 0
    n = 0
    for r in rows:
        c = ov.get(r["id"])
        if c in ORDER and c != r["cat"]:
            r["cat"] = c
            r["why"] = "手动指定"
            r["conf"] = 1.0
            r["manual"] = True
            n += 1
        elif c in ORDER:
            r["manual"] = True
            r["why"] = "手动指定"
    return n


def main(argv=None):
    global OUT
    import argparse
    ap = argparse.ArgumentParser(description="把 X 书签 JSON 分类成 CSV / Excel / items.json")
    ap.add_argument("input", nargs="?", default="")
    ap.add_argument("--out", default="", help="导出目录（默认 exports/）")
    ap.add_argument("--items", default="", help="items.json 输出路径（默认 data/items.json）")
    ap.add_argument("--html", action="store_true", help="另外写一份离线 index.html")
    a = ap.parse_args(argv)

    src = a.input.strip() or str(DATA_DIR / "raw_bookmarks.json")
    if not Path(src).exists() and (DATA_DIR / "raw_likes.json").exists():
        src = str(DATA_DIR / "raw_likes.json")
    OUT = Path(a.out) if a.out else EXPORT_DIR
    ITEMS = Path(a.items) if a.items else (DATA_DIR / "items.json")
    OUT.mkdir(parents=True, exist_ok=True)
    recs, used = load_records(src)
    print(f"读入 {len(recs)} 条原始记录，来源: {', '.join(used) or '（无）'}")
    rows, seen, dup, bad = [], set(), 0, 0
    for x in recs:
        n = norm(x)
        if not n:
            bad += 1
            continue
        if n["id"] in seen:
            dup += 1
            continue
        seen.add(n["id"])
        rows.append(classify(n))
    moved = author_smooth(rows)
    moved += media_only_smooth(rows)
    print(f"作者画像平滑: {moved} 条转私密")
    ns = apply_semantic(rows)
    if ns:
        print(f"语义模型改判: {ns} 条")
    mo = apply_overrides(rows, DATA_DIR / "overrides.json")
    if mo:
        print(f"手动指定分类: {mo} 条")
    rows.sort(key=lambda r: r["created"], reverse=True)
    rows.sort(key=lambda r: ORDER.index(r["cat"]))
    counts = Counter(r["cat"] for r in rows)

    write_csv(rows, OUT / "bookmarks.csv")
    write_xlsx(rows, OUT / "bookmarks.xlsx")
    write_items(rows, ITEMS)
    if a.html:
        write_html(rows, OUT / "index.html")

    top = Counter("@" + r["handle"] for r in rows if r["handle"]).most_common(15)
    lines = [f"生成时间: {datetime.now():%Y-%m-%d %H:%M}", f"原始 {len(recs)} / 去重后 {len(rows)} / 重复 {dup} / 解析失败 {bad}", ""]
    for c in ORDER:
        if counts.get(c):
            lines.append(f"{LABELS[c]:<4} {counts[c]} 条")
    lines += ["", "收藏最多的作者:"] + [f"  {h}  {n}" for h, n in top]
    (OUT / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n产物: {OUT / 'bookmarks.csv'}")
    print(f"      {OUT / 'bookmarks.xlsx'}")
    print(f"      {ITEMS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())





