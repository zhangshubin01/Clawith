# P0 安全缺陷代码级修复方案

- 日期：2026-08-16
- 范围：backend（`backend/app/`）三项 P0 —— 沙箱执行回退、存储 key 前缀逃逸、上传文件名穿越
- 方法：三路子代理并行深研（沙箱 / 存储 key / 上传）+ 主代理逐条复核（含路径穿越实证、死代码确认、调用点清单核对）
- 状态：已按推荐值实施（D1-D7 完成，D4 实际选择"保留加固"，见「实施状态」章节；D8/D9 部署层待做）。本文所有行号以当前 `f-shubin-0806` 分支为准，实施时以引用代码片段定位。

## 0. 总结论

三项 P0 全部成立，且研究后各有修正/扩展：

| # | 原报告结论 | 复核修正 |
|---|---|---|
| P0-1 沙箱回退 | 配置异常回退宿主机 + 全量 `os.environ` | 成立且更严重：`if fallback_config is None`（agent_tools.py:10490）是**死代码**——非 E2B 分支 10419 恒赋值，任何执行前的 `ValueError` 都触发回退；默认 `SANDBOX_TYPE=SUBPROCESS`（config.py:199）；开发机 bwrap 缺失默认裸跑宿主机（config.py:62-64）；builtin 工具 `allow_network=True`（builtin_tool_definitions.py:985）覆盖平台默认 False |
| P0-2 存储 key | files.py 先拼前缀再规范化逃逸 | 引用点不准确：files.py:166-169 先规范化后拼接、本身安全；**真正的逃逸点是 `upload.py:78,84`**（Form 可控 agent_id + 全文无 `check_agent_access`，IDOR+跨租户写）和 **`email_service.py:202`**（LLM 可控附件路径 → 跨租户读 + 邮件外泄，含本地 FS 穿越的独立路径） |
| P0-3 上传穿越 | `fallback_dir / f"{file_id}_{filename}"` 任意写入 | 成立，机制修正：**绝对路径被 `file_id_` 前缀意外中和**（变为相对子路径，不穿透），但 `../` 多级穿越**生产容器内实证**（Linux 上 4 个 `..` 即到根）可覆盖 `/app/app/**/*.py`（**平台自身代码**，属主即主进程 clawith，已实测可覆盖）、`/home/clawith/.bashrc`、任意 agent 工作区文件；fallback 分支零依赖可安全删除 |

---

## 实施状态（2026-08-16，分支 `feat/security-p0`）

三个 commit 已落地（测试全量 2307 passed + 3 个预存在失败，与实施前一致；arch-guard 通过）：

1. `fix(security): strict storage-key normalization rejects path traversal`
   - `normalize_storage_key` 遇 `..` 抛 `InvalidStorageKeyError`（不再静默 pop 前缀）；新增 `join_storage_key`（逐段 strict）；error_contract 注册 400 映射（code=`invalid_storage_key`）；local/s3 `list_dir` 跳过脏条目；pages.py 脏行 → 404；workspace_collaboration 6 处改 `join_storage_key`。
2. `fix(security): fail-closed sandbox config and dead code removal`
   - 删除 legacy fallback（`_execute_code_legacy_outcome`/`_execute_code_legacy`/`_DANGEROUS_*`/`_check_code_safety` 副本，-264 行），配置错误一律 typed failure（fail-closed）；统一 `check_code_safety` 至 `sandbox/security.py`（docstring 注明黑名单非安全边界）；bwrap unsafe-fallback 恒 False（D1）；`allow_network` 默认 False（D2，builtin 定义 + config_schema + SandboxConfig 三处）；非法 `sandbox_type` 抛错不再静默降级。
3. `fix(security): upload endpoint access control and size limits`
   - `/chat/upload`：agent_id 必填 UUID + `check_agent_access`（IDOR/跨租户修复）+ `sanitize_filename` + `agent_workspace_key`；email_service 附件 strict key + `is_relative_to` 前缀校验；新建 `upload_limits.py`（Content-Length 预检 413 + read() 后硬校验），4 个上传端点全部接入 `MAX_UPLOAD_BYTES=50MB`（D3/D6），聊天图片额外 10MB 上限且前移到读前（400）；files.py 手写 replace 改 `sanitize_filename`。

**与推荐值的偏差**：

- **D4**：fallback 分支**保留加固**而非删除。理由：删除需前端 3 处调用点（`AgentDetailPage.tsx`）同步改造并破坏无 agent 上下文的上传场景；加固后（固定 `FALLBACK_UPLOAD_DIR` + sanitize + 50MB 上限）穿越与 OOM 均已消除，风险面与删除等价。
- **改动 5（`_get_tool_config` 键级白名单）**：未实施。其核心目标（非法 sandbox_type 不静默降级、配置错误不落宿主机）已被 sandbox_type 严格化 + fail-closed 回退删除覆盖；键级白名单本身改动大、收益边际，列为后续可选项。

**未完成（部署层，另开任务）**：

- **D8**：开发机 bwrap 引导（Makefile/docs 提示 `brew install bubblewrap` 或设置 `SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true`）。
- **D9**：backend 容器 `/app` 只读化（Dockerfile root 属主交付 + 启动后降权，需验证 bwrap 等运行期写路径）。
- nginx `client_max_body_size` 下调与 8008 直连治理（D3 附项）。

---

## P0-1 沙箱执行：配置异常回退宿主机执行 + 全量环境变量泄漏

### 现状（数据流，已逐行核实）

三个工具入口全部汇聚到 `_execute_code_outcome`（`services/agent_tools.py:10320`）：

1. Durable Runtime：`tool_step_service.py:589` → `execute_builtin_tool_outcome`（agent_tools.py:2913）→ `_execute_code_outcome`（:3038）
2. Legacy LLM：`llm/caller.py:427` → `execute_tool`（:3356）→ `_execute_code`（:3649）
3. 审批直执行：`_execute_tool_direct`（:3287）→ `_execute_code`（:3317）

漏洞链条：

```python
# agent_tools.py:10372-10426（非 E2B 分支）
fallback_config = get_sandbox_config()      # :10419 无条件赋值 → 恒非 None
# ...
# agent_tools.py:10477-10502
except ValueError as e:
    if execution_started:
        return _typed_unknown(...)
    if is_e2b_tool:                          # 仅 E2B 工具 fail-closed
        return _typed_failure(...)
    if fallback_config is None:              # ← 死代码，恒 False
        return _typed_failure(...)
    logger.warning(f"[Sandbox] Config issue, falling back to legacy subprocess: {e}")
    return await _execute_code_legacy_outcome(...)   # ← 宿主机执行
```

- **触发条件**（已实测构造器）：per-agent 配置 `sandbox_type=e2b/codesandbox` 缺 `api_key`、`self_hosted/aio_sandbox` 缺 `api_url` → 构造器 `ValueError`；`default_timeout/max_timeout` 为 0 或 >3600 → pydantic `ValidationError`（`ValueError` 子类）。恰是"配置错误"场景，最不该降级。
- **回退路径**：`_execute_code_legacy_outcome`（:10541）在 10608 行 `safe_env = dict(os.environ)` —— 全仓库唯一完整环境复制点，cwd 为 agent 工作区，裸宿主机执行。唯一防线 `_check_code_safety`（:10284）为字符串黑名单（仅 `shutil.rmtree`/`os.system`/`os.popen`/`os.exec`/`os.spawn` + 网络关键词），`import os` 不在黑名单，`__import__('o'+'s')` 可绕过。
- **相关现状**：
  - `registry.py:25-30` 的 "Sandbox is disabled" / "Unknown sandbox type" 两条 `ValueError` 实际不可达（`get_sandbox_config()` 硬编码 `enabled=True`；非法 type 被 `from_dict` 静默转 subprocess）。
  - `SubprocessBackend` 有白名单 env（`_build_safe_env`，subprocess_backend.py:131-159）和 bwrap fail-closed 分支（:451-465），但 `_default_allow_unsafe_bwrap_fallback() = not _running_in_container()`（config.py:62-64）→ **开发机 bwrap 缺失时默认裸宿主机执行**。
  - `DockerBackend` 构造不抛 ValueError；docker 不可用由 execute 内部吞掉返回 typed failure，无回退链。
  - builtin `execute_code` 默认配置 `allow_network: True`（builtin_tool_definitions.py:985），覆盖平台默认 False（config.py:204）。
  - `_check_code_safety` 有两份副本（agent_tools.py:10284 / subprocess_backend.py），语义相同、装饰性差异（已程序化 diff 确认）。
  - `_execute_code_legacy`（:10706）**零调用者**（死代码）；`_execute_code_legacy_outcome` 唯一调用点是回退分支 :10496。
  - 其它沙箱类工具不回退：`execute_code_e2b` 走 `is_e2b_tool=True` 分支正确 fail-closed（已有测试）；`android_compile` 固定 `sandbox_type=android-build`，异常后 typed failure。

### 修复方案

**改动 1 — 删除回退，fail-closed**（`agent_tools.py:10477-10502`）：

```python
# 改后
except ValueError as e:
    if execution_started:
        return _typed_unknown(
            "Sandbox execution outcome is unknown after ValueError; reconcile before retrying.",
            "sandbox_execution_outcome_unknown",
        )
    return _typed_failure(
        f"Sandbox configuration error: {str(e)[:300]}",
        "sandbox_configuration_invalid",
    )
```

E2B 专属分支（10484-10489）随之冗余，一并删除（上游 :10382-10415 已对 E2B 做了完整校验与专属报错）。

**改动 2 — 删除 legacy 死代码**：删除 `_execute_code_legacy_outcome`（:10541-10703）与 `_execute_code_legacy`（:10706-10719），连带删除 agent_tools 侧 `_DANGEROUS_*` 常量与 `_check_code_safety` 副本（:10255-10317）。

**改动 3 — 统一 `_check_code_safety`**：迁移到新模块 `app/services/sandbox/security.py`；`subprocess_backend.py:391` 改引用；docstring 注明"黑名单不是安全边界，bwrap/容器隔离才是"。

**改动 4 — bwrap 缺失 fail-closed**（推荐项）：`config.py:62-64` 的 `_default_allow_unsafe_bwrap_fallback()` 改为恒 `False`。生产容器已装 bwrap 无影响；开发机报错信息已存在（"Install bwrap ... or enable allow_unsafe_fallback_when_bwrap_missing for local development"），配文档/Makefile 引导 `brew install bubblewrap`。

**改动 5 — 配置硬化**：`_get_tool_config`（:253-328，三层合并：tools.config < tenant_settings[`tool_config:<name>`] < agent_tools.config，60s 缓存）输出做键级白名单 + 范围校验（timeout 范围、sandbox_type 枚举），把 pydantic 校验前移到源头。

**改动 6 — 网络默认对齐**（需产品确认，见决策点 D2）：builtin_tool_definitions.py:985 `allow_network` True→False，与平台默认一致。

### 测试计划

- 新增：配置错误矩阵（e2b 无 key、`max_timeout=0`、registry ValueError）→ 断言 typed failure 且后端 `execute` 未被调用。
- 更新：删除 legacy 后修正 `tests/typed_content_outcomes.py:323`、`tests/typed_e2b_outcome.py:239/289` 两处 monkeypatch 目标。
- 新增：bwrap 缺失 + `allow_unsafe_fallback_when_bwrap_missing=False` → 断言失败；env 白名单键集合精确断言（以 `SECRET_MARKER_VAR` 验证不泄漏）。
- 回归：现有 4 个相关测试文件（`test_sandbox_subprocess_backend` / `typed_e2b_outcome` / `typed_content_outcomes` / `android_compile_outcome`，当前 50 passed）保持通过。

### 回归风险

低。删除的路径仅"配置错误时降级执行"这一条；正常 sandbox 路径（subprocess/bwrap/docker/e2b）不动。需注意测试 monkeypatch 指向已删函数。

---

## P0-2 存储 key 前缀逃逸 + upload 端点缺访问控制

### 现状（已逐行核实）

根因：`storage_runtime/utils.py:4-16` 的 `normalize_storage_key` 对整段 `..` 执行 `parts.pop()`（静默回退父目录）而非报错。`pop` 能消费掉调用点拼接进去的**任何前缀**——凡"先拼前缀再规范化"的调用点都可被 `..` 弹出 agent/租户前缀。`local.py:38-44 _full_path` 的 root 边界校验只挡 root，挡不住前缀；`s3.py:46-48 _object_key` 同模式（pop 改变逻辑命名空间，`victim/...` 键同样可达）。

逐调用点风险表（60 余处调用点中，可外部利用者 2 处）：

| 调用点 | 输入来源 | 可逃逸 | 现有校验 |
|---|---|---|---|
| `upload.py:78,84` | **Form `agent_id` + 文件名** | **是（跨租户写 + IDOR）** | **无**（仅 `get_current_user`） |
| `email_service.py:202` | **LLM 附件路径列表** | **是（跨租户读 + 邮件外泄）** | 无 |
| `pages.py:37` | DB `source_path`，公共端点 | 潜在（脏行） | 无（公共按设计） |
| `workspace_collaboration.py:571/648/737/743/747/799` | 先经 `normalize_workspace_path`（输出必不含 `..`）；agent_id 为 UUID | 否 | API 侧有 `check_agent_access` |
| `files.py:81/90/168/929`、`tenants.py:68`、agent_tools 各工具 | UUID / 已规范化 | 否 | 有 |
| `local.py:39` / `s3.py:47` | 后端层 | 前缀靠上层决定 | root 边界（403） |

两个可外部利用点：

```python
# upload.py:78（agent_id 来自 Form，全文无 check_agent_access）
key = normalize_storage_key(f"{agent_id}/workspace/uploads/{filename}")
# → agent_id = "own_agent/../victim_agent" 时 pop 掉 own_agent，落到 victim 命名空间

# email_service.py:198-202（attachments 来自 send_email 工具参数，LLM 可控）
clean_rel = rel_path.replace("\\", "/").strip().lstrip("/")
storage_key = normalize_storage_key(f"{prefix}/{clean_rel}")   # 跨租户读
# 且 :211-212 本地回退 full_path = workspace_path / rel_path 是独立本地 FS 穿越读
```

影响面评估：合法 key 由 UUID + 已规范化路径 + sanitize 文件名构成，**不存在合法的整段 `..`**（`".."` 作为文件名在 POSIX 不可能存在；`v1..2` 子串不受影响）；pop 语义下存量键永不残留 `..` 段 → strict 化**零数据迁移风险**。DB 中 `source_path`/`revision.path` 同理。

### 修复方案

**改动 1 — normalize 拒绝而非回退**（`storage_runtime/utils.py:4-16`）：

```python
class InvalidStorageKeyError(ValueError):
    """Storage key contains path-traversal semantics."""

def normalize_storage_key(key: str) -> str:
    clean = (key or "").replace("\\", "/").strip().lstrip("/")
    parts: list[str] = []
    for part in clean.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InvalidStorageKeyError(f"Path traversal not allowed in storage key: {key!r}")
        parts.append(part)
    return "/".join(parts)
```

**改动 2 — 新增 `join_storage_key(*parts)` helper**（utils.py，经 `storage_runtime/facade.py` 与 `app/services/storage` 重新导出）：每段先 strict 规范化再拼接，替换所有 `f"{a}/{b}"` 先拼后规范化模式；`agent_files.py` 的 `agent_storage_key`/`agent_workspace_key` 内部改用之（单一规范化入口，C6）。

**改动 3 — 异常映射**：`error_contract.py` 的 `register_error_handlers`（main.py:356 已调用）注册 `InvalidStorageKeyError → 400`；`local.py:38-44` 与 `s3.py` 后端 catch → 400（保留 root 越界 403 分支）；两后端 `list_dir` 跳过含 `..` 的脏条目。

**改动 4 — upload.py 一次修掉三个问题**：

```python
async def upload_file(
    file: UploadFile = File(...),
    agent_id: uuid.UUID = Form(...),          # 必填 + UUID 解析，非法 422
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)   # files.py:233 既有写法（含 404/403/租户隔离）
    filename = sanitize_filename(file.filename)            # agent_files.py:22
    key = agent_upload_key(agent_id, filename)             # agent_files.py:39
    ...
```

一次消灭：IDOR（任意登录用户写任意 agent）、前缀逃逸（不再手工 `f"{agent_id}/..."` 拼接）、缺租户校验。

**改动 5 — email_service.py 附件加固**：`attachments` 每项先 `normalize_workspace_path` 后显式拒绝 `..`（或直接复用 strict `normalize_storage_key` 抛错即拒绝），key 改用 `agent_storage_key`；本地回退 `full_path = (workspace_path / rel).resolve()` 后校验 `startswith(workspace_path.resolve())`，不通过则跳过该附件。

**改动 6 — 其余调用点机械替换**：workspace_collaboration.py 6 处、pages.py:37（外层 catch → 404 兜底脏行）、agent_tools 若干处 → `join_storage_key`。files.py / group_file_service / agent_context / tenants 输入已可信，可不改（或顺手统一）。

### 测试计划

- 新增 `tests/test_storage_utils.py`：strict 抛错（含 `a/../../b`）、合法输入不回归、`join_storage_key`/`agent_upload_key` 组合语义。
- 扩展 `tests/test_upload_api.py`（现有仅 1 个测试、只测 extract_text）：缺省/非 UUID agent_id → 422；越权用户 → 403 且 `write_bytes` 不被调用；合法流程 key 恒以 `{agent_id}/workspace/uploads/` 开头。
- email 附件含 `../` → 拒绝；workspace_collaboration 穿越输入 → key 恒以自身 agent_id 开头。
- 全量回归：现有测试无任何用例断言 pop 行为，strict 化预期零破坏。

### 回归风险

低。`files.py` 的 `path` 参数含 `..` 从"静默解析"变 400（前端不会发）；`/chat/upload` 权限收紧是预期的行为变化；存量数据零影响。

---

## P0-3 上传文件名穿越（任意路径写入）+ 上传无大小限制

### 现状（已实证，2026-08-16 生产容器内复核）

```python
# upload.py:88-94（agent_id 为空时；Form 默认 ""，客户端省略即触发）
fallback_dir = Path("/tmp/clawith_uploads")
fallback_dir.mkdir(exist_ok=True)
file_id = str(uuid.uuid4())[:8]
save_path = fallback_dir / f"{file_id}_{file.filename}"   # filename 未清洗
save_path.write_bytes(content)
```

- **穿越实证**（生产容器 `clawith-agent-backend-1` 内 `Path.resolve()` 矩阵，Linux 语义）：

  | filename（Content-Disposition） | resolve() 落点 | 结论 |
  |---|---|---|
  | `../../../../app/app/api/upload.py` | **`/app/app/api/upload.py`** | ✅ 文件存在、属主 clawith 644，**已实测 `cp` 可覆盖**（覆盖后端自身代码） |
  | `../../../../home/clawith/.bashrc` | `/home/clawith/.bashrc` | ✅ 存在且可写（shell 持久化后门；`.ssh/` 不存在故 authorized_keys 不可写） |
  | `../../../../data/agents/<id>/workspace/mg2/gradlew` | 任意 agent 工作区文件 | ✅ 存在（跨 agent 污染，与 mg2 事故同构） |
  | `../../../../etc/hostname` | `/etc/hostname` | 机制成立，root 属主挡住写入 |
  | `/app/app/main.py`（绝对路径） | `/tmp/clawith_uploads/fid_/app/...` | ❌ 被 `file_id_` 前缀中和，不穿透（对初版报告的修正） |
  | `..\..\app\app\main.py`（反斜杠） | 字面文件名 | ❌ Linux 非分隔符（后端仅部署 Linux） |
  | `a/../../b.txt` | `/tmp/b.txt` | ✅ 仅 2 个 `..` 即出 clawith_uploads |
  | 8 个 `..` 与 4 个等价 | 同 4 个 | root 之上不再弹 |

  - **升级发现 1 — 可覆盖平台自身代码**：`/app/app/**`（main.py/upload.py/agent_tools.py 等）全部属主 `clawith`（= uvicorn 主进程 uid 1000）且目录/文件均可写，已实测覆盖 upload.py 成功并还原。`workers=1` 无 reload，替换的模块下次重启/部署加载 → 代码注入持久化。
  - **升级发现 2 — 落点精确化**：原报告 `/private/Users/x/.ssh/...` 是 macOS 宿主路径推演；生产容器为 Linux，实际可达目标是上述表中"存在且可写"者。uid 1000 可写面 = `/home/clawith/**`、`/app/**`（含代码）、`/data/agents/**`（数据卷）、`/tmp/**`。
  - **fallback 触发条件核实**：`agent_id: str = Form("")`，前端 3 处调用（`src/pages/agent-detail/AgentDetailPage.tsx:4448/4520/4567`）均为 `id ? { agent_id: id } : undefined`——**id 为空时不传 agent_id 即触发 fallback**（非"恒有值"）。API 仅需登录（`get_current_user`），任何租户用户可直接构造请求触发。`/tmp/clawith_uploads` 当前不存在 = 本部署尚未被触发过。
- **大小**：`:70` 在任何检查前全量 `await file.read()` 进内存；`:101` 的 10MB 只限图片且读完才查。settings 无上传上限（grep 零命中）、main.py 仅 TraceId/Tenant/CORS 三个中间件无 BodyLimit；nginx `client_max_body_size 500m`（deploy/nginx/nginx.conf:11,39）但 **8008 端口 0.0.0.0 直连 backend 绕过 nginx**；容器 cgroup `memory.max = max` 无限制 → 单请求 OOM DoS 成立（可打死整个 backend 进程/VM）。
- **访问控制**：全文无 `check_agent_access`（与 P0-2 同源）。
- **fallback 可删除**：`clawith_uploads` 全仓库仅 upload.py:90 一处引用；文件仅本请求内被 `extract_text` 消费。

全部 5 个 UploadFile 端点风险表：

| 端点 | filename 处理 | 大小限制 | 访问控制 |
|---|---|---|---|
| `/api/chat/upload`（upload.py:58） | 主分支 replace 斜杠；**fallback :93 未清洗** | **无** | **无** |
| `/api/agents/{id}/files/upload`（files.py:866） | replace 斜杠 + 白名单前缀；存储层兜底 | 无 | ✅ check_agent_access |
| `/api/enterprise/knowledge-base/upload`（files.py:963） | replace 斜杠；sub_path 经 normalize | 无 | ✅ admin+tenant |
| `/api/groups/{gid}/workspace/upload`（groups.py:1483） | path 显式拒绝 `..` | 无 | ✅ participant |
| `/api/tenants/{tid}/logo`（tenants.py:596） | 固定 key | ✅ 1MB+类型白名单 | ✅ org_admin+租户归属 |

### 修复方案

**改动 1 — 删除 fallback 分支 + agent_id 必填**（与 P0-2 改动 4 合并实施）：

```python
agent_id: uuid.UUID = Form(...),          # 必填，非法 422，fallback 分支随之不可达并删除
```

**改动 2 — 复用现成 helper（C6，不重造轮子）**：`sanitize_filename`（agent_files.py:22）、`agent_upload_key`（:39）、`store_agent_upload`（:67，含 content_type 推断）均已从 `app.services.storage` 导出；resolve+前缀校验范式照抄 files.py:172 `_safe_path`（存储层 `local.py:36 _full_path` 已兜底）。

**改动 3 — 大小上限**：新增 settings `MAX_UPLOAD_BYTES`（建议默认 50MB，见决策点 D3）；端点内 `Content-Length` 预检（超限直接 413）+ `read()` 后硬校验（防伪造/分块）。流式分块落盘需给 storage_runtime 加流式写 API，改动较大，列为后续项。

**改动 4 — 顺手加固**：files.py 两处手写 replace 换 `sanitize_filename`；前端 3 处 `id ? { agent_id: id } : undefined` 改为无 `id` 不发请求；纵深建议 nginx 500m 下调并治理 8008 直连（需用户确认）。

**改动 5 — 容器代码目录只读化**（新发现，独立于上传修复）：`/app/app/**` 属主即运行用户 clawith 且可写，路径穿越（或任何代码执行点）可覆盖平台自身代码。建议 Dockerfile 中 `chmod -R a-w /app` 或以 root 属主交付、clawith 只读运行（P0-1 修复后不再有宿主机执行回退，不影响功能）；验证方式：`docker exec -u 1000 touch /app/app/.wtest` 应失败。

### 测试计划

- 扩展 `tests/test_upload_api.py`：① agent_id 缺失/非 UUID → 422；② `../`、绝对路径、反斜杠文件名 → 落盘 key 恒以 `{agent_id}/workspace/uploads/` 开头；③ 越权用户 403；④ 超限 413 且 `write_bytes` 未被调用（含伪造 Content-Length 场景）；⑤ 主分支计数改名/图片/截断行为不回归。

### 回归风险

低。fallback 零依赖；`/chat/upload` 权限收紧是预期行为变化；前端已恒传 agent_id。

---

## 实施顺序与验收

**顺序**（每步独立 commit，可独立验证）：

1. **P0-2 地基**：`normalize_storage_key` strict 化 + `InvalidStorageKeyError` + `join_storage_key` + error_contract 映射 → 跑 storage/utils/files/workspace_collaboration 相关测试 + 全量回归。
2. **P0-3 + P0-2 端点**：upload.py 重构（必填 UUID + check_agent_access + helper 复用 + 大小上限 + 删 fallback）；email_service 附件加固；workspace_collaboration/pages 机械替换 → `tests/test_upload_api.py` 扩展用例。
3. **P0-1 沙箱**：删回退、删 legacy 死代码、统一 `_check_code_safety`、bwrap fail-closed 默认、`_get_tool_config` 硬化 → sandbox 相关测试矩阵。

**每步验收**：

```bash
cd backend
.venv/bin/python -m pytest tests/<相关文件> -x -q
.venv/bin/python -m pytest -x -q          # 全量（最后一步）
.venv/bin/ruff check app && .venv/bin/ruff format app
cd .. && scripts/arch-guard.sh
```

**手工验证示例**（P0-2/3）：带 `../` 文件名与 `agent_id=own/../victim` 的 curl 请求 → 断言 400/422/403 且落盘位置未变；P0-1：配置 `sandbox_type=e2b` 无 api_key 的 agent 执行代码 → 断言返回 `sandbox_configuration_invalid` 而非宿主机执行。

---

## 待决策点（编号汇总）

| # | 决策 | 推荐 |
|---|---|---|
| D1 | `SANDBOX_TYPE` 默认值：a) 保留 SUBPROCESS + unsafe-bwrap-fallback 恒 False；b) 改 DOCKER 默认（需先补挂载/安全检查）；c) 维持现状仅告警 | a |
| D2 | builtin `execute_code` 的 `allow_network` True→False 对齐平台默认（影响默认 agent 网络能力） | 改（安全优先） |
| D3 | `/chat/upload` 大小上限取值（建议 50MB 新增 settings）+ nginx 500m 是否下调、8008 直连治理 | 50MB；nginx 治理建议另开任务 |
| D4 | `/chat/upload` 的 `/tmp` fallback：删除（推荐）还是保留加固 | **保留加固**（实施时变更决策：fallback 已由 D3 上限 + sanitize_filename + 固定 FALLBACK_UPLOAD_DIR 覆盖，且删除需前端同步改造；见「实施状态」） |
| D5 | strict 化范围：`normalize_storage_key` 直接 strict（推荐）vs 双轨并行；`normalize_workspace_path` 保持 pop（保 LLM 工具可用性，推荐） | 前者 strict、后者保持 |
| D6 | 其余 3 个无大小限制端点（files.py×2、groups.py×1）是否本次一并加统一上限 helper | 本次一并加（改动小） |
| D7 | 分支与提交粒度：建议新分支 `feat/security-p0`，三步各一个 commit | 建议 |
| D8 | 开发机 bwrap 安装引导（文档/Makefile 步骤，随 D1 落地） | 做 |
| D9 | backend 容器代码目录只读化（`/app` 对运行用户 clawith 可写，穿越写可覆盖平台自身代码）：Dockerfile chmod / root 属主交付，P0-1 修复后实施 | 做 |
