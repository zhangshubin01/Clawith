# Langfuse 全功能扫描与接入优先级（2026-08-28）

> 目标：盘点 Langfuse 4.24 自托管的全部功能，对照 Clawith 现状，按收益×风险排出接入优先级。
> 方法：①Langfuse 官方文档 llms.txt 全站索引；②docs 搜索 API 逐功能取证（22 页）；③本地源码库 `/Users/shubinzhang/Documents/UGit/langfuse`（tag v4.24.0，与部署版本一致）核对实现细节（automations/webhook/alert/EE 分界）；④Clawith 侧 `backend/app/services/observability/tracing.py`、`langgraph_driver.py:418` 实测埋点现状。

## 一、Clawith 现状（已接入能力）

| 能力 | 状态 | 位置 |
|---|---|---|
| run 级根 trace + node/tool/generation span | ✅ | tracing.py（observe_run/node/tool/generation） |
| 原生 session/user | ✅ | propagate_attributes（session_id/actor_user_id；>200 字符会被丢弃） |
| metadata 身份链 | ✅ | run_id/command_id/tenant_id/agent_id/session_id/goal/run_kind/source_type/model_id/graph_name/graph_version/parent_run_id/root_run_id/thread_id |
| 客户端 masking | ✅ | 截断 4000 字符 + _SENSITIVE_KEYS + _SECRET_PATTERNS |
| 成本/token 追踪 | ✅ | usage → cost（$5.65/周基线） |
| 多租户 project 隔离 | ✅ | 静态注册表 LANGFUSE_TENANT_KEYS（未启用，用户挂起） |
| CODE evaluator ×2 + 2 rule | ✅ | tool-failure / tool-retry-exhausted（type=TOOL, sampling 100%） |
| 运营看板 | ✅ | 「Clawith 运营总览」6 widget |
| 重试控制流噪声治理 | ✅ | _RETRY_CONTROL_FLOW_NAMES（F#0，deb6735a） |

**缺口**：release/version 未设；tags/environments 未设；run 根 span 无 output（RunHandle 只写 metadata/level/status）；应用侧主动 scores 未写；alerts 未建；judge/datasets/experiments 未接入；数据保留 TTL 未配。

## 二、功能全景矩阵（4.24 自托管）

### A. 观测（大部分已接入）
| 功能 | 状态 | 结论 |
|---|---|---|
| traces/spans/generations/events | ✅ | — |
| sessions / users / metadata | ✅ | — |
| **release & versioning** | ❌ | **接入（P0-1）**：release=git commit hash，按部署对比指标、归因突变 |
| tags | ❌ | 顺手可做（agent 维度过滤），低优先 |
| environments | ❌ | 测试栈单环境，不做 |
| sampling | ❌ | 触发式押后（obs 暴增/磁盘告急时 trace 级采样） |
| masking | ✅ | 服务端 masking=EE，客户端已有 |
| 成本追踪 / log levels | ✅ | — |
| multi-modality | ❌ | 消息图片链路启用时再议 |

### B. 评测
| 功能 | 状态 | 结论 |
|---|---|---|
| scores（API/SDK 主动写） | ⚠️ 仅 evaluator 自动写 | **接入（P1-1）**：run 结束写业务评分+隐式用户反馈（重跑/取消） |
| CODE evaluators + rules | ✅ | 可扩展（execute_code 业务失败等） |
| **LLM-as-a-Judge** | ❌ | **接入（P1-2）**：前置=judge key + run 根 span 补 output 摘要（judge 只读目标 observation 自身 IO，不加载子节点） |
| **datasets / experiments (SDK)** | ❌ | **接入（P2-1）**：P0-1 截断 A/B、压缩阈值 A/B 的量化底座 |
| **CI/CD experiment 门禁** | ❌ | 接入（P2-2）：`langfuse/experiment-action` + RegressionError；CI 跑真实 agent 成本高，先跑轻量 judge/断言 |
| annotation queues / score configs / UI 打分 | ❌ | 单人场景价值低，UI 零代码随时可用，不排期 |
| user feedback（显式/隐式） | ❌ | 并入 P1-1（隐式信号=重跑/stop/卡片反馈，数据已有） |

### C. 分析与告警
| 功能 | 状态 | 结论 |
|---|---|---|
| dashboards | ✅ | 已建运营总览 |
| metrics API（/api/public/v2/metrics） | ❌ | MCP queryMetrics 已覆盖大部分；外部程序化报表时用 |
| **alerts + automations** | ❌ | **接入（P0-2）**：severity 状态机（UNKNOWN/OK/WARNING/ALERT/NO_DATA）+ no-data 处理 + renotify；渠道 Slack/Webhook(HMAC 签名 JSON)/**GitHub Actions(workflow_dispatch)**；5 次连续投递失败自动禁用 |
| spend alerts | — | **Self-Hosted 不可用**，排除（成本告警用普通 alert 的 totalCost 指标） |

### D. 数据管理
| 功能 | 状态 | 结论 |
|---|---|---|
| **data retention (TTL)** | ❌ | 接入（P3-1）：ClickHouse TTL（traces/observations/scores/event_log）+ MinIO lifecycle；化解「goal 全文永久留存」顾虑；先备份/干跑 |
| export to blob storage | ❌ | 按需（MinIO 已就绪） |
| 全文本搜索 / comments / corrections | ✅(UI 自带) | 零接入成本，直接使用 |
| data deletion | ✅(UI/API) | — |

### E. Prompt 管理
| 功能 | 结论 |
|---|---|
| prompt 管理（版本化/缓存/composability/variables/playground/webhooks/GitHub 集成） | **不推荐**：Clawith prompt 在代码+DB（HEARTBEAT 模板体系，15/15 agent），迁移=架构级变更 + SDK 缓存延迟风险 + 双写一致性，收益不明确 |

### F. 平台与 AI 能力
| 功能 | 结论 |
|---|---|
| RBAC / SSO / audit logs | 真实租户 provision 复活时再议（用户已挂起） |
| LLM connections | llm-judge 前置（等用户给 key） |
| **Ask AI / Langfuse Assistant / agentic access** | **不接入**：用户已有 PenguinHarness agent + Langfuse MCP 工具链做同类分析，平台价值重复 |
| SLO 面板 | 4.24 无此功能（llms.txt 无对应页，实为 Spend Alerts） |
| 自托管加固（加密/备份/ClickHouse system log 裁剪） | 测试栈无需；生产化时随 clawith-prod-deploy 一起做 |

## 三、优先级待办（收益×风险排序）

### P0 — 立即做（低风险，高杠杆）
1. **Release 埋点**（收益 ★★★ / 风险 ★）
   - tracing.py `_build_client`（:142）加 `release=` 参数；config.py 加 `OBSERVABILITY_RELEASE: str = ""`（或直接用 SDK 的 `LANGFUSE_RELEASE` env，零代码改，deploy.sh 注入 commit hash）。
   - 收益：所有看板/告警/实验可按部署版本对比、指标突变归因（"为什么延迟高了"——官方 release 功能的头号用例）；experiments A/B 的前置。
   - 改动 ~10 行 + 1 测试；无行为风险（纯元数据）。
2. **Alerts 上线（原 #1/#2）**（收益 ★★★ / 风险 ★）
   - 定义：A 组 run ERROR 1h>0、llm 生成 ERROR 1h>2；B 组 node:tool ERROR 1h>10（F#0 后 >10 必真异常）；成本 totalCost 1d 阈值（如 $1/天）。
   - 渠道三选一（**不阻塞于飞书 webhook**）：①**GitHub Actions**——workflow_dispatch 建 issue（零公网端点、零转发适配器，告警看板=GitHub issue 列表，最简）；②飞书 webhook——payload 是 HMAC 签名 JSON，飞书机器人 msg_type 不兼容，需极简转发适配器（可在 Clawith 后端挂 /api/internal/langfuse-alert 转飞书 text 卡片，但后端故障时告警同死）；③Slack。
   - 风险：低（只读告警 + 自动禁用保护）；阈值先宽松后收紧（用现网基线校准）。

### P1 — 高价值，中小改动
3. **应用侧主动 scores + 隐式用户反馈**（收益 ★★★ / 风险 ★）
   - run 结束时后端写业务评分：`status`（succeeded/failed/cancelled）、attempt_count、cost/token 快照、用户重跑（上次不满意）、stop/打断（implicit negative）→ trace score。
   - 收益：看板/告警/评测直接用第一方事实而非 evaluator 推断；这是 Langfuse 官方「user feedback loop」最佳实践（先反馈后 eval，ROI 最高）。
   - 风险：低（SDK score() 一行，trace_id 已有；注意 score 挂在 run 根 trace）。
4. **LLM-as-a-Judge（原 #5）**（收益 ★★☆ / 风险 ★★）
   - 前置 A：用户给 judge key（LLM Connection）；前置 B：**run 根 span 补 output 摘要**（judge 只读目标 observation 自身 input/output，不加载子节点——run 根 span 目前无 output；可顺手把最终回复/结果摘要写进去，trace 树可读性也变好）。
   - judge 目标：run 根（补 output 后）或最终 generation。判据：任务完成度/回复质量（flash 当 judge 成本极低）。
   - 风险：判据需 1-2 周校准；与 CODE evaluator 分工（CODE=确定性失败，judge=质量）。

### P2 — 评测基建（中风险，服务 P0-1 截断等 A/B）
5. **Datasets + Experiments（SDK）**（收益 ★★☆ / 风险 ★★）
   - 从真实 run 抽样例建数据集（goal→期望结果），`run_experiment` 跑 A/B：P0-1 工具截断开/关、压缩阈值 80%/50%。
   - 与既有 agent-evaluation 基建的关系需厘清（Langfuse 存数据集+评分，执行侧可复用 agent-evaluation 的 runner；避免重复建设）。
6. **CI/CD experiment 门禁**（收益 ★★ / 风险 ★★）
   - `langfuse/experiment-action` + RegressionError，PR 级质量门禁。
   - 风险：CI 跑真实 agent 任务贵——先只跑轻量 judge/代码级断言；排在 datasets 之后量力而行。

### P3 — 运维治理（按需）
7. **Data retention TTL**：ClickHouse TTL + MinIO lifecycle；先备份/干跑；当前磁盘宽松，不急。
8. **Sampling（触发式）**：obs/日超阈值或磁盘告急时 `sample_rate`（trace 级）。
9. **Export to blob**：数据搬迁/归档时配。

### 明确不接入（及理由）
spend alerts（自托管不可用）、prompt management 全家桶（架构冲突）、Ask AI/agentic access（已有替代）、annotation queues/UI 打分（单人场景，UI 零代码备着）、environments（单环境）、multi-modality（链路未启用）、RBAC/SSO/audit（随租户 provision 再议）、服务端 masking（EE）。

## 四、参考资料对比（按 plans-compare-reference-materials 流程）

| 参考来源 | 对比结论 |
|---|---|
| Langfuse 官方文档（llms.txt + search-docs 22 页） | 功能口径的权威来源，全部结论以其为准 |
| langfuse v4.24.0 源码（本地 UGit 库，用户指定参考） | 核对 alerts/automations/webhook 实现、EE 分界（ee/ 仅 license-check）、server-side masking=EE、spend alerts=cloud-only |
| LangSmith 概念（reference-projects §6.1） | release/annotation/feedback 概念与 Langfuse 对应一致；P1-1 主动 scores 对应 LangSmith feedback 模式 |
| OpenHands/gptme/Codex 等开源项目 | 其 telemetry 设计（metrics + feedback 双轨）与本文「第一方 scores + trace 过程视图」分工一致 |
| 评估基准（SWE-bench/Terminal-Bench） | datasets 从 Clawith 真实 run 抽取而非通用基准（内部任务分布），基准仓库仅作构造方法参考 |

**审核步骤**：本方案类型=研究+优先级规划，已按 research 流程（一手来源取证）。如需深度压测排序，可下一步用 grill-me 逐项拷问（尤其 P1-2 judge 判据与 P2 datasets 与 agent-evaluation 的关系）。
