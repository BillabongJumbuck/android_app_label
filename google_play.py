import csv
import time
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from google_play_scraper import app, search, permissions
from google_play_scraper.exceptions import NotFoundError
from tqdm import tqdm

OUTPUT_FILE = 'android_apps_with_perms.csv'
MAX_WORKERS = 3  # 加入权限抓取后请求量翻倍，建议调低线程数防风控

# 生成海量搜索组合：a, b, c... aa, ab, ac... (突破少量关键词的限制)
def generate_search_terms():
    terms = list(string.ascii_lowercase)
    for i in string.ascii_lowercase:
        for j in string.ascii_lowercase:
            terms.append(i + j)
    return terms[:500]  # 先取前 500 个组合测试，全量有 700+ 种组合

def discover_packages(keywords):
    print(f">>> 正在通过 {len(keywords)} 个字母组合进行暴力发现...")
    packages = set()
    for kw in tqdm(keywords, desc="发现包名"):
        try:
            results = search(kw, lang='en', country='us')
            for res in results:
                packages.add(res['appId'])
            time.sleep(random.uniform(0.5, 1.5))
        except Exception:
            pass
    print(f">>> 字典穷举完成，共发现 {len(packages)} 个去重后的包名。")
    return list(packages)

def fetch_app_and_permissions(package_name):
    try:
        # 1. 获取基础详情
        result = app(package_name, lang='en', country='us')
        
        # 2. 获取权限列表 (这是额外的一次网络请求)
        # 返回格式通常是一个字典，我们将其展平为长文本
        app_perms = permissions(package_name, lang='en', country='us')
        perm_text_list = []
        for category, perms in app_perms.items():
            for p in perms:
                perm_text_list.append(p)
        
        # 将权限拼接成一长串文本，方便后续做 TF-IDF
        permissions_str = " | ".join(perm_text_list)
        
        return {
            'package_name': package_name,
            'app_name': result.get('title'),
            'label': result.get('genreId'), # genreId 比 genre 更规范，例如 GAME_ACTION
            'developer': result.get('developer'),
            'permissions': permissions_str
        }
    except Exception:
        return {"error": package_name}

def main():
    terms = generate_search_terms()
    package_list = discover_packages(terms)
    
    with open(OUTPUT_FILE, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['package_name', 'app_name', 'label', 'developer', 'permissions'])
        writer.writeheader()

    print(">>> 开始并发爬取应用详情与权限...")
    success_count = 0
    
    with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['package_name', 'app_name', 'label', 'developer', 'permissions'])
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_pkg = {executor.submit(fetch_app_and_permissions, pkg): pkg for pkg in package_list}
            
            for future in tqdm(as_completed(future_to_pkg), total=len(package_list), desc="抓取详情"):
                result = future.result()
                if result and "error" not in result:
                    writer.writerow(result)
                    success_count += 1
                time.sleep(random.uniform(0.2, 0.6))

    print(f">>> 爬取完成！成功获取 {success_count} 条含权限的数据。")

if __name__ == '__main__':
    main()