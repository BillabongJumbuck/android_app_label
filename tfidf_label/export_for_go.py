#!/usr/bin/env python3
"""
导出 TF-IDF 模型为 Go 可读格式（二进制矩阵 + JSON 元数据）。

输出:
  go_export/vocab.json    — {token: index}
  go_export/idf.json      — [float64, ...]
  go_export/labels.json   — [label_str, ...]
  go_export/records.bin   — 稀疏矩阵二进制格式 (~6MB)
"""

import csv, os, re, json, struct
import numpy as np
from collections import Counter
from sklearn.feature_extraction.text import TfidfVectorizer
import jieba

CSV_DIR = os.path.join(os.path.dirname(__file__), "..")
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "go_export")

CSV_FILES = ["mi_apps_full.csv", "android_apps_with_perms.csv",
             "taptap_apps.csv", "yyb_apps.csv"]

BRAND_NOISE = re.compile(
    r"[-–—\s]*(官网|官方版|客户端|手机版|Android|安卓|HD|Lite|Pro|Plus|"
    r"极速版|国际版|企业版|个人版|免费版|正式版|测试版|内测版|公测版|纯净版|极简版|"
    r"OEM|定制版|联运版|渠道版|TV版|Pad版)$")

for w in ["文件管理", "浏览器", "输入法", "主题商店", "游戏中心", "应用商店",
          "安全中心", "手机管家", "天气", "时钟", "日历", "计算器", "钱包",
          "浏览器视频", "手机银行", "文件管理器"]:
    jieba.add_word(w)


def clean_name(name):
    return BRAND_NOISE.sub("", name.strip())


def perm_keywords(perms):
    if not perms or perms in ("<NONE>", "<BLOCKED>"):
        return []
    kws = []
    for p in perms.replace(" | ", "|").split("|"):
        p = p.strip()
        p = re.sub(r"android\.permission\.", "", p, flags=re.IGNORECASE)
        p = re.sub(r"([A-Z])", r" \1", p).replace("_", " ")
        p = re.sub(r"\s+", " ", p).strip().lower()
        if len(p) > 2:
            kws.append(p)
    return kws


def tokenize(text):
    words = list(jieba.cut(text))
    tokens = [w.strip() for w in words if len(w.strip()) >= 2]
    clean = text.replace(" ", "")
    for i in range(len(clean) - 1):
        tokens.append(clean[i:i+2])
    return tokens


def build_features(name, perms):
    parts = [name, name]
    pk = perm_keywords(perms)
    if pk:
        parts.append(" ".join(pk))
    return tokenize(" ".join(parts))


def load_all_data():
    records = []
    for fname in CSV_FILES:
        path = os.path.join(CSV_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = clean_name(row.get("app_name", ""))
                perms = row.get("permissions", "")
                label = row.get("label", "")
                if not name or not label:
                    continue
                if label in ("<NONE>", "<BLOCKED>"):
                    continue
                if perms in ("<NONE>", "<BLOCKED>"):
                    perms = ""
                records.append((name, perms, label))
    return records


def main():
    print("Loading data...")
    records = load_all_data()
    lbl_count = Counter(r[2] for r in records)
    print(f"  {len(records)} records, {len(lbl_count)} unique labels")

    rare = {l for l, c in lbl_count.items() if c < 5}
    records = [r for r in records if r[2] not in rare]
    print(f"  {len(records)} after filtering rare (<5)")

    all_labels = sorted(set(r[2] for r in records))
    label_to_idx = {l: i for i, l in enumerate(all_labels)}
    print(f"  {len(all_labels)} labels")

    print("Tokenizing...")
    docs = []
    for name, perms, _ in records:
        docs.append(" ".join(build_features(name, perms)))

    print("Training TF-IDF...")
    vectorizer = TfidfVectorizer(
        analyzer="word", token_pattern=r"\S+",
        max_features=10000, sublinear_tf=True,
    )
    matrix = vectorizer.fit_transform(docs)
    print(f"  Vocab={len(vectorizer.vocabulary_)}, shape={matrix.shape}")

    os.makedirs(EXPORT_DIR, exist_ok=True)

    # 1-3: JSON (small files)
    print("Exporting JSON metadata...")
    vocab = {k: int(v) for k, v in vectorizer.vocabulary_.items()}
    with open(os.path.join(EXPORT_DIR, "vocab.json"), "w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False)
    with open(os.path.join(EXPORT_DIR, "idf.json"), "w") as f:
        json.dump([float(x) for x in vectorizer.idf_], f)
    with open(os.path.join(EXPORT_DIR, "labels.json"), "w", encoding="utf-8") as f:
        json.dump(all_labels, f, ensure_ascii=False)

    # 4: Binary sparse matrix
    # Format: [int32 num_records] then for each record:
    #   [uint16 label_idx][uint16 nnz][uint16 feat_idx][float32 weight] × nnz
    print("Exporting records.bin...")
    n_records = matrix.shape[0]
    nnz_total = matrix.nnz

    with open(os.path.join(EXPORT_DIR, "records.bin"), "wb") as f:
        f.write(struct.pack("<i", n_records))
        for i in range(n_records):
            row = matrix[i]
            _, cols = row.nonzero()
            nnz = len(cols)
            label_idx = label_to_idx[records[i][2]]
            f.write(struct.pack("<HH", label_idx, nnz))
            for j in cols:
                f.write(struct.pack("<Hf", j, float(row[0, j])))

    # Stats
    total = 0
    for fn in ["vocab.json", "idf.json", "labels.json", "records.bin"]:
        sz = os.path.getsize(os.path.join(EXPORT_DIR, fn))
        total += sz
        print(f"  {fn:15s} {sz:>10,} bytes")
    print(f"  {'Total':15s} {total:>10,} bytes ({total/1024/1024:.1f} MB)")
    print(f"\nDone!")


if __name__ == "__main__":
    main()
