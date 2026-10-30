# 验证用例与预期结果

## 用例：稳定的仓库身份

任务要求修复 `canonical_repository_key` 的跨平台边界行为。规格来自
`minimax/specs/task-identity.md`，待修改代码来自 `minimax/sdd_context/task_identity.py`，不可修改测试来自
`minimax/validation/test_task_identity.py`。决策和当前进度来自 `minimax/context/`。

### 首轮步骤

1. 执行任务测试，确认初始实现失败。
2. 初始化数据库并执行 `ingest`，核对输出列出的五个来源。
3. 执行 `run`。核对 MiniMax 请求 ID、模型名和用量不是空值。
4. 查看 `evidence/<run-id>/prompt.txt`，核对每个上下文块的类型、路径和摘要。
5. 查看 `minimax-response.json` 与 `change.patch`，确认只有允许的源文件变化。
6. 查看 `verification.txt` 并再次手动执行测试，四个测试必须全部通过。
7. 查询三张 OceanBase 表，确认上下文、完成状态和 PowerContext Citation 均已持久化。

### 第二轮步骤

1. 保持任务、规格、代码和测试不变，再执行同一 `run` 命令。
2. 输出必须为 `reused`，`model_called=false`，`code_changed=false`。
3. `reused_from_run_id` 必须等于首轮运行 ID。
4. `git diff` 在第二轮前后必须一致。
5. OceanBase 中新增一条 `reused` 运行，但不能新增 MiniMax 响应或代码补丁。

### 失效与恢复

- 修改规格、代码、测试、决策或进度后，摘要变化，系统必须重新执行模型流程。
- MiniMax 返回测试路径或仓库外路径时，运行失败且不写文件。
- MiniMax 生成代码未通过验证时，源文件恢复，状态为 `failed`，后续不能复用该运行。
- PowerContext 或 OceanBase 不可访问时，运行直接失败，不能伪造离线成功结果。

## 本地模块测试

`minimax/tests` 使用 SQLite 和 Fake MiniMax/PowerContext，只验证本地状态机、回滚边界和幂等复用。它们不能
替代正式证据中的真实 MiniMax、OceanBase 和 PowerContext 运行：

```bash
python -m unittest discover -s minimax/tests -v
```

## 已附真实运行证据

- 首轮 `8f5244c7-78e1-4056-b373-d7066eda05f7`：MiniMax-M3 实际调用、统一 diff、4 项测试通过、
  PowerContext Memory Citation 完整。
- 复用轮 `0021ad9a-e766-464b-9efd-d9847549d509`：`model_called=false`、`code_changed=false`，
  并引用首轮运行 ID。
- `database-evidence.json` 的服务版本为 `5.7.25-OceanBase seekdb-v1.2.0.0`，包含上下文版本、
  运行账本与 PowerContext recall/remember 引用；不含连接串或密码。
