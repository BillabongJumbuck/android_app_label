#!/usr/bin/env python3
"""Export per-label centroids (much smaller than full training set, much faster inference)"""

import csv, os, re, json, struct
import numpy as np
from collections import Counter, defaultdict
from sklearn.feature_extraction.text import TfidfVectorizer
import jieba

CSV_DIR = os.path.join(os.path.dirname(__file__), "..")
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "go_export")
CSV_FILES = ["mi_apps_full.csv", "android_apps_with_perms.csv", "taptap_apps.csv", "yyb_apps.csv"]
BRAND_NOISE = re.compile(r"[-–—\s]*(官网|官方版|客户端|手机版|Android|安卓|HD|Lite|Pro|Plus|极速版|国际版|企业版|个人版|免费版|正式版|测试版|内测版|公测版|纯净版|极简版|OEM|定制版|联运版|渠道版|TV版|Pad版)$")

for w in ["文件管理","浏览器","输入法","主题商店","游戏中心","应用商店","安全中心","手机管家","天气","时钟","日历","计算器","钱包","浏览器视频","手机银行","文件管理器"]:
    jieba.add_word(w)

def clean_name(n): return BRAND_NOISE.sub("", n.strip())
def perm_keywords(perms):
    if not perms or perms in ("<NONE>","<BLOCKED>"): return []
    ks = []
    for p in perms.replace(" | ","|").split("|"):
        p = re.sub(r"android\.permission\.","",p.strip(),flags=re.IGNORECASE)
        p = re.sub(r"([A-Z])",r" \1",p).replace("_"," ")
        p = re.sub(r"\s+"," ",p).strip().lower()
        if len(p)>2: ks.append(p)
    return ks

def tokenize(text):
    words = list(jieba.cut(text))
    tokens = [w.strip() for w in words if len(w.strip())>=2]
    clean = text.replace(" ","")
    for i in range(len(clean)-1): tokens.append(clean[i:i+2])
    return tokens

def build_features(name, perms):
    parts = [name, name]
    pk = perm_keywords(perms)
    if pk: parts.append(" ".join(pk))
    return tokenize(" ".join(parts))

def load_data():
    recs = []
    for fn in CSV_FILES:
        p = os.path.join(CSV_DIR, fn)
        if not os.path.exists(p): continue
        with open(p,"r",encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                n = clean_name(r.get("app_name",""))
                perms = r.get("permissions","")
                lbl = r.get("label","")
                if not n or not lbl or lbl in ("<NONE>","<BLOCKED>"): continue
                if perms in ("<NONE>","<BLOCKED>"): perms = ""
                recs.append((n, perms, lbl))
    return recs

def main():
    print("Loading...")
    recs = load_data()
    lc = Counter(r[2] for r in recs)
    recs = [r for r in recs if lc[r[2]]>=5]
    labels = sorted(set(r[2] for r in recs))
    print(f"  {len(recs)} records, {len(labels)} labels")

    print("Tokenizing & training...")
    docs = [" ".join(build_features(r[0],r[1])) for r in recs]
    vec = TfidfVectorizer(analyzer="word", token_pattern=r"\S+", max_features=10000, sublinear_tf=True)
    mat = vec.fit_transform(docs)
    print(f"  vocab={len(vec.vocabulary_)}, shape={mat.shape}")

    # Build centroids: mean vector per label
    print("Building centroids...")
    by_label = defaultdict(list)
    for i, (_,_,lbl) in enumerate(recs):
        r = mat[i]
        _, cols = r.nonzero()
        if len(cols)==0: continue
        weights = {int(j): float(r[0,j]) for j in cols}
        by_label[lbl].append(weights)

    centroids = {}
    for lbl, vecs in by_label.items():
        n = len(vecs)
        merged = defaultdict(float)
        for v in vecs:
            for j,w in v.items(): merged[j] += w
        # L2 normalize centroid
        norm = np.sqrt(sum(w*w for w in merged.values()))
        if norm<1e-10: continue
        centroids[lbl] = {j: w/norm for j,w in merged.items()}

    print(f"  {len(centroids)} centroids")

    # Export (same format as before: labels.json + vocab.json + idf.json + centroids.bin)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(os.path.join(EXPORT_DIR,"vocab.json"),"w",encoding="utf-8") as f:
        json.dump({k:int(v) for k,v in vec.vocabulary_.items()}, f, ensure_ascii=False)
    with open(os.path.join(EXPORT_DIR,"idf.json"),"w") as f:
        json.dump([float(x) for x in vec.idf_], f)
    with open(os.path.join(EXPORT_DIR,"labels.json"),"w",encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False)

    # centroids.bin: [uint16 num_centroids] then for each: [uint16 label_idx][uint16 nnz][uint16 feat][float32 weight]...
    with open(os.path.join(EXPORT_DIR,"centroids.bin"),"wb") as f:
        f.write(struct.pack("<H", len(centroids)))
        for lbl, c in centroids.items():
            idx = labels.index(lbl)
            items = sorted(c.items())
            f.write(struct.pack("<HH", idx, len(items)))
            for j,w in items:
                f.write(struct.pack("<Hf", j, float(w)))

    total = 0
    for fn in ["vocab.json","idf.json","labels.json","centroids.bin"]:
        sz = os.path.getsize(os.path.join(EXPORT_DIR, fn))
        total += sz
        print(f"  {fn:16s} {sz:>10,} bytes")
    print(f"  {'Total':16s} {total:>10,} bytes ({total/1024:.1f} KB)")

if __name__=="__main__":
    main()
