# Qoder 开发记录（NoteSeek）

> **Qoder 账号 UID：`01a18a8d-2f61-7a38-b721-8b047d601e75`**
> 开发时间：2026-10-30 · 形式：Qoder 智能体对话（自然语言下发任务 → 它调工具、写代码、跑自测）
> 本文按「任务 → Qoder 做了什么 → 产出文件 → 证据」整理，截图都在 `screenshots/`。

## 0. 一句话

项目**主要开发工作全部在 Qoder 中完成**：需求拆解、接口与数据模型设计、切块与入库、
hybrid_search 接入与回退、前端页面、示例数据、README 与展示页，均由 Qoder 生成并在本机跑通验证。
项目网站通过 **Qoder Sites** 发布。

## 1. 六个开发环节

| # | 下发给 Qoder 的任务 | Qoder 实际做的事 | 产出 | 证据 |
|---|---|---|---|---|
| 1 | 「先不写代码，出目录结构 / 接口字段 / 数据模型 / 最容易踩的 3 个坑」 | 给出 7 段目录方案、`/api/ingest` 与 `/api/search` 的完整字段表、seekdb collection 的 schema（`hnsw_cosine/384维`、`fulltext_strategy=ngram`、metadata 七字段），并预判三个坑：**维度与 embedding 绑定**、**小块不可重叠**、**hybrid 不能把排序真相交给候选数** | 设计稿 | `devlog-01-plan-and-interfaces.jpg` |
| 2 | 「实现 `chunk_markdown`（≤520 字符、重叠 60、每片记住所属 `## 小节`）+ `ingest_document` + 两种连接模式」 | 先核对 pyseekdb 的 `Client` 签名再动手；发现**重叠尾巴把标签吃掉**的顺序缺陷并修掉（`_overlap_prefix` 对齐句末、失败回退原尾）；写一次性不变量脚本验证（长度上限、重叠、小节归属、围栏不切断），再补一轮参数与分支测试 | `app.py` 前半 | `devlog-02-chunk-and-ingest.png` |
| 3 | 「实现 `search()`：优先 `hybrid_search`（全文 `$contains` + knn + RRF），不支持时回退向量并标注 mode，失败必须 500」 | 先读本机 seekdb 实例的真实参数校验 `where` 下推写法；把 `where` 显式分给两路（否则后端只取一路的 where）；**停库验证**接口返回 500 而不是空列表；验证 `tag=不存在的标签 → count=0` 说明两路都过滤了；补一轮回归验证「分支真的能走到」而不只是代码里写着 | `app.py` `search()` | `devlog-03-search-hybrid.png` |
| 4 | 「写 `static/index.html`：单文件、无框架无 CDN、回车可搜、结果带出处、空结果与请求失败要有提示、DOM 全部转义」 | 写完发现**回车不触发**（合成事件隐式提交不可靠）→ 改为显式 `keydown` 监听 + IME 组合态判断；用 stub fetch 验证三种状态（空结果 / 500 / 转义 `img onerror` + `script` 注入）；把截图存进 `screenshots/` | `static/index.html` | `devlog-04-frontend.png`、`step4-search-hybrid.png` |
| 5 | 「放 4 篇示例笔记 + 加 `--ingest` 批导 + 用 5 个问题自查命中」 | 先确认本机 seekdb 的 Mock AI Service 真实形态，避免把示例笔记写成想当然的内容；导入 4 篇 / 16 片段；5 个问题**全部 top1 命中预期文档**（见下表），并用 RRF 分数核验融合确实发生 | `samples/notes/*.md`、`selfcheck.py` | `devlog-05-sample-data-and-selfcheck.png`、`step6-selfcheck.txt` |
| 6 | 「写 README（含分工/环境/步骤/验证样例/限制/来源）+ `static/landing.html` 展示页」 | 产出 7 个板块的 README 与 7 个 section 的展示页；自查 `landing.html` 的 DOM 配对、锚点 id、两处本地图片可加载；发现 `screenshots/` 不在 `StaticFiles` 挂载范围内，改为把截图复制到 `static/assets/` | `README.md`、`static/landing.html` | `devlog-06-readme-and-landing.png` |

## 2. 步骤 5 的自查结果（Qoder 自己跑出来的）

| # | 问题 | 命中文档 | 出处小节 | 判定 |
|---|---|---|---|---|
| 1 | REPEATABLE READ 是怎么避免幻读的 | 事务与隔离级别笔记 | REPEATABLE READ 怎么避免幻读 | ✅ |
| 2 | 为什么用 B+ 树而不是哈希索引 | 索引结构笔记 | 为什么是 B+ 树 | ✅ |
| 3 | RRF 融合排序怎么算 | 混合检索融合排序笔记 | RRF 要解决什么 | ✅ |
| 4 | seekdb 的 Mock AI Service 是干什么的 | seekdb AI 服务笔记 | Mock AI Service 是干什么的 | ✅ |
| 5 | 回表和覆盖索引 | 索引结构笔记 | 回表是什么 | ✅ |

原始记录：`screenshots/step6-selfcheck.txt`。

## 3. 提交前的人工改动（如实记录）

Qoder 完成开发后，提交前由参赛者手工改了 3 处，**都不涉及核心逻辑**，在此说明以免核对时产生误解：

| 改动 | 原因 |
|---|---|
| `static/landing.html` 里 3 处仓库链接占位 `github.com/your-name/noteseek` → 真实仓库地址 | 发布前必须替换（Qoder 在步骤 6 就提示过这点） |
| `app.py` 增加 `EMBEDDING_MODE=hash` 的离线兜底（+ `embedding.py`） | Qoder 版用 SDK 默认 ONNX 嵌入（384 维），**首次运行要联网下载权重**。评审环境可能无外网，故补一个本地确定性哈希嵌入（256 维）作为兜底；维度不同，集合名自动加 `_hash` 后缀。README 已加「离线兜底」小节 |
| 删除开发早期生成的 `data/notes/`（3 篇草稿笔记） | 已被 `samples/notes/` 的 4 篇正式示例取代，避免仓库里两套数据打架 |

## 4. 复现开发过程的方式

1. 按 `README.md`「从零运行步骤」起 seekdb、装依赖、导入示例笔记、启动服务；
2. `python selfcheck.py` 复跑步骤 5 的自查（5/5 返回 0）；
3. `python acceptance.py` 复跑验收（15 片段 / 3 篇 / 无一挂在文档大标题上）；
4. 浏览器打开 `http://127.0.0.1:8090/` 与 `/landing.html`。

## 5. 截图清单

| 文件 | 内容 |
|---|---|
| `screenshots/devlog-01-plan-and-interfaces.jpg` | 步骤 1：Qoder 输出目录结构、接口字段、数据模型与三个坑 |
| `screenshots/devlog-02-chunk-and-ingest.png` | 步骤 2：切块与入库实现、重叠顺序缺陷定位与修复、不变量验证 |
| `screenshots/devlog-03-search-hybrid.png` | 步骤 3：hybrid_search 校验、停库测 500、tag 下推、回归验证 |
| `screenshots/devlog-04-frontend.png` | 步骤 4：前端实现、回车/IME 修复、三种状态验证、截图归档 |
| `screenshots/devlog-05-sample-data-and-selfcheck.png` | 步骤 5：示例数据导入与 5 题自查结果 |
| `screenshots/devlog-06-readme-and-landing.png` | 步骤 6：README 与展示页产出、DOM 与静态资源自查 |
| `screenshots/step4-search-hybrid.png` | 检索界面实际运行（命中 8 条 · 向量 + 全文混合 RRF · 329.3ms） |
| `screenshots/step6-selfcheck.txt` | 自查实测输出（top1 命中表） |
| `screenshots/app-result-btree.png` | 页面实测：问「为什么用 B+ 树而不是哈希索引」 |
| `screenshots/app-result-isolation.png` | 页面实测：问「REPEATABLE READ 是怎么避免幻读的」 |
