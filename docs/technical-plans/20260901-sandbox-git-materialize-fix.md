# 沙箱 .git 根治方案：物化/发布两侧放行 + 凭证脱敏（run c52c5ffc 修复）

> 日期：2026-09-01 · 根因报告：`docs/analysis/2026-09-01-sandbox-git-filter-root-cause.md`
> 状态：**已实施（2026-09-02，用户拍板 B 口径）**。实施摘要与偏离记录见文末「七、实施记录」。

## 〇、2026-09-02 二次事故复核（本次证据，全部核验属实）

2026-09-02 13:00 UTC 真实工具探测 mydome1（agent 950a1943，run 40ea58a3 遗留交付）复现同一故障：沙箱 `.git` 为空壳（无 HEAD/config/index、`refs/heads/` 空目录、objects 0 文件），`git status/rev-parse/count-objects` 全部 `fatal: not a git repository`（exit 128），`--git-dir` 直读已知提交 6b848dd 同样失败。与本文档 §1 根因链逐项对账一致：目录骨架是物化递归 mkdir 的产物，文件是 derived 过滤剥掉的。

**复核新增事实（决定方案可行性的关键证据）**：

1. **行号全部复核命中**（当前分支 `f-shubin-0806` @ `6a5a9928` 之后）：物化过滤 `agent_tools.py:1892-1896`、发布收集 `2531`、flush 写点 `2047`、candidate 冻结写点 `2348`、删除侧 `2146/2411`、预算常量 `160-161`（50MB/500MB）、`TEMP_WORKSPACE_DEFAULT_PATHS=175`（默认含整个 `workspace`）。
2. **体积实测安全**：生产 storage 全库所有项目 `.git` 体积 = 最大 6.0M（mg2），其余 ≤720K（mydome1=720K/160 文件/objects 576K）。50MB/500MB 预算护栏下全量物化 + CAS 发布无压力，§3.3 半拉子保护几乎不会触发，仅为安全不变量保留。
3. **凭证脱敏机制全仓不存在**（grep 确认），需按 §3.2 新增。
4. **测试锚点属实**：`test_path_classification_boundaries`（`tests/test_workspace_publication_filter.py:399`）断言 `.git/HEAD → derived`；物化测试（同文件 ~456-467）断言 `.git/HEAD` 不物化且不进 manifest——实施时两处翻红即绿。
5. **网络与 push 正交**：execute_code 沙箱默认 `network_mode: none`（`config.py:363 SANDBOX_ALLOW_NETWORK` / 每工具 `allow_network`）。git push/MR 收尾依赖网络开关与凭证，与 .git 物化修复无关，但作为交付前置条件需确认 trigger 所在沙箱已具备。

**待拍板（本文档唯一未决项）**：本方案 = §3.1「物化+发布两侧放行」；此前小票 `02-materialize-git-in-sandbox.md` 的拍板口径是「仅物化放行、发布仍排除」。二者冲突，须二选一：

- **仅物化放行（小票 02 口径）**：沙箱 git 可用、可 commit，但 commit/refs/objects 发布时被剥 → storage 的 .git 停留旧 HEAD，工作树却是新内容 → 下一轮 run `git status` 全量幽灵 diff、`git log` 缺失刚完成的 commit——**两侧证据矛盾复发（较轻形态）**，且 40ea58a3 类交付流（add→commit→push→MR）每轮 run 都会重复 commit。非根治。
- **两侧放行（本文档）**：commit 链持久回 storage，下一轮 run 状态一致，真正闭环；代价 = §3.2 脱敏（`config` userinfo/extraheader 重写 + `.git-credentials`/`.netrc` 拒绝名单）。

**推荐：两侧放行**——用户当前交付流本身依赖 git commit 链，仅物化放行会让「git 修 git 修好一半」成为常态。

## 一、根因一行版

`.git` 在 `workspace_policy.py:19` 被列入 `DERIVED_SEGMENTS` → `classify_publish_path` 判为 derived → **物化侧**（`agent_tools.py:1892`）剥掉所有 .git 文件、仅剩目录骨架，**发布侧**（`agent_tools.py:2531`）拒绝 .git 文件入 CAS。双向剥离构成自锁：沙箱里 git 永远不可用，clone 成果也永远无法持久化。

## 二、调查确立的关键事实（方案依据）

1. **生产 storage 与真实 workspace 同源**：`app/config.py:142` `STORAGE_LOCAL_ROOT = _default_agent_data_dir()`，容器内即 `/data/agents`。storage key `<agent_id>/workspace/...` 直指真实 workspace 文件。复盘证据中真实 workspace 的 `.git/` 完整（HEAD 指向 f_android_ai）⇒ **storage 里 .git 文件本来就是完整的**，只是物化时被 1892 过滤器剥掉。放行物化后存量项目立即自愈，**无需任何数据迁移**。
2. **分类器共 4 个生产调用点**：物化（1892）、flush 删除（2146）、candidate 删除（2411）、发布收集（2531）。放行 .git 后四点的语义自动统一（source 语义），**只有发布收集一处需要显式处理脱敏**。
3. **发布写入点共 2 处**：flush 主循环（2047 `for rel_path, local_path in cas_files.items()`）与 candidate 冻结（2348 同名循环）。脱敏 hook 只插这两处。
4. **预算护栏已存在**：`agent_tools.py:160-161` 单文件 50MB / 总 500MB。放行 .git 后 `objects/**` 天然受护栏保护；由此引入的「半拉子仓库」风险需显式处理（见 3.3）。
5. **全仓无 git 凭证脱敏机制**（已 grep 确认），需新增。
6. **存量测试断言需同步**：`tests/test_workspace_publication_filter.py:399` 参数化边界表断言 `.git/HEAD` → derived，放行后应改 source。

## 三、设计

### 3.1 放行策略：.git 全量 source 化（不做白名单）

**决策**：`DERIVED_SEGMENTS` 移除 `".git"`，.git 内**全部文件**走 source 语义（物化 + CAS 发布 + 可删）。

**否决的备选**：仅放行文本元数据（HEAD/config/refs，排除 objects）——会在沙箱里制造「HEAD 指向 objects、objects 缺失」的**坏仓库**（`fatal: bad object`），比现状的 `not a git repository` 更误导模型，且 git 只读操作（log/status/diff）都需要 objects。半个 .git 不是根治。

### 3.2 凭证脱敏：发布侧重写，物化侧不动

- **位置**：`workspace_policy.py` 新增纯函数 `redact_git_secrets(rel_path: str, data: bytes) -> bytes`；在两个发布写入点（2047、2348 的 `data = local_path.read_bytes()` 之后）调用。
- **规则**（仅当路径以 `.git/config` 结尾）：
  1. `url = https://user:token@host/...` → 剥 userinfo：`https://host/...`；
  2. `extraheader = Authorization: ...` → 替换为 `Authorization: <redacted>`（覆盖 `[http] extraheader` 的 PAT 挂载法）；
  3. 非 UTF-8 或无需重写时原样返回（字节级不做无谓转换）。
- **拒绝名单（derived 化）**：`classify_publish_path` 对 basename 为 `.git-credentials` / `.netrc` 的文件判 derived（纯凭证文件永不入 CAS；已存在的历史文件沿用 derived「不反向删除」语义）。改动 2 行，顺带堵住 git credential store 的同类泄露面。
- **为什么物化侧不脱敏**：沙箱是 agent 自己的执行环境，agent 本来持有自己的凭证；脱敏后物化会破坏 remote 可用性（token 被剥后 pull/fetch 失败）。持久层（CAS）才需要脱敏。语义：**沙箱 = 私有环境，storage = 可能被共享/同步的持久层**。
- **权衡（明示）**：agent 在沙箱 config 写入 token → 发布时被脱敏 → 下次物化回沙箱的 config 无 token，pull 需重新配置。这是「凭证绝不落共享层」的必然代价，接受。

### 3.3 物化侧半拉子保护：不完整 .git 整体删除

预算护栏可能跳过 .git 内大文件（pack >50MB 或累计超 500MB）→ 沙箱出现「部分 objects 缺失」的坏仓库。维持不变量：**沙箱里 .git 要么完整，要么不存在**。

- **实现**：`_prepare_temp_workspace` 物化循环结束后（`agent_tools.py:1840` return 前），遍历 `budget["skipped"]`，凡路径含 `.git` 段者，定位其 `.git` 目录并 `shutil.rmtree`（ignore_errors，去重，记 warning 日志）。**同时必须从 `manifest` 中剔除该 `.git` 前缀下已物化条目**（2026-09-02 评审发现：flush/candidate 删除侧遍历 `manifest.items()`，rmtree 后这些条目在 temp 中消失、不在 local_files → 会被当成「沙箱删除」生成 DELETE candidate，把 storage 里仅因预算未物化而被剔除的 .git 文件误删）。
- **代价**：超预算项目退化回现状（无 .git + git 报 not a git repository），但绝不产生「半拉子」这一更坏状态。
- **范围外（记录不实现）**：暂不给「退化态」附加显式错误提示（原方案 2）；若退化态在真实项目上高频出现，再评估单独提示或调预算。

### 3.4 删除语义

放行后 .git 文件在 flush 删除侧（2146/2411）走 source 的 CAS 删除语义：沙箱里 `rm -rf .git` 会真实删掉 storage 里的 .git。这是正确行为（agent 有意删除应生效），但意味着 .git 与其他 source 文件一样可被误删——接受（与 workspace 内任何文件同权）。

## 四、实施清单（按序）

| # | 文件 | 改动 | 量级 |
|---|---|---|---|
| 1 | `backend/app/services/sandbox/workspace_policy.py` | ① `DERIVED_SEGMENTS` 移除 `".git"`；② `classify_publish_path` 加 basename 黑名单（`.git-credentials`/`.netrc` → derived）；③ 新增 `redact_git_secrets`（config url userinfo + extraheader 重写）；④ docstring 同步 | ~40 行 |
| 2 | `backend/app/services/agent_tools.py` | ① 2047、2348 两处发布写入点调用 `redact_git_secrets`；② `_prepare_temp_workspace` 加不完整 .git 清理（3.3）；③ 1892 注释更新（删除「.git 不物化」的过时说明） | ~25 行 |
| 3 | `backend/tests/test_workspace_publication_filter.py` | ① 边界表 `.git/HEAD` → `source`，补 `.git/objects/x`、`workspace/proj/.git/config` 等 case；② 新增 `.git-credentials`/`.netrc` derived 断言；③ 新增 `redact_git_secrets` 单测（userinfo 剥离 / extraheader 重写 / 非 config 路径原样 / 二进制原样） | ~60 行 |
| 4 | 新增物化完整性测试 | MemoryStorageBackend 注入超预算 .git 文件 → 断言物化结果中 .git 目录整体不存在 | ~25 行 |
| 5 | `docs/analysis/2026-09-01-sandbox-git-filter-root-cause.md` | 更新「修复方向」节：方案 1 已定稿为本文档，删除三选表述 | 小 |
| 6 | 验证 | `cd backend && python -m pytest tests/test_workspace_publication_filter.py -x -q` + `scripts/arch-guard.sh` | — |

## 五、影响面与风险

- **并发/冲突**：.git 文件入 CAS 后，两个并行会话各自物化并改写 .git（如都做 commit）会撞版本冲突。现有 CAS 冲突机制（fail/overwrite 按模式）原样兜底，不做特殊处理——.git 与普通源文件同权是刻意设计。
- **体积**：objects 入 CAS 受 50MB/500MB 护栏；storage 内容寻址（`content_hash_bytes` 判等）使未变 pack 不重复写。物化大 .git 增加首 run 延迟，可接受。注意：`redact_git_secrets` 必须在 hash 计算**之前**调用（2047/2348 的 `data = local_path.read_bytes()` 之后紧接），否则写入内容与 manifest 记录的 hash 不一致；由此 `.git/config`（物化时未脱敏、发布时脱敏）每次 flush 都会重写一次——单文件极小，接受。
- **脱敏覆盖面**：仅 `.git/config`（url/extraheader）与 `.git-credentials`/`.netrc` 拒绝名单。SSH remote（`git@host:`）无 token 风险不处理；`~/.gitconfig` 用户级配置不物化（不在 agent workspace 内），不涉及。**2026-09-02 评审补充**：`FETCH_HEAD` 与 `logs/refs/**` 重放日志可内嵌含 userinfo 的远端 URL——建议把 url-userinfo 剥离规则从「仅 config」放宽为「对所有 `.git/` 路径生效」（同一正则，成本零，覆盖 config/FETCH_HEAD/logs 三处；extraheader 重写仍仅限 config）。
- **回归风险点**：`test_workspace_publication_filter.py` 中可能有其他隐含依赖 .git derived 行为的断言（如 materialize 测试 456 行注入的 `.git/HEAD`）——实施时全文核对该测试文件。

## 六、未决问题（评审时确认）

1. **脱敏边界**：是否还需要覆盖 `.git/config` 中 `[remote]` 之外的其他含凭证配置（如 `insteadOf` 改写含 token 的 URL）？本方案仅覆盖 `url =` 与 `extraheader`，建议最小起步，评审确认。
2. **预算退化提示**：3.3 的退化态暂不加提示（范围外），是否接受？
3. 实施顺序：先单测（红）再实现，还是直接实现后补测——按仓库惯例（tdd 可选）由实施者定，建议先改参数化表观察红。

## 七、实施记录（2026-09-02）

按 §四 清单 + 2026-09-02 评审修正实施，TDD 红→绿四切片：

1. `workspace_policy.py`：`DERIVED_SEGMENTS` 移除 `.git`；`classify_publish_path` 新增 `_GIT_CREDENTIAL_FILES`（`.git-credentials`/`.netrc`）basename 拒绝名单；新增纯函数 `redact_git_secrets`（userinfo 剥离对所有 `.git` 路径生效、extraheader 全值重写仅限 config、非 UTF-8/非 .git 路径原样返回）。
2. `agent_tools.py`：flush 与 candidate 冻结两处 CAS 写点（`data = redact_git_secrets(rel_path, local_path.read_bytes())`，hash 计算之前）；`_prepare_temp_workspace` 新增 `_drop_incomplete_git_dirs`（预算跳过的 .git 整体 rmtree + manifest 前缀剔除，含 `[ToolWorkspaceGitIncomplete]` 日志）；物化侧过时注释与 `_derived_publication_note` 文案同步更新（移除 `.git/`）。
3. 测试（`tests/test_workspace_publication_filter.py`）：边界表 `.git/HEAD`→source + objects/config/凭证文件用例；物化断言反转（.git/HEAD 物化且入 manifest）；新增半拉子保护、flush 脱敏、candidate 脱敏三个测试；`redact_git_secrets` 五用例。目标文件 106 passed。
4. 文档：`docs/analysis/2026-09-01-sandbox-git-filter-root-cause.md` §四/五 更新为「已定稿并实施」。

**偏离记录（相对 §3 原稿，均为评审修正方向）**：

- §3.2 规则 2 放宽：extraheader 重写为**全值** `<redacted>`（原稿仅 Authorization 前缀）——GitLab CI 常用 `PRIVATE-TOKEN:` 挂载，全值重写一处覆盖；
- §3.2 规则 1 放宽：userinfo 剥离对**所有 `.git/` 路径**生效（原稿仅 config）——覆盖 FETCH_HEAD/reflogs 内嵌 URL；
- §3.3 补 manifest 剔除（原稿缺失，2026-09-02 评审发现：不剔除会把 budget 剔除的文件当沙箱删除反向 DELETE storage）。

**已知边界（不扩大范围）**：`redact_git_secrets` 只挂在两条发布写点；L2 直写工具（write_file/edit_file 写 `.git/config`）不脱敏——那是 agent 自主行为（等价于往任意文件写凭证），与「沙箱 git 操作不泄漏 token 进共享层」的威胁模型正交。

**验收口径**：部署后 mydome1 沙箱 `git status` 应可用、`git log` 应显示 f_android_ai 历史；沙箱内 commit 后 storage `.git/config` 应无 token（脱敏）；下一轮 run 物化后 `git status` 与 storage 一致（无幽灵 diff）。
