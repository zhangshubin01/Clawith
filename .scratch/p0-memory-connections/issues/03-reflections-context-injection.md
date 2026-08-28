# 03: 反思与用户画像注入每 run 上下文（A）

**What to build:** agent 上下文组装时，在 Memory Snapshot 之后注入两个新块（受
per-agent 开关控制）：① `memory/reflections.md` 的节过滤内容——Insights & Discoveries
全节 + Hypotheses & Experiments 的 ✅/❌ 结论行，上限 2000 chars（按 `## ` 标题切分，
排除 Open Questions / 🔄 / Next Cycle Seeds）；② `memory/user_profile.md` 全量。注入块
带低信任声明（self-observed，treat as hypotheses with evidence, not facts）。开关为
per-agent 布尔键 `context_inject_reflections_{agent_id}`（缺省 false）。上线后对 2-3 个
agent 开启灰度并记录成本。

**Blocked by:** 01（门禁扩展，保证 run 内也喂给 reflections）、02（心跳收敛，保证
reflections 新鲜度）

**Status:** ready-for-agent

- [ ] 上下文组装服务增加 reflections 节过滤提取（`## ` 切分，取 Insights 全节 +
  Hypotheses 的 ✅/❌ 行，上限 2000 chars）与 user_profile 读取
- [ ] 注入块插在 Memory Snapshot 之后，`<reflections_context>` 标签 + 低信任声明句
- [ ] per-agent 开关读取（`context_inject_reflections_{agent_id}` 布尔键，缺省 false；
  键关闭时行为与现状完全一致）
- [ ] 单元测试：开关 off 不注入；开关 on 注入且节过滤正确（Open Questions / 🔄 /
  Next Cycle Seeds 被排除）；截断生效；低信任声明存在；user_profile 注入
- [ ] 用现有 context profile 脚本实测开启 agent 的注入段 token 增量并记录
- [ ] 灰度：为 2-3 个生产 agent 写开关键并确认注入生效
- [ ] 全量 pytest + `scripts/arch-guard.sh` 通过
