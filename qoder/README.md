# NoteSeek · 课程与文献速查台

把 Markdown 笔记切块存进 seekdb，用一句话检索，返回带出处的原文片段——出处形如 `文档 › 小节 › 片段 #n`，能直接翻回原文。

后端是一个文件 `app.py`（FastAPI + pyseekdb），前端是两个静态页（搜索台 + 展示页），没有框架、没有 CDN、没有 ORM。

## 项目简介

两条能力线：

- 导入：`python app.py --ingest screenshots/            # 开发过程截图与自查输出（开发记录的一部分）
samples/notes`，一次吞一个目录的 `.md`，逐篇报片段数和 `doc_id`，单篇坏文件不中断整批。
- 检索：`GET /api/search?q=...&k=8&tag=...`，优先走 seekdb 的混合检索（全文 + 向量，RRF 融合），后端不支持时自动回退纯向量，并在响应里标 `mode`。

出处可追溯靠的是切块时就把结构记进 metadata（`doc_id / title / heading / seq`），而不是检索后再猜：每个片段只从属于它自己的 `## 小节`，找不到小节才退回文档标题。

## Qoder 与 seekdb 的分工

| | 负责什么 |
| --- | --- |
| **Qoder** | 六步 prompt + 六条验收，逐步产出并当场验：切块规则、`ingest_document`、`search`、HTTP 层、单文件前端、命令行入口和自查脚本。踩到的坑也是在这一步修掉的——重叠对齐把 60 字符吃成空串、`seq` 该按小节还是按文档编号、hybrid 不可用时的静默空结果、连接失败必须冒泡成 500 而不是 `count=0`。 |
| **seekdb** | 一个引擎同时提供向量索引（HNSW + cosine）、全文索引（中文 IK 分词）和 `DBMS_HYBRID_SEARCH` 的 RRF 融合，外加 metadata 标量过滤。项目里不需要再单挂 Elasticsearch / Milvus，也不需要自己写融合排序。 |
| **pyseekdb 1.4.0.post1** | 官方 SDK，`Client()` 按参数返回嵌入式或服务端客户端；`hybrid_search` 内部拼 search_parm → `DBMS_HYBRID_SEARCH.GET_SQL` → 执行 SQL，返回值按查询双层嵌套。 |

## 运行环境

- Python 3.10+（实测 3.12.10）
- `fastapi==0.162.0`、`uvicorn==0.54.0`、`pyseekdb==1.4.0.post1`
- seekdb 服务端 ≥ 1.3（实测容器 `seekdb` = oceanbase/seekdb 1.4.0.0，监听 2881）。低于 1.3 时 `refresh_index()` 是 no-op，刚写入的片段可能查不到。
- 嵌入式模式只在 linux / mac-arm64 可用：`pylibseekdb` 的 wheel 平台标记限制了范围，Windows 上必须连服务端。
- 首次运行会下载默认 embedding 模型（all-MiniLM-L6-v2 ONNX，约 90MB，走 `HF_ENDPOINT`，默认 hf-mirror）。中断过就重跑一次，下载器按文件续传。
- Windows 控制台默认 GBK，`app.py` / `selfcheck.py` 已把 stdout 重配成 UTF-8；从外部脚本调用时建议再带 `PYTHONIOENCODING=utf-8`。

## 离线兜底（评审环境拿不到模型权重时）

默认使用 pyseekdb 内置的 ONNX 句向量模型（384 维），**首次运行会自动下载权重**（约 80MB）。
如果评审环境没有外网或 HuggingFace 不可达，加一个环境变量即可切到仓库内置的确定性哈希嵌入（256 维，完全离线）：

`ash
EMBEDDING_MODE=hash SEEKDB_HOST=127.0.0.1 SEEKDB_COLLECTION=noteseek_notes \
  python app.py --ingest samples/notes
EMBEDDING_MODE=hash SEEKDB_HOST=127.0.0.1 SEEKDB_COLLECTION=noteseek_notes python app.py
`

两种模式的向量维度不同（384 / 256），不能共用同一个集合，所以 EMBEDDING_MODE=hash 时
集合名会自动加 _hash 后缀；换模式记得重新导入一次数据。

## 从零运行步骤

```bash
# 1. 起 seekdb（已有一个能用的实例就跳过）
docker run -d --name seekdb -p 2881:2881 -p 2886:2886 oceanbase/seekdb:latest

# 2. 装依赖
pip install -r requirements.txt

# 3. 指连接；集合名用环境变量隔离，别和别人的 notes 撞
export SEEKDB_HOST=127.0.0.1:2881
export SEEKDB_COLLECTION=noteseek_samples

# 4. 导入示例笔记（4 篇 / 16 片段）
python app.py --ingest samples/notes --tag samples

# 5. 起服务
python -m uvicorn app:app --host 127.0.0.1 --port 8090

# 6. 浏览器打开
#    搜索台  http://127.0.0.1:8090/
#    展示页  http://127.0.0.1:8090/landing.html
#    接口    http://127.0.0.1:8090/api/search?q=回表和覆盖索引&k=5
```

自查与验收：

```bash
python selfcheck.py      # 连库跑 5 个问题，打印「命中的文档 / 出处小节 / 耗时」表，全部命中才返回 0
python acceptance.py     # 不连库，用内存集合验切块不变量（heading 归属、重叠、id 唯一、段号连续）
```

端口被占（`[Errno 10048]`）就换 8091，别去动占端口的进程。

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEEKDB_HOST` | 空 | 非空走服务模式，支持写成 `host:port` |
| `SEEKDB_PORT` | 2881 | `SEEKDB_HOST` 里带端口时以 `HOST` 为准 |
| `SEEKDB_TENANT` / `SEEKDB_DATABASE` | `sys` / `test` | |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` | `root` / 空 | |
| `SEEKDB_COLLECTION` | `notes` | 共享实例上换名隔离 |
| `SEEKDB_PATH` | `./data/seekdb.db` | 仅嵌入式模式用到 |

## 接口与数据模型

`GET /api/search` 响应：

```json
{
  "query": "回表和覆盖索引",
  "mode": "hybrid",
  "took_ms": 145.9,
  "count": 5,
  "results": [
    {
      "title": "索引结构笔记",
      "heading": "回表是什么",
      "snippet": "二级索引的叶子节点存的是索引列加主键值，不含其他列……",
      "source": "索引结构笔记 › 回表是什么 › 片段 #1",
      "distance": 0.0315
    }
  ]
}
```

- `mode` 只会是 `hybrid` 或 `vector`；`distance` 是 RRF 的排名倒数和，只能比名次，不能当置信度。
- 库连不上、SQL 报错一律冒泡成 HTTP 500，绝不用 `count=0` 糊过去——空结果和查不到是两件事。
- `tag` 非空时 `where={"tag": tag}` 会同时下推进全文路和向量路两个分支。

集合里每个片段：

| 字段 | 内容 |
| --- | --- |
| `id` | `sha1(标题)[:12]` + `#` + 文档内全局序号（4 位补零），重复导入是 upsert |
| `document` | `"标题 · 小节\n正文"`——小节标题拼进正文，让全文路和向量路都看得见它 |
| `metadata` | `doc_id` / `title` / `heading` / `seq` / `tag` / `chars` / `added_at` |

切块规则：段落边界优先、块间重叠 60 字符、单块上限 520 字符、代码围栏内不断块、`---` front matter 剥离、一级标题行本身不进正文、`seq` 按小节内从 1 计。

## 验证样例表（问题 → 预期 → 实测）

库内容：`samples/notes/` 4 篇示例笔记 = 16 片段，集合 `noteseek_samples`。

| 问题 | 预期命中 | 实测 Top1（文档 › 小节） | mode | 耗时 |
| --- | --- | --- | --- | --- |
| REPEATABLE READ 是怎么避免幻读的 | `01-事务与隔离级别.md` | 事务与隔离级别笔记 › REPEATABLE READ 怎么避免幻读 | hybrid | 760.5 ms |
| 为什么用 B+ 树而不是哈希索引 | `02-索引结构.md` | 索引结构笔记 › 为什么是 B+ 树 | hybrid | 147.8 ms |
| RRF 融合排序怎么算 | `03-RRF融合排序.md` | 混合检索融合排序笔记 › RRF 要解决什么 | hybrid | 213.5 ms |
| seekdb 的 Mock AI Service 是干什么的 | `04-seekdb-AI服务.md` | seekdb AI 服务笔记 › Mock AI Service 是干什么的 | hybrid | 143.0 ms |
| 回表和覆盖索引 | `02-索引结构.md` | 索引结构笔记 › 回表是什么 | hybrid | 145.9 ms |

5 题 Top1 全部落在预期笔记，没有一题串到别的文档。耗时说明：第一条带模型冷启动（760 ms，纯进程首调用实测到过 1540 ms），进程热了以后稳定在 140–210 ms。原始输出存成一张表在 `screenshots/step6-selfcheck.txt`。

## 已知限制

1. **中文语义路偏弱。** 默认 embedding 是英文 all-MiniLM-L6-v2（384 维，且 `dimension` 是 SDK 建集合时定死的），中文查询主要靠全文路和 RRF 兜住。换中文模型必须连维度一起改并整库重建，`get_collection()` 因此刻意不带隐式参数。
2. **只有命令行导入。** 没有 `POST /api/ingest`，网页端不能投稿。
3. **重导入会留孤儿。** `upsert` 让同一片段幂等，但小节改名或删段后，旧片段不会被清掉，需要先按 `doc_id` 删再写。
4. **混合检索可能静默为空。** pyseekdb 里 `DBMS_HYBRID_SEARCH.GET_SQL` 没返回 SQL 时，SDK 直接给 `ids: [[]]`，"没命中"和"索引没建好"从接口上分不出来。
5. **`tag` 是单值标量等值过滤**，多标签、模糊匹配都得另设计。
6. **`snippet` 截 160 字符**，看整段要拿 `doc_id` + `seq` 回查，前端没做这个跳转。
7. **`seq` 在小节内会重号**，所以出处必须带小节名才唯一（`文档 › 小节 › 片段 #1` 在多个小节下都存在）。
8. **无鉴权、无分页、无并发写治理**，单机演示用。共享 seekdb 实例上集合名会撞车，务必用 `SEEKDB_COLLECTION` 隔离。

## 数据来源声明

- `samples/notes/` 与 `data/notes/` 里的笔记是本项目自撰的教学性说明，目的是给检索造一批可验证的语料，**不是 seekdb / OceanBase 官方文档，也不构成产品配置依据**；落地前请以官方文档核对。
- 其中关于 AI 函数的说法（`04-seekdb-AI服务.md`）来自对本机 seekdb 1.4.0.0 实例的实测：`select ai_generate('hi')` 报 `1305 FUNCTION ai_generate does not exist`；`ai_embed('model','text')` 报 `ai model not found`；`oceanbase.__all_ai_model` 与 `oceanbase.__all_ai_model_endpoint` 存在且为空。换版本需要重测。
- 全文索引 / IK 分词（`ik_smart`、`ik_max_word`）与 `rank_window_size`、`rank_constant` 的说法依据 pyseekdb 1.4.0.post1 的 SDK 签名和 seekdb 官方文档。
- 展示页截图 `static/assets/shot-search-hybrid.png` 是本地实测页面（8 条命中、hybrid、耗时 329.3 ms），无外部数据。

## 目录结构

```
app.py                    后端：连接 / 切块 / 导入 / 检索 / HTTP + 命令行入口
acceptance.py             离线验收：内存集合，验切块不变量
selfcheck.py              连库自查：5 个问题打印命中表
requirements.txt          fastapi / uvicorn / pyseekdb
static/index.html         搜索台
static/landing.html       作品展示页
static/assets/            展示页截图
samples/notes/*.md        4 篇示例笔记（本次自查语料）
data/notes/*.md           3 篇开发期笔记（集合 noteseek_notes，tag=cs101）
screenshots/              每步验收记录
```
