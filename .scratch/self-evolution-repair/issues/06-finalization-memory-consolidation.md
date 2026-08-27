# 06: 任务收尾自动记忆固化检查（方案 B）

**What to build:** 每次任务交卷前（最终回复之前），agent 固定执行一次记忆固化判定：
本次工作是否产生了跨会话耐用信息——有且未记录则读改写 `memory/memory.md` 并同步
`memory/MEMORY_INDEX.md`；没有则什么都不做。用户无需显式说「记住这个」，
问答/审查/优化等任务在收尾时自动判定是否沉淀。

**Blocked by:** None (can start immediately；在 #01 的 Memory Maintenance 小节上扩展)

**Status:** ready-for-agent

- [x] `_MEMORY_MAINTENANCE` 末尾含收尾判定条款（"before returning the final answer" 语境 + 判一次 + 无则 do nothing）
- [x] 条款与既有「Before returning the final Assistant response, verify…」收尾清单同位（_BASE_PROMPT_OUTPUT 之后）
- [x] 措辞为条件义务（非祈使目标句），read_file+write_file 门控不变
- [x] tests/test_agent_context.py 断言新条款存在（先红后绿）
- [x] 全量 pytest + arch-guard 通过
