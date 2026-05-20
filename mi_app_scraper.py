#!/usr/bin/env python3
"""小米应用商店爬虫 - 爬取 app 的 package_name, app_name, label, developer, permissions
支持断点续爬和自动应对 WAF 封锁"""

import requests
from bs4 import BeautifulSoup
import csv
import time
import re
import os
import sys
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://app.mi.com"
API_URL = f"{BASE_URL}/categotyAllListApi"
DETAIL_URL = f"{BASE_URL}/details"
OUTPUT_FILE = "mi_apps_full.csv"
CACHE_FILE = "mi_apps_cache.csv"
MAX_WORKERS = 3
MIN_DELAY = 1.5
MAX_DELAY = 3.0
MAX_RETRIES = 2
BATCH_SIZE = 120       # 每批处理数量（低于 WAF 阈值）
COOLDOWN_SECONDS = 120  # WAF 封锁后冷却时间

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
]

lock = threading.Lock()
stats = {"done": 0, "total": 0, "blocked": 0, "ok": 0, "batch_blocked": False}


def random_ua():
    return random.choice(USER_AGENTS)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    })
    return s


def fetch_json(url, params, session):
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, params=params, timeout=15)
            if resp.status_code == 403:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
    return None


def fetch_html(url, session):
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url,
                headers={"Referer": "https://app.mi.com/", "User-Agent": random_ua()},
                timeout=15)
            if resp.status_code == 403:
                return None  # WAF block
            resp.raise_for_status()
            return resp.text
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
    return None


def collect_all_apps():
    print("Phase 1: Collecting app list...", flush=True)
    session = make_session()
    apps = {}

    for cat_id in range(1, 31):
        page = 0
        while True:
            data = fetch_json(API_URL, {
                "page": page, "categoryId": cat_id, "pageSize": 100,
            }, session)

            if not data or not data.get("data"):
                break

            for item in data["data"]:
                pkg = item.get("packageName", "")
                if pkg and pkg not in apps:
                    apps[pkg] = {
                        "package_name": pkg,
                        "app_name": item.get("displayName", ""),
                        "label": f"{item.get('level1CategoryName', '')}/{item.get('level2CategoryName', '')}",
                        "developer": item.get("publisherName", ""),
                        "permissions": "",
                    }

            if not data.get("hasNext"):
                break
            page += 1
            time.sleep(0.05)

    print(f"Total unique apps: {len(apps)}", flush=True)
    return apps


def scrape_permissions(package_name, session):
    url = f"{DETAIL_URL}?id={package_name}"
    html = fetch_html(url, session)
    if html is None:
        return "<BLOCKED>"

    soup = BeautifulSoup(html, "html.parser")
    permissions = []

    for span in soup.find_all("span", style=re.compile(r"color:\s*#343434")):
        text = span.get_text(strip=True)
        if text.startswith("●"):
            perm = text[1:].strip()
            if perm and len(perm) < 200:
                permissions.append(perm)

    return " | ".join(permissions) if permissions else "<NONE>"


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
        writer = csv.DictWriter(f,
            fieldnames=["package_name", "app_name", "label", "developer", "permissions"],
            extrasaction="ignore")
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
    """处理一批 app，如果触发 WAF 则提前停止"""
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
        perms = scrape_permissions(pkg, session)

        with lock:
            apps[pkg]["permissions"] = perms
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


def scrape_all_permissions(apps):
    print("Phase 2: Scraping permissions...", flush=True)

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
            # 将被封的 app 重新加入队列
            blocked_pkgs = [pkg for pkg in batch
                           if apps[pkg].get("permissions") == "<BLOCKED>"]
            remaining = blocked_pkgs + remaining
            print(f"  {len(blocked_pkgs)} apps will be retried after cooldown.", flush=True)
            time.sleep(COOLDOWN_SECONDS)
            print("  Cooldown complete, resuming...", flush=True)
        else:
            print(f"  Batch complete: OK={stats['ok']}, blocked={stats['blocked']}",
                  flush=True)

    print(f"\nPermissions scraping done.", flush=True)


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

    scrape_all_permissions(apps)
    write_final_csv(apps)

    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)

    print(f"Final: {len(apps)} total, {stats['ok']} with permissions", flush=True)
    print("Done!", flush=True)


if __name__ == "__main__":
    main()
