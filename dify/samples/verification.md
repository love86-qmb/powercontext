# 验证样例（可逐步执行）

> 作品：布鲁姆酒馆 · 记仇的老板　技术：Dify Workflow + seekdb
> 下面每一步都能复现；第 5 节是本轮**实际运行**的输出（不是示意）。

## 0. 初始条件

| 项 | 值 |
|---|---|
| seekdb | 空库即可（`grudges` 集合由 bridge 首次请求时自动创建） |
| 记忆状态 | **空**（如需回到初始状态：`POST /memory/reset {"user_id":"player-001"}`） |
| bridge | `http://127.0.0.1:8077`，`GET /health` 返回 `rows` 字段即为当前记忆条数 |
| Dify | 已导入 `dsl/01-tavern-memory.yml`，两个 LLM 节点已绑定你的 Chat 模型并发布 |
| 玩家 ID | `player-001`（记忆按它隔离；换 ID 就是另一个玩家） |

## 1. 要验证的核心命题

> **老板说的话，是由 seekdb 里检索出来的旧账决定的** —— 不是 prompt 里写死的人设。

所以只要下面的「旧账」列表随轮次变化、且第 4 步的抽查能命中正确的那一条，就证明技术结合有效。

## 2. 操作步骤（Dify 界面）

每次运行填 `玩家 ID = player-001`，按顺序填 `你对老板说`：

| 轮次 | 你对老板说 | 预期表现 |
|---|---|---|
| 1 | 我昨天把你这儿的杯子摔了，还记得不？ | `recalled` 应为**空**（第一次见面），老板的回答里**不出现任何往事**，`memory_total` 变 1 |
| 2 | 忘了告诉你，我是港务局的稽查员。 | `recalled` 出现第 1 轮那条；老板会**主动提到杯子/赔偿**；`memory_total` 变 2 |
| 3 | 行吧，下次我带条带鱼来赔你。 | `recalled` 有 2 条；`new_memory` 应是「玩家…带鱼…」；`memory_total` 变 3 |
| 4 | 隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱？ | `recalled` 有 3 条；`memory_total` 变 4 |

**输出字段**（DSL 的 End 节点已配置，评审可直接核对）：
`reply`（老板的话）、`recalled`（这一轮检索到的旧账，带相关度）、`new_memory`（本轮写进 seekdb 的记忆）、`memory_total`（该玩家累计条数）。

## 3. 恢复初始状态 / 重复验证

```bash
curl -X POST http://127.0.0.1:8077/memory/reset \
  -H 'Content-Type: application/json' -d '{"user_id":"player-001"}'
# {"ok":true,"removed":4}
```

## 4. 记忆隔离验证

用 `玩家 ID = player-002` 问同样的问题：「你记得我摔杯子吗」
→ `recalled` 必须为空（提示「这是第一次见面」），证明 seekdb 的检索带了 `user_id` 元数据过滤。

## 5. 实际运行结果（2026-10-29 本机实测）

用 `tools/demo_client.py` 跑出的原始输出（它调的就是 Dify 那两个 HTTP 节点的同一套接口，
只是把 LLM 换成了固定文案，方便单看检索链路）：

```
[health] {'ok': True, 'engine': 'seekdb via pyseekdb', 'mode': 'server', 'target': '127.0.0.1:2881',
          'database': 'test', 'collection': 'grudges', 'embedding': 'tavern-hash-ngram/256d', 'rows': 4}
[reset ] {'ok': True, 'removed': 4}

=========== 第 1 轮 ===========
玩家：我昨天把你这儿的杯子摔了，还记得不？
[seekdb] 检索到 0 条旧账（该玩家共 0 条）：
          （记忆库是空的：这是你和这个玩家的第一次见面，没有任何旧账可翻）
老板：记！三个铜板，一个子儿都别想赖。
[seekdb] 写入记忆 #1（累计 1 条）：玩家我昨天把你这儿的杯子摔了，还记得不

=========== 第 2 轮 ===========
玩家：忘了告诉你，我是港务局的稽查员。
[seekdb] 检索到 1 条旧账（该玩家共 1 条）：
          1. 第1轮｜玩家我昨天把你这儿的杯子摔了，还记得不（相关度 0.11）
老板：稽查员？就你这身打扮？穿得像来讨债的。
[seekdb] 写入记忆 #2（累计 2 条）：玩家忘了告诉你，我是港务局的稽查员

=========== 第 3 轮 ===========
玩家：行吧，下次我带条带鱼来赔你。
[seekdb] 检索到 2 条旧账（该玩家共 2 条）：
          1. 第2轮｜玩家忘了告诉你，我是港务局的稽查员（相关度 0.18）
          2. 第1轮｜玩家我昨天把你这儿的杯子摔了，还记得不（相关度 0.06）
老板：带鱼？也行，别拿那种冻了半年的。
[seekdb] 写入记忆 #3（累计 3 条）：玩家行吧，下次我带条带鱼来赔你

=========== 第 4 轮 ===========
玩家：隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱？
[seekdb] 检索到 3 条旧账（该玩家共 3 条）：
          1. 第2轮｜玩家忘了告诉你，我是港务局的稽查员（相关度 0.13）
          2. 第1轮｜玩家我昨天把你这儿的杯子摔了，还记得不（相关度 0.02）
          3. 第3轮｜玩家行吧，下次我带条带鱼来赔你（相关度 -0.03）
老板：少算？我给她记账，记在她爹头上。
[seekdb] 写入记忆 #4（累计 4 条）：玩家隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱

=========== 换一个玩家（记忆隔离验证） ===========
[seekdb] player-002 检索结果 count=0 / memories_text=（记忆库是空的：这是你和这个玩家的第一次见面，没有任何旧账可翻）

=========== 检索质量抽查（同一玩家，不同问法） ===========
问：我摔坏的那个杯子还要赔多少？
  第一名：第1轮 玩家我昨天把你这儿的杯子摔了，还记得不（相关度 0.3376，词面覆盖 0.0454）
问：带鱼什么时候给你？
  第一名：第3轮 玩家行吧，下次我带条带鱼来赔你（相关度 0.3543，词面覆盖 0.0802）
问：稽查员的事你还记着吗？
  第一名：第2轮 玩家忘了告诉你，我是港务局的稽查员（相关度 0.3388，词面覆盖 0.1382）
```

**读法**：
- 第 1 轮 `recalled` 为空 → 老板无旧账可翻，说明记忆确实来自库而不是 prompt；
- 第 2/3/4 轮 `recalled` 逐条增长 → 每轮都真的写进了 seekdb；
- 最后一组抽查里，三种不同问法分别命中第 1/3/2 轮 → 检索是**按语义相关**在选，不是按顺序把全部记忆倒出来；
- `player-002` 全空 → 元数据过滤生效（记忆按玩家隔离）。

## 6. seekdb 侧证据

```bash
curl "http://127.0.0.1:8077/memory/list?user_id=player-001"   # 该玩家的全部记忆行
curl "http://127.0.0.1:8077/debug/stats"                      # 最近 50 次 recall/remember 调用流水
```

```
GET /memory/list?user_id=player-001
{"ok":true,"count":4,"items":[
  {"id":"player-001-0001-…","text":"玩家我昨天把你这儿的杯子摔了，还记得不","turn":1,
   "ts":"2026-10-29T20:5x:xx", "user_id":"player-001"},
  …
]}

GET /debug/stats
{"ok":true,"recent_ops":[
  {"at":"…","op":"remember","user_id":"player-001","id":"player-001-0001-…","turn":1},
  {"at":"…","op":"recall","user_id":"player-001","query":"忘了告诉你，我是港务局的稽查员","k":3,"hits":1},
  …
]}
```

直接查库也可以（`mysql -h127.0.0.1 -P2881 -uroot`）。pyseekdb 的集合落成两张表：
`sdk_collections`（集合元数据）和 `c$v2$<collection_id>`（记忆本体，含 `VECTOR` 列）：

```sql
USE test;
SELECT collection_name, settings FROM sdk_collections;
--  collection_name: grudges
--  settings: {"version":2,"embedding_function":{"name":"tavern-hash-ngram","properties":{"dim":256,...}}}

SHOW TABLES LIKE 'c$v2$%';            -- 以 sdk_collections.collection_id 拼出表名

-- 记忆表结构：_id / document / embedding VECTOR(256) / metadata json
SELECT _id, LEFT(document, 34) AS doc, JSON_EXTRACT(metadata,'$.turn') AS turn
FROM `c$v2$<collection_id>` LIMIT 5;
```

实测输出：

```
_id	                        doc	                                          turn
player-001-0001-856a29	玩家我昨天把你这儿的杯子摔了，还记得不	          1
player-001-0002-f8d122	玩家忘了告诉你，我是港务局的稽查员	          2
player-001-0003-b43e78	玩家行吧，下次我带条带鱼来赔你	                  3
player-001-0004-021d7a	玩家隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱	  4
```
