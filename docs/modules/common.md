# 公共契约、配置与日志

## 意图和边界
config.py、contracts.py、trace.py 只提供组件共同需要的值对象与协议，不创建模型、不执行命令，也不进行评测。__init__.py 仅声明包版本。保持依赖单向：核心循环依赖协议，具体后端实现协议。

## Settings
Pydantic frozen 配置禁止未知字段，校验正数范围。默认模型 `deepseek-flash`、API 地址 `https://api.deepseek.com`、推理强度 `high`；密钥不进入 Settings。默认最多30轮主请求、900秒、累计100000 token，主请求输出上限8192 token。

上下文 V2 的参数分工：
- `context_tokens=24000`：主循环输入的软阈值，包含系统/任务、检查点、完整历史和工具 schema。
- `model_context_tokens=128000`：本地配置的保守窗口上限，不声称等于供应商实时窗口；`context_margin_tokens=1024` 是显式余量。
- `input_budget(output_tokens, action=True)` 返回 `min(context_tokens, model_context_tokens - output_tokens - margin)`；摘要调用 `action=False` 可用剩余硬窗口，便于处理略超过软阈值的历史。保留输出和余量仍不可突破。
- `recent_context_tokens=6000`：近期完整回合的 token 预算；`keep_recent_turns=2` 保留兼容，现为近期回合数上限，不保证始终保留两轮。最新一轮必须完整保留，即使它无法放进预算；此时受控停止。
- `checkpoint_output_tokens=2048`：结构化摘要输出上限；主请求与摘要的最大输出加余量必须小于配置窗口。
- `max_output_chars=12000`：证据入库后的模型结果预览字符上限，不再决定执行环境实际捕获量。
- `execution_capture_bytes=1048576`：执行环境每个输出流的捕获上限，范围1..4194304字节；执行层继续排空超限输出并标记真实截断。
- `evidence_max_item_bytes=16777216`、`evidence_max_total_bytes=67108864`：单次运行的证据字节限制。默认单项可容纳两路各1MiB输出的最坏 JSON 控制字符转义及通常的参数。提高捕获量或提交异常大的参数时，应一并调高证据限额；无法完整保存时停止，不把截断记录当完整证据。

估算使用 UTF-8 字节启发式，包含 schema，但不是准确 tokenizer。操作边界预算包含摘要用量和时间；SDK重试、在途请求和输入 token 仍可能越过累计用量限制，不是账单硬上限。CLI 保存本次实际配置以复现。

## 数据契约
Usage记录input/output/cached_tokens，total_tokens=input+output，缓存token是输入子集，不重复求和；add为累计可变操作。Reply包含原始assistant message、Usage和finish_reason。CommandResult统一stdout/stderr/exit_code/timed_out/truncated，as_dict供工具回传。
Model.complete(messages, tools, timeout, max_output_tokens, purpose)返回Reply；模型错误由调用者处理。Environment.execute返回CommandResult，apply_patch返回结构化结果，collect_patch返回Git patch文本，close负责释放本次资源。协议不负责生命周期策略。

## Trace
Trace(directory, secrets)创建产物目录；event向trajectory.jsonl追加JSON事件，write_json及result写结构化产物。所有写入通过clean替换明确提供的密钥字符串；不将系统环境整体记录，也不声称自动发现所有可能的秘密。模型请求、响应、工具结果和压缩事件均留原始脱敏记录，压缩不重写历史日志。
写文件失败会向调用者传播；调用者不可因为日志失败重放工具写操作。日志可能含任务源码，应视作本地敏感产物，Git默认忽略artifacts目录。
