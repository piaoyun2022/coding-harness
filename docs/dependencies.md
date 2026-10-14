# 依赖与运行环境

核心循环自行实现。OpenAI SDK 只负责 DeepSeek 的 OpenAI 兼容接口；Pydantic 校验参数；python-dotenv 读取本地密钥。openai-agents 仅复用 apply_diff 补丁处理，不调用 Runner。

开发依赖 pytest、ruff、build。benchmark extra 只安装 pyarrow，下载固定版本的 SWE-bench Lite Parquet 数据。eval extra 固定 swebench 4.1.0，与当前官方报告解析及镜像协议一致；官方评测在 Linux Python 中运行，Windows 使用 WSL，避免 shell 脚本 CRLF 问题。Modal 是可选后端，本地默认 Docker Desktop 的 WSL2 Linux 容器。

本机安装与验证记录另见 docs/local-setup.md；真实 API 和真实容器结果分别记录，不用离线测试替代。
