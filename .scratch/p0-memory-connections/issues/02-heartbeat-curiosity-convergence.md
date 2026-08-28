# 02: 心跳收敛 curiosity 待办条目（G）

**What to build:** heartbeat Phase 3 新增一步收敛——把 Phase 2 写进
`memory/curiosity_journal.md` 的 Follow-up 与 Active Questions 中值得跟进的条目
promote 进 `memory/reflections.md` 的 Next Cycle Seeds（≤3 条），原条目行尾标
`→promoted YYYY-MM-DD`（不删除）。Phase 2 的措辞同步把 curiosity 定位为纯探索日志
（durable findings 留到 Phase 3）。既有 agent 的心跳文件按 SHA 精确匹配批量迁移到
新模板，不匹配的列清单人工合并。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 心跳模板 Phase 3 增加收敛步：读 curiosity 的 Follow-up 与 Active Questions，
  promote 到 Next Cycle Seeds（≤3），原条目行尾标 `→promoted YYYY-MM-DD`，不删除
- [ ] 心跳模板 Phase 2 定位句改为：curiosity 是 raw exploration log，durable findings
  留到 Phase 3 进 reflections
- [ ] 模板内容测试断言（Phase 3 含收敛步、Phase 2 含定位句）
- [ ] 对全部生产 agent 跑迁移脚本 dry-run：输出匹配/不匹配清单；匹配的 apply；
  不匹配的逐个人工合并（抽样已确认至少 b1a73489 为模板版）
- [ ] 迁移后抽查：生产 agent 心跳文件 Phase 3 含收敛步；原有自定义内容未被覆盖
- [ ] 全量 pytest + `scripts/arch-guard.sh` 通过
