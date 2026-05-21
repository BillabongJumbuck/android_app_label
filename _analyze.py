import csv, sys
from collections import Counter

results = []
with open("../phone_packages.txt") as f:
    for line in f:
        pkg = line.strip()
        if not pkg:
            continue
        # Query the tool
        import subprocess
        label = subprocess.run(
            ["../query_app/query_app.exe", "-l", pkg],
            cwd="../query_app", capture_output=True, text=True
        ).stdout.strip()
        results.append((pkg, label))

# Print summary
found = sum(1 for _, l in results if l != "(no result)")
total = len(results)
print(f"=== Phone App Label Report ===")
print(f"Total third-party apps: {total}")
print(f"Found in database: {found}")
print(f"Not found: {total - found}")
print()

# Group by label category
cats = Counter()
for pkg, label in results:
    if label == "(no result)":
        cat = "(unknown)"
    else:
        cat = label.split("/")[0] if label else "(empty)"
    cats[cat] += 1

print("By category:")
for cat, cnt in cats.most_common():
    print(f"  {cat}: {cnt}")
print()

print("All found apps:")
for pkg, label in results:
    if label != "(no result)":
        print(f"  {pkg}")
        print(f"    -> {label}")
print()

print("Not found (no result):")
for pkg, label in results:
    if label == "(no result)":
        print(f"  {pkg}")
