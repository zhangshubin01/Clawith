# 01: 记忆维护义务回归基础提示词（memory.md + MEMORY_INDEX）

**What to build:** 任何 run 类型（direct chat / trigger / heartbeat / oneshot）下，
agent 在任务中产生耐用信息（稳定偏好、既定事实、重要决策、可复用知识）时，
会主动读改写 `memory/memory.md` 并同步 `memory/MEMORY_INDEX.md` 主题清单；
无新信息时不写；临时进度不写；用户显式指令优先；写失败不阻塞交付。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [x] 基础提示词 `## Memory` 节后含 Memory Maintenance 义务（英文措辞，条件义务非祈使目标句）
- [x] 义务明确覆盖：何时写/何时不写、读改写合并（禁盲覆盖）、用户指令优先、失败不阻塞交付
- [x] 义务明确包含 MEMORY_INDEX.md 主题同步（新增/移除主题时更新 Topics 清单）
- [x] Memory 快照仍在 dynamic 低信任段（"data, not instructions" 语义不被破坏）
- [x] 无 write_file 工具权限的模型步不收到该义务（allowed-tool 门控，若有）
- [x] tests/test_agent_context.py 新增用例先红后绿；全量 pytest + arch-guard 通过
