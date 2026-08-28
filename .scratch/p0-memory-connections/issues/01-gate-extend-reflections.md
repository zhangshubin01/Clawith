# 01: 门禁与常驻提示扩展至 reflections（B）

**What to build:** run 收尾的门禁强制轮和每 run 常驻的 Memory Maintenance 提示，都把
「本次 run 学到的教训」引导写入 `memory/reflections.md` 的四节之一，而不是只能写
`memory.md`。行为不变的部分：门禁触发条件、条件义务（无耐用信息则跳过）、路径计数
（`memory/` 前缀天然覆盖 reflections，不改检测）。

**Blocked by:** None (can start immediately)

**Status:** done

- [x] 门禁强制轮 prompt 增加分类判据：跨会话稳定事实/偏好/决策 → memory.md；本次 run
  学到的教训、验证过的假设、原因分析 → reflections.md 语义匹配节
- [x] 门禁 prompt 点名四节（Open Questions / Hypotheses & Experiments / Insights &
  Discoveries / Next Cycle Seeds），要求 append 到匹配节、默认 Insights & Discoveries、
  不存在的节不动（措辞 "do not create new sections"）
- [x] 常驻 Memory Maintenance 段同步扩展，与门禁共用同一分类判据（两处一致，非两套标准）
- [x] MEMORY_INDEX 义务句保留不删（废弃 INDEX 是 P2 独立决策）
- [x] 测试：门禁 prompt 常量断言含分类判据与四节；常驻段断言含 reflections 分类；
  票 07 门禁行为回归全绿（有写无记忆→强制轮、有记忆写→直通、无写→直通等既有场景）
- [x] 全量 pytest（3182 passed）+ `scripts/arch-guard.sh` 通过
- [x] code-review 两轴 pass；修正 Spec-1（"nothing durable" 跳过句分支覆盖——
  "neither durable facts nor lessons"）、Spec-2（判据句/append/defaulting 入断言）、
  Spec-3（"do not create new sections"）、Std-1/2（`-> None` 注解、顶部 import）
