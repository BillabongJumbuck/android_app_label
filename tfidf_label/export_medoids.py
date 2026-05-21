#!/usr/bin/env python3
"""Export per-label medoids: one REAL record per label (closest to centroid). Fast + accurate."""

import csv, os, re, json, struct
import numpy as np
from collections import Counter, defaultdict
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
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

    # Group by label
    print("Selecting medoids...")
    by_label = defaultdict(list)
    for i, (_,_,lbl) in enumerate(recs):
        by_label[lbl].append(i)

    medoids = {}  # label → sparse vector dict
    for lbl, indices in by_label.items():
        if len(indices) == 1:
            # 只有一条记录，直接用
            r = mat[indices[0]]
            _, cols = r.nonzero()
            medoids[lbl] = {int(j): float(r[0,j]) for j in cols}
        else:
            # 取质心，选与该标签内所有记录平均余弦相似度最高的那条
            group_mat = mat[indices]
            centroid = group_mat.mean(axis=0)
            centroid = np.asarray(centroid).flatten()
            sims = cosine_similarity(group_mat, centroid.reshape(1,-1)).flatten()
            best_local = indices[int(np.argmax(sims))]
            r = mat[best_local]
            _, cols = r.nonzero()
            medoids[lbl] = {int(j): float(r[0,j]) for j in cols}

    print(f"  {len(medoids)} medoids selected")

    # Export
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(os.path.join(EXPORT_DIR,"vocab.json"),"w",encoding="utf-8") as f:
        json.dump({k:int(v) for k,v in vec.vocabulary_.items()}, f, ensure_ascii=False)
    with open(os.path.join(EXPORT_DIR,"idf.json"),"w") as f:
        json.dump([float(x) for x in vec.idf_], f)
    with open(os.path.join(EXPORT_DIR,"labels.json"),"w",encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False)

    with open(os.path.join(EXPORT_DIR,"medoids.bin"),"wb") as f:
        f.write(struct.pack("<H", len(medoids)))
        for lbl, c in medoids.items():
            idx = labels.index(lbl)
            items = sorted(c.items())
            f.write(struct.pack("<HH", idx, len(items)))
            for j,w in items:
                f.write(struct.pack("<Hf", j, float(w)))

    total = 0
    for fn in ["vocab.json","idf.json","labels.json","medoids.bin"]:
        sz = os.path.getsize(os.path.join(EXPORT_DIR, fn))
        total += sz
        print(f"  {fn:16s} {sz:>10,} bytes")
    print(f"  {'Total':16s} {total:>10,} bytes ({total/1024:.1f} KB)")

if __name__=="__main__":
    main()
