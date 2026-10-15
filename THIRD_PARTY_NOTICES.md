# 第三方依赖与来源说明

本文件区分本项目代码、安装时取得的依赖、独立对照实现和评测数据。**它不是本项目的 LICENSE，也不为本项目新增 MIT、Apache 等项目级授权。** 本次公开展示源码；第三方项目的许可只覆盖其相应内容。

## 使用方式

核心循环、上下文状态机、工具调度、补丁外壳和生命周期代码在本仓库实现。源码审查中，补丁文本应用通过 `src/coding_harness/patching.py` 的 `from agents import apply_diff` 引用已安装 SDK；没有把该 SDK 的 parser 源码复制到产品目录，也没有调用其 Agent Runner。

mini-SWE-agent 对照由 `scripts/run_mini_swe_batch.py` 导入独立安装的官方实现，不将 vendor 源码加入发布仓库。SWE-bench 由已安装官方验证器运行。公开 `results/` 是筛选后的实验元数据与计数，不包括完整被评测仓库、参考补丁、测试补丁或镜像。

## 直接依赖与构建工具

下表许可依据已安装发行包的 `METADATA` / 随包 LICENSE，以及链接的官方来源核对。版本是本次开发环境核验值，**不是完整依赖锁定文件**；安装约束以 `pyproject.toml` 为准。

| 项目 | 本项目用途 | 已核验版本 | 许可与来源 |
|---|---|---:|---|
| OpenAI Python SDK | DeepSeek 的 OpenAI 兼容 Chat Completions 传输 | 2.54.0 | Apache-2.0；[官方 LICENSE](https://github.com/openai/openai-python/blob/main/LICENSE) |
| OpenAI Agents SDK / openai-agents | 仅调用 `agents.apply_diff` | 0.20.0 | MIT，OpenAI；[官方 LICENSE](https://github.com/openai/openai-agents-python/blob/main/LICENSE) |
| Pydantic | Settings、工具参数、Checkpoint 校验与 JSON Schema | 2.14.1 | MIT；[官方 LICENSE](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| python-dotenv | 读取显式本地 `.env` | 1.2.4 | BSD-3-Clause；[官方 LICENSE](https://github.com/theskumar/python-dotenv/blob/main/LICENSE) |
| Modal | 可选云端执行后端 | 1.6.1 | Apache-2.0；[官方源码](https://github.com/modal-labs/modal-client) |
| PyArrow | 读取 SWE-bench Lite Parquet | 23.0.1 | Apache-2.0；发行包附 LICENSE.txt 与 NOTICE.txt；[Apache Arrow](https://github.com/apache/arrow) |
| SWE-bench | 独立官方评分器 | 固定 4.1.0 | MIT；[官方 LICENSE](https://github.com/SWE-bench/SWE-bench/blob/main/LICENSE) |
| pytest | 离线回归测试 | 9.1.1 | MIT；[官方 LICENSE](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |
| Ruff | 静态检查 | 0.17.0 | MIT；[官方 LICENSE](https://github.com/astral-sh/ruff/blob/main/LICENSE) |
| build | Python 构建前端 | 1.6.1 | MIT；[官方 LICENSE](https://github.com/pypa/build/blob/main/LICENSE) |
| Hatchling | `pyproject.toml` 构建后端 | 约束 `>=1.27`，本次未记录独立安装版本 | MIT；[Hatch 官方 LICENSE](https://github.com/pypa/hatch/blob/master/LICENSE.txt) |

上表只列本仓库直接声明的主要依赖，不是所有传递依赖、系统包或镜像层的完整 SBOM。本仓库不打包上述发行包；重新分发依赖、可执行包或镜像时，需一并核对实际随附的 LICENSE/NOTICE，不能用本文件替代它们。

## mini-SWE-agent 对照

- mini-SWE-agent 2.4.6 固定源码：`04d809ceab9df28f9adaed044884180159172930`，MIT，Kilian A. Lieret 与 Carlos E. Jimenez；[固定提交 LICENSE.md](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/LICENSE.md)。默认 Agent、SWE 配置与提交协议属于上游实现，不应表述为本项目原创。
- 对照环境使用 LiteLLM 1.105.1 适配模型。其仓库区分 MIT 范围与独立 enterprise 许可；本项目使用模型调用接口，不复制或包含 enterprise 内容。[官方许可说明](https://github.com/BerriAI/litellm/blob/main/LICENSE)
- 对照所需的 YAML 等传递依赖由其独立安装环境提供，不是本项目核心循环的依赖。具体运行边界见 [mini 对照文档](docs/mini-swe-batch.md)。

## 数据、仓库与课程材料

- 评测数据来源为 [princeton-nlp/SWE-bench_Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite)，固定 revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。官方评测器的软件许可不能自动替代数据中各原始仓库、issue、补丁或镜像内软件的权利说明。本发布不重新分发这些完整内容。
- 数据对应项目及其代码仍归原作者，需保留各自的许可与来源。本项目公开任务 ID、版本、实验设置、结果和用于绑定证据的哈希，具体白名单见 [评测结果](docs/evaluation-results.md)。
- CMU AI Agents 课程是设计概念参考，未将课程讲义、课程作业包或图片复制入本仓库。[课程站点](https://www.cmu-agents.com/)
- Git、Docker、Python、基础镜像与 Conda 生态是外部运行环境，各自的软件许可或服务条款独立适用；它们不因被本项目调用而成为本项目授权内容。DeepSeek 为调用的模型 API，不随源码分发模型权重。

本说明基于当前可见源码与直接依赖核对，不对任意未来依赖升级或镜像内容作覆盖性承诺。新增复制、vendor 或二进制分发时，应同步更新来源与许可说明。
