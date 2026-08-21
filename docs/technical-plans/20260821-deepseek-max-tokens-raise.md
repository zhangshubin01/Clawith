# DeepSeek 输出上限 8192→32768/64K + 截断修复指令收紧 —— 事故分析与决策

> 2026-08-21。事故 run `a4f3f879`（trace `d85e932aec96`）以 `model_incomplete_output`
> 失败（「The model output remained truncated after one bounded repair.」）为引，定位到
> deepseek provider 默认输出上限沿用 V3 时代的 8192，并发现截断修复指令未针对成因。
> 本文记录证据、参考对比与决策。

## 1. 事故还原（已按 checkpoint 表时间戳逐条核实）

| 时间(UTC) | 事件 |
|---|---|
| 16:03:26 | 模型调用开始 |
| 16:04:21.9 | 该调用流式生成 ~55s 后返回 `finish_reason=length`（第 1 次截断）→ 修复消息 `[74]` 落盘，`repairs.incomplete_output` 0→1 |
| 16:04:21 | 修复轮调用 → 模型选择继续工具调用（upsert_focus_item/write_file）而非直接回答 |
| 16:04:56 | 下一调用（input=64963 tokens）→ delete_file + write_file |
| 16:05:01 | 工具结果落盘 + 最后一次模型调用开始（请求日志就在该秒） |
| 16:05:01→58 | 最终答复流式生成 ~57s，打满 8192 上限，`finish_reason=length`（第 2 次截断） |
| 16:05:58.089 | `repairs["incomplete_output"]=1 ≥ 1` → 硬失败 `model_incomplete_output` |

关键事实：**57 秒的「日志空白」不是日志缺失，而是单次流式调用打满 8192 token 的生成时长**；
截断是 Clawith 自己传的 `max_tokens=8192` 造成的，不是 API 能力上限。

## 2. 证据：8192 是 V3 遗留值

- DeepSeek 官方 Pricing 页（2026-08-21 抓取）：`deepseek-v4-flash`(V4-Flash-0731) /
  `deepseek-v4-pro`(V4-Pro-0813) —— **CONTEXT LENGTH 1M，MAX OUTPUT MAXIMUM 384K**。
  旧 deepseek-chat（V3）输出上限 8192，`PROVIDER_REGISTRY` 的 deepseek 条目自那时未更新。
- 取值链：`get_max_tokens()` 优先级 = DB `llm_models.max_output_tokens`（该模型行为 NULL）
  > model 前缀表（deepseek 无）> provider 默认（8192）> 4096。实际生效 8192。
- 隐藏硬伤：`write_file` 的文件内容内嵌在工具调用 `arguments` 里，同样计入输出 token ——
  8192 会卡死大文件写入（约 600-800 行代码）。

## 3. 参考项目对比（本地源码逐项核实，2026-08-21）

| 项目 | 输出 token 策略 |
|---|---|
| openai-agents-python | `ModelSettings.max_tokens: int \| None = None`（交给 API 默认） |
| Codex CLI | `models-manager/models.json` 无 max_output_tokens 字段（交给 API） |
| SWE-agent | None → litellm 注册表 → 兜底 **64000**（`models.py:604-615`） |
| gemini-cli | per-profile：prompt-completion 16000、classifier 1024（`defaultModelConfigs.ts`） |
| Clawith 其他 provider | openai 16384、minimax 16384、anthropic 8192 |

行业一致倾向：不设上限或兜底 16K–64K；8192 垫底，比自家 openai 档还低一档。

## 4. 决策

1. **deepseek 默认上限 8192 → 32768**，并加模型映射 `deepseek-v4-flash/pro → 65536`
   （`client.py` PROVIDER_REGISTRY）。理由：32K 覆盖 write_file 大文件与长总结，
   与行业兜底同数量级；`max_tokens` 只设上限不强制生成，常规轮输出仍短。
   成本上限可控（flash 输出 $0.66/1M off-peak，32K 打满 ≈ $0.021/次）。
   不设 384K：无界输出在长上下文里会诱发失控超长答复（延迟/成本/体验三输）。
2. **截断修复指令改为针对成因**（`model_step_service.py:1292`、`caller.py:773`）：
   「exceeded the output limit → produce a complete but **much shorter** final answer」。
   即使上限调大，超长最终总结仍是坏体验；两处测试只断言含 "truncated"，不破坏。
3. **不放松「第 2 次截断即失败」的有界修复语义**：修复指令收紧后第 2 次截断触发条件
   已消除；若再复现同款失败，再考虑改「连续截断计数」。
4. 灰度高优先（未执行）：DB `llm_models.max_output_tokens` 优先级最高，可单模型覆盖试跑。

## 5. 验证

- `tests/test_finish_protocol.py::test_call_llm_truncated_output_repair_is_bounded`
- `tests/test_agent_runtime_model_step_service.py::test_truncated_plain_text_is_not_treated_as_a_final_candidate`
- `scripts/arch-guard.sh`
