# DeepSeek 官方框架评测参考

核验日期：2026-10-14。这里整理模型厂商公开成绩，供面试图表引用；不是本仓库重跑的结果。

## 可引用数据

模型为 **DeepSeek-V4.1-Flash（Instruct）**。以下三项来自官方模型卡的跨框架表，数据集为 **DeepSWE v1.1**，指标为 **Resolved**；该基准有 **113 题**，不是 SWE-bench Lite 的 300 题。[官方模型卡](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash#performance-across-agent-scaffolds-deepswe-v11-and-terminal-bench-21-max-reasoning-effort)、[DeepSWE 官方基准页](https://deepswe.datacurve.ai/)

| 框架 | 官方解决率 | 论文记录的实现 |
| --- | ---: | --- |
| Codex | 65.6% | v0.147.0；app-server 模式，适配工具 schema |
| OpenCode | 65.5% | v1.18.15；build agent，shell / 文件工具及原生任务委派 |
| mini-SWE | 74.2% | mini_swe_v2 port；单 bash 工具，提交标记结束；脚注引用 mini-swe-agent commit 04d809ceab9d |

分数见 [技术报告 Table 4，第 35 页](https://arxiv.org/pdf/2609.19969v1#page=35)，版本见 [Appendix B.1，第 48 页](https://arxiv.org/pdf/2609.19969v1#page=48)。

**OpenCode 与 OpenHands 是不同项目。** 本次核对 DeepSeek 官方模型卡、技术报告及评测说明，没有找到同模型的 OpenHands 对应成绩；应标“未核实 / N/A”，不能把 OpenCode 的 65.5% 移给 OpenHands，也不能把支持该模型的集成说明当作跑分。

## 共同设置与已知边界

- 每题 N=8 个采样；图表保留官方 Resolved 名称，不改成 pass@8。
- 最大推理 effort=100；temperature=1.0、top_p=0.95。
- Linux 容器；1M-token 上下文；每个 agent 最多 500 个模型生成轮。
- 官方跨框架比较固定模型 checkpoint、解码设置和题集。各框架的原生提示与工具仍不同。
- Table 4 / Appendix B.1 未给出可统一引用的整条轨迹累计 token 或墙钟上限；1M 上下文不能写成 1M 累计 token 预算。

以上依据 [技术报告 §5.3.4 与 Table 4，第 34–35 页](https://arxiv.org/pdf/2609.19969v1#page=34)。同报告 §5.3.3 说明 API low / high / max 分别对应 effort 50 / 75 / 100。

官方另提供 [DeepSWE 复现说明](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/resolve/main/evaluation/README.md?download=true)，固定 Pier 0c802fc067a425345b24d1c69411aa98acf61a1d 和 DeepSWE 0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea。mini 示例使用 reasoning_effort=max、cost_limit=0，并说明多次运行取平均。这是复现示例，不据其参数反推所有原始框架实验的未披露预算。

## 与本项目图表并列时

本项目 [历史实测](evaluation-results.md) 是 SWE-bench Lite 按 instance_id 排序后的固定前 30 题：裸模型 9/30（30.0%），C harness 20/30（66.7%）；动作 / 裸模型请求使用 high，C 摘要请求使用 none。C 上限为 300 动作轮、5M 累计 tokens、900 秒循环。

图中应把“本地同题对照”和“厂商公开参考”分成两个区域，并在各区标出题集、样本数和推理设置。五个数值可以同时展示，但不能据此声称本项目超过 Codex / OpenCode，或将 66.7% 推广为完整 Lite 300 题成绩。本项目实际跑过的 mini 三题对照也不是这里的官方 74.2%。服务端模型别名不能证明历史请求与论文 checkpoint 的权重完全相同。
