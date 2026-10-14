# 模型接口 provider.py

## 意图与职责
只做 DeepSeek Chat Completions 协议适配，复用 OpenAI Python SDK，不执行工具、不管理会话生命周期。
默认模型、地址与推理强度由 Settings 给出；summary 请求关闭推理，给压缩留出更直接的输出预算。
上下文中的 reasoning_content 与 tool_calls 原样返回，避免下一轮丢失模型要求的字段。

## 方法约定
- DeepSeekModel(settings, api_key, client=None)：可注入离线 SDK 客户端；默认客户端设置两次重试与请求超时。密钥不进入配置或日志。
- complete(messages, tools, timeout=..., max_output_tokens=None, purpose="action") -> Reply：
  action 使用配置推理强度；summary 使用 none。空工具列表省略 tools 参数。
  每次传入 max_tokens 与 timeout；返回 assistant 消息、结束原因和输入/输出/命中缓存 token。
  无 choices 或非 assistant 消息抛 ValueError；SDK 请求失败向上传递，由 Runner 形成终止产物。
- close()：关闭 SDK 客户端；调用者负责生命周期，不由 Agent 自动关闭。

## 用量及限制
缺失 usage 计为零，不能据此声称请求免费；缓存 token 是输入 token 的子集，不另加到总 token。
SDK 的重试、网络调度可能使整体耗时超过单次预算；Runner 在操作边界检查总预算，没有伪装成严格实时限额。
此适配器目前只支持 Chat Completions，不宣称 Responses API 或完整 Codex 协议兼容。

## 验证
tests/test_provider.py 使用真正 OpenAI SDK 与 HTTP MockTransport 验证序列化、reasoning_content 回放、工具调用、
缓存用量、摘要参数和空 choices。它不产生真实 API 成绩；真实连通性另行记录。
