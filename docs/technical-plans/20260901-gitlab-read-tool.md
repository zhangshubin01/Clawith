# gitlab_read — 内网 GitLab 只读工具（MR/Issue/Commit/文件）

- 日期：2026-09-01
- 状态：方案待确认
- 背景：run d11c8c20 读内网 GitLab MR `http://192.168.5.254/zhangshubin/mydome1/-/merge_requests/1` 时
  read_webpage 拒内网、jina_read 走云端死路 + 10 次重试卡死 2.5min（已修复：jina_read 拒私有 URL）。
  根需求=让 agent 能读内网 GitLab 资源。实测容器 → 192.168.5.254 连通（根 302 / MR 302 登录页 / api v4 401=端点活）。

## 设计

**工具**：`gitlab_read(url, max_chars=8000, include_diff=false)`。模型直接贴用户给的 GitLab URL，
后端解析后走 GitLab REST API v4 只读端点。零新配置：复用现有 agent GitLab 绑定。

### 复用的现有资产
- 绑定：`ChannelConfig`（channel_type="gitlab"），`app_secret`=加密 PAT、
  `extra_config`={project_path, base_url, default_branch}（`app/api/gitlab_binding.py` 已上线）。
- 解密：`decrypt_data(app_secret, SECRET_KEY)`（app.core.security）。
- 全局兜底：`GITLAB_BASE_URL`（config.py:155 = http://192.168.5.254）。

### SSRF / 越权边界（硬约束）
1. 输入 URL scheme 仅 http/https；解析后 host:port **必须等于绑定 base_url 的 host:port**（规范化后比对）。
2. 只允许两类路径：GitLab Web URL（`/-/merge_requests/{iid}`、`/-/issues/{iid}`、`/-/commit/{sha}`、`/-/blob/{ref}/{path}`）
   与 `/api/v4/...` 路径；其余 path 拒绝（含 `/-/` 下的 admin/其他端点）。
3. 目标 project **只允许绑定 project_path**（one token + one project 语义；防止 agent 借 token 读别的仓库）。
4. 内部仅发 GET；PAT 只进 `PRIVATE-TOKEN` header，绝不进日志/错误摘要/URL。

### API 映射（GitLab REST v4）
| Web URL | API |
|---|---|
| `/-/merge_requests/{iid}` | `projects/{urlenc project_path}/merge_requests/{iid}` + notes 摘要 + changes 文件列表/统计 |
| `/-/issues/{iid}` | `projects/{...}/issues/{iid}` + notes 摘要 |
| `/-/commit/{sha}` | `projects/{...}/repository/commits/{sha}` |
| `/-/blob/{ref}/{path}` | `projects/{...}/repository/files/{path}/raw?ref={ref}`（path 逐段编码保留 `/`） |
| `/api/v4/...` | 原样转发（仍限 GET + 绑定 host） |

输出=markdown 摘要（MR：标题/状态/作者/labels/描述/变更文件/首 N 条 notes；文件：原始内容；commit：message+文件列表），
`max_chars` 截断（≤20000），附 `web_url`。

### 错误分类与重试（与 jina_read 同一套口径）
- 未绑定 / URL 校验失败 / 401 / 403 / 404 → `retryable=False`，摘要带指引。
- 401 特殊提示：绑定 PAT 可能只有 read_repository scope（原为 git clone），需 **read_api** scope；用户重绑后可用。
- 5xx / timeout / ConnectError → `retryable=True`；TLS 证书类错误复用 `_is_ssl_verification_error` → 不重试。

### 注册
- `builtin_tool_definitions.py`：定义（category=search，icon=🦊 或 📚）+ `_READ_TOOL_NAMES` 加入（policy=read/safe/parallel_safe）
  + `_TIMEOUT_SECONDS["gitlab_read"]=60` + readiness=**`configured_channel`**（未绑定不出现在工具列表，比运行时失败更早收敛）。
- `agent_tools.py`：`_gitlab_read_outcome` + `execute_builtin_tool_outcome` 分支。

## 待确认决策点
1. **project 范围**：本期严格「仅绑定项目」；是否需要放开到「同 GitLab 实例任意可见项目」（token 权限即读）？
2. **include_diff**：MR changes 的 diff 可能巨大，默认 false（只给文件列表+统计），需要全文 diff 时模型显式传 true。
3. 要不要同时做「通用内网白名单 fetch」工具（非 GitLab 内网站点）？建议二期，本期聚焦 GitLab。

## 实施与验收
- 测试：绑定缺失/host 不匹配/project 不匹配/路径白名单/API 成功映射（monkeypatch httpx）/401/403/404/5xx 分类。
- `scripts/arch-guard.sh`；部署走常规 deploy 流程（测试环境，不灰度红线）。
- 验收：真实 run 中让绑定 agent 读 `http://192.168.5.254/zhangshubin/mydome1/-/merge_requests/1` 返回 MR 内容。
