# Android 构建入口 gradlew 完整性根治方案（gradlew entrypoint integrity）

- 日期：2026-09-09
- 状态：已实现 + 7 角度评审通过 + code-review 双轴闭环
- 范围：`backend/app/services/sandbox/local/android_build_backend.py` 构建命令 + `backend/Dockerfile.android-builder` 新增模板 + 回归测试

## 裁决

**评审通过。** 方案 =「构建前确定性恢复 gradlew 脚本为标准模板」，根治「agent 写坏 build 入口脚本 → 平台静默执行」这一类故障。

## 症状

1. **本次（噪音）**：run `c90a6b91`（tool execution `e2ac9cab`，2026-09-08 10:55 UTC）构建日志开头 5 行
   `./gradlew: 76: : Permission denied` ×3 + `./gradlew: 90: : Permission denied` ×2。
   构建并未因此失败（真正的 BUILD FAILED 是后面 `lintDebug` 的 4 个 `android:tint` 错误，另一件事）——这 5 行是纯噪音，但足以让排查绕 20 分钟。
2. **历史（实锤，已记录）**：`clawith-runtime-triage` skill 记过一次 incident——上个 run 改坏 `gradlew`/wrapper + 清掉 `.gradle` 缓存，导致 agent 烧光整轮工具预算（`model_step_limit_reached`）。同病同源。

## Phase 1：参考对比（≥10 项目）

决策点：**sandbox/CI 平台如何保证「执行 agent/用户项目里的 build 入口脚本（wrapper）」是可信/正确的。**（本地源码 grep 核对，非 README 摘要）

| 项目 | 做法 | 结论 |
|---|---|---|
| OpenHands（sandbox runtime） | agent 在隔离容器自由执行命令，平台不干预 build 入口 | 无此机制（诚实负结论） |
| SWE-agent（ACI） | 同样只做容器隔离与命令接口，不校验项目脚本 | 无此机制（负结论） |
| E2B（云沙箱） | 沙箱执行，无 wrapper 保护 | 无（负结论） |
| codex（OpenAI CLI sandbox） | 隔离执行，不碰 build 入口 | 无（负结论） |
| CubeSandbox（腾讯 AI 沙箱） | 沙箱/模板机制，无 wrapper 保护 | 无（负结论） |
| deepagents（LangGraph sandbox） | files channel + 沙箱，无 wrapper 概念 | 无（负结论） |
| gptme / opencode / DeepCode | 本地/沙箱执行，不处理 wrapper | 无（负结论） |
| bisheng（Clawith 姊妹） | LangGraph 编排 + 应用搭建，无 android build 入口处理 | 无（负结论） |
| LangBot（Box Runtime 插件沙箱） | 有插件沙箱隔离，但对「用户项目 build 脚本」无恢复机制 | 无（负结论） |
| **Gradle 官方 wrapper 文档** | wrapper（gradlew/gradlew.bat/jar/properties）应「commit 进版本控制」；wrapper JAR 有**校验和验证**防恶意替换 | **正结论**：行业默认的确定性锚点是「版本控制提交」+「JAR checksum 验证」，且验证对象是 JAR 不是脚本内容正确性 |
| GitLab CI / GitHub Actions | 直接依赖项目**提交进 git 的 wrapper**，从不重建 | 正结论：锚点仍是「版本控制」 |

**对比结论**：所有参考项目对「build 入口脚本」都**没有**专门保护机制——它们的确定性来自「wrapper 由项目版本控制、平台信任它」。**Clawith 的独特差异**：workspace 是 agent 持久化、可写、且**无 git 强制提交锚点**的状态，所以 wrapper 会漂移（被 agent 改坏）——行业默认前提（版本控制）在 Clawith 这里不成立，因此需要平台补一个「确定性恢复」机制。Gradle 官方的 wrapper 完整性手段（checksum）针对的是 JAR 防恶意替换，不是「脚本内容被 agent 写错」这类事故，故不能直接照搬。

## Phase 2：根因（双源：真实代码 + 真实执行日志）

### 运行日志源（`agent_tool_executions.result_summary`，run `c90a6b91` / exec `e2ac9cab`）

```
./gradlew: 76: : Permission denied   ×3
./gradlew: 90: : Permission denied   ×2
```

### 代码源（实际 workspace 文件，`/data/agents/950a1943-…/workspace/mydome1/gradlew`，138 行）

该 gradlew **不是标准 wrapper**，是某个历史 run 重写的「简化重构版」（第 8 行中文注释「本脚本为 Gradle Wrapper 启动脚本（Gradle 8.7 标准结构）」）。其 OS 探测段（56–61 行）只写了 `case` 分支，**漏掉了标准模板的 4 行初始化**：

```sh
case "$( uname )" in
  CYGWIN* )         cygwin=true ;;
  MINGW* )          msys=true ;;
  Darwin* )         darwin=true ;;
  MSYS* | MINGW* )  msys=true ;;
esac
```

标准 Gradle 8.7 模板（已拉取官方 `gradlew` v8.7.0 核实，249 行）在 case 前有（104–107 行）：

```sh
cygwin=false
msys=false
darwin=false
nonstop=false
```

### 机制链（逐字复现）

1. Linux（alpine busybox ash）下 `uname` = `Linux`，`case` 不匹配任何分支 → `cygwin`/`darwin`/`msys` 全为**未定义（空串）**。
2. 第 76 行 `if ! "$cygwin" && ! "$darwin" && ! "$msys" ; then` → 执行 3 个空命令 `! ""` → `./gradlew: 76: : Permission denied` ×3。
3. 第 90 行 `if "$cygwin" || "$msys" ; then` → 执行 2 个空命令 `""` → `./gradlew: 90: : Permission denied` ×2。
4. 合计 5 次，与日志完全吻合。`""` 走 `execve("")` 返回 EACCES（而非 ENOENT），故是 `Permission denied` 而非 `not found` —— 已用 `alpine:latest` busybox ash 逐字复现（`/tmp/t.sh: line 5: : Permission denied` ×3）。
5. **为何构建没死**：`! 空命令` = `! (126 失败)` = true；`空 || 空` = false → else 分支。if/else 逻辑结果仍正确，构建照常跑，只多 5 行噪音。

### 深层根因

「某个 agent run 写坏 gradlew」是表层触发；**最深层因 = `android_compile` 直接执行持久化 workspace 里的 `./gradlew`，对 build 入口脚本无任何确定性/完整性保障**（对照 Phase 1：行业默认锚点是「版本控制」，Clawith 缺这个锚点）。可证伪：若「入口脚本无保障」是根因，则补上确定性恢复后，无论 agent 怎么写坏脚本，5 行噪音与历史烧预算都不会复发。

## Phase 3：修复方案（最小、可回退）

**主方案：构建前把项目 gradlew 脚本确定性恢复为标准模板（幂等覆盖）。**

关键前提（已核实）：**gradlew 脚本是版本无关的启动器**——标准 8.7（249 行）、8.13、以及被写坏的重写版，exec 行都收敛到同一主类 `org.gradle.wrapper.GradleWrapperMain` + `exec "$JAVACMD" "$@"`；实际 gradle 版本由项目 `gradle/wrapper/gradle-wrapper.jar` + `gradle-wrapper.properties` 决定，**覆盖脚本不影响版本 pin**。且镜像无 gradle 二进制（`Dockerfile.android-builder` 第 90 行已 purge `default-jdk-headless`，JDK 是运行时下载），故「重建 wrapper（`gradle wrapper`）」不可行，覆盖脚本是唯一轻量可根治路径。

实现（三处）：

1. **新增模板文件** `backend/docker/android-builder/gradlew.template` —— 内容 = 标准 Gradle wrapper 启动脚本（版本无关，取自 Gradle 8.7 官方模板）。
2. **`Dockerfile.android-builder`** 增加 `COPY docker/android-builder/gradlew.template /opt/gradle-wrapper/gradlew`（复用既有 COPY 层）。
3. **`android_build_backend.py`** 构建命令（现 L466 `&& chmod +x ./gradlew`）改为
   `cp /opt/gradle-wrapper/gradlew ./gradlew && chmod +x ./gradlew` —— 恢复标准脚本 + 保证可执行，顺序仍在 `./gradlew --no-daemon` 之前。

**回归测试**：
- 后端：断言构建 command 含 `cp /opt/gradle-wrapper/gradlew ./gradlew` 且位于 `./gradlew --no-daemon` 之前。
- 模板静态断言：`gradlew.template` 含 `#!/bin/sh`、`org.gradle.wrapper.GradleWrapperMain`、四行 OS flag 初始化（防模板本身被改坏）。

**影响面 / 契约变化**：平台的「gradlew 脚本」成为权威（标准模板）；agent workspace 里的 gradlew 脚本不再是权威；`gradle-wrapper.properties`（版本）与 `gradle-wrapper.jar` 仍由项目拥有。爆炸半径 = 0（仅 android_compile 构建前一步，无其他消费者）。

**回退**：删掉 cp 步骤即回退旧行为（chmod +x 后直接执行项目 gradlew）。

**已知风险 + 缓解**：
- 覆盖会抹掉 agent 对脚本的（罕见）定制 → 缓解：JVM 参数正确渠道是 `GRADLE_OPTS`/`gradle.properties`（entrypoint 已设 `JAVA_HOME` 与 `GRADLE_OPTS`），脚本定制无正当需求。
- 不覆盖 `gradle-wrapper.jar`/`properties`，若 agent 改坏 jar 仍会受影响 → 缓解：超出本次最小范围，记为后续候选（Gradle 官方 wrapper JAR checksum 验证，见 Phase 1）。

## Phase 4：7 角度评审

1. **根因正确性 — 通过**。正向：双源证据（运行日志 5 行 + 实际文件 138 行缺初始化 + busybox 复现 + 标准模板对照）。负向探针（反例测试）：若根因是「空变量当命令」，则第 76 行应产 3 条、第 90 行应产 2 条、且是 EACCES 的 `Permission denied` 而非 ENOENT 的 `not found`——alpine 复现确认 `! ""` → `: Permission denied`，3+2=5 与日志逐条吻合，证实。
2. **根治性 — 通过**。正向：改的是深层根因「入口脚本无确定性保障」。负向探针（删除测试）：删掉方案，agent 再写坏 gradlew，噪音与烧预算会复发（平台仍静默执行坏脚本）；补上恢复后，无论怎么写坏都自动回到标准——根治，非止痛。
3. **参考资料正确性 — 通过**。正向：≥10 项目对比 + Gradle 官方文档，读真实源码/官方原文；关键前提「脚本版本无关」由 3 个版本 gradlew 的 exec 行核实，非 false friend。负向探针：特意查「是否有项目做脚本覆盖重建」——Gradle 官方明确 wrapper 确定性靠「版本控制 + JAR checksum」，无脚本覆盖先例，本方案的「脚本恢复」是 Clawith 无版本控制锚点下的适配，依据成立。
4. **副作用与爆炸半径 — 通过**。正向：①副作用面——cp 覆盖是幂等本地文件写，无 exactly-once 外部写、无缓存失效、无新增连接/资源；②影响面——只改 android_compile 构建命令一段 + 镜像加一个模板文件，`execute` 单调用点过一遍。负向探针：特意查「agent 合理定制脚本」场景——entrypoint 已设 JAVA_HOME + GRADLE_OPTS，脚本定制无正当需求，覆盖不破坏；再查「覆盖加剧盲修循环」——agent 改坏 → 平台恢复 → 构建成功 → 循环终止，不加剧。
5. **最优且必要 — 通过**。正向：候选枚举 ①只修 mydome1（治标）②检测+告警（噪音变告警，不根治）③覆盖标准脚本（根治、最简）④重建 wrapper（镜像无 gradle，不可行）⑤平台内置 gradle 发行版直调（重）。选③，Ponytail 阶梯最低可根治档。负向探针：试「更简单的检测+告警」——不能根治（噪音仍在，只是多一行告警），而③只多一个 `cp`，成本相当却根治，③不冗余。
6. **可复用逻辑 — 通过**。正向：复用既有「构建前 hygiene」模式（本文件 chmod +x、APK 清理、sdk-provision），复用 entrypoint.sh「运行时写入确定性配置」模式，复用 Dockerfile 既有 COPY 层。负向探针：查知识图谱/grep 是否已有 gradlew 模板/恢复逻辑——唯一相关是 chmod +x，无现成模板，需新增文件；复用的是模式而非重复实现。
7. **不破坏 Clawith 特性 — 通过**。正向：逐条过 C1–C6 与红线（durable run/checkpoint、多租户隔离、exactly-once、前缀缓存、WS 状态机、飞书）——改动只在每次新建的构建容器内对 workspace 的 gradlew 做幂等 cp，不碰 checkpoint/租户/工具收据/外部写。负向探针：试「方案是否碰 checkpoint 语义/前缀缓存/多租户隔离」——cp 作用于 bind-mount 的 agent 工作区，与 runtime checkpoint、LLM 前缀缓存、其他租户零交集，确认不碰。

## Phase 5：实现落地闭环（已完成）

实现三处改动 + 两条回归测试后，跑 `code-review` 双轴（Standards + Spec，并行子代理）复核 diff。

**Spec 轴（忠实实现）— 通过，无发现**：
- 三处改动与 Phase 3 一一对应、无遗漏（模板 249 行；Dockerfile `COPY docker/android-builder/gradlew.template /opt/gradle-wrapper/gradlew` 位于 `USER builduser` 之前；python L473 `cp /opt/gradle-wrapper/gradlew ./gradlew && chmod +x ./gradlew`）。
- cp 顺序正确（位于 `./gradlew --no-daemon` 之前，中间隔着 heredoc 与 APK 清理语句，无颠倒）。
- 模板三要素齐全（`#!/bin/sh` / `org.gradle.wrapper.GradleWrapperMain` / 四行 OS flag 初始化）。
- 两条回归测试均落地，锚点 `./gradlew --no-daemon` 判序，避开 cp/chmod 里的 `./gradlew` 误判。
- 无 scope creep，无夹带范围外改动。

**Standards 轴 — 硬违例零**：
- 根 AGENTS.md §2 三对齐（code/Agent Note/commit）满足；宪法 II（最小范围）、宪法 IV（测试证明行为）均符合。
- 两条判断性坏味（非硬违例，作回归契约可接受，未改）：① 与既有 `test_command_contains_apk_cleanup_before_gradlew` 同构的「command 定序」断言，出现 2 次、尚未到提取 helper 的阈值；② `cp ... && chmod +x ./gradlew` 字面量在生产与测试双处硬编码，属脆弱精确匹配断言（回归契约的常态）。

**验证现状**：目标测试类 2/2 通过；全文件 `test_android_build_backend_fixes.py` 29 passed；ruff check 无本次新增错误（5 条既有错误在 29/32/260/576/701 行）；arch-guard P0 全 clean。

**结论**：diff 忠实实现 Phase 3，闭环。
