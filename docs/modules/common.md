# 公共契约、配置与日志

## 意图和边界
config.py、contracts.py、trace.py 只提供组件共同需要的值对象与协议，不创建模型、不执行命令，也不进行评测。__init__.py 仅声明包版本。保持依赖单向：核心循环依赖协议，具体后端实现协议。

## Settings
Pydantic frozen 配置禁止未知字段，校验步数、时间、token、命令超时等正数范围。默认模型 deepseek-flash，API地址 https://api.deepseek.com，reasoning_effort high；密钥从不进入 Settings。请求最大输出8192、主请求30步、总时长900秒、累计token100000，上下文启发式阈值24000，保留最近2个完整回合。
预算在操作边界检查，SDK重试、在途请求和输入token可能越过限制；这不是账单硬上限。CLI保存本次实际配置以复现。

## 数据契约
Usage记录input/output/cached_tokens，total_tokens=input+output，缓存token是输入子集，不重复求和；add为累计可变操作。Reply包含原始assistant message、Usage和finish_reason。CommandResult统一stdout/stderr/exit_code/timed_out/truncated，as_dict供工具回传。
Model.complete(messages, tools, timeout, max_output_tokens, purpose)返回Reply；模型错误由调用者处理。Environment.execute返回CommandResult，apply_patch返回结构化结果，collect_patch返回Git patch文本，close负责释放本次资源。协议不负责生命周期策略。

## Trace
Trace(directory, secrets)创建产物目录；event向trajectory.jsonl追加JSON事件，write_json及result写结构化产物。所有写入通过clean替换明确提供的密钥字符串；不将系统环境整体记录，也不声称自动发现所有可能的秘密。模型请求、响应、工具结果和压缩事件均留原始脱敏记录，压缩不重写历史日志。
写文件失败会向调用者传播；调用者不可因为日志失败重放工具写操作。日志可能含任务源码，应视作本地敏感产物，Git默认忽略artifacts目录。
