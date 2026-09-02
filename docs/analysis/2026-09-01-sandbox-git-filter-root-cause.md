# 沙箱 .git 被过滤：精确代码位置与根因链（run c52c5ffc 复盘收口）

> 日期：2026-09-01 · 证据来源：run c52c5ffc（「按你推荐的执行吧」41 LLM 步 / 111 工具 / ~25 分钟）
> 结论先行：`.git` 被 workspace 发布策略归类为 **derived**，在「物化进沙箱」和「发布回存储」两道关口被**双向剥离**——不是 clone 函数丢文件，而是策略层把 .git 当构建产物排除。沙箱内因此只剩 .git 空目录骨架，git 全部失败，模型陷入试错黑洞。

## 一、四处精确位置（自顶向下）

### 1. 分类器：.git 段级 → derived
`backend/app/services/sandbox/workspace_policy.py`

- **第 19 行**：
  ```python
  DERIVED_SEGMENTS = frozenset({"build", ".git", ".gradle", "node_modules", "target", "dist", "__pycache__", "_exec_tmp"})
  ```
- **第 38–39 行**（`classify_publish_path` 内）：路径任一段命中 `DERIVED_SEGMENTS` → 返回 `"derived"`。
  唯一的放行例外是 `build` 紧跟 `outputs`（第 35–36 行返回 `"artifact"`）——`.git` 没有任何例外。

### 2. 物化侧（进沙箱）：derived 文件不拷贝 → 空壳 .git
`backend/app/services/agent_tools.py` `_materialize_storage_entry`

- **第 1891–1897 行**：
  ```python
  if await storage.is_file(storage_key):
      if classify_publish_path(rel_path) == "derived":
          # L2 derived history never materializes into the sandbox copy ...
          return
  ```
  文件命中 derived 直接跳过。
- **第 1937–1949 行**（目录分支）：`mkdir` 目录骨架后递归子项——子目录本身**会被创建**，其下文件再被上面的文件分支过滤。

  这精确解释了铁证：staging 残留 `/data/agents/.sandbox-staging/c52c5ffc-321bbb4a/workspace/mydome1/.git/` 里有 `branches/hooks/info/logs/objects/refs` 空目录、0 个文件、**缺 HEAD/config**——目录骨架是第 1938 行 `mkdir` 的产物，文件是被第 1892 行剥掉的。git 见到这个空壳报 `fatal: not a git repository (or any parent up to mount point)`。

### 3. 发布侧（出沙箱→存储）：derived 既不写 CAS 也不覆盖
`backend/app/services/agent_tools.py` `_collect_temp_workspace_files`

- **第 2530–2537 行**（`classify_and_put`）：`derived` → 追加进 `derived_paths` 列表，**排除在 cas_files / overwrite_files 之外**（docstring 第 2523 行明示「never written and never deleted」）。

  后果：即使模型在沙箱里重新 `git clone` 出一份**完整 .git**，flush 回存储时 .git 内所有文件也被丢弃。下次 run 物化，沙箱里又是空壳。**这是一个自锁闭环**：git 修 git 永远修不好。

### 4. clone 函数：无辜
`backend/app/services/sandbox/local/shared.py` **第 554 行** `clone_workspace_to_staging`：copytree 全量拷贝，只跳过 `.venv` / `.tmp` / `_exec_tmp*` 文件，不涉及 .git。物化到 staging 之前，临时 workspace 里就已经是空壳了。

## 二、根因链（一行版）

```
DERIVED_SEGMENTS 含 ".git" (workspace_policy.py:19)
  → classify_publish_path 返回 derived (workspace_policy.py:38)
  → 物化跳过 .git 文件、保留目录骨架 (agent_tools.py:1892 / 1938)
  → 沙箱 .git 空壳 → git 全部 fatal
  → 发布侧再剥一层 (agent_tools.py:2532)，clone 成果也无法持久化
  → 15 次 execute_code git 失败 + 33 次重复 read_file 试错 → 25 分钟黑洞
```

## 三、认知冲突放大器（模型为什么停不下来）

- L2 真实 workspace `/data/agents/<agent_id>/workspace/mydome1/.git/` **是完整的**（HEAD 指向 f_android_ai）：read_file / list_files 走 L2 路径，不过滤。
- 沙箱 execute_code 里同一路径 `.git` **是空壳**：git 报 not a git repository。
- 模型两边看到的证据互相矛盾，且 git 错误信息为零信息量（只有 fatal 一行），模型只能脑补原因（以为 HEAD 损坏/路径不对/权限问题）→ 33 次重复 read_file 试图「找到 HEAD 到底在哪」。
- 完整 .git 的来源疑点：真实 workspace 的 .git 是通过哪条路径写入存储的（沙箱发布侧会剥离，故应是 L2 直写路径，如早期 merge 模式或 write_file），值得单独核实，但不影响本根因结论。

## 四、修复（已定稿并实施）

**方案 1（根治）已定稿为技术方案 `docs/technical-plans/20260901-sandbox-git-materialize-fix.md` 并于 2026-09-02 按 B 口径（物化/发布两侧放行 + 凭证脱敏）实施完毕**：

- 物化侧：`.git` 全量 source 化（DERIVED_SEGMENTS 移除 `.git`），预算跳过的 .git 整体剔除（要么完整要么不存在）；
- 发布侧：`.git` 文件入 CAS，`redact_git_secrets` 在写入前剥离 userinfo/extraheader 凭证，`.git-credentials`/`.netrc` 拒绝名单保持 derived；
- 脱敏边界：沙箱=私有环境（保留真实 token 供 pull/push），storage=共享持久层（token 绝不落盘）。

方案 2（错误提示）与方案 3（同错熔断）不再作为本缺陷的修复路径：git 修好后方案 2 无存在意义；方案 3 已在第三代修复（6a5a9928）中作为通用熔断落地，与本文档无关。
