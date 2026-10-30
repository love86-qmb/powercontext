# 一页技术说明 · 端侧知识库（seekdb × 阶跃星辰）

> 赛题：端侧 AI 推理性能优化与 seekdb 混合检索
> 应用：`stepfun/`（Express + 浏览器端 Transformers.js + seekdb-js）
> 实测环境：提交者本机 · Chrome（WebGPU 可用）· 语料 `samples/corpus` 6 篇 → **12 片段** · 评测集内置 10 题

## 1. 模型与量化

| 项 | 值 |
|---|---|
| 模型 | `Xenova/multilingual-e5-small`（384 维，多语种，ONNX 转换版） |
| 来源 | HuggingFace `Xenova/multilingual-e5-small`；界面可切 `Xenova/all-MiniLM-L6-v2` |
| 量化方案 | **INT8（`dtype: 'q8'`，默认）**，界面可切 `fp16` / `fp32` 做对照 |
| 处理方式 | 直接加载模型仓库发布的 ONNX 量化权重（`onnx/model_quantized.onnx` / `model_fp16.onnx` / `model.onnx`），由 Transformers.js 按 `dtype` 选择文件，不需要自己再跑量化脚本 |
| 文本前缀 | E5 系列要求 `passage: ` / `query: ` 前缀，代码里按用途分别加（`public/app.js` 的 `withPrefix()`），不加会明显掉质量 |

**量化质量结论（实测）**：同一语料、同一评测集、同一后端（WebGPU），
INT8 与 FP32 的 **Recall@5 都是 100%（10/10）**，即本语料下 INT8 没有带来可观测的质量损失；
同时 INT8 的模型下载与加载耗时显著更低（见第 5 节）。两者端到端延迟差异在噪声范围内。

**模型体积**：按「模型目录内实际 `.onnx` 文件总大小」统计，读法见第 7 节
（本地抓包或 HF 文件列表），压缩比 = FP32 体积 ÷ INT8 体积。

## 2. 端侧运行

| 项 | 做法 |
|---|---|
| 运行时 | Transformers.js 3.7.5（ESM，CDN 加载）+ ONNX Runtime Web |
| 后端选择 | 先 `navigator.gpu.requestAdapter()` 探测；成功则 `device: 'webgpu'`，否则 `device: 'wasm'` |
| 自动降级 | WebGPU 初始化抛错时捕获并**重试 WASM**，流程不中断；徽标实时显示 `后端：WebGPU` / 黄色 `WASM（降级）` |
| 推理位置 | 全部在浏览器。`GET /api/health` 返回 `inference: "browser-side only (this server never embeds)"`，服务端代码零模型 |
| 批处理 | 文档按 `批大小`（4/8/16/32）分批推理，一次 `pipeline(texts, {pooling:'mean', normalize:true})` 出一批向量 |
| 降级验证 | 用不带 WebGPU 的浏览器（或 `chrome --disable-features=WebGPU`）重复操作即可复现 WASM 路径 |

## 3. seekdb 使用

| 项 | 做法 |
|---|---|
| 接入方式 | `seekdb@1.4.0`（官方 JS SDK，底层 mysql2），服务端模式连本地 seekdb |
| 集合结构 | `kb_chunks`：`document`（原文，含小节标题）、`embedding`（`VECTOR(384)`）、`metadata`（`kb_id` / `doc_id` / `title` / `heading` / `seq`） |
| 索引配置 | 省略 schema 时 SDK 默认开启 **dense 向量索引 + 全文索引**；嵌入函数显式传 `embeddingFunction: null`（向量由浏览器给，库里不挂模型，避免去注册 `default-embed`） |
| 混合检索 | 一次 `collection.hybridSearch({ query: { whereDocument: { $contains: 关键词 }, where, nResults }, knn: { queryEmbeddings, where, nResults }, rank: { rrf: { rankWindowSize: 60, rankConstant: 60 } }, nResults })` —— **两路召回与 RRF 融合都在库内完成**，服务端不做二次拼合 |
| 元数据过滤 | `kb_id` 同时下推到全文路与向量路（两处 `where`），保证 top-k 不被别的知识库占满 |
| 增量写入 | 片段 id = `doc_id + 序号`；`doc_id` 由标题稳定派生（保留中文 + 哈希兜底，**不用随机数**），因此重复导入是 `upsert` 幂等 |
| 索引刷新 | `upsert` 之后立刻 `refresh_index()`，并分别返回「写入耗时」与「刷新耗时」；不刷新就搜不到，是本题最典型的坑 |

## 4. 状态与优化

**状态管理**
- 前端持有当前 `kb_id`；「清空知识库」只删该 `kb_id` 下的片段（`/api/reset`），
  所以切换/重建知识库后检索结果始终与当前库一致；
- 服务端单例持有 SDK 连接与集合句柄，避免每次请求重建连接。

**做过的 5 处优化**
1. **批量向量化**：文档按批送入 pipeline，减少每次调用的固定开销（界面可切批大小观察）。
2. **模型只加载一次**：模型实例缓存在内存，导入与查询复用；「模型加载」单独计时，不计入单次查询延迟。
3. **一次 upsert + 一次刷新**：整批片段合并成一次写入、只刷新一次索引，而不是每片刷新。
4. **检索只走一次库内混合检索**：不做「先查向量再查全文再本地拼」，既合规也省一次往返。
5. **结果直出**：结果卡片用 `textContent` 组装，避免大结果集的模板重排与注入风险。

**踩到并修掉的坑（都在提交的代码里）**
- `getOrCreateCollection` 默认要注册 `default-embed` 嵌入函数 → 显式 `embeddingFunction: null`；
- 整句中文当 `$contains` 永远匹配不上 → 去疑问词后取句尾 4 字（`refreshIndex` 那类词才命中）；
- 切块时把重叠尾巴塞回缓冲，会让所有片段继承第一块的起点、出处全变成文档大标题 → 改为 `carry` 变量；
- `slug()` 过滤掉中文后用 `Date.now()` 兜底，导致重复导入不是幂等（实测片段数 12 → 24）→ 改为保留中文 + 稳定哈希。

## 5. 实测结果（提交者本机，界面「跑一次基准」原样数据）

**评测集**：内置 10 题，每题 Top5 中含预期文档即算命中，指标 Recall@5。

| 阶段 | INT8（q8）+ WebGPU | FP32 + WebGPU |
|---|---|---|
| 模型下载 + 加载（首次，含下载与图编译） | 38.0 s | 120.0 s |
| 模型下载 + 加载（中位数） | 38.0 s（1 次） | 79.0 s（2 次） |
| 文档向量化（12 片段） | 22.2 s（中位，4 次） | 22.4 s |
| 写入 seekdb + 刷新索引 | 0.81 s（中位） | 0.70 s |
| 查询向量化 | 233.3 ms（中位，12 次） | 219.7 ms（中位，22 次） |
| **seekdb 混合检索** | **12.8 ms**（中位） | 12.4 ms |
| **端到端（回车 → 结果可看）** | **261.1 ms**（中位） | 239.8 ms |
| **Recall@5** | **100%（10/10）** | **100%（10/10）** |
| 模型体积 | 见第 7 节读法 | 见第 7 节读法 |

**证据截图**（`screenshots/`）：
`screenshots/04-bench-q8-webgpu.png`（INT8 基准表 + 10/10 全绿）、
`screenshots/05-bench-fp32-webgpu.png`（FP32 基准表）、
`screenshots/03-ingest-log-q8.png`（导入日志：切块 6 篇→12 片、写入 253.8 ms、刷新 648.5 ms、本机向量化 13083 ms ≈ 0.9 片/秒）、
`screenshots/01-search-btree-q8.png`、`screenshots/02-search-int8-q8.png`（检索结果与出处）。

**怎么读这些数字（诚实说明）**
- 「模型加载」是冷启动项，首次包含权重下载与 WebGPU 图编译，**不进入单次查询延迟**；FP32 更大所以更慢。
- 「文档向量化」的中位数偏高，因为样本里混入了首次（含编译）的记录；单独看预热后的那轮是 **13083 ms / 12 片 ≈ 0.9 片/秒**（见导入日志截图）。
- **端到端 240–260 ms 里，seekdb 只占 12 ms**，瓶颈在查询向量化的冷启动尾巴；预热后单条查询降到几十毫秒（FP32 表里最近一次为 43.5 ms）。
- 语料只有 12 个片段，Recall@5 的分辨率有限（每错 1 题就差 10 个百分点），
  所以「INT8 与 FP32 都是 100%」应理解为「**没有可观测的质量损失**」，而不是「两者完全等价」。

## 6. 怎么补齐剩下两格（WASM 路径 / 模型体积）

1. **WASM 降级**：Chrome 启动参数加 `--disable-features=WebGPU`（或用不支持 WebGPU 的浏览器），
   重开页面 → 徽标应显示黄色 `后端：WASM（降级）` → 重新加载模型、导入、跑基准，把表格补成第三行。
2. **模型体积**：DevTools → Network → 过滤 `onnx`，分别记录 `model_quantized.onnx`（INT8）、
   `model_fp16.onnx`（FP16）、`model.onnx`（FP32）的 `Size`（或本地 HF 缓存目录里看实际文件大小）。
   三者之和即「模型目录体积」，压缩比 = FP32 ÷ INT8。

## 7. 复现方式

1. 按 `README.md` 第 5 节起 seekdb 与服务（`npm install && npm start`）；
2. 浏览器打开 `http://127.0.0.1:8090`：加载模型 → 载入内置语料 → 检索 → 跑基准；
3. 切 `量化精度` 为 `fp32` 再点一次「加载模型」并重跑基准 → 得到 FP32 行；
4. 用第 6 节的办法再补一档 WASM。
