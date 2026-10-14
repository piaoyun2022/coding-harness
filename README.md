# Coding Harness

独立的最小 coding agent：DeepSeek + 自写循环 + 简单上下文压缩 + 终端/补丁工具 + 本地 Docker。代码位于 src/，对应设计意图与方法约定位于平行 docs/；每次代码改动必须更新对应文档（见 AGENTS.md）。

## 开始

Python3.11+，Git，本地Linux Docker引擎。Windows使用Docker Desktop；官方SWE-bench评测器在WSL中运行。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,benchmark]"
# 将 .env.example 复制为 .env 并填写本地密钥，不提交
.venv\Scripts\python -m coding_harness --help
```

在仓库根目录执行：

```powershell
.venv\Scripts\python -m coding_harness probe
.venv\Scripts\python -m coding_harness run --repo C:\path\to\repo --task "修复描述中的问题并运行测试" --output artifacts\run-001
```

每次output使用新目录；产物包含changes.patch、trajectory.jsonl与result.json。Docker安装后如果旧终端仍找不到docker，重新打开终端。密钥留在宿主本地，不传入容器。

具体命令见 [CLI契约](docs/modules/cli.md)，本机环境见 [安装记录](docs/local-setup.md)。

## 设计与验证

- [五个组件和主循环](docs/architecture.md)
- [公共数据契约](docs/modules/common.md)
- [上下文压缩](docs/modules/context.md)
- [补丁编辑](docs/modules/patching.md)
- [运行边界](docs/modules/environment.md)
- [评测方法](docs/benchmark.md)
- [开发与文档同步](docs/development.md)
- [依赖选择](docs/dependencies.md)

离线测试、真实API、真实容器与官方评分分别验收。模型finish不代表测试通过，示例修错不代表SWE-bench成绩。目前最新证据见 docs/local-setup.md 与忽略提交的 artifacts/。
