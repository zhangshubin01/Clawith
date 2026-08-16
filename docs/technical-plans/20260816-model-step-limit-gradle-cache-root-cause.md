# mg2 model_step_limit_reached 根因分析与处置

- **日期**：2026-08-16
- **分支**：`f-shubin-0806`
- **状态**：已完成清理与构建成功验证；产品侧建议待决策

## 0. 总结论

Run d232bb3a（agent「Android 工程师02」`08a739c1`，mg2 编译任务，09:52:29 创建 → 10:02:48 失败）报 `model_step_limit_reached` 只是**症状**——模型烧光了 agent 的 50 轮上限。真正的根因是：**模型在上一 Run 通过 execute_code 修改了项目内 gradlew/wrapper（无回滚机制），污染了跨 Run 持久的工作区**，导致本 Run 3 次 android_compile 全部失败，模型随后用 40+ 次 execute_code 诊断命令烧光轮数。

## 1. 报错机制（症状）

`app/services/agent_runtime/node_executor.py:579-597`：`_model` 节点 step_count 超过 `context.model_turn_limit`（= `agent.max_tool_rounds`，该 agent=50）→ status failed + `model_step_limit_reached`。

## 2. 因果链（全部有 DB/日志证据）

1. **模型污染构建入口**（决定性证据）：
   - Run 0a8c73d6（08:27-09:02，上一 Run）命令输出显示 `head -3 gradlew` = `#!/usr/bin/env sh` + `export GRADLE_USER_HOME="/workspace/.gradle-home"`——模型为绕缓存问题硬编码改写 gradlew（更早还用过 `GRADLE_USER_HOME=/tmp/gh-42`）；wrapper 被改成腾讯镜像 8.10.2；.gitignore 加了 `.gradle-home/`。全部未提交、跨 Run 持久。
   - builder 直接执行项目内 `./gradlew`（backend 代码 L350），被改的 gradlew 生效 → 产生 `.gradle-home`（daemon 日志 `daemonRegistryDir=/workspace/.gradle-home/daemon`、`javaHome=/opt/jdks/jdk-17` 证明是 builder 容器内进程）。
2. **三次构建失败**：
   - 构建 1/2：项目本地 `.gradle/8.10.2/dependencies-accessors/77e7e2bd…/metadata.bin` 不可读（dependencies-accessors 是**项目本地** `.gradle/<版本>/`，与 GRADLE_USER_HOME 无关）；
   - 构建 3：模型刚 `rm -rf` 掉 `.gradle-home/caches/8.10.2/kotlin-dsl`。
   - 模型 09:59 `git checkout -- .` 自行还原 gradlew/wrapper（现仅 mode 位变化）。
3. **更早史**：mg2 自 **2026-07-27 09:47 起从未再构建成功**——最初失败是项目脚本 bug（`lateinit property reportFile has not been initialized`）；随后（同日 09:52 起）出现 metadata.bin 不可读；08-16 的 `/tmp/gh-42`（tmpfs 1GB）被撑爆 `No space left on device`。模型 Jul-27 造了 clean-cache.gradle init 脚本试图绕过（无效）。`.gradle-home` hack 模式在 CalculatorApp 等其它项目也出现过（Aug-11 起）。
4. **平台侧放大因素**：
   - builder 直接执行项目内 gradlew，模型可无回滚地修改构建入口；
   - execute_code（bwrap 沙箱，HOME=/workspace，无 GRADLE_USER_HOME，不挂 /opt/jdks）与 builder 共享同一份项目目录；
   - AAPT2/Gradle 错误未被结构化解析，模型乱试；
   - max_tool_rounds=50 对编译类任务偏紧。

## 3. 已执行的处置与验证（真实结果）

1. **清理 mg2 工作区**：`.gradle-home`、`.gradle`、`gradle-8.7-bin.zip`、`gradle-8.10.2-bin.zip`、`clean-cache.gradle` 全部 mv 到可回滚垃圾目录 `/data/agents/08a739c1…/trash-20260816/`；`git checkout -- gradlew` + chown/chmod 修正（root exec 会制造 root 属主文件，backend 主进程与 builder builduser 均为 uid 1000；gradlew 已恢复 1000:1000/644，git status 干净）。
2. **以平台同等参数真实触发构建**（macOS 宿主 docker CLI，bind 源路径 `/var/lib/docker/volumes/clawith-agent_agentdata/_data/<agent_id>/workspace/mg2:/workspace`；卷 global_jdk_cache/global_android_sdk/gradle_cache_global；env 含 GRADLE_USER_HOME=/home/builduser/.gradle、GRADLE_OPTS 全量；tmpfs 7 处；--user builduser --read-only；命令 `yes | sdkmanager --licenses …; echo sdk.dir… > local.properties && chmod +x ./gradlew && ./gradlew --no-daemon --console=plain assembleDebug`）→ **BUILD SUCCESSFUL in 2m 54s**，APK `app/build/outputs/apk/debug/ColDinero_1.0.0_202608161023_debug.apk`（12.4MB）产出。
3. **构建后验证**：无断链（entrypoint F1 trap 生效）；同 hash 的 `metadata.bin` 健康再生（99 字节）→ 缓存损坏不是活体机制，是残留污染 + 模型操作。

## 4. 产品侧建议（待决策）

- **(a)** 编译类 agent 的 `max_tool_rounds=50` 是否调高；
- **(b)** builder 执行前检测 gradlew/wrapper 是否处于非 git 干净状态，若是则告警或自动重置（防模型无回滚地污染构建入口）；
- **(c)** AAPT2/Gradle 错误结构化解析，减少模型瞎试。

## 5. 排查经验（沉淀）

- android_compile 的 builder 直接执行项目内 `./gradlew`；模型通过 execute_code 修改 gradlew/wrapper 会**无回滚地污染下一次构建**——排查「构建诡异失败」先查 `git status` 项目文件被模型改了什么，而非只清缓存。
- `dependencies-accessors/metadata.bin` 在**项目本地** `.gradle/<版本>/`，与 GRADLE_USER_HOME 无关；`kotlin-dsl/scripts` 在 user home 下。
- daemon 日志里的 `daemonRegistryDir` 与 `javaHome=/opt/jdks/jdk-17` 可判定 gradle 进程跑在哪个环境（builder 容器 vs execute_code 沙箱）。
- `docker exec` 默认 root，会制造 root 属主文件导致 builder（uid 1000）chmod 失败——修复后用 `chown 1000:1000` 还原。
- macOS 宿主 docker CLI 可直通 VM 卷路径 `/var/lib/docker/volumes/...` 做 bind mount（已验证）。
