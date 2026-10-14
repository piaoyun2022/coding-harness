# Tools：模型协议边界

三个公开 function tool：execute、apply_patch、finish。Pydantic 生成 JSON Schema，同时执行严格参数校验；额外字段拒绝，命令/补丁/摘要必须非空字符串，超时只能为 1–600 秒的整数。

`dispatch(environment, name, args_json, timeout) -> dict` 仅执行 execute 和 apply_patch；finish 由核心循环处理，要求单独调用。execute 的参数超时不能扩大循环传入的剩余预算。命令非零退出/超时是正常 CommandResult，由模型读取并修复，不等于调度异常。

正常返回 ok=true 与结果字段；参数、路径、I/O、未知工具错误返回 ok=false，不自动重试写操作。Pydantic 错误不回显原始输入，未知异常只返回类型，避免把 SDK 详细错误作为工具观察泄露出去；任务日志仍由控制层统一脱敏。

工具调度不实现 MCP，不维护模型消息，也不判断用户任务是否真正完成。应用修改由补丁工具承担；终端依然可执行任意 shell，因此这只是协议约定，真正隔离由执行后端承担。测试见 tests/test_tools.py。
