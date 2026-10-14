# Tools：模型协议边界

四个公开 function tool：execute、apply_patch、read_evidence、finish。Pydantic 生成 JSON Schema，同时执行严格参数校验；额外字段拒绝，命令/补丁/摘要必须非空字符串，执行超时只能为 1–600 秒的整数。

`dispatch(environment, name, args_json, timeout, *, evidence=None) -> dict` 保留原四个位置参数调用兼容性。execute 与 apply_patch 行为不变：execute 的参数超时不能扩大剩余预算；命令非零退出/超时仍返回正常 CommandResult。调度层不会自动将结果存入证据库或缩短输出，入库、预览与历史呈现由 Agent 统一负责。

read_evidence 通过可选 EvidenceStore 读取当前运行登记的观察，不能访问任意路径。参数 id 必须符合 opaque ID 格式，start 为非负整数，limit 为 1–16000 整数（默认 4000），拒绝布尔值和字符串自动转换。返回内容按字符分页，next_start=null 表示已无后续存储内容；truncated 的单项截断语义见 evidence.md。未配置证据仓库、未知 ID、文件完整性失败均返回 ok=false，不调用 Environment；工具预算已耗尽时所有调度同样拒绝。

finish 由核心循环处理，要求单独调用；它不证明任务已经正确完成。正常执行返回 ok=true 与结果字段；参数、路径、I/O、未知工具错误返回 ok=false，不自动重试写操作。Pydantic 错误不回显原始输入，未知异常只返回类型；任务日志仍由控制层统一脱敏。

工具调度不实现 MCP，不维护模型消息。应用修改由补丁工具承担；终端可执行 shell，因此真正隔离由执行后端承担。tests/test_tools.py 覆盖执行兼容、预算、参数拒绝与证据分页；证据验证细节见 docs/testing-evidence.md。
