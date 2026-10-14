# 核心离线验证

## 执行范围
`tests/test_provider.py` 用 SDK 替身验证 DeepSeek 请求参数、工具省略、usage/cache 和 reasoning_content 保留；不发送网络请求。

`tests/test_context.py` 验证上下文 V2 的边界：
- 多次结构化压缩后原始 system/task 逐字不变，近期完整 assistant/工具结果和 reasoning_content 保留。
- 新摘要看到旧检查点；程序自动继承旧证据 ID 和真实失败事实，调用者修改返回副本不影响内部状态。
- 严格结构、截断响应、额外工具事实字段、伪造逐条/全局引用、缺失证据、过期计划、候选仍超预算时均保留原历史。
- tail 受 token 和回合数上限约束，最新一轮不可拆开；孤立、缺失、乱序工具结果拒绝。
- 摘要计划保留未清理预览，包括证据前缀之外的关键标记；计划生成不改历史，成功后保留最近完整 tail。补充证据前缀仍最多8000字符。
- 工具 schema 与消息一起参与估算，不冒充准确 tokenizer；摘要计划给出扣除固定事实/完整 tail后的剩余预算，较大 schema 会降低该预算，提示要求重复失败去重而非历史复述。

`tests/test_agent.py` 用模型/环境替身验证循环行为：
- 同轮重复 ID/截断响应不执行，跨轮 ID 可复用；坏参数/未知工具可恢复；混合 finish 全批拒绝。
- 步数、时间、响应后 token 用尽都能阻止后续动作，批内中断仍配对，不重放工具。
- execute 完整结果和命令参数先落证据，预览截短后仍可 read_evidence 回读末尾，读取自身不重复入库。
- 保存之前发生过的补丁在证据总量耗尽时只执行一次，后续命令不运行；单条证据截断也受控停止，原始完整有界结果留在日志。
- 已知密钥字符串在证据、模型预览和轨迹中均脱敏。
- 超软预算时直接触发结构化摘要，摘要消耗计入总量，轨迹保留原始结果；结构/传输/伪造引用/过长/窗口失败只尝试一次，摘要前后序列化历史相同、不重放工具。
- 相同 execute 参数与完整结果在含当前调用的8次窗口内第三次出现，只附一次证据提醒；所有5次重复请求仍执行并保持配对。成功补丁后重置、结果变化不误报、窗口之外记录不计数（含第1/8/9次相同的边界）。
- schema 过大或输出预留压缩输入窗口时可在模型调用前停止。

## 运行与解释
建议执行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_context.py tests/test_agent.py tests/test_provider.py tests/test_tools.py tests/test_evidence.py -q
.venv/Scripts/python.exe -m ruff check src/coding_harness/context.py src/coding_harness/agent.py src/coding_harness/config.py tests/test_context.py tests/test_agent.py
```

工具/证据测试由对应模块维护；上述组合确保 Agent 与新 dispatch/evidence 接口实际联通。测试使用临时目录与离线替身，不运行题库代码、不请求模型、不改变评测状态。

通过这些测试只证明约定的协议和失败边界，不证明自然语言摘要完整正确，也不是 coding benchmark 得分。严格 JSON、引用存在及原始事实回填可以防止结构性丢配对和状态篡改，不能自动验证模型对证据的语义解释。

## 根任务集成验收（2026-10-14）
- Windows 全套离线测试：236 passed、2 skipped；随后新增的摘要预算测试已在相关套件通过。
- WSL/Linux 全套离线测试：239 passed（含独立评分脚本测试）。
- `artifacts/context-v2-live-summary/` 保留第一次真实摘要验证的失败：摘要逐次膨胀，第三次候选超预算，旧历史未被替换。
- 修复去重和剩余预算提示后，`artifacts/context-v2-live-summary-dedup/` 完成 8 轮受控流程、6 次真实 DeepSeek 摘要压缩；原始失败 exit_code=1 保留，read_evidence 成功取回超过短预览范围的中段标记。
- 该真实验证只有摘要调用真实模型；动作由脚本固定、执行环境为替身，未运行仓库修复或官方评测，不能作为 Agent 能力分数。用量 31,258 tokens，耗时 15.609 秒。
- 真实 Docker 的有界输出、超时进程树和清理验证见 testing-execution.md。完整 30 题评分仍是独立验收，不由上述测试推断。


## 上下文事务改动 B 验证
事务改动单独提交，不调整 finish 提示、默认预算、实验 runner 或既有结果。离线测试还检查连续压缩的失败事实与最新调用配对；提醒是行为提示，不宣称能够识别全部无效探索。长轨迹临时探针使用300步、500万累计token、短固定摘要及每次不同命令，确认事实累计可在第175轮耗尽24000输入窗口（最后有效检查点172条事实）。探针不调用API、不留下评测产物，不能作为真实能力或成本结论；该容量问题留作独立改动，不夹带进事务修复。
