# Android App Labeler

给任意 Android 包名打分类标签。数据来自四个应用商店（小米、Google Play、TapTap、应用宝），本地 SQLite 查不到时依次回退到 OEM 规则和 TF-IDF 推理。

## 使用方式

```bash
# 编译（可选交叉编译 Android ARM64）
go build -o query_app .
GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build -o query_app_arm64 .

# 放到手机上
adb push query_app_arm64 /data/local/tmp/
adb push *.csv /data/local/tmp/
adb shell chmod 755 /data/local/tmp/query_app_arm64

# 查询标签
./query_app -l com.tencent.mm              # 仅输出标签
./query_app com.tencent.mm                  # JSON 完整信息
./query_app -l com.miui.securitymanager     # 冷门应用自动回退到 [OEM]
./query_app -l com.github.kr328.clash       # 无匹配时回退到 [TF-IDF]
```

**输出格式：**
- 数据库命中 → 直接输出标签（多源用逗号分隔）
- OEM 规则命中 → `[OEM] 系统/安全防护`
- TF-IDF 推理 → `[TF-IDF] COMMUNICATION`
- 无结果 → `(no result)`

**前置要求：** 首次运行会自动从 CSV 文件导入 SQLite 数据库。CSV 需放在二进制同目录下。

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

四个数据源的 CSV 导入 SQLite，按 `package_name` 精确查询。同一个包名可能命中多个来源（如微信在小米、Google Play、应用宝都有），输出时逗号分隔。

### 第二层：OEM 硬规则（覆盖 ~25%，可信度：高）

覆盖 12 个手机厂商（小米/OPPO/Vivo/华为/三星/Pixel/一加/联想/魅族/努比亚/华硕/索尼）的系统预装应用。通过包名前缀 + 应用名关键词匹配，映射到 30+ 个预定义类别：

`安全中心→系统/安全防护` `小爱语音→工具/语音助手` `相册→摄影/图片管理` `换机助手→系统/数据迁移` ...

规则文件：`tfidf_label/oem_rules.py` / `query_app/tfidf/oem.go`（Python/Go 双版本）。

### 第三层：TF-IDF 推理（覆盖 ~10%，可信度：中）

用 20,943 条已标注数据训练 char-bigram + jieba 分词的 TF-IDF 模型，通过余弦相似度找最相似的已标注应用，加权投票输出标签。预测结果带 `[TF-IDF]` 前缀以区别于确定性结果。

模型导出为四个文件嵌入 Go 二进制：`vocab.json`（1万词）、`idf.json`（权重）、`labels.json`（412 个标签）、`records.bin`（20,943 条训练向量，18MB）。

**局限性：** 基于字面相似度而非语义理解。短名字（"clash""mail"）和训练数据中无同类样本的应用容易出错。手机端推理耗时 2-3 秒（20,943 次稀疏向量余弦相似度）。

### 兜不住（~5%）

随机包名、小众测试工具、金融安全组件（银联 TSM）、OEM 深度定制模块。

## 实测情况

| 手机 | 第三方应用 | 命中率 | 备注 |
|------|----------|--------|------|
| OnePlus（ColorOS）| 52 | 73% | 14 个 OEM 系统应用无法识别 |
| 小米（MIUI）| 81 | 99% | 含 OEM 规则 + TF-IDF 兜底 |

## 改进方向

### 方向一：把 `[TF-IDF]` 前缀用起来

目前 Go 二进制输出已带前缀标记。调用方可以根据前缀区别对待：

- 无前缀 → 直接展示
- `[OEM]` → 展示，标注"系统预装"
- `[TF-IDF]` → 折叠展示，或附加置信度说明，或交给用户二次确认

成本：零。就是把前缀当信号用，不改模型。

### 方向二：人工标注一批冷门应用

TF-IDF 对"代理工具""金融安全组件""OEM 深度集成"这类应用表现差，因为训练数据里根本没有。如果有 200 条精确标注的电话端常见冷门应用，可以直接提升这一段的准确率，比改模型架构效果大得多。

标注格式与现有 CSV 完全兼容，写入即可被 SQLite 层直接命中：

```
package_name,app_name,label,developer,permissions
com.github.kr328.clash,Clash,工具/网络代理,Clash Developers,访问网络 | ...
com.unionpay.tsmservice,银联TSM服务,金融理财/安全组件,中国银联,<NONE>
```

### 方向三：传入真实应用名

目前 CLI 查询只传包名，TF-IDF 从包名末段猜测应用名（`com.miui.securitymanager`→"securitymanager"）。如果能从手机的 `packages.xml` 或 launcher 数据库读取到真实应用名（"安全中心"），OEM 规则就能精准匹配，不需要 TF-IDF。

实现方式：CLI 增加 `-n` 参数，或接受 `package_name|app_name` 格式输入。

## 项目结构

```
├── mi_app_scraper.py          # 小米应用商店爬虫
├── google_play.py              # Google Play 爬虫
├── taptap_scraper.py           # TapTap 爬虫
├── yyb_scraper.py              # 应用宝爬虫
├── mi_apps_full.csv            # 小米数据（4,923 条）
├── android_apps_with_perms.csv # Google Play 数据（9,177 条）
├── taptap_apps.csv             # TapTap 数据（820 条）
├── yyb_apps.csv                # 应用宝数据（9,110 条）
├── tfidf_label/                # Python TF-IDF 训练 + 导出
│   ├── predict_label.py        # CLI: train / predict / phone
│   ├── oem_rules.py            # OEM 硬规则
│   ├── export_for_go.py        # 导出模型为 Go embed 格式
│   └── go_export/              # 导出文件
├── query_app/                  # Go 查询工具
│   ├── main.go                 # CLI + SQLite + 三层回退逻辑
│   ├── tfidf/                  # TF-IDF 推理引擎
│   │   ├── engine.go           # 模型加载 + 分词 + 余弦相似度
│   │   ├── oem.go              # OEM 规则（Go 版）
│   │   └── data/               # 嵌入的模型文件（18MB）
│   ├── go.mod / go.sum
│   └── query_app_arm64         # 编译好的 Android 二进制
└── ANDROID_APP_LABELER.md      # 本文档
```
