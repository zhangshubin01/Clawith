# 04: skill-creator 描述优化循环去 Anthropic 依赖

**What to build:** `improve_description.py` / `run_loop.py` 不再 `import anthropic`
（平台内包未安装、无 ANTHROPIC_API_KEY），改用轻量 OpenAI 兼容 client（httpx），
模型/端点走与 #03 相同的环境变量族；优化循环在平台当前模型下可运行。

**Blocked by:** None (can start immediately；与 #03 同属 skill-creator 脚本，建议紧随其后实施以共享环境变量约定）

**Status:** ready-for-agent

- [x] anthropic SDK 引用从 improve_description/run_loop 中移除（import 与 client 构造）
- [x] 新 client 走 httpx + 环境变量（与 #03 一致的 CLAWITH_EVAL_* 变量族）
- [x] 缺凭证时报可操作错误（不静默失败）
- [x] 优化逻辑（blinded history、train/test 拆分、best 选择）行为不变
- [x] fake client 单测：无凭证错误路径 + 一次迭代的 improve 调用序列
- [x] 全量 pytest + arch-guard 通过
