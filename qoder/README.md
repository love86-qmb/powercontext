# NoteSeek · 课程与文献速查台

> 赛题：Build with Qoder——用 Qoder 做一个让学习、科研和工作更高效的项目
> 技术选型：**Qoder（开发 + Qoder Sites 发布）+ seekdb（向量与全文混合检索）**

## 1. 解决什么问题

学生和做项目的人都有同一个痛点：**资料是散的**。课程笔记在 Markdown 里、论文摘录在 PDF 里、
比赛文档在几十个网页里。想找"上次那个隔离级别是怎么说的"，只能靠记忆里的关键词去一个个文件里翻，
翻到了也说不清它出自哪一页。

NoteSeek 做的就是这件事：**把资料丢进去，用一句话问回来，并且告诉你它出自哪一篇、哪一小节、第几段。**

## 2. Qoder 与 seekdb 的分工

| 组成 | 在项目里干的活 |
|---|---|
| **Qoder** | 项目的主要开发工作：需求拆解、生成 FastAPI 后端与前端页面、切块与检索逻辑、调试（见 `QODER-LOG.md`），并通过 **Qoder Sites** 发布项目网站 |
| **seekdb** | 核心检索能力：每个片段一行（向量 + 全文索引 + 元数据），`/api/search` 走 **向量 + 全文的混合检索（RRF 融合）**，并按 `tag` 做元数据过滤；写入后 `refresh_index()` 立即可查 |

**为什么必须用 seekdb，而不是把所有笔记塞进 prompt**：笔记会越攒越多，不能每次全塞；
而且"专有名词/型号/缩写"靠向量不可靠、"同义改写"靠全文不可靠——所以用 seekdb 的混合检索
（向量路 + 全文路，RRF 按排名融合），这也正是 seekdb 与普通向量库的区别。

## 3. 目录结构

```
qoder/
├── app.py                # FastAPI：切块入库、混合检索、静态页
├── embedding.py          # 零依赖的本地嵌入函数（避免评审时下载模型）
├── static/
│   ├── index.html        # 检索界面（本地运行时的入口）
│   └── landing.html      # 作品展示页（Qoder Sites 发布用）
├── samples/
│   └── notes/*.md        # 4 篇示例笔记（课程 3 篇 + 比赛笔记 1 篇）
├── README.md             # 本文件：运行说明 + 验证样例
└── requirements.txt
```

## 4. 运行环境

| 项 | 要求 |
|---|---|
| Python | 3.10+ |
| seekdb | `oceanbase/seekdb:latest`（本文实测 1.4.0.0，Docker 单机） |
| pyseekdb | `1.4.0.post1`（`requirements.txt` 已锁定） |
| 嵌入服务 | **不需要**：本地哈希嵌入（256 维，`embedding.py`），完全离线可复现 |
| 浏览器 | 任一新版浏览器（检索界面是单文件 HTML） |

## 5. 从零跑起来

```bash
# 1) 起 seekdb
docker run -d --name seekdb -p 2881:2881 -p 2886:2886 \
  -v /your/data/seekdb:/var/lib/oceanbase oceanbase/seekdb:latest

# 2) 装依赖
cd qoder
python -m pip install -r requirements.txt

# 3) 灌入示例笔记（4 篇 / 13 个片段）
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_DB=test \
  python app.py --ingest samples/notes

# 4) 起服务
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_DB=test \
  python app.py --port 8090
```

打开 **http://127.0.0.1:8090** 就是检索界面。

> Windows / macOS 必须用 `SEEKDB_HOST` 这种服务模式（`pylibseekdb` 嵌入式库只有 Linux 版）。
> 不设 `SEEKDB_HOST` 时走嵌入式本地库（`SEEKDB_PATH`，默认 `./seekdb_data`）。

## 6. 验证样例（可逐步执行）

**初始状态**：`GET /health` 返回 `{"chunks": 13}`（灌完 4 篇示例笔记后）。

| # | 输入 | 预期 | 实际（本机实测） |
|---|---|---|---|
| 1 | `REPEATABLE READ 是怎么避免幻读的` | 命中「01-数据库事务与隔离级别」，出处指向**四种隔离级别**小节 | ✅ `01-… › 四种隔离级别 › 片段 #2`，hybrid 301ms |
| 2 | `为什么用 B+ 树而不是哈希索引` | 命中「02-B+树与索引优化」 | ✅ `02-… › B+ 树与索引优化 › 片段 #1`，hybrid 14.6ms |
| 3 | `RRF 融合排序怎么算` | 命中「03-向量检索与HNSW」 | ✅ 命中该篇（`CAGRA 与 GPU 建图` 小节内含 RRF 段落） |
| 4 | `seekdb 的 Mock AI Service 是干什么的` | 命中「04-OceanBase比赛笔记」 | ✅ `04-… › seekdb 赛道要点 › 片段 #2`，hybrid 99.5ms |
| 5 | `回表和覆盖索引` | 命中「02-B+树与索引优化」，出处指向**聚簇索引/EXPLAIN** 相关小节 | ✅ `02-… › EXPLAIN 要看的字段 › 片段 #3` |
| 6 | `脏读和不可重复读的区别` | 命中「01-数据库事务与隔离级别」 | ✅ 第 1 条即该篇首段 |

**seekdb 起作用的证据**（三种，任选其一核对）：

1. **接口返回里带检索方式与耗时**：`{"mode":"hybrid","took_ms":301.2,...}` —— `hybrid` 说明走的是
   seekdb 的向量 + 全文混合检索，而不是内存里遍历。
2. **每条结果的出处**：`source` 字段（`文档 › 小节 › 片段 #n`）来自 seekdb 里存的元数据
   （`title / heading / seq / tag`），不是从全文重新切的。
3. **直接查库**：`GET /health` 的 `chunks` 计数，或用 SQL 看片段（见下）。

```bash
curl "http://127.0.0.1:8090/health"
# {"ok":true,"collection":"notes","chunks":13,"embedding":"tavern-hash-ngram"}

curl "http://127.0.0.1:8090/api/search?q=RRF%20%E8%9E%8D%E5%90%88%E6%8E%92%E5%BA%8F&k=2"
```

```sql
-- seekdb 里（mysql -h127.0.0.1 -P2881 -uroot）
USE test;
SELECT collection_name, settings FROM sdk_collections;     -- 集合元数据（维度 / 嵌入函数）
SHOW TABLES LIKE 'c$v2$%';                                 -- 片段表：_id / document / embedding VECTOR(256) / metadata json
```

## 7. 已知限制

1. 只支持 Markdown / 纯文本；PDF 要先自己转成文本（比赛笔记里那篇就是从 PDF 摘的）。
2. 单机单库、没有账号体系：`tag` 字段预留了"按课程/项目分库"的能力，多用户需要再加一层隔离。
3. 本地哈希嵌入对**同义不同词**（"隔离级别" vs "isolation level"）不敏感，换成真实句向量模型会明显变好。
4. 全文路的关键词提取是启发式的（取查询里最长的英文词 / 中文片段），极端 query 下融合排序会偏。
5. `collection()` 在进程内缓存句柄：**重建集合后必须重启服务**，否则会拿着旧表名报 1146。

## 8. 数据与来源声明

- `samples/notes/` 四篇笔记为本次参赛自写（内容依据公开教材与官方文档整理），不含他人版权内容。
- 仓库内不含任何 API Key 或凭据；`embedding.py` 的哈希嵌入为自写实现。
- seekdb 官方镜像与 `pyseekdb` 的使用见各自仓库许可。
