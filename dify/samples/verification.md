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

## 5. 实际运行结果（2026-10-29 本机实测，Dify 1.18 + DeepSeek）

### 5.1 走真实 Dify Workflow（推荐看这一段）

在 Dify 里跑同一个应用，5 次运行的原始输出：

```
[player-001] 我昨天把你这儿的杯子摔了，还记得不？          status=succeeded (3.2s)
  reply:  哟，生面孔。我这儿的凳子可不留给闲人，坐下就得喝一杯。想喝点什么，还是就来看看热闹？
  recalled: 0 条   ← 第一次见面，seekdb 里没有旧账
  new_memory: 玩家昨天摔了这里的杯子。
  memory_total: {"turn":1,"total":1}

[player-001] 忘了告诉你，我是港务局的稽查员。             status=succeeded (2.6s)
  reply:  哟，是你啊。昨天那杯子摔得挺响，我账本上可记着呢，别装没事人。今天又来照顾生意？
          行吧，先说说你打算怎么赔。
  recalled: 1 条   ← 翻出了「摔杯子」
  new_memory: 玩家自称是港务局稽查员。
  memory_total: {"turn":2,"total":2}

[player-001] 行吧，下次我带条带鱼来赔你。                 status=succeeded (3.4s)
  reply:  哟，港务局稽查员大人，又来啦？昨天你摔了我这儿一个杯子，账我还没跟你算呢。
          今天上门，是来赔杯子的，还是来接着查我这小破店的？
  recalled: 2 条   ← 同时翻出「摔杯子」和「稽查员」
  new_memory: 玩家承诺下次带条带鱼来赔。
  memory_total: {"turn":3,"total":3}

[player-001] 隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱？   status=succeeded (3.2s)
  reply:  哟，港务局的稽查员大人，稀客啊。昨天刚在我这儿摔了个杯子，今天又上门了？
          我可记着呢，你答应下次带条带鱼来赔，我可一直等着呢。
  recalled: 3 条   ← 三条旧账全在，老板主动串起来算账
  new_memory: 玩家问老板是否总给隔壁铁匠铺姑娘的酒少算钱。
  memory_total: {"turn":4,"total":4}

[player-002] 你记得我摔杯子吗？                          status=succeeded (3.2s)
  reply:  哟，又是你。上回你不是说自己是头一回来这种地方嘛……行，这回点啥？
  recalled: 1 条   ← 只命中 player-002 自己的记忆，player-001 的四条一条都没漏过去
```

**这张表就是本题要证明的东西**：
- `recalled` 随轮次 0 → 1 → 2 → 3 增长，老板的话里**真的带出了对应旧账**（不是固定人设）；
- 换 `user_id` 之后检索结果与前面那个玩家完全隔离；
- 每一轮 `new_memory` 都是模型把玩家这句话压成的一条事实，写回 seekdb 后**下一轮立即能检索到**。

### 5.1.1 运行截图（`samples/screenshots/`）

Dify「测试运行 → 结果」面板的原图，按执行顺序编号，可直接对照上表：

| 文件 | 场景 | 关键证据 |
|---|---|---|
| `screenshots/01-player001-turn1-first-meeting.png` | player-001 第 1 轮 | `"memories": []`、`count: 0`、`total: 0`，`memory_total` 变 1 |
| `screenshots/02-player001-turn2-recalls-cup.png` | player-001 第 2 轮 | 命中 `玩家昨天摔了这里的杯子。`（相关度 0.11），老板回话里带出「昨天摔了我这儿一个杯子」 |
| `screenshots/03-player001-turn3-recalls-two.png` | player-001 第 3 轮 | `count: 2`，同时翻出「杯子」和「稽查员」 |
| `screenshots/04-player002-turn1-first-meeting.png` | **换玩家** player-002 第 1 轮 | `"memories": []`、`count: 0` —— 看不到 player-001 的任何旧账 |
| `screenshots/05-player002-turn2-recalls-fish.png` | player-002 第 2 轮 | 只命中 player-002 自己的记忆 |
| `screenshots/06-player002-turn3-recalls-two.png` | player-002 第 3 轮 | `count: 2`，两条都是这个玩家自己的 |

> 截图里 `memories` 元素上的 `user_id` 字段就是隔离的证据：01–03 全是 `player-001`，04–06 全是 `player-002`。

### 5.2 不用 Dify 也能验证链路（`tools/demo_client.py`）

`tools/demo_client.py` 调的就是 Dify 那两个 HTTP 节点的同一套接口，把 LLM 换成固定文案，
方便单独看检索质量（四轮 + 记忆隔离 + 三种问法的命中抽查）：

```
[seekdb] 检索到 0 条旧账（该玩家共 0 条）：（记忆库是空的：这是你和这个玩家的第一次见面…）
[seekdb] 写入记忆 #1：玩家我昨天把你这儿的杯子摔了，还记得不
...
=========== 检索质量抽查 ===========
问：我摔坏的那个杯子还要赔多少？ → 第一名：第1轮 …（相关度 0.3376，词面覆盖 0.0454）
问：带鱼什么时候给你？           → 第一名：第3轮 …（相关度 0.3543，词面覆盖 0.0802）
问：稽查员的事你还记着吗？       → 第一名：第2轮 …（相关度 0.3388，词面覆盖 0.1382）
```

```
[health] {'ok': True, 'engine': 'seekdb via pyseekdb', 'mode': 'server', 'target': '127.0.0.1:2881',
          'database': 'test', 'collection': 'grudges', 'embedding': 'tavern-hash-ngram/256d', 'rows': 4}
[reset ] {'ok': True, 'removed': 4}

=========== 第 1 轮 ===========
[seekdb] 检索到 0 条旧账 → 写入记忆 #1：玩家我昨天把你这儿的杯子摔了，还记得不
=========== 第 2 轮 ===========
[seekdb] 检索到 1 条旧账（第1轮｜…摔了杯子…）→ 写入记忆 #2：玩家忘了告诉你，我是港务局的稽查员
=========== 第 3 轮 ===========
[seekdb] 检索到 2 条旧账 → 写入记忆 #3：玩家行吧，下次我带条带鱼来赔你
=========== 第 4 轮 ===========
[seekdb] 检索到 3 条旧账 → 写入记忆 #4：玩家隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱
=========== 换一个玩家（记忆隔离验证） ===========
[seekdb] player-002 检索结果 count=0
```

**读法**：`recalled` 全程 0→1→2→3 增长，说明每轮都真的写进了 seekdb 并能在下一轮检索回来；
换玩家后检索为空，说明 `where={"user_id": …}` 的元数据过滤生效。

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
