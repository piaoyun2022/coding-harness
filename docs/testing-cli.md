# CLI 集成验证

tests/test_cli.py 的离线测试验证以下实际边界：

1. 本地可信副本中 apply_patch 修复函数、execute 跑真实 Python 检查并 finish，原始仓库保持不变。
2. 根目录 AGENTS.md 进入模型上下文，源目录 .env 不进入副本。
3. local 未显式 --allow-local 时，创建产物与启动模型之前拒绝。
4. 环境启动失败仍有结构化 result 与空补丁，不暴露异常正文中的假密钥。
5. 主循环模型失败、补丁导出失败、环境清理失败分别记录；客户端仍关闭。
6. KeyboardInterrupt 仍导出补丁并关闭客户端。
7. probe 限制输出，响应包含假密钥时日志、结果与控制台均脱敏。
8. JSON 设置文件可被显式模型/推理参数覆盖。
9. 已存在产物目录不会覆盖。
10. 精确 env-file 不存在时不向父目录寻找凭证。
11. baseline 在固定 Git 基线取上下文，仅一次无工具模型请求，生成补丁但不应用；
    dataset 与 grade-command 不要求模型凭证，也不偷偷启动官方 grader。

运行：python -m pytest tests/test_cli.py -q。
所有模型请求均由替身处理；真实 subprocess 只执行本测试自己生成的可信小文件。
测试通过不能解释为 DeepSeek 代码能力或 SWE-bench 分数。


WSL真实平台验收发现CLI离线fixture硬编码python别名，在仅有python3且未激活venv时返回127；测试现使用当前sys.executable，PowerShell加调用运算符，避免把主机别名配置误判为harness失败。产品命令仍由任务环境决定。

V2 新增参数化装配测试：Local、Docker、Modal 均接收独立的捕获字节限额和模型预览字符限额。
此项使用构造器替身验证 CLI 接线，既不连接 Docker/Modal，也不能替代真实容器输出捕获测试。
