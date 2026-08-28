# 03: 反思与用户画像注入每 run 上下文（A）

**What to build:** agent 上下文组装时，在 Memory Snapshot 之后注入两个新块（受
per-agent 开关控制）：① `memory/reflections.md` 的节过滤内容——Insights & Discoveries
全节 + Hypotheses & Experiments 的 ✅/❌ 结论行，上限 2000 chars（按 `## ` 标题切分，
排除 Open Questions / 🔄 / Next Cycle Seeds）；② `memory/user_profile.md` 全量（硬上限
2000，用户撰写非增长日志）。注入块带低信任声明（self-observed，treat as hypotheses with
evidence, not facts）。开关为 per-agent 布尔键 `context_inject_reflections_{agent_id}`
（缺省 false）。上线后对 2-3 个 agent 开启灰度并记录成本。

**Blocked by:** 01（门禁扩展，保证 run 内也喂给 reflections）、02（心跳收敛，保证
reflections 新鲜度）——均已 code-done/提交

**Status:** done（部署 b222006b 后灰度开启 3 agent + 生产注入验证）

- [x] 上下文组装服务增加 reflections 节过滤提取（`## ` 切分，取 Insights 全节 +
  Hypotheses 的 ✅/❌ 行，上限 2000 chars）与 user_profile 读取
- [x] 注入块插在 Memory Snapshot 之后，`<reflections_context>` 标签 + 低信任声明句
- [x] per-agent 开关读取（`context_inject_reflections_{agent_id}` 布尔键，缺省 false；
  键关闭时 dynamic 块字节级不变）
- [x] 单元测试：开关 off 不注入；开关 on 注入且节过滤正确（Open Questions / 🔄 /
  Next Cycle Seeds 被排除）；截断生效；低信任声明存在；user_profile 注入；
  开关使能分支直测（enabled true/false、无行、畸形 value 四例）
- [x] 灰度：3 个 agent 开关键已写（b1a73489 / 27d55a64 / 475264c9，reflections 内容
  最新最丰富）；容器内 probe 验证注入真实生效（Reflections Snapshot=Insights 2 条、
  Open Questions 被排除、User Profile、低信任声明齐备）；实测注入段 ~0.7K token/step
  （低于估算 1.6K，因节过滤裁掉待办与种子）
- [x] 全量 pytest（3189 passed）+ `scripts/arch-guard.sh` 通过
- [x] code-review 两轴 pass；修正 Spec-1（开关使能分支补直测）、Spec-2（user_profile
  硬上限注释）、Std-2（测试改名 off_switch_injects_nothing）；Spec-3/4、Std-1 评估为
  nit 不改
