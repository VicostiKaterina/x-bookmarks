"""语义分类（本地小模型）。

思路：用本地 embedding 模型（BAAI/bge-small-zh-v1.5，512 维，随 fastembed 下载一次即可）
把每条书签的正文变成向量，然后用「已有标签的条目」当种子做 kNN：
  - 手动改过分类的条目权重最高（最可信）
  - 规则系统给出、且置信度够的条目当弱标签
对每条书签取同类最近 k 个邻居的平均相似度，softmax 成概率。

输出的 data/semantic.json 只提供「建议」，是否采纳由 organize.py 按阈值决定；
默认只会影响原本落在「待定」的条目，不会动私密判定。
"""
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MODELS = ROOT / "models"
CATS = ["ai", "tech", "life", "private", "misc"]
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
TOPK = 6
TEMP = 0.035
SEED_MIN_CONF = 0.45


def clean(t):
    t = re.sub(r"https?://\S+", " ", t or "")
    t = re.sub(r"[@#]\w+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def load_items():
    p = DATA / "items.json"
    if not p.exists():
        raise SystemExit("还没有 data/items.json，先跑一次 organize.py")
    return json.loads(p.read_text(encoding="utf-8")).get("items") or []


def embed(texts):
    from fastembed import TextEmbedding
    MODELS.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODELS))
    out = []
    for v in model.embed(texts, batch_size=32):
        out.append(np.asarray(v, dtype=np.float32))
    m = np.vstack(out)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    return m


def cache_path():
    return DATA / "emb_cache.npz"


def embeddings_for(items):
    """按文本哈希缓存，只有新增/改过的条目才重新算。"""
    import hashlib
    texts = [clean(it.get("text")) for it in items]
    keys = [hashlib.md5(t.encode("utf-8")).hexdigest() for t in texts]
    cache = {}
    if cache_path().exists():
        try:
            z = np.load(cache_path(), allow_pickle=True)
            old_keys = list(z["keys"])
            old_vecs = z["vecs"]
            cache = {k: old_vecs[i] for i, k in enumerate(old_keys)}
        except Exception:
            cache = {}
    todo_idx = [i for i, k in enumerate(keys) if k not in cache or not texts[i]]
    if todo_idx:
        fresh = embed([texts[i] or "（无正文）" for i in todo_idx])
        for j, i in enumerate(todo_idx):
            cache[keys[i]] = fresh[j]
    vecs = np.vstack([cache[k] for k in keys])
    try:
        np.savez_compressed(cache_path(), keys=np.array(keys), vecs=vecs)
    except Exception as e:
        print("写 embedding 缓存失败:", e)
    return vecs


def build_seeds(items, cap=80):
    """每类种子数封顶，避免「私密」这种大类的密度把别的类吃掉。"""
    per = {}
    for i, it in enumerate(items):
        cat = it.get("cat") or "misc"
        if cat == "misc":
            continue
        if it.get("manual"):
            per.setdefault(cat, []).append((3.0, i, cat))
        elif float(it.get("conf") or 0) >= SEED_MIN_CONF:
            per.setdefault(cat, []).append((1.0, i, cat))
    seeds = []
    for cat, lst in per.items():
        lst.sort(key=lambda x: (-x[0], -float(items[x[1]].get("conf") or 0)))
        for w, i, c in lst[:cap]:
            seeds.append((i, c, w))
    return seeds


def classify(vecs, seeds, rows=None):
    idx = np.array([s[0] for s in seeds])
    cats = [s[1] for s in seeds]
    w = np.array([s[2] for s in seeds], dtype=np.float32)
    rows = np.arange(vecs.shape[0]) if rows is None else np.asarray(rows)
    S = vecs[rows] @ vecs[idx].T                 # (len(rows), m)
    S = (S + 1.0) / 2.0                          # 余弦 → [0,1]
    out = []
    for i in range(S.shape[0]):
        row = S[i]
        scores = {}
        for c in CATS:
            mask = np.array([x == c for x in cats])
            if not mask.any():
                scores[c] = 0.0
                continue
            sims = row[mask]
            k = min(TOPK, len(sims))
            top = np.sort(sims)[-k:]
            weights = w[mask][np.argsort(sims)[-k:]]
            scores[c] = float((top * weights).sum() / (weights.sum() + 1e-9))
        best = max(scores, key=scores.get)
        # softmax
        exps = {c: math.exp((scores[c] - max(scores.values())) / TEMP) for c in CATS}
        z = sum(exps.values())
        probs = {c: exps[c] / z for c in CATS}
        second = sorted(probs.values())[-2] if len(probs) > 1 else 0.0
        out.append({"cat": best, "p": round(probs[best], 3),
                    "prob": {c: round(probs[c], 3) for c in CATS},
                    "margin": round(probs[best] - second, 3)})
    return out


def main(argv=None):
    t0 = time.time()
    items = load_items()
    print("条目:", len(items))
    seeds = build_seeds(items)
    by = {}
    for _, c, _wt in seeds:
        by[c] = by.get(c, 0) + 1
    print("种子:", len(seeds), by)
    if len(seeds) < 20:
        print("种子太少，先跑一次规则分类或手动改几条分类再来")
        return 1

    vecs = embeddings_for(items)
    print("向量:", vecs.shape, "耗时 %.1fs" % (time.time() - t0))
    res = classify(vecs, seeds)

    # 自检：留一法看种子能不能被自己找回来
    hit = 0
    rng = np.random.default_rng(7)
    sample = list(rng.choice(len(seeds), size=min(120, len(seeds)), replace=False))
    for si in sample:
        i, cat, _w = seeds[si]
        sub = [s for j, s in enumerate(seeds) if j != si]
        r = classify(vecs, sub, rows=[i])[0]
        hit += int(r["cat"] == cat)
    print("种子自检准确率: %.1f%%（%d 条留一样本）" % (100.0 * hit / len(sample), len(sample)))

    payload = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "model": MODEL_NAME,
               "topk": TOPK, "temp": TEMP, "count": len(items),
               "items": {it["id"]: r for it, r in zip(items, res)}}
    (DATA / "semantic.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    misc = [(it, r) for it, r in zip(items, res) if (it.get("cat") or "misc") == "misc"]
    moved = {}
    for it, r in misc:
        if r["cat"] != "misc" and r["p"] >= 0.6:
            moved[r["cat"]] = moved.get(r["cat"], 0) + 1
    print("待定 %d 条里，模型建议改判: %s" % (len(misc), moved or "无"))
    shown = 0
    for it, r in misc:
        if r["cat"] != "misc" and r["p"] >= 0.6 and shown < 6:
            print("  · [%s %.2f] %s" % (r["cat"], r["p"], clean(it.get("text"))[:60]))
            shown += 1
    print("写出 data/semantic.json，总耗时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
