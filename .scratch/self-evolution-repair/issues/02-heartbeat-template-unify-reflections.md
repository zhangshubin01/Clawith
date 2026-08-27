# 02: HEARTBEAT 模板统一 + reflections 维护指令

**What to build:** 心跳周期重新驱动 `memory/reflections.md` 的自进化——读旧反思、
记录新假设/验证洞见/开放问题、写 next cycle seed；同时仓库只剩一版 HEARTBEAT
模板，杜绝「旧模板要求写 reflections、新模板不要求」的再分叉。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [x] `agent_template/HEARTBEAT.md`（新 agent 实际读取的来源）合并旧版 Phase 1（读 reflections）/ Phase 3（写发现）/ Phase 4（next cycle seed）
- [x] 保留新版「无兴趣点则 HEARTBEAT_OK」的防空转护栏（与 curiosity_journal Phase 2 并存）
- [x] `app/templates/HEARTBEAT.md`（fallback 来源）与 agent_template 版内容一致（逐字一致或 hash 断言防漂移）
- [x] 新增模板内容断言测试（参照 tests/test_migrate_legacy_heartbeat_template.py 先例）：reflections 维护指令存在、HEARTBEAT_OK 护栏存在、两版一致
- [x] ticket 附存量 agent HEARTBEAT.md 的一次性同步说明（不写批量回写代码）
- [x] 全量 pytest + arch-guard 通过

**一次性同步动作（存量 agent）**：容器内执行 `python -m app.scripts.migrate_legacy_heartbeat_template --apply`——迁移清单已扩展覆盖统一前的最小模板（cb4dfa9c）与四阶段模板（5aed0d8c），dry-run 先行核对。
