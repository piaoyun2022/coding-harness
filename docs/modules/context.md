# 上下文管理 context.py

## 意图与职责
维护“原始任务和规则 + 较早回合摘要 + 最近完整回合”。这里只保留模型下一次需要的工作上下文，
完整历史永远由 Trace 追加保存；不加入长期记忆、技能、MCP 或断点恢复。

回合指一条 assistant 消息及其全部 tool observations。压缩边界只能在回合之间，不能留下孤立调用或结果。
模型纯文本回合附一条 user 提醒，也作为完整回合存储。

## 方法约定
- Context(system, task, token_limit, keep_recent_turns)：复制并保存原始 system/task；两个限制必须为正。
- messages() -> list[dict]：返回深复制，调用方修改不会污染内部状态。
- add_turn(assistant, observations)：验证工具 ID 的顺序与结果角色；不完整回合抛 ValueError。
- estimated_tokens：UTF-8 JSON 字节数除二向上取整，再加每条消息开销。
  这是偏保守的触发启发式，不是 DeepSeek tokenizer，也不保证永远高估真实 token。
- plan_compaction() -> CompactionPlan | None：仅当超过阈值且有可压缩的老回合时，
  组合固定摘要说明、原任务、上次摘要和老回合；保留最近指定数量的完整回合。
- apply_compaction(plan, reply) -> bool：只接受 assistant 角色、非空、未截断、不含工具调用、至多 12000 字符的文本。
  摘要必须让估计上下文变短；拒绝过期计划。验证失败保留原上下文，返回 False。

## 压缩契约
摘要保留目标约束、已改文件、发现、测试实际结果、未解决问题、下一步，并区分事实和推测。
摘要模型调用由 Agent 执行，因此 token、耗时、请求和响应日志都与主任务一起计算。
一次压缩仍不能降到阈值时停止，不丢掉最近回合来强行继续。
原始任务或最近回合本身过长也受控停止；当前不递归分块压缩摘要输入。

## 风险与验证
摘要可能遗漏语义，不是无损压缩；原始日志支持复查。单纯摘要无法消除 prompt injection，
固定说明用于减少将历史文件内容当指令的风险。
tests/test_context.py 验证多工具回合保留、深复制、无效摘要不破坏历史、不完整观察被拒绝。
Agent 集成测试进一步验证压缩失败、用量预算和全日志保留。
