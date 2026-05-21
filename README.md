# Android App Labeler

给任意 Android 包名打分类标签。数据来自四个应用商店（小米、Google Play、TapTap、应用宝），本地 SQLite 查不到时依次回退到 OEM 规则和 TF-IDF 推理。

## 使用方式

### 编译

```bash
cd query_app

# 当前平台
go build -buildvcs=false -o query_app .

# 交叉编译 Android ARM64
GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build -buildvcs=false -o query_app_arm64 .
```

### 部署到手机

CSV 文件和二进制放在同一目录，首次运行自动建库。

```bash
# 推送二进制和 CSV
adb push query_app_arm64 /data/local/tmp/query_app
adb push ../mi_apps_full.csv            /data/local/tmp/
adb push ../android_apps_with_perms.csv /data/local/tmp/
adb push ../taptap_apps.csv             /data/local/tmp/
adb push ../yyb_apps.csv                /data/local/tmp/
adb shell chmod 755 /data/local/tmp/query_app
```

### 查询

```bash
adb shell /data/local/tmp/query_app -l com.tencent.mm
adb shell /data/local/tmp/query_app -l com.miui.securitymanager
adb shell /data/local/tmp/query_app com.tencent.mm
```

**输出：**
```
COMMUNICATION,聊天社交/交友,社交/好友社交          # DB 命中，多源逗号分隔
[OEM] OEM系统预装                                   # OEM 规则命中
[TF-IDF] GAME_STRATEGY                              # TF-IDF 推理
(no result)                                         # 无结果
```

## 数据规模

| 数据源 | 应用数 | 权限数据 | 收录范围 |
|--------|--------|---------|---------|
| 小米应用商店 | 4,923 | 100% | 国内安卓生态 |
| Google Play | 9,177 | 100% | 国际应用 |
| TapTap | 820 | 0% | 手游 |
| 应用宝 | 9,110 | 74% | 国内全品类 |
| **合计** | **24,030** | — | — |

## 三层体系

### 第一层：SQLite 数据库（覆盖 ~60%，可信度：高）

四个数据源的 CSV 导入 SQLite，按 `package_name` 精确查询。同一个包名可能命中多个来源，输出时逗号分隔。

### 第二层：OEM 硬规则（覆盖 ~25%，可信度：高）

覆盖 12 个手机厂商（小米/OPPO/Vivo/华为/三星/Pixel/一加/联想/魅族/努比亚/华硕/索尼）的系统预装应用。通过包名前缀 + 应用名关键词匹配，映射到 30+ 个预定义类别：

`安全中心→系统/安全防护` `小爱语音→工具/语音助手` `相册→摄影/图片管理` `换机助手→系统/数据迁移` ...

规则文件：`tfidf_label/oem_rules.py` / `query_app/tfidf/oem.go`（Python/Go 双版本）。

### 第三层：TF-IDF 推理（覆盖 ~10%，可信度：中）

用 20,943 条已标注数据训练 jieba + char-bigram 的 TF-IDF 模型，通过余弦相似度找最相似的已标注应用，加权投票输出标签。预测结果带 `[TF-IDF]` 前缀以区别于确定性结果。

模型嵌入 Go 二进制：`vocab.json`（1 万词）、`idf.json`（权重）、`labels.json`（412 个标签）、`records.bin`（20,943 条训练向量，18MB）。

**局限性：** 基于字面相似度而非语义理解。短名字和训练数据中无同类样本的应用容易出错。手机端推理耗时 2-3 秒。

### 兜不住（~5%）

随机包名、小众测试工具、金融安全组件（银联 TSM）、OEM 深度定制模块。

## 实测情况

| 手机 | 第三方应用 | 命中率 | 备注 |
|------|----------|--------|------|
| OnePlus（ColorOS）| 52 | 73% | 14 个 OEM 系统应用无法识别 |
| 小米（MIUI）| 81 | 99% | 含 OEM 规则 + TF-IDF 兜底 |

## 改进方向

### 方向一：把 `[TF-IDF]` 前缀用起来

Go 二进制已输出前缀标记。调用方可以根据前缀区别对待：无前缀直接展示，`[OEM]` 标注"系统预装"，`[TF-IDF]` 折叠展示或附加置信度说明。成本：零。

### 方向二：人工标注一批冷门应用

TF-IDF 对训练数据里没有的应用类型（代理工具、金融安全组件等）表现差。标注 200 条电话端常见冷门应用可直接命中 SQLite 层，比改模型架构效果大得多。格式与现有 CSV 完全兼容：

```
package_name,app_name,label,developer,permissions
com.github.kr328.clash,Clash,工具/网络代理,Clash Developers,访问网络 | ...
com.unionpay.tsmservice,银联TSM服务,金融理财/安全组件,中国银联,<NONE>
```

### 方向三：传入真实应用名

目前 CLI 只传包名，TF-IDF 从包名末段猜测应用名（`securitymanager` 而非 `安全中心`）。如果能从手机 `packages.xml` 或 launcher 数据库读到真实应用名，OEM 规则就能精准匹配，不需要 TF-IDF。实现方式：CLI 增加 `-n` 参数，或接受 `pkg|name` 格式输入。

## 项目结构

```
├── mi_app_scraper.py              # 小米应用商店爬虫
├── google_play.py                  # Google Play 爬虫
├── taptap_scraper.py               # TapTap 爬虫
├── yyb_scraper.py                  # 应用宝爬虫
├── *.csv                           # 四个数据源 CSV
├── tfidf_label/                    # Python TF-IDF 训练 + 导出
│   ├── predict_label.py            # CLI: train / predict / phone
│   ├── oem_rules.py                # OEM 硬规则
│   ├── export_for_go.py            # 导出模型为 Go embed 格式
│   └── go_export/                  # 导出文件
├── query_app/                      # Go 查询工具
│   ├── main.go                     # CLI + SQLite + 三层回退逻辑
│   ├── tfidf/                      # TF-IDF 推理引擎
│   │   ├── engine.go               # 模型加载 + 分词 + 余弦相似度
│   │   ├── oem.go                  # OEM 规则（Go 版）
│   │   └── data/                   # 嵌入的模型文件（18MB）
│   └── go.mod
└── ANDROID_APP_LABELER.md          # 本文档
```
