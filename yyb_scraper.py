#!/usr/bin/env python3
"""腾讯应用宝爬虫 - 通过搜索发现应用，抓取详情及权限信息。
应用宝使用 Next.js SSR，数据内嵌在 __NEXT_DATA__ 中。"""

import requests
import csv
import time
import os
import re
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://sj.qq.com"
SEARCH_URL = f"{BASE_URL}/search"
DETAIL_URL = f"{BASE_URL}/appdetail"
OUTPUT_FILE = "yyb_apps.csv"
CACHE_FILE = "yyb_cache.csv"
MAX_WORKERS = 3
MIN_DELAY = 1.0
MAX_DELAY = 2.0
MAX_RETRIES = 2
BATCH_SIZE = 120
COOLDOWN_SECONDS = 120

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
]

# 搜索关键词：拉丁字母 + 高频中文字
SEARCH_TERMS = (
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [chr(c) for c in range(ord("0"), ord("9") + 1)]
    + [
        "的", "一", "是", "不", "了", "人", "我", "在", "有", "他",
        "这", "中", "大", "来", "上", "国", "个", "到", "说", "们",
        "为", "子", "和", "你", "地", "出", "会", "可", "也", "时",
        "要", "就", "下", "得", "着", "自", "年", "过", "发", "后",
        "作", "里", "用", "道", "行", "所", "然", "家", "种", "事",
        "成", "方", "多", "经", "么", "去", "法", "学", "如", "都",
        "同", "现", "当", "没", "动", "面", "起", "看", "定", "天",
        "分", "还", "进", "好", "小", "部", "其", "些", "主", "样",
        "理", "心", "她", "本", "前", "开", "但", "因", "只", "从",
        "想", "实", "日", "军", "者", "意", "无", "力", "它", "与",
        "长", "把", "机", "十", "民", "第", "公", "此", "已", "工",
        "使", "情", "明", "性", "知", "全", "三", "又", "关", "点",
        "正", "业", "外", "将", "两", "高", "间", "由", "问", "很",
        "最", "重", "并", "物", "手", "应", "战", "向", "头", "文",
        "体", "政", "美", "相", "见", "被", "利", "什", "二", "等",
        "产", "或", "新", "己", "制", "身", "果", "加", "西", "斯",
        "月", "话", "合", "回", "特", "代", "内", "信", "台", "网",
        "影", "游", "音", "车", "医", "购", "教", "银", "旅", "食",
        "视", "阅", "摄", "办", "财", "交", "健", "育", "融", "理",
        "记", "聊", "播", "频", "店", "商", "宝", "支", "付", "贷",
        "保", "险", "基", "金", "股", "票", "证", "券", "税", "法",
        "安", "全", "防", "毒", "杀", "清", "管", "助", "手", "锁",
        "壁", "纸", "桌", "主", "题", "输", "入", "法", "日", "历",
    ]
)

lock = threading.Lock()
stats = {"done": 0, "total": 0, "blocked": 0, "ok": 0, "batch_blocked": False}


def random_ua():
    return random.choice(USER_AGENTS)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def extract_next_data(html):
    """从 HTML 提取 __NEXT_DATA__ JSON"""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def fetch_html(url, params, session, referer=None):
    """获取页面 HTML，返回 __NEXT_DATA__ 中的 pageProps 或 None"""
    headers = {"Referer": referer or BASE_URL + "/"} if referer else {}
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, params=params, timeout=15, headers=headers)
            if resp.status_code == 403:
                return None
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = extract_next_data(resp.text)
            if data:
                return data.get("props", {}).get("pageProps", {})
            return None
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
    return None


def collect_all_apps():
    print("Phase 1: Collecting app list via search...", flush=True)
    session = make_session()
    apps = {}
    seen = set()  # dedup by pkg_name

    total_searches = len(SEARCH_TERMS)
    for i, term in enumerate(SEARCH_TERMS):
        props = fetch_html(SEARCH_URL, {"q": term}, session, referer=BASE_URL + "/")
        if not props:
            continue

        dcr = props.get("dynamicCardResponse", {})
        if not dcr:
            continue

        comps = dcr.get("data", {}).get("components", [])
        term_count = 0
        for comp in comps:
            items = comp.get("data", {}).get("itemData", [])
            for item in items:
                pkg = (item.get("pkg_name") or "").strip()
                if not pkg or pkg in seen:
                    continue
                seen.add(pkg)
                apps[pkg] = {
                    "package_name": pkg,
                    "app_name": item.get("name", ""),
                    "label": item.get("cate_name_new", ""),
                    "developer": item.get("developer", ""),
                    "permissions": "",
                }
                term_count += 1

        if term_count > 0:
            print(f"  [{i+1}/{total_searches}] '{term}' -> {term_count} new apps "
                  f"(total unique: {len(apps)})", flush=True)

        time.sleep(0.3)

    print(f"Total unique apps: {len(apps)}", flush=True)
    return apps


def scrape_detail(pkg, app, session):
    """获取详情页，提取 permissions 和 developer"""
    props = fetch_html(DETAIL_URL + "/" + pkg, {}, session, referer=BASE_URL + "/")
    if props is None:
        return "<BLOCKED>", None, None

    dcr = props.get("dynamicCardResponse", {})
    comps = dcr.get("data", {}).get("components", [])
    for comp in comps:
        if comp.get("cardId") == "yybn_game_basic_info":
            items = comp.get("data", {}).get("itemData", [])
            if items:
                info = items[0]
                dev = info.get("developer", "")
                perms_list = info.get("permissions_list", [])
                if perms_list:
                    perms = " | ".join(p.get("title", "") for p in perms_list if p.get("title"))
                else:
                    perms = "<NONE>"
                tags = info.get("tags", "")
                cate = info.get("cate_name_new", "")
                label_parts = [cate]
                if tags:
                    label_parts.append(tags.replace(",", "/"))
                label = "/".join(filter(None, label_parts))
                return perms, dev, label
    return "<NONE>", "", app.get("label", "")


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    apps = {}
    with open(CACHE_FILE, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pkg = row.get("package_name", "")
            if pkg:
                apps[pkg] = row
    return apps


def save_cache(apps):
    with open(CACHE_FILE, "w", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["package_name", "app_name", "label", "developer", "permissions"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for app in apps.values():
            writer.writerow(app)


def write_final_csv(apps):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f,
            fieldnames=["package_name", "app_name", "label", "developer", "permissions"],
            extrasaction="ignore")
        writer.writeheader()
        for app in apps.values():
            writer.writerow(app)
    print(f"Wrote {len(apps)} records to {OUTPUT_FILE}", flush=True)


def process_batch(apps, batch_pkgs):
    stats["batch_blocked"] = False
    thread_local = threading.local()

    def get_session():
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session()
        return thread_local.session

    def worker(pkg):
        if stats["batch_blocked"]:
            return pkg
        session = get_session()
        app = apps[pkg]
        perms, dev, label = scrape_detail(pkg, app, session)

        with lock:
            if perms != "<BLOCKED>":
                app["permissions"] = perms
            if dev:
                app["developer"] = dev
            if label:
                app["label"] = label
            stats["done"] += 1
            if perms == "<BLOCKED>":
                stats["blocked"] += 1
                stats["batch_blocked"] = True
            elif perms not in ("", "<NONE>"):
                stats["ok"] += 1

        delay = MIN_DELAY + random.random() * (MAX_DELAY - MIN_DELAY)
        time.sleep(delay)
        return pkg

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, pkg): pkg for pkg in batch_pkgs}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  [ERROR] {futures[future]}: {e}", flush=True)

    save_cache(apps)
    return stats["batch_blocked"]


def scrape_all_details(apps):
    print("Phase 2: Fetching detail pages for permissions...", flush=True)

    pkg_list = [pkg for pkg, info in apps.items()
                if not info.get("permissions") or info["permissions"] in ("", "<BLOCKED>")]
    stats["total"] = len(pkg_list)
    stats["done"] = 0
    stats["ok"] = sum(1 for a in apps.values()
                      if a.get("permissions") and a["permissions"] not in ("", "<BLOCKED>", "<NONE>"))
    stats["blocked"] = 0

    if not pkg_list:
        print("All apps already have permissions data.", flush=True)
        return

    print(f"Apps to process: {len(pkg_list)}, already OK: {stats['ok']}", flush=True)

    batch_num = 0
    remaining = list(pkg_list)

    while remaining:
        batch = remaining[:BATCH_SIZE]
        remaining = remaining[BATCH_SIZE:]
        batch_num += 1

        print(f"\n[Batch {batch_num}] Processing {len(batch)} apps "
              f"(remaining: {len(remaining)}, OK: {stats['ok']}, blocked: {stats['blocked']})",
              flush=True)

        hit_waf = process_batch(apps, batch)

        if hit_waf:
            print(f"  WAF detected! Cooling down for {COOLDOWN_SECONDS}s...", flush=True)
            blocked_pkgs = [pkg for pkg in batch
                           if apps[pkg].get("permissions") == "<BLOCKED>"]
            remaining = blocked_pkgs + remaining
            print(f"  {len(blocked_pkgs)} apps will be retried after cooldown.", flush=True)
            time.sleep(COOLDOWN_SECONDS)
            print("  Cooldown complete, resuming...", flush=True)
        else:
            print(f"  Batch complete: OK={stats['ok']}, blocked={stats['blocked']}",
                  flush=True)

    print(f"\nDetail scraping done.", flush=True)


def main():
    apps = load_cache()
    if apps is None:
        apps = collect_all_apps()
        if not apps:
            print("No apps collected, exiting.", flush=True)
            return
        save_cache(apps)
    else:
        print(f"Loaded {len(apps)} apps from cache.", flush=True)

    scrape_all_details(apps)
    write_final_csv(apps)

    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)

    print(f"Final: {len(apps)} total, {stats['ok']} with permissions", flush=True)
    print("Done!", flush=True)


if __name__ == "__main__":
    main()
