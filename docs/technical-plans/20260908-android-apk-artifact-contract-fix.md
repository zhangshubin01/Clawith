# 生产级修复方案：Android 成功构建的产物清单混入历史残留 APK（产物契约污染）

> 结论（评审定稿）：**评审通过**（主推候选 A「构建前清理 APK/AAB」，7 问全过；唯一已知风险=「非 APK 任务顺带删掉上次 APK」，已标注为已知风险 + 缓解）。
> 结论（grilling 复审 Round 1，3 决策定稿）：**全按推荐裁决**——RQ1 清理范围收窄到 `app/build/outputs`（与扫描兜底路径不对称，标注为已知边界）；RQ2 无条件清理（不按 task 名收窄）；RQ3 直接实现、不复现。三决策均确认原方案，正文已按裁决修正（见「Phase 4½」）。
> 触发：`android_compile` **成功**（`exit_code == 0`）时，后端 `_android_compile_outcome` 成功分支 rglob 无条件扫 `app/build/outputs/**` 下所有 `*.apk`/`*.aab`，把「上次构建的其他变体 APK」也扫进 `apk_files` → 污染 artifact_refs / freshness ledger / run-scoped workspace manifest。本问题与「失败列旧产物」（`20260908-apk-output-paths-failure-fix.md`）同源但独立：那个是「失败也列产物」的方向性误导，这个是「成功但产物清单不精确」的精度污染。
> 关联：`docs/technical-plans/20260908-apk-output-paths-failure-fix.md`（问题 1，已闭环）、`docs/technical-plans/20260816-broken-symlink-remediation-plan.md`（F4 时代引入的「目录扫描产物清单」）。

---

## Phase 1 — 参考项目对比结论（12 项目）

核心决策点：**「构建产物清单」的权威来源是什么？「本次产物」如何界定？**

| 决策点 | 参考项目（本地源码已核对） | 结论 |
|---|---|---|
| ① 产物清单权威来源 | Gradle（task output 由 task 声明 `outputs.files`，增量 up-to-date 判定）、Bazel（`outputs` attr 确定性声明） | **产物 = task 显式声明的 output，不是「构建后递归扫目录」** |
| ② CI 产物收集 | GitLab CI（`artifacts: paths` 显式 + `when: on_success`）、GitHub Actions（`upload-artifact` 显式 path） | 显式 paths；**且 CI workspace 每次 job 干净**，故「目录 glob 收集」无害 |
| ③ 产物显式读取 | E2B（`files.list`/`files.read` API，本地 `packages/python-sdk/tests/.../files/test_files_list.py`）、deepagents（`StateBackend` 把文件状态放 graph state `files` channel，`evals` 里 `get_run_artifacts` 显式 API） | 产物经显式 API/状态读取，非目录扫描 |
| ④ 诚实负结论（无此机制） | OpenHands（`openhands/` grep `artifact` 零命中，sandbox 只返回 exit_code+stdout/stderr）、SWE-agent（grep 零命中）、codex（`artifact` 指诊断产物/CA 证书，无构建产物扫描）、orca（`artifactSharingEnabled` 是 IDE 产物分享视图非构建输出）、herdr（命令输出重定向临时文件）、LangBot/bisheng（无 Android 构建产物输出机制） | **六者均无「构建后递归扫目录列产物」逻辑，天然无此 bug** |

**诚实负结论**：读本地源码确认（非 README 摘要）。OpenHands/SWE-agent/codex/orca/herdr/LangBot/bisheng 七者无「目录扫描产物清单」机制，只能作「产物=显式声明/显式读取」的旁证，不冒充直接依据。

**方法论结论**：本问题不是「正则/阈值」类，而是**「产物清单权威来源选错」的语义缺陷**——用「目录递归扫描」代替「显式声明/干净前提」。行业默认是「显式声明（task output / artifacts paths）+ 干净 workspace」；Clawith 的 workspace 跨 run 持久化（`android_build_backend.py` rw bind-mount + `auto_remove` 删容器不删卷）**打破了「目录扫描」隐含的「干净 workspace」前提**，导致扫描把历史残留当产物。修复方向 = 恢复「干净前提」（清理）或改权威来源（显式声明）。

---

## Phase 2 — 双源定根因

### 根因（三层，追到底）

**第一层（直接）：`_android_compile_outcome` 成功分支 rglob 无条件扫，不区分「本次」vs「历史」。**
- `backend/app/services/agent_tools.py:5641-5661`（源码，已 read_file 核实）：
  ```python
  if result.success and result.exit_code == 0:
      apk_files: list[Path] = []
      for scan_dir in [resolved_path / "app/build/outputs/apk",
                        resolved_path / "app/build/outputs/bundle"]:
          ...
          for pattern in ("*.apk", "*.aab"):
              for f in scan_dir.rglob(pattern):
                  apk_files.append(f.relative_to(resolved_path))
      # 标准路径未找到 → 递归扫描全项目
      if not apk_files:
          for pattern in ("*.apk", "*.aab"):
              for f in resolved_path.rglob(f"**/build/outputs/**/{pattern}"):
                  apk_files.append(...)
  ```
  无条件 `rglob`，**无「本次构建」界定基准**（无时间戳、无快照、无 task 绑定）。

**第二层（上游）：workspace 跨 run 持久化，`app/build/outputs/` 残留历史 APK。**
- `backend/app/services/sandbox/local/android_build_backend.py:387`：`host_project_path` 以 `mode:"rw"` bind-mount 到 `/workspace`。
- 同文件 `483-486` 注释 + `tmpfs` 配置（源码直接证据）：「`app/build` 和 `.gradle` 不挂 tmpfs — **APK 产物 + 配置缓存落在 bind mount 持久化**」；只有 `/workspace/build`（项目根）挂 tmpfs。
- 同文件 `501`（`auto_remove=True`）+ `802`（`container.remove(force=True)`）：删容器**不删卷** → 上次构建的 APK 永远留在 `app/build/outputs/`。

**第三层（最深）：产物清单权威来源选错——用「目录递归扫描」代替「显式声明」，而「目录扫描」隐含的「干净 workspace」前提被跨 run 持久化破坏。**
- 这是 F4（`20260816`）时代引入的「目录扫描产物清单」范式残留；问题 1 方案删掉了 entrypoint 层的同名冗余 `find`，但后端 `_android_compile_outcome` 这一层 rglob 的「目录扫描」范式未动。

### 双源证据

- **代码源**（充分，已 read_file）：上述三层的行号 + 函数名均为真实源码抄录。
- **日志源（旁证）**：问题 1 的真实失败日志证明「残留确实发生」——`task=lint`（不产 APK）失败时，`=== APK_OUTPUT_PATHS ===` 段列出 `./app/build/outputs/apk/{debug/app-debug.apk, androidTest/debug/app-debug-androidTest.apk, release/app-release.apk}` 三个路径。本次 lint 不产 APK，这三个**必是历史残留**，直接证明 `app/build/outputs/` 跨 run 残留多变体 APK。成功场景下后端 rglob 会扫到这同一批残留（扫描路径与 entrypoint find 完全同构），故「成功场景产物污染」是同一残留的必然结果。
- **诚实标注**：尚无「同一 workspace 连续两次构建、第二次产物清单混入第一次残留」的直接运行日志（成功场景产物清单被污染的真实 trace 未采集到）；结论由「代码无条件扫描 + 残留已证实的日志旁证」双源推出，非凭空。

### 可证伪性

若「无条件目录扫描 + 跨 run 残留」是根因，则：同一 workspace 先 `assembleRelease` 再 `assembleDebug`，第二次成功构建的 `apk_files` 会同时含 `release` 与 `debug` 两个 APK（debug 是本次、release 是残留）。反之，若构建前清理 APK 产物目录，第二次 `apk_files` 只含 `debug`——症状消失。

> 复审 RQ3 裁决（直接实现、不复现）：代码路径无歧义（无条件 rglob + 跨 run 持久化是确定事实），且问题 1 日志已直接证明「`app/build/outputs` 跨 run 残留多变体 APK」这一前提成立；复现需真实 Docker 连续两次构建、成本高，故不先复现，以回归测试锁定终态。

---

## Phase 3 — 最小修复方案（Ponytail 阶梯从低到高）

### 候选枚举（≥3，从最低档挑）

1. **A｜构建前清理 APK/AAB（最简，主推）**：在构建容器命令里、`./gradlew` 之前，精确删 `app/build/outputs` 下所有 `*.apk`/`*.aab`（保留 mapping/logs 等非 APK 产物）。构建后 rglob 扫到的 = 本次产物。
   - 优点：一条 `find ... -delete`，恢复「干净 workspace」前提，**一次性消灭「历史残留」和「up-to-date 复用产物 mtime 旧」两个歧义**（output 目录被清空 → package task 必然重打包或 from-cache 恢复，产物 mtime 均为本次）。
   - 缺点：① APK 的 up-to-date 失效 → package task 重跑（build cache `org.gradle.caching=true` 缓解，不重编译，代价几秒）；② 非 APK 任务（lint/test）也顺带删掉上次 APK（见 Phase 4 Q4 已知风险）。

2. **B｜构建前快照 + 构建后 diff（保留旧文件，被否）**：构建前 rglob 记录 APK 路径 set，构建后 diff 取新增。**被否理由**：up-to-date 复用产物（mtime 旧、非新增）被漏 → 「成功但无产物」误报；且需跨层传递快照状态。

3. **C｜构建前时间戳 + 构建后 mtime 过滤（被否）**：同 B 的 up-to-date 漏检；mtime 受时钟/时区/拷贝保留时间戳影响，不可靠。

4. **D｜Gradle init script 收集 task output（最精确，被否/过度）**：在 `_GRADLE_PROGRESS_INIT_SCRIPT` 的 `doLast` 收集 `task.outputs.files` 里的 APK/AAB 写侧信道，后端读。**被否理由**：AGP 的 `assemble*` 是 lifecycle task 无 output，产物在 `package*` task，收集逻辑复杂且需验证 AGP output 暴露方式；违背「最小改动」（宪法 II），属投机式加固。

**取舍**：选 **A（清理）**——Ponytail 最低档（一条 `find -delete`），恢复「干净 workspace」是 CI 行业默认先例（GitLab/GitHub runner 每次干净），B/C 的 up-to-date 漏检是硬缺陷，D 过度。

### 修复内容（`backend/app/services/sandbox/local/android_build_backend.py`）

在 `execute()` 的构建 command（`containers.run(command=[...])`，约 451-473 行）里，`./gradlew` 之前插入一条清理语句（独立换行，避开 heredoc 后不能 `&&` 的约束）：

```python
f"GRADLE_PROGRESS_EOF\n"
f"find app/build/outputs -type f \\( -name '*.apk' -o -name '*.aab' \\) -delete 2>/dev/null || true\n"
f"./gradlew --no-daemon --console=plain -I /tmp/gradle-progress.gradle {_quote_gradle_tasks(str(gradle_task))} 2>&1 ",
```

要点：
- 只删 `*.apk`/`*.aab`，**保留** `mapping/`（release 混淆映射）、`logs/` 等非 APK 产物。
- `working_dir="/workspace"`，`app/build/outputs` 即容器内相对路径（与后端 `resolved_path` 同一 bind-mount 存储，容器内删 = 宿主机删，后端扫描能看到效果）。
- `2>/dev/null || true`：全新项目 `app/build/outputs` 不存在时 find 报错，吞掉且不阻断构建。
- **`agent_tools.py` 的扫描逻辑不改**——靠「构建前清理」保证「构建后扫到的 = 本次产物」。
- **清理范围裁决（RQ1）**：只清 `app/build/outputs`（标准应用模块），**有意不覆盖**扫描的兜底路径 `**/build/outputs/**`（非 `app` 应用模块 / 自定义 buildDir）。已知边界：若应用模块不叫 `app`，清理会静默 no-op（`2>/dev/null || true`），兜底扫描仍可能扫到该模块的历史残留；但污染证据（问题 1 日志）全在 `app/build/outputs`，非 `app` 应用模块极罕见且无污染证据，扩大范围属投机加固（宪法 II）。
- **清理时机裁决（RQ2）**：无条件（每次构建都清，含 lint/test），不按 task 名收窄。按 task 名判断「是否产 APK」脆弱（多任务 / 自定义 task / `bundle` vs `assemble` 枚举不全 → 漏判）；且 APK 可再生成、旧 APK 跨 run 本就不该新鲜（见 Phase 4 Q4）。

### 影响面（blast radius）

- **契约改动**：无 Python 契约改动（`_android_compile_outcome` 扫描逻辑、artifact_refs 格式、freshness verifier 均不动）。改的是「构建前清一次 APK/AAB 产物」这个运行时动作。
- **消费者影响**：
  - `_android_compile_outcome` 成功分支 rglob → 扫到清理后的产物 = 本次产物（受益，精确）。
  - freshness verifier（`verification.py` `artifact_refs` 消费）→ artifact_refs 不再混入历史 APK（受益）。
  - 旧 APK 的 `workspace://` 引用 → 旧 APK 被删后变 stale（正确语义：旧产物跨 run 本就不该新鲜）。
  - Gradle 增量 → APK up-to-date 失效，package task 重跑（build cache 缓解，见 Phase 4 Q4）。
- **爆炸半径**：`android_build_backend.py` 单文件（command 字符串）+ 测试文件 `test_android_build_backend_fixes.py`。
- **验证**：`scripts/arch-guard.sh` + `pytest tests/test_android_build_backend_fixes.py tests/test_agent_tools_android_compile_outcome.py`。

### 回归测试

- **根因路径（防回归）**：`test_android_build_backend_fixes.py` 新增一条源码断言——`containers.run` 的 `command` 含 APK/AAB 清理命令（`find app/build/outputs ... -delete`），且清理命令位于 `./gradlew` 之前（复用既有 `mock_docker_client` + `last_run_kwargs` 手法，`test_dev_shm_tmpfs_has_1g` 同款）。
- **终态（既有测试覆盖，无需新增）**：`test_agent_tools_android_compile_outcome.py` 既有 `test_apk_artifacts_listed_in_summary` / `test_apk_artifacts_emitted_as_workspace_refs` / `test_success_no_apk_returns_no_apk_message` 锁定后端扫描语义（本次不触碰，这些用例即终态回归）。

---

## Phase 4 — 7 角度评审

### Q1 根因是否正确？——通过
- **正向依据**：三层根因解释双源全部证据——源码 `agent_tools.py:5641-5661` 无条件 rglob（代码源）；`android_build_backend.py:483-486` tmpfs 注释「APK 产物落在 bind mount 持久化」+ `501` auto_remove（代码源）；问题 1 失败日志列 3 个历史 APK（日志旁证）。已追到最深因（「目录扫描」范式 + 跨 run 持久化打破干净前提）。
- **负向探针（反例测试）**：若「无条件扫描 + 残留」是根因，则 `task=lint` 失败日志里 `=== APK_OUTPUT_PATHS ===` 应列历史 APK 而非 `NO_APK_FOUND`——对照问题 1 日志，确实列 3 个历史 APK，**证实**。对立假设「会不会是扫描只发生在失败、成功不扫」——排除：源码 5641 `if result.success and result.exit_code == 0` 明确**只在成功时扫描**，失败走 5697 行失败分支不 rglob；且问题 1 失败场景的 APK 列表来自 entrypoint 的 find（已删），成功后端 rglob 与它是同一 `app/build/outputs` 目录、同构扫描。

### Q2 根治方案是否正确？——通过
- **正向依据**：方案改的是 Q1 定的最深因（「目录扫描」缺干净前提）——构建前清理恢复干净前提，`apk_files` 恒等于本次产物，非止痛药。
- **负向探针（删除测试）**：删掉本方案（即不清理），第二次 `assembleDebug` 后 `app/build/outputs/apk/release/` 的旧 APK 仍在，rglob 仍扫进 → **会复发**。故清理是根治。

### Q3 参考资料是否正确？——通过
- **正向依据**：Gradle task output 声明 / GitLab `artifacts paths + when:on_success` / GitHub `upload-artifact` 显式 path / E2B files API / deepagents StateBackend 是**同类问题（产物清单权威来源 + 干净前提）**；OpenHands/SWE-agent/codex/orca/herdr/LangBot/bisheng 是读本地源码后的诚实负结论（非 README 摘要）；停更项目未当第一依据。
- **负向探针**：找一个可能引用错的点——GitLab `artifacts` 的 `when` 默认值，核对是 `on_success`（非 `always`），失败 job 默认不发布产物，**无误**。

### Q4 副作用与爆炸半径是否排查完？——通过（含已知风险）
- **正向依据**：①副作用面——无外部写、无缓存失效、无连接/资源、无权限边界变化；唯一副作用是「删 APK 文件」。②影响面——消费者全过：`_android_compile_outcome`（受益）、freshness verifier（受益）、旧 APK workspace:// 引用（变 stale，正确语义）、Gradle 增量（package task 重跑，build cache 缓解）。
- **负向探针**：特意找一个会漏掉的副作用——**「非 APK 任务（lint/test）顺带删掉上次 assemble 的 APK」**：用户 `assembleDebug` 成功拿 APK → 再跑 `lint`（本方案会清 APK）→ 旧 APK 没了。核下来**这是唯一的已知风险**：但（a）APK 可再生成（重跑 assemble）；（b）旧 APK 跨 run 引用本就不该被支持（freshness 语义 stale）；（c）lint 后「无 APK」与「产物清单为空」一致。**已标注为已知风险 + 缓解**（不阻断交付）。
- **负向探针**：另一个会漏掉的——「清理会不会误删 mapping.txt」：清理命令精确匹配 `*.apk`/`*.aab`，`mapping/`、`logs/` 不受影响，**无误**。

### Q5 这是最优且必要的方案吗？——通过
- **正向依据**：①枚举 4 候选（A 清理 / B 快照 diff / C mtime / D Gradle output），从 Ponytail 最低档 A 起步；A 是「一条 find -delete」的最简档，且彻底消灭残留 + up-to-date 歧义。②修的是「已发生的故障」（日志旁证证明残留已发生、成功场景必然污染），非臆想风险。
- **负向探针（更简单档测试）**：试过「有没有比 A 更简单的档」——**没有**，A 是「删文件」（负行数方向），不存在更简；B/C 是「不删但过滤」方向、行数更多且有 up-to-date 漏检，故 A 是最优。

### Q6 是否已有可复用的逻辑？——通过
- **正向依据**：方案**不新增产物扫描逻辑**——复用现有「构建前准备工作在 command 里做」的既有范式（`sdkmanager --licenses`、`chmod +x ./gradlew`、进度 init script 注入同处），清理是同一范式的又一前置步骤，无重复造。
- **负向探针**：查过知识图谱/代码是否已有「构建产物清理/快照」等价逻辑——**无**（`app/build` 由 Gradle 自管，Clawith 无产物清理先例）；但「构建前 command 预处理」范式已存在（同 command 里 sdkmanager/chmod），清理复用该范式而非新机制，**无需**新增替代逻辑。

### Q7 会破坏 Clawith 的特性吗？——通过（含已知风险）
- **正向依据**：逐条过宪法 C1–C6 + 红线。C1 证据先行（源码 + 日志旁证双源）；C2 最小改动（一条 find -delete）；C3 契约与状态所有权（不改任何契约，产物契约仍由后端 artifact_refs 持有，清理只影响「目录里有没有旧文件」）；C4 测试证行为（防回归源码断言 + 既有终态测试）；C5 保留既有工作（成功场景产物信息由后端继续提供；mapping/logs 等非 APK 产物不动）；C6 数据边界（单文件 command 改动，无跨模块）。红线核对：durable run/checkpoint、多租户隔离、exactly-once、前缀缓存稳定性、WS 状态机、飞书通道——**均不触碰**（纯构建容器内删一次 APK 文件）。
- **负向探针**：把方案对红线过一遍找是否碰到某红线——最可疑的是「exactly-once」（删除动作是否重复执行/幂等）：清理命令每次构建跑一次、`find -delete` 幂等（文件不存在返回 0）、`|| true` 兜底，**不破坏** exactly-once；「前缀缓存稳定性」：不碰 LLM 输入，**不碰**。

---

## Phase 4½ — grilling 复审（Round 1，3 决策定稿）

7 角度评审后追加一轮 grilling 复审（`grill-with-docs`），针对 3 个评审未显式拍板的决策点逐条裁决。facts 已独立重核（行号/函数名/路径映射/命令转义/测试手法全部与方案一致，无硬伤）；以下 3 决策按推荐裁决并已写回正文。

### RQ1 清理范围：收窄到 `app/build/outputs`（已裁决）
- **事实**：扫描有兜底 `rglob("**/build/outputs/**/{pattern}")` 覆盖全项目，方案 A 只清 `app/build/outputs`，两者不对称。
- **裁决**：保持窄范围。污染证据全在 `app` 模块；非 `app` 应用模块极罕见且无污染证据，扩大属投机加固（宪法 II）。已作为「已知边界」写入 Phase 3 要点。

### RQ2 清理触发条件：无条件（已裁决）
- **裁决**：无条件清理，不按 task 名收窄。按 task 名判断「是否产 APK」脆弱（枚举不全 → 漏判）；APK 可再生成、旧 APK 跨 run 本就不该新鲜。与 Phase 4 Q4 已知风险一致。

### RQ3 可证伪性缺口：直接实现（已裁决）
- **裁决**：不复现。代码路径无歧义 + 问题 1 日志已证明残留前提成立；复现成本高。已写入 Phase 2 可证伪性。

---

## Phase 5 — 实现落地闭环（待实现后执行）

方案落地为 diff 后，**必须**跑 `code-review`（Spec 轴对照本方案 + Phase 4 裁决）复核 diff：无偏离、无夹带范围外改动、无「评审时没提过的机制」。跳过或存在未解决偏离 → 不算交付闭环。

---

## 交付结论

- **裁决：评审通过**（7 问全过 + grilling 复审 3 决策定稿；唯一已知风险=「非 APK 任务顺带删掉上次 APK」，已标注为已知风险 + 缓解；已知边界=「非 `app` 应用模块清理 no-op」，见 Phase 4½ RQ1）。
- **主推**：`android_build_backend.py` 构建 command 里、`./gradlew` 前插入 `find app/build/outputs -type f \( -name '*.apk' -o -name '*.aab' \) -delete 2>/dev/null || true`（候选 A）。
- **实现状态**：**待实现**（方案已定稿）。实现后按 Phase 5 跑 `code-review` 闭环。
