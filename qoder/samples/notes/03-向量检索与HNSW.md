# 向量检索与 HNSW（比赛 + 自学笔记）

## 向量检索基本流程

1. **嵌入 embedding**：把文本/图片编码成定长浮点向量，语义相近的向量距离近。
2. **建索引**：暴力检索（flat）精确但 O(n) 太慢；近似最近邻（ANN）用图、量化或哈希把复杂度降到次线性。
3. **检索**：把查询也编码成向量，取 Top-k 后按距离排序返回。
4. **评价**：Recall@k（召回率）、QPS、P95 延迟。

## 距离度量

- **L2 欧氏距离**：向量未归一化时常用，`l2_distance`。
- **内积 IP**：归一化后等价于余弦相似度，语义检索常用。
- 归一化很重要：很多模型输出未归一化，混用 L2 和余弦会得到不同的排序。

## HNSW 的直觉

HNSW（Hierarchical Navigable Small World）是**分层可导航小世界图**：

- 上层稀疏、下层稠密，像跳表一样先粗后细地逼近；
- 每层是一个近邻图，检索时从入口点贪心走向离查询更近的邻居；
- 关键参数：`M`（每层邻居数，越大越准也越占内存）、`efConstruction`（建图时的候选队列大小）、`efSearch`（检索时的候选队列大小，越大召回越高越慢）。

**为什么建索引慢**：每个新插入的点都要在多层里做近邻搜索并连边，还要做邻居裁剪，本质上是 O(n·M·log n) 次距离计算，而且随机访问多、缓存不友好。这就是「GPU 加速 HNSW 构建」这类赛题的由来。

## CAGRA 与 GPU 建图

CAGRA 是 NVIDIA cuVS / AMD hipVS 里的 GPU 图索引：把密集距离计算搬到 GPU 上，建图和查询都走 GPU。它和 HNSW 的目标一致（可导航图 + 近邻贪心），但建图阶段高度并行化。

在 seekdb 里可以用 SQL 建 GPU 索引：

```sql
CREATE VECTOR INDEX idx_vec ON docs(embedding)
  WITH (distance=l2, type=hnsw, lib=cuvs);
SELECT id FROM docs ORDER BY l2_distance(embedding, :q) APPROXIMATE LIMIT 10;
```

`lib=vsag` 是 CPU 路径，`lib=cuvs` 是 GPU（hipVS）路径。计时口径容易踩坑：**第一条查询的惰性建图必须算进构建时间**，否则性能数字是假的。

## 向量 + 全文混合检索

纯向量对**专有名词、型号、罕见缩写**不敏感（嵌入模型没见过就编码得很随意）；纯全文对**同义改写**不敏感。所以生产上常用混合检索：

- 两路各自召回（向量 Top-N + BM25/全文 Top-N）；
- 用 **RRF（Reciprocal Rank Fusion）** 融合排序：`score = Σ 1/(k + rank_i)`，`k` 一般取 60；
- 或者用加权分数融合，但要先做分数归一化。

RRF 的好处是不需要两路分数可比，只看排名，鲁棒性好。

## 常见工程坑

- 写完数据要 **refresh index**：异步建索引的库（含 seekdb/pyseekdb）刚写入时查不到，容易误判为 bug。
- 元数据过滤要**下推到检索层**（`where={"user_id": ...}`），不要取回来再在内存里过滤，否则 top-k 会被别人占满。
- 分块策略比模型更影响效果：块太大稀释相关性，块太小丢上下文；把小节标题拼进被向量化的文本通常能明显提升命中。
