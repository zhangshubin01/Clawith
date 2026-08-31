# ADR-0010: GitLab 绑定 —— per-binding URL + init 状态机根治

- **状态**: 已接受（2026-08-31）
- **前置事故**: 2026-08-31 保存 GitLab 绑定「无响应」。nginx/DB 证据链：04:23:27–36 用户连点
  5 次保存全部 200（每次 PUT 都调度一个重复 init 任务），后台任务 04:23:39 完成 `done`；
  但前端只轮询 `initializing`、对 `pending` 无徽标无轮询 → UI 冻结、零反馈。
- **相关**: docs/technical-plans/20260820-gitlab-agent-binding（原设计）

## 背景

1. 保存后 PUT 先把 `init_status` 重置为 `pending`，后台 init 任务要抢到 agent 级锁后才写
   `initializing`；前端 `load()` 竞态读回 `pending` 的概率极高。
2. 前端 `GitlabBinding.tsx` 对 `pending` 完全失明：`StatusBadge` 返回 null、轮询 effect 只在
   `initializing` 时启动 → 保存后无徽标、无轮询、无反馈；连点 5 次保存还各排一个重复任务。
3. 全局 `GITLAB_BASE_URL` 定死 GitLab 实例；用户要求每个绑定可填完整 URL
   （`http://192.168.5.254/zhangshubin/mydome1`），支持指向任意实例。
4. 静默失败路径：token 解密失败（SECRET_KEY 变更）或解绑后重存未填 token 时，保存返回 ok 但
   init 永不被调度，前端还会因乐观 `pending` 显示假进展。

## 决策

1. **per-binding URL**：`project_path` 字段接受两种形式——`group/repo`（沿用全局
   `GITLAB_BASE_URL`）或完整 URL。PUT 时后端解析为 `extra_config.base_url`（
   `scheme://host[:port]`）+ 归一化 `project_path`（host 之后全部路径，天然支持多级子组）。
   旧数据零迁移（`base_url` 缺省回落全局）。解析规则：仅 http/https、必须含 host、拒绝
   userinfo/query/fragment、允许端口；**不支持 GitLab 挂在子路径下**（host/gitlab/group/repo）。
2. **init 调度幂等**：`schedule_gitlab_workspace_init` 注册表 `{agent_id: (task, signature)}`；
   同签名任务在途时复用不重复排队，签名变化才新调度；任务完成后自清理。签名 =
   (project_path, default_branch, base_url, pat)。
3. **保存时状态写入**：有在途 init 任务时不再把 `init_status` 重置为 `pending`（消除覆盖乒乓）；
   在途任务自己的终态写回是权威。
4. **前端状态机**：`pending` 与 `initializing` 同义为「进行中」——都显示「初始化中…」徽标、
   都轮询；轮询读回 `pending` 继续轮询不清定时器；轮询 2 分钟（40×3s）超限显示
   「初始化超时，请刷新查看」（clone 本身有 600s 超时，超时不等于失败，不误标 failed）。
   状态机抽纯函数模块 `frontend/src/lib/gitlabBindingState.ts`，合同测试直接覆盖。
5. **保存反馈**：PUT 成功后乐观置 `pending` 立即出徽标；`window.confirm` 换成 `useDialog`
   的 Promise confirm（仅项目路径变更这一处）。
6. **静默失败置态（fail-early）**：token 解密失败 → `init_status=failed` +
   「Token 解密失败…请重新填写」；绑定无 token（解绑后重存未填）→ `failed` +「未配置 Token」。
7. **解绑凭证清理**：insteadOf 键改用 per-binding `base_url`（回落全局），不再只认全局配置。

## 后果

- 旧绑定（无 `base_url`）行为不变；`channel_configs.extra_config` 增加可选 `base_url` 键，
  无需迁移。
- 在途任务期间的「改参数保存」不打断旧任务；新参数在旧任务结束后下次保存（或当前任务终态写回
  后用户重试）时生效——用户重试成本 = 一次保存点击，可接受。
- 前端对 URL 表单的展示 = `base_url + '/' + project_path`（有 base_url 时），保证表单
  往返无损；路径变更确认比较同口径展示串。
