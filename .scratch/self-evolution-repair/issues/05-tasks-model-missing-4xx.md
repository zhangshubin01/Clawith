# 05: 任务创建「租户未配置模型」返回 4xx 而非 500

**What to build:** 新租户（模型池未配置/未启用模型）在界面或 API 创建任务时，
得到明确的 400 + `error_code`（如 agent_model_not_configured）+ 可操作文案
（"请先在模型池配置并启用模型"），而不是 500 Internal server error；
真正的内部错误仍返回 500。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [x] 创建任务端点捕获 TaskRuntimeIntakeError（及同类「配置缺失」分支）→ 400 + 结构化 error_code + 可操作文案
- [x] 只改错误映射层，任务创建业务语义不变
- [x] 先红后绿 API 测试（参照 tests/test_tasks*.py 现有用例）：配置缺失 → 400+error_code；真实内部错误仍 500
- [x] 全量 pytest + arch-guard 通过
