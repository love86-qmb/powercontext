# 一页技术说明 · 端侧知识库（seekdb × 阶跃星辰）

> 赛题：端侧 AI 推理性能优化与 seekdb 混合检索
> 应用：`stepfun/`（Express + 浏览器端 Transformers.js + seekdb-js）

## 1. 模型与量化

| 项 | 值 |
|---|---|
| 模型 | `Xenova/multilingual-e5-small`（384 维，多语种，ONNX 转换版） |
| 来源 | HuggingFace：`Xenova/multilingual-e5-small`（MIT 系授权，详见模型卡）；可切换 `Xenova/all-MiniLM-L6-v2` |
| 量化方案 | **INT8（`dtype: 'q8'`，默认）**；界面可切 `fp16` / `fp32` 三种精度做对照 |
| 处理方式 | 直接使用模型仓库发布的 ONNX 量化权重（`onnx/model_quantized.onnx` / `model_fp16.onnx`），运行时由 Transformers.js 选择对应文件，不需要自己再跑一遍量化脚本 |
| 文本前缀 | E5 系列要求 `passage: ` / `query: ` 前缀，代码里已按用途分别加（见 `public/app.js`） |

**体积与压缩比 / 质量变化**：按同一口径统计模型目录内实际 `.onnx` 文件总体积，
压缩比 = FP32 体积 ÷ 量化后体积；质量用同一评测集（内置 10 题）的 **Recall@5** 对比，
两次运行只有 `dtype` 不同，其余条件（语料、切块、seekdb 配置、批大小、后端）保持一致。

> 实测数据见第 5 节表格（由提交者在浏览器中实际运行填写）。

## 2. 端侧运行

| 项 | 做法 |
|---|---|
| 运行时 | Transformers.js 3.7.5（ESM，CDN 加载）+ ONNX Runtime Web |
| 后端选择 | 先 `navigator.gpu.requestAdapter()` 探测；成功则 `device: 'webgpu'`，否则 `device: 'wasm'` |
| 自动降级 | WebGPU 初始化抛错时**捕获并重试 WASM**，不会中断流程；徽标实时显示 `后端：WebGPU` 或 `WASM（降级）`（降级时变黄） |
| 推理位置 | 全部在浏览器。Node 服务只接收向量：`GET /api/health` 返回 `inference: "browser-side only (this server never embeds)"`，服务端代码里没有任何模型加载 |
| 批处理 | 文档按 `批大小`（4/8/16/32）分批 `pipeline(texts, {pooling:'mean', normalize:true})`，批越大 GPU 利用率越高 |
| 降级验证 | 在 Chrome 里用 `--disable-features=WebGPU`（或旧版浏览器）启动即可复现 WASM 路径，检索流程完全一致，仅徽标与耗时不同 |

## 3. seekdb 使用

| 项 | 做法 |
|---|---|
| 接入方式 | `seekdb@1.4.0`（官方 JS SDK，底层 mysql2 协议），服务端模式连本地 seekdb |
| 集合结构 | 集合 `kb_chunks`：`document`（原文，含小节标题）、`embedding`（`VECTOR(384)`）、`metadata`（`kb_id` / `doc_id` / `title` / `heading` / `seq`） |
| 索引配置 | 省略 schema 时 SDK 默认开启 **dense 向量索引 + 全文索引**；嵌入函数显式设为 `null`（向量由浏览器给，库里不挂模型） |
| 混合检索 | 一次调用 `collection.hybridSearch({ query: { whereDocument: { $contains: 关键词 }, where, nResults }, knn: { queryEmbeddings, where, nResults }, rank: { rrf: { rankWindowSize: 60, rankConstant: 60 } }, nResults })` —— **两路召回与 RRF 融合都在 seekdb 内完成**，服务端不做二次拼合 |
| 元数据过滤 | `kb_id` 同时下推到全文路与向量路（两处 `where`），保证 top-k 不被别的知识库占满 |
| 增量写入 | 片段 id = `doc_id + 序号`，用 `upsert` 保证重复导入幂等 |
| 索引刷新 | `upsert` 之后立即 `refresh_index()`，并把「写入耗时」与「刷新耗时」分别返回给前端；不刷新就搜不到，是本题最典型的坑 |

## 4. 状态与优化

**状态管理**
- 前端持有当前 `kb_id`；「清空知识库」只删该 `kb_id` 下的片段（`/api/reset`），因此切换/重建知识库后检索结果始终与当前库一致；
- 集合与数据库由服务端单例持有，SDK 连接复用，避免每次请求重建连接。

**做过的优化**
1. **批量向量化**：文档按批送进 pipeline，相比逐条调用减少 GPU/WASM 调度开销（界面可切批大小观察差异）。
2. **模型只加载一次**：模型实例缓存在内存，后续导入与查询复用；首屏的「模型加载」是冷启动项，单独计时，不计入单次查询延迟。
3. **写入合并 + 单次刷新**：一次导入的所有片段合并成一次 `upsert`，只刷新一次索引，而不是每片刷新一次。
4. **检索侧只做一次库内混合检索**：不做「先查向量再查全文再本地拼」——既符合题面要求，也省掉一次往返。
5. **前端结果直出**：结果卡片用 `textContent` 组装，不做模板重排，避免大结果集时的布局抖动。

## 5. 实测结果（提交者本机，按界面「跑一次基准」得到）

设备 / 浏览器：`待填：CPU / GPU 型号，Chrome 版本`
语料：`samples/corpus` 6 篇 → `待填` 个片段；评测集：内置 10 题，指标 Recall@5

| 配置 | 模型体积 | 文档向量化吞吐 | seekdb 检索延迟 | 端到端查询延迟 | Recall@5 |
|---|---|---|---|---|---|
| FP32（基线） | `待填` MB | `待填` 片/秒 | `待填` ms | `待填` ms | `待填` % |
| INT8 + WebGPU | `待填` MB | `待填` 片/秒 | `待填` ms | `待填` ms | `待填` % |
| INT8 + WASM | `待填` MB | `待填` 片/秒 | `待填` ms | `待填` ms | `待填` % |

压缩比（FP32 ÷ INT8）：`待填`

计时口径：
- **文档向量化**：从切块完成到全部向量拿到（不含网络与写库）；
- **seekdb 检索**：服务端 `hybridSearch` 往返耗时（响应里的 `tookMs`）；
- **端到端**：按下检索到结果渲染完成的时间，含查询向量化；
- 首次请求含冷启动，表内为**预热后的中位数**（界面表格第 3 列）。

## 6. 复现方式

1. 按 `README.md` 第 5 节起 seekdb 与服务；
2. 浏览器打开 `http://127.0.0.1:8090`，依次：加载模型 → 载入内置语料 → 检索 → 跑基准；
3. 换 `量化精度` 再点一次「加载模型」并重跑基准，即可得到上面表格的第二行与第三行；
4. 想验证 WASM 降级：用不带 WebGPU 的浏览器（或禁用 WebGPU 标志）重复第 2 步。
