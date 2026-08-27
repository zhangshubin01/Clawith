# 03: skill-creator 评估 runner 平台化（clawith runner + 触发判定）

**What to build:** 在 Clawith 里用 skill-creator 的 agent 能对评测集跑触发率测试：
`run_eval.py --runner clawith`（默认）经 OpenAI 兼容协议调平台模型端点，
以「响应是否含 read_file 工具调用且路径命中 `skills/<name>/`」判定触发，
结果 JSON schema 与上游一致（aggregate/generate_report 零改动）。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [x] run_eval.py 支持 `--runner {clawith|claude}`，默认 clawith；claude runner 原样保留
- [x] clawith runner 凭证走环境变量（base_url/api_key/model），缺失时报可操作错误不崩溃
- [x] 触发判定等价原语义：read_file 工具调用 + 技能路径匹配，避免假阳性
- [x] 输出 JSON schema 不变：query/should_trigger/trigger_rate/pass
- [x] 判定函数有纯函数级单测（fake client 注入），不依赖真实模型外呼
- [x] 新增文件（runner 模块）可被存量 agent 经 seeder 自动补齐；改动文件保持向后兼容
- [x] 全量 pytest + arch-guard 通过

**一次性同步动作（存量 agent）**：clawith_runner.py 是新文件，会被 seeder 自动补齐到存量 agent；旧版 run_eval.py/SKILL.md 指引文本不会被覆盖——存量 agent 需手动用新版 run_eval（--runner clawith 已是新版默认）或等技能重建。
