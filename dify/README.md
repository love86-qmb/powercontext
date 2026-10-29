# 布鲁姆酒馆 · 记仇的老板（Dify × seekdb）

> 赛题：Just for Fun——用 Dify 做一个好玩的智能应用
> 技术选型：**Dify（流程编排）+ seekdb（记忆存储与语义检索）**

## 1. 作品是什么

一个**会记仇的酒馆 NPC**。你跟老板布鲁姆说话，他会把你干过的每件事都记进 seekdb；
下一轮（甚至换一次会话、隔一天再来）他都能**主动把旧账翻出来**——

> 玩家：我昨天把你这儿的杯子摔了，还记得不？
> 布鲁姆：记！三个铜板，一个子儿都别想赖。
> ……
> 玩家：隔壁铁匠铺那姑娘的酒，你是不是总给她少算钱？
> 布鲁姆：（翻出的旧账里就有你摔杯子这笔）少算？我给她记账，记在她爹头上。

好玩的地方在于**记忆是真存下来的，不是 prompt 里写死的人设**：你越玩，他记得越多；
换一个 `user_id` 他就是个对你毫无印象的陌生人（记忆按玩家隔离）。

体验方式：Dify 应用界面直接对话（一个输入框 + 一句话），无需额外前端。

## 2. Dify 与 seekdb 的分工

| 组成 | 在作品里干的活 |
|---|---|
| **Dify Workflow** | 承担核心流程：收输入 → 调 HTTP 拿旧账 → LLM 扮老板回话 → LLM 把本轮压成一条记忆 → 调 HTTP 写回 → 输出 reply / 旧账 / 新记忆 |
| **seekdb**（必选其一，本题选它） | 记忆库：每条记忆一行（向量 + 元数据），`/memory/recall` 做**向量相似检索 + 元数据过滤（按 user_id 隔离）**，`/memory/remember` 写入并 `refresh_index()` |

**为什么必须用 seekdb 而不是塞进 prompt**：跨会话保留、按玩家隔离、按相关性检索 top-k，
这三件事都不是 prompt 能做的。检索结果直接决定老板这一轮说什么（见验证样例第 3 步）。

## 3. 目录结构

```
dify/
├── dsl/
│   └── 01-tavern-memory.yml    # 唯一一个 DSL，导入即用（Workflow 应用）
├── bridge/                     # seekdb 记忆桥（Dify 通过 HTTP Request 节点调它）
│   ├── app.py                  # 标准库 HTTP 服务 + pyseekdb
│   ├── embedding.py            # 零依赖的本地嵌入函数（避免评审时下载模型）
│   ├── requirements.txt
│   └── Dockerfile
├── tools/
│   └── demo_client.py          # 不起 Dify 也能验证整条链路（评审排障用）
├── samples/
│   └── 验证样例.md             # 初始状态、操作步骤、预期表现、实际运行结果
├── compose.yaml                # seekdb + bridge 一键起
└── README.md
```

## 4. 运行环境与版本

| 项 | 要求 |
|---|---|
| Dify | **1.4.0 及以上**（Community 或 Cloud 均可；本文按 1.4.x 的 Workflow 导入路径写） |
| 模型 | 任一 **Chat 模型**（OpenAI 兼容均可）。DSL 里默认写的是 `openai / gpt-4o-mini`，导入后请在「模型供应商」里配好你自己的模型**并重新选择**（见 5.3） |
| seekdb | `oceanbase/seekdb:latest`（本文实测版本 1.4.0.0，Docker 单机） |
| pyseekdb | `1.4.0.post1`（`bridge/requirements.txt` 已锁定） |
| Python | 3.10+（bridge 只用标准库 + pyseekdb） |
| 网络 | Dify 所在容器/主机需能访问 bridge（默认 `127.0.0.1:8077`）；bridge 需能访问 seekdb（默认 `127.0.0.1:2881`） |
| 嵌入服务 | **不需要**：本地哈希嵌入（`tavern-hash-ngram`，256 维，见 `bridge/embedding.py` 的说明），完全离线、可复现 |

> 说明：pyseekdb 的官方默认嵌入函数是 ONNX 句向量模型，首次使用要从 HuggingFace 拉权重。
> 为了让评审「clone 下来就能跑」，这里换成确定性的字符 n-gram 哈希嵌入（中文单字 + 二元组）。
> 短句记忆场景下检索效果足够（见验证样例第 4 步的抽查）；想换真实模型时只需替换 `embedding.py`
> 里的 `HashEmbedding` 为任一同协议的嵌入函数，并重建集合。

## 5. 从零到能玩的完整步骤

### 5.1 起 seekdb + 记忆桥（二选一）

**A. 一键（推荐）**

```bash
cd dify
docker compose up -d --build
docker compose ps           # seekdb 与 bridge 都 Up
curl http://127.0.0.1:8077/health
# {"ok":true,"engine":"seekdb via pyseekdb","mode":"server","target":"seekdb:2881",
#  "database":"test","collection":"grudges","embedding":"tavern-hash-ngram/256d","rows":0}
```

**B. 手动（已有 seekdb 实例时）**

```bash
# 1) 起 seekdb（本机已有实例可跳过）
docker run -d --name seekdb -p 2881:2881 -p 2886:2886 \
  -v /your/data/seekdb:/var/lib/oceanbase oceanbase/seekdb:latest

# 2) 起记忆桥
cd dify/bridge
python -m pip install -r requirements.txt
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_DB=test \
  python app.py --port 8077
```

环境变量（都有默认值，一般不用改）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEEKDB_HOST` | 空 | seekdb 地址；**Windows/macOS 必须用这种服务模式**（嵌入式库只有 Linux 版） |
| `SEEKDB_PORT` | `2881` | seekdb 端口 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` | `root` / 空 | seekdb 账号 |
| `SEEKDB_DB` | `test` | 数据库名 |
| `SEEKDB_PATH` | `./seekdb_data` | 不设 `SEEKDB_HOST` 时的嵌入式数据目录（仅 Linux） |

### 5.2 初始化数据

**不需要任何初始化数据**：记忆库从空开始，第 1 轮老板就当第一次见你。
需要重置时（回到干净的初始状态）：

```bash
curl -X POST http://127.0.0.1:8077/memory/reset \
  -H 'Content-Type: application/json' -d '{"user_id":"player-001"}'
```

### 5.3 导入 DSL

1. Dify →「工作室」→「创建空白应用」→ 选 **Workflow**（也可直接「导入 DSL」）
2. 右上角「…」→ **导入 DSL 文件** → 选 `dsl/01-tavern-memory.yml`
3. 打开两个 LLM 节点（「2. 老板回话」和「3. 压缩成一条记忆」），把**模型重新选成你自己配置好的模型**
   （DSL 只保存了 `openai/gpt-4o-mini` 这个名字，换供应商后必须手动重绑）
4. 若 bridge 不在本机 8077，改两个 HTTP Request 节点的 URL：
   - 「1. 检索旧账（seekdb）」→ `http://<bridge>:8077/memory/recall`
   - 「4. 记住这一轮（seekdb）」→ `http://<bridge>:8077/memory/remember`
   > Dify 跑在 Docker 里而 bridge 跑在宿主机时，URL 要写 `http://host.docker.internal:8077`。
5. 保存 → **发布**

**应用入口**：应用「运行」面板（或「访问 API」）。输入两个字段：
`玩家 ID`（随便填，如 `player-001`）和 `你对老板说`。

### 5.4 跑验证样例

按 `samples/verification.md` 走 4 步即可；不想开 Dify 时，`tools/demo_client.py` 会把同样的链路跑一遍：

```bash
python tools/demo_client.py --reset
```

## 6. 验证材料

- **运行截图/日志**：`samples/verification.md` 内附本轮实测的完整输出（含 seekdb 真实检索结果与分数）。
- **seekdb 侧核对**：除 `/debug/stats`、`/memory/list?user_id=` 外，也可直接查库：

```sql
-- 在 seekdb 里（mysql -h127.0.0.1 -P2881 -uroot）
USE test;
SELECT collection_name, settings FROM sdk_collections;   -- 集合元数据（向量维度、嵌入函数）
SHOW TABLES LIKE 'c$v2$%';                               -- 记忆数据表：_id/document/embedding VECTOR(256)/metadata json
SELECT _id, LEFT(document,34), JSON_EXTRACT(metadata,'$.turn') FROM `c$v2$<collection_id>` LIMIT 5;
```

## 7. 已知限制

1. **单轮一句话 = 一轮游戏**：Workflow 无内置会话状态，靠 `user_id` 串起来；想要真正的连续对话，
   可改用 Chatflow 版本（DSL 里的四个节点可原样保留，把 start 换成会话变量即可）。
2. 记忆条目只增不减：玩很久后 `/memory/recall` 的候选池会变大，重排仍是常数级；
   生产场景应加衰减/合并策略（`kind` 字段已预留）。
3. 老板的回话质量取决于你绑定的模型；哈希嵌入对**同义不同词**（「杯子」vs「玻璃杯」）不敏感，
   换成真实句向量模型会明显变好。
4. seekdb 需要持久化目录；容器删除即丢记忆（compose 里已挂 volume）。

## 8. 数据与来源声明

- 记忆内容全部来自玩家输入（运行时产生），仓库内不含任何预置数据、不含任何 API Key。
- 玩法、人设、prompt 均为本次参赛自写；未复用公开 DSL。
- seekdb 官方镜像与 `pyseekdb` 的使用见各自仓库许可。
