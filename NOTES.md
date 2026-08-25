# NOTES — 用户的世界（loop-me 工作底稿）

> 记录用户反复使用的工具、处理的通道、以及他自己的术语。工作流 spec 的素材来源，不是产出物。

## 我是谁
- Clawith 开发者/运维者。Clawith = 多租户企业 Agent 平台（FastAPI + SQLModel + LangGraph 后端，React 前端）。
- 工作区 `/Users/shubinzhang/Documents/agent/Clawith`，远端 `github.com/zhangshubin01/Clawith`，主分支 `f-shubin-0806`。

## 工具
- **git / gh**（已鉴权）；部署用 `git worktree` 干净检出，从不直接用工作树。
- **docker compose**（OrbStack 引擎，项目名 `clawith-agent`）；容器 backend(8008)/frontend(3008)/postgres/redis 均不发布宿主端口。
- **postgres / redis**：`docker exec` 进入，只读默认。
- **知识图谱**（codebase-memory MCP）：结构性代码问题优先查图谱，不盲 grep。
- **Clash Verge 代理**（TUN + fake-ip）：反复造成 DNS 解析失败、gradle/google 下载挂起、`git push` SSL_ERROR_SYSCALL——宿主编外因素，代码无关。

## 通道
- **飞书**：WS 长连接（无公网 URL，卡片按钮回调不可用，交互一律走对话内指令）；卡片流式（CardKit）。
- **Web Chat**：localhost:3008，/ws/chat 与 /ws/group 两条传输。
- **GitHub**：PR / issue。

## 用户术语（已 sharpen 的规范词）
- **部署/上线/回滚** → 对测试 compose 栈的镜像重建 + 容器 recreate。
- **worktree** → `/tmp/clawith-deploy-<commit>`，是运行中容器的 bind-mount 源，**绝不能删**。
- **回滚标签** → 替换镜像前打的 `clawith-agent-backend:pre-<新commit>-<旧镜像短sha>`，是回滚的唯一把手。
- **三件套** → 部署后特征验证：`upload_limits.py` 在 `api/`、`DB_RESERVED_CONNECTIONS`+`get_shared_checkpoint_pool`、`_bind_if_exists`×8 在、`_execute_code_legacy` 不在。
- **并行会话** → 同一工作区常有另一 agent 会话同时改代码/部署，是最大的污染源。
- **红线** → 不可违反的硬约束（动容器前必须用户指令、worktree 不删、alembic 头须一致）。

## 反复出现的 loop（候选工作流）
1. **部署循环**（已 22+ 次，最频繁）：改完代码 → 测试过 → 上线 → 验证 → 打回滚标签 → 记录。← 本次先 spec 这个。
2. **运行时故障 triage**：报错/断流/僵尸/构建失败 → 定位 → 修复 → 复盘。
3. **code review（双轴）**：branch/PR → Standards + Spec 并行评审 → 报告。
4. **研究 → 文档**：研究一个框架/模式 → 对比 → 产出 `docs/*-research-*.md`（本次 LangGraph 集成研究即一例）。
