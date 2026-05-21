#!/usr/bin/env python3
"""
TF-IDF 标签预测器
用法:
  python predict_label.py train              # 训练模型，保存到 tfidf_model.pkl
  python predict_label.py predict <name>     # 为单个应用预测标签
  python predict_label.py phone              # 预测手机上 14 个未匹配应用的标签
"""

import csv, os, re, sys, pickle, jieba
import numpy as np
from collections import Counter
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from oem_rules import match as oem_match

CSV_DIR = os.path.join(os.path.dirname(__file__), "..")
MODEL_FILE = os.path.join(os.path.dirname(__file__), "tfidf_model.pkl")

CSV_FILES = [
    "mi_apps_full.csv",
    "android_apps_with_perms.csv",
    "taptap_apps.csv",
    "yyb_apps.csv",
]

BRAND_NOISE = re.compile(
    r"[-–—\s]*(官网|官方版|客户端|手机版|Android|安卓|HD|Lite|Pro|Plus|"
    r"极速版|国际版|企业版|个人版|免费版|正式版|测试版|内测版|公测版|纯净版|极简版|"
    r"OEM|定制版|联运版|渠道版|TV版|Pad版)$"
)

# App 领域常用词
for w in ["文件管理", "浏览器", "输入法", "主题商店", "游戏中心", "应用商店",
          "安全中心", "手机管家", "天气", "时钟", "日历", "计算器", "钱包",
          "浏览器视频", "手机银行", "文件管理器", "一加社区", "一加会员"]:
    jieba.add_word(w)


def clean_name(name):
    return BRAND_NOISE.sub("", name.strip())


def jieba_tokens(text):
    words = list(jieba.cut(text))
    tokens = [w.strip() for w in words if len(w.strip()) >= 2]
    # char bigram 兜底
    clean = text.replace(" ", "")
    for i in range(len(clean) - 1):
        tokens.append(clean[i:i+2])
    return tokens


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


def build_features(name, perms):
    parts = [name, name]  # 名字加权 2×
    pk = perm_keywords(perms)
    if pk:
        parts.append(" ".join(pk))
    return " ".join(parts)


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


class LabelPredictor:
    def __init__(self):
        self.records = []
        self.vectorizer = None
        self.matrix = None

    def fit(self, verbose=True):
        if verbose:
            print(f"Loading {len(CSV_FILES)} CSVs...")
        self.records = load_all_data()
        labels = Counter(r[2] for r in self.records)
        if verbose:
            print(f"  {len(self.records)} labeled records, {len(labels)} unique labels")

        rare = {l for l, c in labels.items() if c < 5}
        self.records = [r for r in self.records if r[2] not in rare]
        if verbose:
            print(f"  {len(self.records)} after filtering rare (<5) labels")

        corpus = [build_features(r[0], r[1]) for r in self.records]
        self.vectorizer = TfidfVectorizer(
            tokenizer=jieba_tokens, max_features=10000, sublinear_tf=True)
        self.matrix = self.vectorizer.fit_transform(corpus)
        if verbose:
            print(f"  Vocab={len(self.vectorizer.vocabulary_)}, shape={self.matrix.shape}")

    def save(self, path=MODEL_FILE):
        # 只存 records，不存 vectorizer（含 tokenizer 函数，pickle 跨模块有问题）
        with open(path, "wb") as f:
            pickle.dump(self.records, f)

    def load(self, path=MODEL_FILE):
        with open(path, "rb") as f:
            self.records = pickle.load(f)
        self.fit(verbose=False)

    def predict(self, app_name, permissions="", top_k=8):
        name = clean_name(app_name)
        text = build_features(name, permissions)
        vec = self.vectorizer.transform([text])
        sims = cosine_similarity(vec, self.matrix)[0]
        top_idx = np.argsort(sims)[::-1][:top_k]
        results = []
        for i in top_idx:
            score = sims[i]
            if score < 0.03:
                continue
            ni, _, li = self.records[i]
            results.append((li, score, ni))
        return results

    def predict_best(self, app_name, permissions="", package_name=""):
        # 1. OEM rules first
        oem_label, oem_conf = oem_match(app_name, package_name)
        if oem_conf >= 0.8:
            return oem_label, [("OEM", oem_conf, app_name)]
        # 2. TF-IDF
        results = self.predict(app_name, permissions, top_k=5)
        if not results:
            if oem_conf >= 0.3:
                return oem_label, [("OEM-fallback", oem_conf, app_name)]
            return "unknown", []
        votes = Counter()
        for label, score, _ in results:
            votes[label] += score
        best_label, best_score = votes.most_common(1)[0]
        # 3. low confidence -> OEM fallback
        if best_score < 0.08:
            if oem_conf >= 0.3:
                return oem_label, [("OEM-fallback", oem_conf, app_name)]
            return "unknown", results
        return best_label, results


# ---- CLI ----
def cmd_train():
    p = LabelPredictor()
    p.fit()
    p.save()
    print("Model saved to", MODEL_FILE)

def cmd_predict():
    if len(sys.argv) < 3:
        print("Usage: python predict_label.py predict <app_name>")
        return
    p = LabelPredictor()
    p.load()
    best, candidates = p.predict_best(sys.argv[2])
    print(f"  -> {best}")
    for label, score, matched in candidates[:5]:
        print(f"    {score:.3f}  [{label}]  <- \"{matched}\"")

def cmd_phone():
    p = LabelPredictor()
    p.load()

    unmatched = [
        ("com.unionpay.tsmservice",     "银联TSM服务",        ""),
        ("ryyakf.zf.iggofhrsc",         "ryyakf",             ""),
        ("com.heytap.music",            "音乐",               ""),
        ("com.oneplus.member",          "一加会员",            ""),
        ("com.coloros.filemanager",     "文件管理",            ""),
        ("com.coloros.alarmclock",      "时钟",               ""),
        ("com.finshell.wallet",         "钱包",               ""),
        ("com.sohu.inputmethod.sogouoem","搜狗输入法",         ""),
        ("com.messidor.upload",         "Upload",             ""),
        ("com.nearme.gamecenter",       "游戏中心",            ""),
        ("com.oplus.melody",            "Melody",             ""),
        ("com.heytap.themestore",       "主题商店",            ""),
        ("com.heytap.yoli",             "浏览器视频",          ""),
        ("com.oplus.games",             "游戏",               ""),
    ]

    for pkg, name, perms in unmatched:
        best, candidates = p.predict_best(name, perms)
        if best == "unknown":
            print(f"  ?? {pkg} ({name}) -> unknown")
        else:
            print(f"  OK {pkg} ({name}) -> {best}")
            for label, score, matched in candidates[:2]:
                print(f"        {score:.3f} [{label}] <- \"{matched}\"")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: train | predict <name> | phone")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "train":
        cmd_train()
    elif cmd == "predict":
        cmd_predict()
    elif cmd == "phone":
        cmd_phone()
    else:
        print(f"Unknown command: {cmd}")
