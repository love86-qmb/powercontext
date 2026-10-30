# MiniMax × OceanBase：面向持续演进软件的 SDD 上下文管理

这个目录是一套可运行的开发上下文模块。它从 seekdb/OceanBase 读取当前有效的规格、代码、测试、决策和进度，
把带来源与摘要的上下文交给 MiniMax 完成受限代码修改，运行独立验证，再把结果写回数据库和 PowerContext。
同一任务在代码与上下文均未变化时会复用已验证结果，不再次调用模型或改写代码。

## 1. 组件

- `sdd_context/store.py`：seekdb/OceanBase 任务上下文、运行状态和 PowerContext 引用账本。
- `sdd_context/minimax_client.py`：MiniMax 官方文本生成 API 调用与结构化变更解析。
- `sdd_context/powercontext_bridge.py`：通过 PowerContext SDK 检索和保存跨运行上下文。
- `sdd_context/runner.py`：上下文装配、路径约束、修改、验证、失败回滚和复用。
- `tasks/task-identity.json`：真实开发任务入口。
- `specs/`、`context/`、`validation/`：有效规格、现有决策/进度和不可修改的回归测试。
- `evidence/<run-id>/`：可审查的提示、MiniMax 响应、代码差异、测试输出和 SQL 查询摘要。

数据库是任务状态的真源；`evidence/` 只是便于评委复核的导出，不参与复用判定。

## 2. 环境准备

要求 Python 3.11+、一个 MySQL 协议可访问的 seekdb/OceanBase 数据库、MiniMax API Key，以及一个正在运行的
PowerContext Server。

在仓库根目录安装当前项目和数据库驱动：

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[client,server]"
.venv/Scripts/python -m pip install -r minimax/requirements.txt
```

Linux/macOS 将 `.venv/Scripts/python` 换成 `.venv/bin/python`。

创建数据库：

```sql
CREATE DATABASE sdd_context DEFAULT CHARACTER SET utf8mb4;
```

按 PowerContext 的交互式配置生成服务配置并启动服务：

```bash
powercontext config init --language zh --output .env
powercontext server run --env-file .env
```

复制 `minimax/.env.example` 为 `minimax/.env`，填入真实连接信息。命令行会自动读取该文件；它已被
`.gitignore` 排除，真实密钥不能写入仓库。
远程 seekdb/OceanBase 应在数据库 URL 中保留 `ssl=required`。
默认调用 MiniMax 国内开放平台 `api.minimaxi.com`；国际站账户可通过 `MINIMAX_BASE_URL` 覆盖。

## 3. 完整运行

下面命令都在仓库根目录执行。

先证明任务确实尚未完成；此时回归测试应失败：

```bash
python -m unittest minimax.validation.test_task_identity -v
```

初始化 OceanBase 表并导入带来源的上下文：

```bash
python -m minimax.sdd_context --task minimax/tasks/task-identity.json init-db
python -m minimax.sdd_context --task minimax/tasks/task-identity.json ingest
```

第一次运行会查询 OceanBase 和 PowerContext、调用 MiniMax、只修改允许的源文件并执行不可修改的测试：

```bash
python -m minimax.sdd_context --task minimax/tasks/task-identity.json run
```

预期 `status` 为 `completed`、`model_called` 为 `true`，且包含 MiniMax 请求 ID、用量、PowerContext Scope 和
Memory Citation。测试随后必须通过：

```bash
python -m unittest minimax.validation.test_task_identity -v
```

不改动任何输入，第二次运行同一命令：

```bash
python -m minimax.sdd_context --task minimax/tasks/task-identity.json run
```

预期 `status` 为 `reused`、`model_called` 为 `false`、`code_changed` 为 `false`，并返回首轮
`reused_from_run_id`。这证明系统复用了已完成状态，没有重复调用 MiniMax 或重复修改代码。

查看运行账本：

```bash
python -m minimax.sdd_context --task minimax/tasks/task-identity.json show-runs
```

## 4. 数据库复核

评委可直接执行：

```sql
SELECT kind, source_path, source_digest, status
FROM sdd_context_items
WHERE project_key = 'powercontext-minimax-sdd'
  AND task_id = 'stable-repository-identity'
ORDER BY kind, source_path, created_at;

SELECT run_id, status, model, reused_from_run_id,
       prompt_sha256, response_sha256, patch_sha256,
       input_code_digest, result_code_digest
FROM sdd_task_runs
WHERE task_id = 'stable-repository-identity'
ORDER BY started_at;

SELECT run_id, operation, scope_id, citation_json
FROM sdd_powercontext_events
ORDER BY id;
```

应能看到源代码更新前的记录被标记为 `superseded`、更新后的代码上下文为 `active`；首轮状态为
`completed`，第二轮为 `reused`，并且第二轮引用首轮运行 ID。

## 5. 安全与失败语义

- MiniMax 只能修改任务配置 `allowed_changes` 中的路径，不能修改规格或测试。
- 路径必须位于仓库内；重复路径、无实际差异或越界路径会被拒绝。
- 任何测试失败都会恢复全部文件，运行状态记为 `failed`，不会产生可复用成功状态。
- API Key、数据库密码和 PowerContext Token 只从环境变量读取，日志不会记录这些值。
- 上下文输出始终携带类型、源路径和 SHA-256，不返回脱离输入的固定答案。

架构、数据流、状态机和验收步骤见 [docs/architecture.md](docs/architecture.md) 与
[docs/validation.md](docs/validation.md)。

## 6. 随仓库提交的真实证据

- `evidence/8f5244c7-78e1-4056-b373-d7066eda05f7/`：MiniMax-M3 首次实际修改、请求 ID、Token 用量、
  完整响应、代码差异、测试输出和 PowerContext Citation。
- `evidence/0021ad9a-e766-464b-9efd-d9847549d509/`：同任务复用结果及 seekdb 查询快照，证明第二轮
  未调用模型、未修改代码，并引用首轮运行。

证据不包含 API Key、数据库连接串或密码；真实密钥仍只存在于被 Git 忽略的 `minimax/.env`。
