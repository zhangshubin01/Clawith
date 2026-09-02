# ADR-0014: 同会话任务状态——会话级任务状态确定性落库与 phase-aware 无条件注入

- **状态**: 已接受（2026-09-01，ask-matt 路由 → grill-with-docs 评审定案；用户裁定「按三方参考资料推荐做法」）
- 关联：`docs/technical-plans/20260901-same-session-task-state.md`（实施细节）；票 04/05/06（`.scratch/run-context-inheritance/issues/`）

## 背景

事故 61c27271 根治（R1/R3/R4，96535129 已上线）后遗留：**任务状态不是一等公民**。
`_prior_run_summary` 对上一 run 一律宣称「上一轮已完成」，三种真实形态失真——waiting
反问（且其清单不落库，R1 只挂 completed）、失败/取消、已完成但有未决事项。main 基线
（上游 45fc701c）的衔接机制只有「全量回放 + LangGraph 滚动摘要」两个 LLM 通道，零
确定性状态投影层。三方参考实证（本地源码已核实）：dsh GoalPhase 状态机
（active/paused/blocked/complete）+ CHECKPOINT_PREAMBLE 无条件回注 + append-only
事件日志（一切投影派生，无「非终态不持久」）；OpenHands goal 状态以
ConversationStateUpdateEvent(key="goal") 事件在状态转换时持久化 + resume_goal_loop()
显式续做；Letta core memory 无条件注入；LangGraph 官方 checkpointer=thread 内短时
（同 thread 自动延续）。共识：紧邻衔接=状态延续/无条件注入，不靠检测、不靠模型自觉。

## 决策

1. **D-7 存储**：会话级任务状态（goal/phase/ended/未决事项）确定性落库——
   `memory/任务状态.md` 文件权威（每会话一节、同会话最新替换）+ open_items 纯索引指针
   `{"task_state_ref": ...}`。已核实 direct chat 的 open_items 无 LLM 写入方（后台
   scanner 只扫 group、D-015 跳过 delta 合并），指针行零重述风险；零 schema 迁移。
2. **D-8 phase 语义**（对齐 dsh GoalPhase 四相位）：waiting_*→paused；failed/cancelled
   →blocked；completed→complete（无未决事项）/ active（有未决事项）。`ended` 记录 run
   落点，措辞按 (phase, ended) 派生。active 因清单指针无 resolve 方可长期持续——期望
   语义（「继续」有据可依）。
3. **D-9 注入无条件**：同会话 phase≠complete 每轮开局注入（桥活跃=替换桥内固定短语；
   桥不活跃=独立注记）；complete 零注入。不引入检测正则、不新增模型工具。
4. **D-6 作用域**：写入+注入仅 direct chat（thread==session）；group 任务状态为观察项。
5. **D-10 容错**：写失败不阻塞收尾、读失败不阻塞开局、重放幂等、自 run 守卫
   （state.source_run_id == context.run_id 跳过）。
6. **D-11 模型可见契约**：5 短语（blocked 按 ended 区分 failed/cancelled）+ 注记模板
   为契约常量，同步 backend/AGENTS.md「Model-facing contracts」段，单测钉死。
7. **D-12 waiting 清单落库（票 06）**：R1 触发条件扩 waiting_*、`_closing_content` 增
   waiting_request 分支——对齐 dsh append-only 哲学，非终态产物同样确定性持久。
8. **跨线程**（新会话）任务快照泛化、显式 session mention（dsh session-reference
   协议）：暂不做，事故驱动。

## 后果

- 正向：waiting/失败/未决三形态的上下任务衔接有确定性状态可依；「继续」不靠模型
  自觉；waiting 反问附带的清单不再丢失；补齐 main 基线缺失的状态投影层。
- 负向：active 注记长期常驻（每轮 +1-2 行，有界），模型可能把无关新任务锚到旧清单
  （过去时「非当前任务」措辞对冲）；若实测出现锚定事故，收紧为「清单指针更新后仅
  首轮注记」（观察项，非回滚）。
- 中立：group 会话任务状态未治理（多 agent 交错语义不匹配「上一轮任务」概念）；
  未决事项指针无执行完毕清理方（R1 版本合并已有替代路径，观察）。
