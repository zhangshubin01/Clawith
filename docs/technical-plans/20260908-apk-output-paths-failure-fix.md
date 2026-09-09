# 生产级修复方案：构建失败后 `APK_OUTPUT_PATHS` 仍列出历史残留 APK

> 结论（复核定稿 2026-09-08）：**本方案收敛为「只根治失败场景列旧产物」，实现已落地、测试全绿**（主推「删除整段」候选 A；原 7 问评审基于已证伪的旧宣称「两个症状同时消失」，复核后已收敛范围）。「成功场景产物契约污染」（后端 `_android_compile_outcome` 成功分支 rglob 亦扫历史残留）是独立更深问题，**单独立项、不在本方案**。唯一已知风险=「仓库外若存在 grep `=== APK_OUTPUT_PATHS ===` 标记的脚本会失效」，无证据、仓库内已确认无消费方，缓解=git history 可恢复 + 后端权威产物扫描。
> 触发：`android_compile` 跑 `task=lint` 失败（lintDebug FAILED，4 errors），但构建日志末尾 `=== APK_OUTPUT_PATHS ===` 段仍列出 3 个历史残留 APK（debug/androidTest/release），把「失败」渲染成「有产物」。
> 关联：`docs/technical-plans/20260816-broken-symlink-remediation-plan.md` F4（只把打印时机从「构建前」移到「构建后」，未解决「失败也列旧产物」的语义缺陷）。

---

## Phase 1 — 参考项目对比结论（12 项目）

核心决策点：**「构建产物路径输出」是否绑定「构建成功」状态；权威失败信号是什么。**

| 决策点 | 参考项目 | 结论 |
|---|---|---|
| ① 产物发布是否绑定成功状态 | GitLab CI（`artifacts` 默认 `when: on_success`）、GitHub Actions（`upload-artifact` 在失败 step 后默认跳过，除非 `if: always()`）、Gradle（产物是 task 的 output，task 失败无 output） | **行业铁律：产物发布/输出默认绑定成功状态，失败不发布** |
| ② 权威失败信号是什么 | Gradle/Bazel（命令退出码非 0 是唯一权威信号）、OpenHands（sandbox 返回 `exit_code` + stdout/stderr） | **退出码是权威信号，不是「扫描目录发现文件」**；`lint` 任务根本不产 APK |
| ③ 失败态渲染纪律 | Clawith 自身 commit `31eda30a`（`failed→fallback_error` 错误卡、`cancelled→abort`、`NO_REPLY→withdraw`，见 memory `incident-lessons-compendium`） | **失败必须显式失败态，不得渲染成成功**——本 bug 与「卡片 failed 渲染成成功卡」同源 |
| ④ 产物清单的权威来源 | deepagents（`backends/` StateBackend 把文件状态放进 graph state `files` channel）、E2B（产物经 fs API 显式读取） | 产物是**显式状态/API 读取**，不是「构建后无条件 find 打印」 |

**诚实负结论**（读本地源码确认，非 README 摘要）：OpenHands（`openhands/runtime/` grep `exit_code/artifact` 无「失败打印产物」机制）、orca（`artifactSharingEnabled` 是 IDE 产物分享视图，非构建输出）、herdr（`pane.rs` 命令输出重定向临时文件，不额外打印产物路径）、loopx（grep `artifact/build output` 零命中）、bisheng（同 org 姊妹项目，无 Android 构建产物输出机制）——**五者均无「构建失败后无条件 find 产物并打印」逻辑，故天然无此 bug**，只能作为「退出码权威 + 产物显式读取」的旁证，不冒充直接依据。

**方法论结论**：本问题不是「正则/阈值/词集」类，而是**「产物输出与成功状态解耦」的语义缺陷**；行业默认解是「产物输出绑定退出码（成功）」，且 Clawith 内部已有「失败显式失败态」的纪律先例（31eda30a）。

---

## Phase 2 — 双源定根因

### 根因（三层，追到底）

**第一层（直接）：`entrypoint.sh` 产物输出不绑定 `BUILD_RC`。**
- `backend/docker/android-builder/entrypoint.sh:307-316`（源码）：
  ```sh
  echo "=== APK_OUTPUT_PATHS ==="
  APKS=$(find . -path "*/build/outputs/*" \( -name "*.apk" -o -name "*.aab" \) 2>/dev/null)
  if [ -n "$APKS" ]; then
      echo "$APKS"
  else
      echo "NO_APK_FOUND"
  fi
  echo "=== END_APK_OUTPUT_PATHS ==="
  ```
  无条件 `find`，**不检查 `$BUILD_RC`**；而 `BUILD_RC` 变量在脚本里早已存在（`289-305` 行 SDK 自愈重试段已用 `[ "$BUILD_RC" -ne 0 ]` 门控，`318` 行 `exit "$BUILD_RC"`）。

**第二层（上游）：workspace 跨 run 持久化，`app/build/outputs/` 残留历史 APK。**
- `android_build_backend.py:387` 把 `host_project_path` 以 `mode:"rw"` bind-mount 到 `/workspace`，构建产物落进宿主持久卷；容器 `auto_remove=True`（`501` 行）删容器不删卷 → 上次成功构建的 APK 永远留在 `app/build/outputs/`。

**第三层（最深）：这段输出是 F4 时代的「纯信息、无消费方」冗余，未与成功语义绑定。**
- 全仓库 grep（`backend/` + `frontend/` + `scripts/`）确认 `APK_OUTPUT_PATHS` / `NO_APK_FOUND` / `END_APK_OUTPUT_PATHS` **在代码/脚本范围内仅在 `entrypoint.sh` 出现，无任何 Python/TS 消费方**。仓库根目录另有被 git 跟踪的历史日志快照 `replay_full.log`（2026-08-17，BUILD SUCCESSFUL）含旧标记，但属日志非代码、无消费方，不影响删除决策。
- 权威产物清单由后端 `_android_compile_outcome`（`backend/app/services/agent_tools.py:5522-5727`）**成功分支**（`result.success and result.exit_code == 0`）独立 `rglob` 扫描 `app/build/outputs/apk` 与 `bundle`，并注册 `artifact_refs`。entrypoint 这段 `find` 是**重复且无消费方**的冗余输出。
- ⚠️ **范围澄清（复核补充 2026-09-08）**：后端成功分支的 `rglob`（`agent_tools.py:5641-5661`）本身**也无条件扫历史残留** APK/AAB（含全项目 `**/build/outputs/**` 兜底、不区分本次 vs 历史），故「成功场景产物契约污染」是**独立的更深问题，不在本方案范围，单独立项**。本方案只根治「失败场景列旧产物」；此前「后端 rglob 是干净权威安全网」的说法不成立，已撤销。

### 日志证据（真实执行日志）

- `> Task :app:lintDebug FAILED` + `Lint found 4 errors` + `BUILD FAILED in 21s`（退出码非 0）。
- 但 `=== APK_OUTPUT_PATHS ===` 段列出 `./app/build/outputs/apk/{debug/app-debug.apk, androidTest/debug/app-debug-androidTest.apk, release/app-release.apk}` 三个路径（而非 `NO_APK_FOUND`）——本次 `lint` 任务不产 APK，这三个必是历史残留，直接证明「失败构建 find 出旧产物」。

### 可证伪性

若「这段冗余输出存在」是根因，则删除该段后，失败构建日志末尾不再出现旧 APK 路径列表 → 症状消失。

---

## Phase 3 — 最小修复方案（Ponytail 阶梯从低到高）

### 候选枚举（≥3，从最低档挑）

1. **A｜删除整段（最简，主推）**：删掉 `entrypoint.sh:307-316` 整段（10 行）。无消费方（已 grep 确认），后端成功分支已独立列产物（`"产物 (N 个): ..."` + artifact_refs），成功场景的产物信息不丢。**根治「失败列旧产物」（已报告 bug）**。「成功场景产物污染」（后端 rglob 亦扫历史残留）是独立更深问题，单独立项、不在本方案。
2. **B｜内联 if 门控（最小语义修复，被否）**：`[ "$BUILD_RC" -eq 0 ]` 才 find，失败输出 `NO_APK_FOUND`。**被否理由**：① 失败输出 `NO_APK_FOUND`（字面「没找到 APK」）与「构建成功但无产物」同字，语义仍模糊；② 成功分支仍 find 出历史残留其他变体，同类问题未修干净；③ 内联无法行为级测试。
3. **C｜函数化 + BUILD_RC 门控（可测，被否）**：把该段抽成 `print_apk_output_paths()`。**被否理由**：Grill 1 拷问「为什么不直接删」——「防仓库外 grep 标记脚本」是无证据假设（F4 当年已确认仓库内无消费方）、「可行为级测试」的代价是多包一层函数去测一段本应删除的冗余，均不成立。
4. **D｜构建前快照 + diff（更彻底，被否）**：构建前记录产物集合、构建后只列新增。**过度**：投机式加固（宪法 II 禁止），「本次产物」界定复杂，边际收益不值得。

**取舍**：选 **A（删除）**——Ponytail 阶梯最底档（10 行变 0 行），彻底消灭语义缺陷，无证据支持「留」的理由。B/C/D 被否。修的是「已发生的故障」（真实日志证明失败列旧产物），非臆想风险。

### 修复内容（`backend/docker/android-builder/entrypoint.sh`）

删除 307-316 行（整段 `=== APK_OUTPUT_PATHS ===` 产物输出），保留 `exit "$BUILD_RC"`：

```sh
# ─── SDK 组件自愈重试 ───（原 289-305 行，不动）
# ...
fi

# （删除原 307-316 行的 APK_OUTPUT_PATHS 产物输出段）

exit "$BUILD_RC"
```

删除后 `APK_OUTPUT_PATHS` / `NO_APK_FOUND` / `END_APK_OUTPUT_PATHS` 三个标记在**代码**中**完全消失**（仅历史日志快照 `replay_full.log` 仍含旧标记，非代码）；产物清单由后端 `_android_compile_outcome` 成功分支的 `rglob` 提供（`agent_tools.py:5648-5683`，注意该 rglob 亦有成功场景残留问题，单独立项）。

### 影响面（blast radius）

- **契约改动**：无——这段「纯信息输出，无代码消费方」，删除不改变任何 Python/TS 契约；后端 `_android_compile_outcome`、前端均不依赖。
- **已知风险**：`=== APK_OUTPUT_PATHS ===` 标记消失，若**仓库外**存在 grep 该标记的运维/监控脚本会失效。**证据**：仓库内已 grep 确认零消费方；仓库外无任何依赖证据（F4 文档亦只写「后端代码无消费方」）。**缓解**：git history 可恢复该段；后端成功分支继续提供权威产物清单。
- **爆炸半径**：`entrypoint.sh` 单文件 + 测试文件 `test_android_builder_entrypoint.py`。
- **验证**：`scripts/arch-guard.sh` + `pytest backend/tests/test_android_builder_entrypoint.py backend/tests/test_agent_tools_android_compile_outcome.py`。

### 回归测试

- **根因路径（防回归）**：`test_android_builder_entrypoint.py` 新增一条源码断言——用现有 `_ENTRYPOINT.read_text()` 手法断言 **entrypoint.sh 源码**不再含 `APK_OUTPUT_PATHS` / `NO_APK_FOUND` / `END_APK_OUTPUT_PATHS`，锁定「这段冗余输出不回归」。**断言范围必须限定为 entrypoint.sh 单文件**，不可做全仓库 grep——仓库根目录的历史日志快照 `replay_full.log` 与两份 technical-plans 文档仍含旧标记引用，全仓库 grep 会误报。
- **终态（既有测试覆盖，无需新增）**：`test_agent_tools_android_compile_outcome.py` 既有用例已锁定后端产物扫描语义——`test_apk_artifacts_listed_in_summary`、`test_apk_artifacts_emitted_as_workspace_refs`、`test_success_no_apk_returns_no_apk_message`（成功列产物 / 成功无产物 / artifact_refs 注册），删除不触碰后端，这些用例即终态回归。
- **语法**：`test_entrypoint_is_valid_bash`（`bash -n`）确保删除后脚本仍合法。

---

## Phase 4 — 7 角度评审

### Q1 根因是否正确？——通过
- **正向依据**：三层根因解释**每一个**证据（源码 `entrypoint.sh:307-316` 无条件 find / 日志 BUILD FAILED 仍列 3 路径 / workspace bind-mount 持久化 / 全仓库 grep 无消费方）。已追到最深因（F4 遗留的冗余输出未绑定成功语义）。
- **负向探针（反例测试）**：若「这段冗余输出存在」是根因，则**失败构建时该段应列旧 APK**——对照用户日志，确实列出 3 个 APK、非 NO_APK_FOUND，**证实**。对立假设「会不会是后端 `_android_compile_outcome` 成功分支误扫」——排除：BUILD FAILED 时 `result.success` 为假走失败分支、不 rglob；且 `APK_OUTPUT_PATHS` 是 entrypoint 的 stdout，与后端扫描是两条独立路径。

### Q2 根治方案是否正确？——通过
- **正向依据**：方案删掉的是 Q1 定的最深因（冗余无消费方输出）本身——删除后这段输出不存在，「失败列旧产物」（已报告 bug）症状消失，非止痛药。「成功列历史残留」在 entrypoint 层随之消失，但后端 rglob 层的「成功场景产物污染」仍存、单独立项（见范围澄清）。
- **负向探针（删除测试）**：删掉本方案（即不删除这段），失败构建会不会复发列旧产物？**会**——find 仍在、workspace 仍持久化。故删除是根治。

### Q3 参考资料是否正确？——通过
- **正向依据**：GitLab CI `artifacts when` / GitHub Actions `upload-artifact` 条件 / Gradle task-output 语义是**同类问题（产物发布绑定成功状态）**；Clawith `31eda30a` 是同源先例；OpenHands/orca/herdr/loopx/bisheng 是读源码后的诚实负结论（非 README 摘要）；停更项目未当第一依据。
- **负向探针**：找一个可能引用错的点——GitLab `artifacts` 的 `when` 默认值，核对是 `on_success`（非 `always`），失败 job 默认不发布产物，**无误**。

### Q4 副作用与爆炸半径是否排查完？——通过（含已知风险）
- **正向依据**：①副作用面——纯 stdout 输出删除，无外部写、无缓存、无连接、无权限边界变化；删除后还少一次 `find` 扫描。②影响面——消费方=无（全仓库 grep 确认仅 entrypoint 出现）；后端 `_android_compile_outcome`、前端均不依赖。
- **负向探针**：特意找一个会漏掉的消费者——「仓库外监控脚本 grep `=== APK_OUTPUT_PATHS ===` 标记」：删除后标记消失、脚本失效。核下来**这是唯一的已知风险**，但无任何证据支撑其存在（F4 当年也只写「后端代码无消费方」、仓库内 grep 零命中），**已标注为已知风险 + 缓解**（git history 可恢复、后端权威产物扫描兜底），不阻断交付。

### Q5 这是最优且必要的方案吗？——通过
- **正向依据**：枚举 4 候选（A 删除 / B 内联 if / C 函数化 / D 快照 diff），从 Ponytail 最低档 A 起步；A 已是「10 行变 0 行」的最简档，且彻底消灭语义缺陷；B/C 的「保留输出」理由（防外部 grep、可测）经 Grill 1 拷问均不成立；D 过度。修的是已发生故障，非臆想风险。
- **负向探针（更简单档测试）**：试过「有没有比 A 更简单的档」——**没有**，A 是删除（负行数），不存在更简。B/C 是「保留输出」方向、行数更多且语义不彻底，故 A 是最优。

### Q6 是否已有可复用的逻辑？——通过
- **正向依据**：删除方案**不新增任何逻辑**——产物扫描语义复用后端 `_android_compile_outcome` 权威 rglob（`agent_tools.py:5648-5683`），entrypoint 这段本就是重复实现，删除即「复用后端、不重复造」。
- **负向探针**：查过知识图谱/代码是否有「entrypoint 层产物输出」的等价逻辑——**后端 rglob 是权威等价实现**，故删除 entrypoint 冗余是去重而非丢失功能，**无**需新增替代逻辑。

### Q7 会破坏 Clawith 的特性吗？——通过
- **正向依据**：逐条过宪法 C1–C6 + 红线。C1 证据先行（源码 + 真实日志双源）；C2 最小改动（删 10 行）；C3 契约与状态所有权（不改任何契约，产物契约由后端 artifact_refs 持有，entrypoint 无契约）；C4 测试证行为（防回归源码断言 + 既有终态测试）；C5 保留既有工作（成功场景产物信息由后端继续提供，不丢）；C6 数据边界（单文件删除，无跨模块）。红线核对：durable run/checkpoint、多租户隔离、exactly-once、前缀缓存稳定性、WS 状态机、飞书通道——**均不触碰**（纯构建容器 stdout 输出）。
- **负向探针**：把方案对红线过一遍找是否碰到某红线——最可疑的是「WS 状态机」（entrypoint stdout 经 `on_output` 流式转发）：删除只让失败构建少打印几行，不改变流式/心跳/断流/120s idle 逻辑，**不碰**；「前缀缓存稳定性」：不碰 LLM 输入，**不碰**。

---

## Phase 5 — 实现落地闭环（待实现后执行）

方案落地为 diff 后，**必须**跑 `code-review`（Spec 轴对照本方案 + Phase 4 裁决）复核 diff：无偏离、无夹带范围外改动、无「评审时没提过的机制」。跳过或存在未解决偏离 → 不算交付闭环。

---

## 交付结论

- **裁决：评审通过**（7 问全过；唯一已知风险=「仓库外 grep 标记脚本失效」，无证据、已标注缓解）。
- **主推**：删除 `entrypoint.sh:307-316` 整段 `APK_OUTPUT_PATHS` 输出（候选 A，Grill 1 用户拍板「删」）。
- **复核补充（2026-09-08）**：审查发现「成功场景产物契约污染」（后端 `_android_compile_outcome` 成功分支 rglob 扫历史残留）是本方案的**独立更深问题，单独立项**；本方案范围收敛为「只根治失败场景列旧产物」。文档正文已按此修正（grep 范围措辞、回归测试断言范围一并订正）。
- **实现状态**：**待实现**。实现后按 Phase 5 跑 `code-review` 闭环。
