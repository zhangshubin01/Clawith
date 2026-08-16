# 2026-08-16 运行日志问题修复方案与计划

状态：待执行
关联：日志分析结论 P1-P4（session 2026-08-16）
范围：backend 代码 + 构建容器脚本 + 运维清理

---

## 背景（已核实证据）

- **P1 断链软链**：`backend/docker/android-builder/entrypoint.sh:165-169` 的 tmpfs 加速逻辑在
  `app/build/intermediates` 已是真实目录时仍执行 `ln -sf /dev/shm/intermediates app/build/intermediates`，
  在目录内部生成 `intermediates/intermediates -> /dev/shm/intermediates`。工作区 bind-mount 持久化，
  容器销毁（`docker run --rm` + auto_remove）后目标消失 → 宿主残留断链（今晨 06:23 构建产生，已知 6 个：
  agent 62bc9c81×4、08a739c1×2）。
- **后果链**：`LocalStorageBackend.list_dir`（`storage_runtime/local.py`）对每个条目 `entry.stat()`
  跟随断链抛 `FileNotFoundError` → `_storage_walk_files` 全工作区递归崩溃 →
  `find_files`/`search_files` 失败（safe-read 重试 3 次后 `tool_retry_exhausted`，DB 证据：
  `attempt_count=3, runtime_retry_exhausted=true`）+ `materialize skip missing` 每 ~15s 刷 4 条 WARNING。
- **P2**：`APK_OUTPUT_PATHS` 块打印在构建**之前**（列出旧产物），无代码消费方（纯信息输出，易误导）。
- **P3**：`tool_lease_reconcile.py` 在租约释放/过期时经 `enqueue_resume` 创建 resume 命令；
  若 LangGraph 进程内重试已把调用 settle，命令成为悬空 pending（现共 5 条，attempt_count=0）。
- **P4**：`[Proxy] All SS nodes failed` 为启动期预存在警告，Discord 直连可用，本次不处理。
- 分支事实：`entrypoint.sh`/`android_build_backend.py` 是本分支新增（main 无）；
  `storage_runtime/local.py` 与 main 完全一致（该修复对 main 同样适用）。

---

## 修复设计

### F1. entrypoint.sh 软链根因修复（治本）

文件：`backend/docker/android-builder/entrypoint.sh`

1. tmpfs 目标变量化（可测试，且允许无 /dev/shm 环境）：
   ```sh
   TMPFS_INTERMEDIATES="${ANDROID_TMPFS_DIR:-/dev/shm/intermediates}"
   ```
2. 修正创建条件——链接不存在且路径也不存在时才创建：
   ```sh
   CREATED_INTERMEDIATES_LINK=0
   if [ -d "app/build" ] && [ ! -L "app/build/intermediates" ] && [ ! -e "app/build/intermediates" ]; then
       mkdir -p "$TMPFS_INTERMEDIATES"
       ln -sf "$TMPFS_INTERMEDIATES" app/build/intermediates
       CREATED_INTERMEDIATES_LINK=1
   fi
   ```
   - `! -e` 同时排除真实目录与普通文件；断链（`-L` 为真）也不重建（容器内 entrypoint 每次重建
     `/dev/shm/intermediates`，链接在容器内始终可解析）。
3. 退出清理——容器销毁前移除**本容器自己创建**的软链，杜绝宿主持久残留：
   ```sh
   cleanup_intermediates_link() {
       if [ "$CREATED_INTERMEDIATES_LINK" = "1" ]; then
           rm -f app/build/intermediates
       fi
   }
   trap cleanup_intermediates_link EXIT
   ```
   （覆盖正常退出与 docker stop 的 SIGTERM；`docker kill -9` 无法覆盖，属可接受残余，F2 兜底。）
4. 测试：新增 pytest（`backend/tests/test_android_builder_entrypoint.py`）用 `subprocess` 在临时目录
   执行改造后的逻辑片段（`ANDROID_TMPFS_DIR` 指向 tmp 目录），断言：
   - intermediates 为真实目录 → 不产生嵌套链接、目录原样保留；
   - 不存在 → 正确创建链接；退出后链接被 trap 移除；
   - 已存在断链 → 不重建、不报错。

### F2. LocalStorageBackend.list_dir 断链容错（防御治本）

文件：`backend/app/services/storage_runtime/local.py`

- `list_dir` 中 `stat = entry.stat()` 改为容错：`FileNotFoundError/OSError` 时跳过该条目并
  debug 日志（不吞其他异常）。任何一个坏条目都不应瘫痪整个目录列举。
- 同文件复查：`is_dir/is_file` 对断链返回 False 不抛错，无需改；`read_bytes` 断链抛
  FileNotFoundError，调用方已有重试/容错，不动。
- 测试：`backend/tests/test_storage_local_broken_symlink.py`——tmp_path 建断链 →
  `list_dir` 不抛错且返回其余条目；经 `_storage_find_files` 全工作区搜索不再失败。
- 该文件与 main 完全一致，修复可直接 cherry-pick 到 main。

### F3. 现有断链清理（运维，需用户确认后执行）

```sh
docker exec clawith-agent-backend-1 find /data/agents -xtype l    # 全量扫描（已知 6 个，确认无其他）
docker exec clawith-agent-backend-1 find /data/agents -xtype l -delete
```

只删断链（链接无内容、目标已不存在，无数据价值）；不删除任何常规文件/目录。

### F4. APK_OUTPUT_PATHS 打印时机

文件：`backend/docker/android-builder/entrypoint.sh`（同 F1 一起改）

- 现状：`echo APK_OUTPUT_PATHS … find … END` 在 `exec "$@"` 之前执行，列出的是上次构建旧产物；
  后端代码无任何消费方（已 grep 确认），纯信息输出但误导排查。
- 修复：把 `exec "$@"` 改为 `"$@"` + 记录退出码，**构建结束后**再打印 APK 块，`exit $rc` 保持
  退出码语义：
  ```sh
  "$@"
  build_rc=$?
  echo "=== APK_OUTPUT_PATHS ==="
  APKS=$(find . -path "*/build/outputs/*" \( -name "*.apk" -o -name "*.aab" \) 2>/dev/null)
  [ -n "$APKS" ] && echo "$APKS" || echo "NO_APK_FOUND"
  echo "=== END_APK_OUTPUT_PATHS ==="
  exit $build_rc
  ```
- 备选：若评估后认为该块无价值可直接删除（改动更小）；默认方案为后移保留信息。

### F5. 悬空 resume 命令治理

1. **settle 侧 supersede**（主修复）：
   - 文件：`backend/app/services/agent_runtime/tool_execution.py`
   - 新增 helper `_supersede_stale_resume_commands`，挂在两个终态收口点：
     `_mark_terminal`（succeeded / failed / unknown 的统一收口）与
     `mark_expired_safe_read_result_unavailable`（safe-read 探针路径——事故根因：Run 自驱重放
     经 safe-read 结果探针 settle 执行，而对账 resume 在同一事务中先 settle 后 enqueue，
     `_mark_terminal` 的 UPDATE 永远匹配不到刚 enqueue 的 resume）。
   - `agent_run_commands.status` 有 CheckConstraint（pending/claimed/applied/rejected），
     不新增状态值也不引入迁移，改为 `status='rejected'` +
     `error_code='superseded_tool_execution'`（同时清 claimed_by / claim_expires_at /
     applied_checkpoint_id，置 applied_at）：
     ```sql
     UPDATE agent_run_commands
     SET status='rejected', error_code='superseded_tool_execution', claimed_by=NULL,
         claim_expires_at=NULL, applied_checkpoint_id=NULL, applied_at=now()
     WHERE tenant_id=:tenant_id AND run_id=:run_id AND command_type='resume' AND status='pending'
       AND payload->'payload'->>'tool_call_id' = :tool_call_id
     ```
   - 只影响「工具租约对账」产生的 resume（按 payload 内 tool_call_id 过滤），不影响用户 resume。
2. **消费端兜底**（防历史残留与其它 settle 路径）：
   - 文件：`backend/app/services/agent_runtime/command_worker.py`（resume 命令处理入口）
   - 守卫条件（三者同时满足才拒绝，避免死锁与误伤）：
     1. resume payload 引用了 `tool_call_id`（`payload.payload.tool_call_id`）；
     2. 该执行在 resume 自身 run_id 下已是终态（succeeded/failed/unknown）——按命令自己的
        run_id 查询，线程持有者唤醒 resume（run_id 不同）天然不受影响；
     3. checkpoint 未在等待该 resume 的 correlation_id（`lifecycle.waiting_request`）——
        对账是先 settle 后 enqueue，parked Run 的唤醒 resume 必须照常执行，否则死锁。
   - 命中后 `reject(error_code='superseded_tool_execution')` 并跳过 Graph 执行。
3. 测试：单测覆盖「settle 后 pending resume 被 rejected+error_code」与「消费端守卫的
   五种情形（终态+非等待→拒绝；等待 correlation→执行；无 tool_call_id→不查库；
   run 内查不到执行→执行；执行 started→执行）」。
4. **现有 5 条清理**（运维，随 F3 一起）：按上述 SQL 一次性标记
   rejected/superseded_tool_execution。

### F6（可选后续，不阻塞本次）构建冷启动预热

- 一次性预热：向 `gradle_cache_global` 卷预下载 gradle-8.7 / gradle-8.11.1 发行版，
  向 `global_android_sdk` 卷预装 Build-Tools 35/36（当前两个 Run 的 10 分钟下载均为首次冷缓存）。
- gradle 发行版下载走国内镜像（与既有「下载源切换国内镜像」提交同思路）以规避
  `services.gradle.org` 下载中断（mg2 首次尝试 50% 时 exit 1 的疑似原因）。
- 另开任务，不纳入本次验收。

---

## 实施计划

### Phase 1 — 代码 + 测试（今天）
1. F1 + F4：改 `entrypoint.sh`（条件修复 + trap + APK 块后移），新增 entrypoint 行为测试；
2. F2：改 `local.py` list_dir 容错，新增断链测试；
3. F5：改 `tool_execution.py` settle 侧 supersede + `command_worker.py` 消费端兜底，新增单测；
4. 回归：`cd backend && uv run pytest`（排除 `tests/test_sso_toggle.py`）+ `uv run ruff check` 全绿；
5. 提交推送 `origin/f-shubin-0806`（沿用会话约定：拆分 2-3 个语义化提交）。

### Phase 2 — 运维清理（等用户确认）
6. F3：全量扫描 `find /data/agents -xtype l` → 删除断链（仅断链）；
7. F5-4：现有 5 条悬空 pending resume 标记 rejected/superseded_tool_execution。

### Phase 3 — 部署与验证
8. `docker compose build backend && docker compose up -d backend`；容器与宿主 `/api/health` 200；
9. 容器内验证矩阵：
   - 构造断链后 `find_files(path="workspace", pattern="**/build.gradle.kts")` 成功返回（不再
     FileNotFoundError / 重试耗尽）；
   - 工具调用日志不再出现 `materialize skip missing`；
   - 触发一次真实 `android_compile`：完成后 workspace 无新增断链（F1 trap 生效），
     容器日志 APK 块出现在构建完成之后且为本次产物；
   - resume 命令不再悬空（settle 后自动 rejected/superseded_tool_execution）。
10. 观察 1 小时日志：无 `RetryableToolNodeError`、无 `materialize skip missing` 循环。

## 风险与回滚

- F1/F4：改动仅影响构建容器启动脚本，最坏情况（trap 未触发、`kill -9` 残留）由 F2 兜底不再瘫痪
  搜索；回滚 = git revert。
- F2：仅跳过 stat 失败的条目并记 debug 日志，不吞其他异常，不掩盖真实 I/O 错误。
- F4：若后续发现外部（CI/人）依赖容器输出中 APK 块的「预构建」位置（无代码消费方已确认），
  快速 revert 该块即可。
- F5：supersede 严格限定 `command_type='resume' AND status='pending' AND payload 内 tool_call_id`
  三条件（rejected + `superseded_tool_execution`，不引入新状态值）；消费端守卫额外要求
  「执行在命令自身 run_id 下终态」且「checkpoint 未在等待该 correlation」，不影响用户
  resume / cancel 命令，也不阻塞线程持有者唤醒与对账唤醒路径。

## 验收标准

1. `find_files`/`search_files` 全工作区搜索在存在断链时不再失败、不触发安全读重试耗尽；
2. 构建容器退出后 workspace 不残留指向容器 tmpfs 的软链；
3. 现有 6 个断链已清理且不复发；
4. `materialize skip missing` 不再循环刷屏；
5. 悬空 pending resume 命令为 0；
6. 测试与 ruff 全绿，部署后健康检查通过。
