# 架构与数据流

## 输入与输出

任务输入由 `TaskDefinition` 声明：任务描述、仓库根目录、规格/代码/测试/决策/进度来源、允许修改的文件和
验证命令。模块读取文件后立即计算 SHA-256 并写入 OceanBase；后续提示上下文只来自数据库的 `active` 记录。

输出包括：MiniMax 生成的受限完整文件、统一 diff、独立验证输出、OceanBase 运行状态、PowerContext 引用，
以及第二次运行时的复用决定。每个上下文块都带 `kind`、`source_path` 和 `source_digest`。

## 执行数据流

```text
任务 JSON + 仓库文件
        │ 采集、摘要、版本化
        ▼
OceanBase.sdd_context_items ──查询 active──┐
                                           ├─> 来源标注的 Prompt ─> MiniMax
PowerContext Memory ────────检索历史───────┘                          │
                                                                      ▼
                                                          允许路径校验与写入
                                                                      │
                                                        不可修改测试/回归验证
                                                        │通过              │失败
                                                        ▼                  ▼
                                            更新 OceanBase + PowerContext  回滚文件
                                                        │
                                                        ▼
                                              后续同任务直接复用
```

## 存储模型

- `sdd_context_items`：同一来源的新摘要激活时，旧摘要转为 `superseded`，确保失效规格不会继续进入提示。
- `sdd_task_runs`：记录定义、上下文和代码摘要，MiniMax 请求/响应/补丁摘要，验证命令和结果。
- `sdd_powercontext_events`：保存每轮 PowerContext recall/remember 的 Scope 和精确 Citation。

复用键由任务定义摘要、当前有效上下文摘要和当前允许修改文件摘要共同组成。成功运行在代码修改后重新采集
上下文，因此第二轮看到的是完成后的代码和对应上下文摘要。任何一个输入变化都会使复用失效并重新进入模型流程。

## MiniMax 约束

MiniMax 收到的是当前任务的数据库检索结果和 PowerContext 召回记录，不是固定模板答案。响应必须是一个 JSON
对象，包含完整文件内容。Runner 拒绝测试修改、仓库外路径、重复路径和空变更。验证在独立子进程运行，失败时
恢复原文件。

## PowerContext 作用

首轮开始时按任务 ID 和描述检索相关 Memory；完成后将任务、模型、验证命令和结果摘要保存为 `progress`
Memory。第二轮再次检索该 Memory，同时 OceanBase 的完成状态阻止重复模型调用。OceanBase 提供可查询的权威
任务状态，PowerContext 提供跨会话可复用的语义上下文，两者职责不混淆。
