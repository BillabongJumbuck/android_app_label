#!/usr/bin/env python3
"""TapTap.cn 爬虫 - 通过排行榜 API 发现应用并抓取开发者信息。
权限数据在 TapTap Web API 中不可用，统一设为 <NONE>。
使用 curl_cffi 以绕过 Alibaba Cloud WAF 的 TLS 指纹检测。"""

from curl_cffi import requests
import csv
import time
import os
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://www.taptap.cn"
API_BASE = f"{BASE_URL}/webapiv2"
RANKING_URL = f"{API_BASE}/app-top/v2/hits"
DETAIL_URL = f"{API_BASE}/app/v6/detail"
TERMS_URL = f"{API_BASE}/top/v3/terms"
OUTPUT_FILE = "taptap_apps.csv"
CACHE_FILE = "taptap_cache.csv"
MAX_WORKERS = 3
MIN_DELAY = 1.5
MAX_DELAY = 3.0
MAX_RETRIES = 2
BATCH_SIZE = 120
COOLDOWN_SECONDS = 120

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
]

# 排行榜类型 —— 先从 API 获取，失败时回退到此列表
FALLBACK_RANKING_TYPES = [
    "hot", "reserve", "pop", "new", "sell", "exclusive",
    "action", "strategy", "idle", "single", "casual",
    "sandbox_survival", "management", "unriddle", "shooter",
    "multiplayer", "acgn", "music", "scenario", "swordsman",
    "otome", "independent", "roguelike", "in_app_event_reserve",
]

lock = threading.Lock()
stats = {"done": 0, "total": 0, "blocked": 0, "ok": 0, "batch_blocked": False}


def make_xua():
    uid = str(uuid.uuid4())
    return (f"V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&LOC=CN&PLT=PC"
            f"&DS=Android&UID={uid}&OS=Windows&OSV=10&DT=PC")


def random_ua():
    return random.choice(USER_AGENTS)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.taptap.cn/",
    })
    return s


def fetch_json(url, params, session):
    for attempt in range(MAX_RETRIES):
        try:
            params = dict(params)
            params["X-UA"] = make_xua()
            resp = session.get(url, params=params, timeout=15,
                               impersonate="chrome120")
            if resp.status_code == 403:
                return None
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                return None
            return data.get("data")
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
    return None


def get_ranking_types(session):
    """尝试从 API 获取排行榜分类；失败则使用内置列表"""
    try:
        resp = session.get(TERMS_URL,
            params={"X-UA": make_xua()},
            timeout=15, impersonate="chrome120")
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data"):
                lst = data["data"].get("list", [])
                types = [item.get("identification", "") for item in lst
                        if item.get("identification")]
                if types:
                    print(f"Got {len(types)} ranking types from API.", flush=True)
                    return types
    except Exception:
        pass
    print(f"Falling back to {len(FALLBACK_RANKING_TYPES)} built-in ranking types.", flush=True)
    return FALLBACK_RANKING_TYPES


def collect_all_apps():
    print("Phase 1: Collecting app list from ranking APIs...", flush=True)
    session = make_session()
    ranking_types = get_ranking_types(session)
    apps = {}

    for type_name in ranking_types:
        from_idx = 0
        limit = 10
        type_apps = 0
        while True:
            data = fetch_json(RANKING_URL, {
                "type_name": type_name, "platform": "android",
                "from": from_idx, "limit": limit,
            }, session)

            if not data:
                break

            lst = data.get("list", [])
            if not lst:
                break

            for item in lst:
                app = item.get("app", {})
                pkg = (app.get("identifier") or "").strip()
                if not pkg:
                    continue
                if pkg not in apps:
                    tags = [t.get("value", "") for t in app.get("tags", []) if t.get("value")]
                    apps[pkg] = {
                        "package_name": pkg,
                        "app_name": app.get("title", ""),
                        "label": "/".join(tags),
                        "developer": "",
                        "permissions": "<NONE>",
                        "_app_id": str(app.get("id", "")),
                    }

            type_apps += len(lst)
            total = data.get("total", 0)
            if from_idx + limit >= total:
                break
            from_idx += limit
            time.sleep(0.1)

        if type_apps > 0:
            print(f"  [{type_name}] collected {type_apps} apps (total unique: {len(apps)})", flush=True)

    print(f"Total unique apps: {len(apps)}", flush=True)
    return apps


def scrape_developer(app_id, app, session):
    """获取开发者名称。detail API 返回结构: data.app.developers[0].name"""
    data = fetch_json(DETAIL_URL, {"id": app_id}, session)
    if data is None:
        return "<BLOCKED>"

    app_data = data.get("app") or {}
    devs = app_data.get("developers") or []
    if devs:
        return devs[0].get("name", "")

    return ""


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    apps = {}
    with open(CACHE_FILE, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pkg = row.get("package_name", "")
            if pkg:
                row["_app_id"] = row.get("_app_id", "")
                apps[pkg] = row
    return apps


def save_cache(apps):
    with open(CACHE_FILE, "w", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["package_name", "app_name", "label", "developer", "permissions", "_app_id"]
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
        app_id = app.get("_app_id", "")
        dev = scrape_developer(app_id, app, session)

        with lock:
            app["developer"] = dev
            stats["done"] += 1
            if dev == "<BLOCKED>":
                stats["blocked"] += 1
                stats["batch_blocked"] = True
            elif dev:
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


def scrape_all_developers(apps):
    print("Phase 2: Fetching developer details...", flush=True)

    pkg_list = [pkg for pkg, info in apps.items()
                if not info.get("developer") or info["developer"] in ("", "<BLOCKED>")]
    stats["total"] = len(pkg_list)
    stats["done"] = 0
    stats["ok"] = sum(1 for a in apps.values()
                      if a.get("developer") and a["developer"] not in ("", "<BLOCKED>"))
    stats["blocked"] = 0

    if not pkg_list:
        print("All apps already have developer data.", flush=True)
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
                           if apps[pkg].get("developer") == "<BLOCKED>"]
            remaining = blocked_pkgs + remaining
            print(f"  {len(blocked_pkgs)} apps will be retried after cooldown.", flush=True)
            time.sleep(COOLDOWN_SECONDS)
            print("  Cooldown complete, resuming...", flush=True)
        else:
            print(f"  Batch complete: OK={stats['ok']}, blocked={stats['blocked']}",
                  flush=True)

    print(f"\nDeveloper scraping done.", flush=True)


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

    scrape_all_developers(apps)
    write_final_csv(apps)

    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)

    print(f"Final: {len(apps)} total, {stats['ok']} with developer", flush=True)
    print("Done!", flush=True)


if __name__ == "__main__":
    main()
