# 端侧知识库 · 浏览器本地向量化 + seekdb 混合检索

> 赛题：seekdb × 阶跃星辰 · 端侧 AI 推理性能优化与 seekdb 混合检索（20 分）
> 技术选型：**Transformers.js（端侧推理）+ seekdb-js（存储与混合检索）**

## 1. 这个应用做什么

导入一批文本文档 → **在浏览器里**把每一片编码成 384 维向量 → 写入 seekdb →
输入一句话查询时，查询向量**也在浏览器里**算 → 交给 seekdb 做**向量 + 全文混合检索（RRF 融合）** → 展示排序结果与原文出处。

一句话：**算力在端侧，检索在库内**。

## 2. 架构与「端侧」边界

```
浏览器（你的设备）                          Node 服务（只做数据层）              seekdb
─────────────────────────────────          ─────────────────────────          ────────────────
文件/粘贴文本                                                              kb_chunks 集合
   │ 切块（≤480 字符，重叠 60）                                              ├─ document（原文+小节）
   │                                                                        ├─ embedding VECTOR(384)
   ├─▶ Transformers.js ── 向量化（WebGPU → WASM 自动降级）                  ├─ metadata(kb_id/doc_id/title/heading/seq)
   │      模型：Xenova/multilingual-e5-small（INT8/FP16/FP32 可选）          └─ 全文索引（ngram，默认开启）
   │                                                                              ▲
   └─ POST /api/ingest（向量 + 原文 + 元数据）──────────────▶ upsert + refresh_index ┘

   查询 ──▶ 同一模型向量化 ──▶ POST /api/search ──▶ hybridSearch(向量路 + 全文路 + RRF) ──▶ 排序结果
```

**关键约束（题面要求）**：

| 要求 | 本项目的做法 |
|---|---|
| embedding 必须在用户设备本地算 | 前端 `public/app.js` 里调 Transformers.js；Node 服务里**没有任何模型代码**，`/api/health` 会回 `inference: "browser-side only"` |
| 不调云端推理 API | 模型权重从 CDN/HF 下载一次进浏览器缓存，推理全在 WebGPU/WASM 上跑 |
| 混合检索必须由 seekdb 完成 | 只调一次 `collection.hybridSearch({query, knn, rank:{rrf}})`，两路召回与 RRF 融合都在库里 |
| 写入后刷新索引才能检索到 | `/api/ingest` 里 `upsert` 之后显式 `refresh_index()`，并把两段耗时都返回给前端 |

## 3. 目录结构

```
stepfun/
├── server/index.mjs       # Express：/api/ingest、/api/search、/api/reset、/api/health、静态托管
├── public/
│   ├── index.html         # 界面：模型/精度/批大小、导入、检索、基准表
│   ├── app.js             # 端侧推理 + 切块 + 调用后端 + 分阶段计时 + 评测集
│   └── styles.css
├── samples/
│   ├── index.json         # 内置语料清单（6 篇）
│   └── corpus/*.md        # 语料：事务 / 索引 / RRF / HNSW / 端侧量化 / 工程笔记
├── package.json
├── README.md              # 本文件：运行说明 + 验证样例
└── TECH-NOTE.md           # 一页技术说明（模型量化 / 端侧运行 / seekdb 用法 / 实测表）
```

## 4. 运行环境

| 项 | 要求 |
|---|---|
| Node.js | 20+（实测 v24.15.0），`npm 10+` |
| 浏览器 | Chrome / Edge **最新版**（WebGPU 需要较新版本；不支持时自动降级 WASM） |
| seekdb | `oceanbase/seekdb:latest`（实测 1.4.0.0，Docker 单机） |
| seekdb-js | `seekdb@1.4.0`（`package.json` 已锁定，底层 mysql2） |
| 嵌入模型 | `Xenova/multilingual-e5-small`（384 维，多语；可选 `Xenova/all-MiniLM-L6-v2`） |

## 5. 从零启动

```bash
# 1) 起 seekdb
docker run -d --name seekdb -p 2881:2881 -p 2886:2886 \
  -v /your/data/seekdb:/var/lib/oceanbase oceanbase/seekdb:latest

# 2) 装依赖并启动
cd stepfun
npm install
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_DB=test \
SEEKDB_COLLECTION=kb_chunks npm start
#   → http://127.0.0.1:8090
```

Windows（PowerShell）：

```powershell
$env:SEEKDB_HOST="127.0.0.1"; $env:SEEKDB_PORT="2881"; $env:SEEKDB_COLLECTION="kb_chunks"
npm start
```

打开 **http://127.0.0.1:8090** 后按界面顺序操作：

1. 选模型 / 精度 / 批大小 → 点 **加载模型**（右上角徽标会显示 `后端：WebGPU` 或 `WASM（降级）`、`精度：Q8`）
2. 点 **载入内置语料（samples/corpus）** → 自动切块 → 本地向量化（进度条 + 日志）→ 写入 seekdb + 刷新索引
3. 在检索框里问一句 → 看结果卡片（出处 = `标题 › 小节 › 片段 #n`）
4. 点 **跑一次基准（10 题）** → 得到 Recall@5 与分阶段耗时

## 6. 验证样例（可按步骤执行）

**初始条件**：seekdb 空库（或先点界面上的「清空知识库」，它会删掉当前 `kb_id` 的所有片段）。

| # | 操作 | 预期 |
|---|---|---|
| 1 | 加载模型（INT8 + 自动后端） | 徽标显示当前后端与精度；日志出现「模型加载完成 xxx ms」 |
| 2 | 载入内置语料 | 日志：`6 篇 → N 片`；写入成功，`refresh_index` 耗时可见 |
| 3 | 检索 `为什么 B+ 树比哈希索引更适合范围查询` | 命中「02-索引结构」，出处指向「为什么 B+ 树比哈希索引更适合范围查询」小节 |
| 4 | 检索 `REPEATABLE READ 是怎么避免幻读的` | 命中「01-事务与隔离级别」，出处指向同名小节 |
| 5 | 检索 `INT8 量化对模型体积和精度的影响` | 命中「05-端侧推理与量化」 |
| 6 | 检索 `增量导入后为什么要刷新索引` | 命中「06-知识库工程笔记」 |
| 7 | 点「清空知识库」再检索 | 结果为空（说明检索确实走库，不是前端缓存） |
| 8 | 跑一次基准（10 题） | 输出 `Recall@5 = xx%（n/10）` 与端到端中位数 |

**seekdb 起作用的证据**（三选一）：

1. 检索响应的 `mode` 固定为 `hybrid (vector + fulltext, RRF inside seekdb)`，且返回 `keyword`（全文路用的词）；
2. 「清空知识库」后再检索为空，重新导入后又命中 —— 数据只在库里；
3. 直接查库：

```sql
-- mysql -h127.0.0.1 -P2881 -uroot
USE test;
SELECT collection_name, settings FROM sdk_collections;   -- kb_chunks 的维度 / 嵌入函数（应为 null）
SHOW TABLES LIKE 'c$v2$%';                               -- 片段表：_id / document / embedding VECTOR(384) / metadata json
```

## 7. 已知限制

1. 模型权重需要**首次联网下载**（约几十 MB，进浏览器缓存）；离线环境需预置权重。
2. 语料只支持纯文本 / Markdown；PDF 需先转文本。
3. `refresh_index()` 是同步阻塞点，单批太大时导入会明显卡住，界面里如实显示这段耗时。
4. 单机单集合，用 `kb_id` 元数据隔离多个知识库；没有账号体系与鉴权。
5. 重导入同一文档用 `upsert` 幂等，但**小节改名/删段后旧片段不会自动清理**（需按 `doc_id` 先删后写）。
6. WebGPU 的实际吞吐受显卡与驱动影响，本仓库的实测数字来自提交者本机，换设备会有差异。

## 8. 数据来源声明

- `samples/corpus/` 六篇语料为本次参赛自写（依据公开教材与官方文档整理），不含他人版权内容。
- 模型来自 HuggingFace 公开仓库（`Xenova/*` 的 ONNX 转换版本），量化文件为官方提供的 `q8` / `fp16` 精度。
- 仓库不含任何 API Key；seekdb 用默认本地账号。
