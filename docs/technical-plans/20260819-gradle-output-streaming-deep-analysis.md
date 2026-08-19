# Gradle 构建输出「真·实时流」深度分析

> 日期：2026-08-19
> 对象：`backend/app/services/sandbox/local/android_build_backend.py` 的 Gradle 输出流
> 依据：Gradle 官方文档（`gradle_daemon.html`、`logging.html`，v9.7）+ 前序实验证据链 + TeamCity Gradle 集成最佳实践 + 本次探针实测（gradle 8.10.2 源码核对）

## 0. 验证结论（2026-08-19 晚探针实测，已证伪）

**核心假设「让客户端 JVM 参数与 `org.gradle.jvmargs` 匹配 → 走 No Daemon 内联 → 任务行逐行实时流」已证伪。**

最小 Gradle 探针（`probe` task 每 500ms `println` 一行，共 6 行）+ `script` PTY + 逐行时间戳，5 组对照全部**缓冲**（`TASK_LINE_1..4` 在 2–8ms 内一起到达，非 500ms 间隔）：

| 对照 | 配置 | 结果 |
|---|---|---|
| A | `--no-daemon --console=plain`（当前生产配置） | 打印 "single-use Daemon will be forked"，缓冲 |
| B | `--no-daemon` + `GRADLE_OPTS` 加 `-Xmx512m`（让客户端堆=512m） | **仍** "single-use Daemon will be forked"，缓冲 |
| C | 直启客户端 JVM `-Xmx512m -Xms512m`（无 `-Xmx64m`）+ jvmargs 匹配 | **仍** fork single-use daemon，缓冲 |
| D | **持久 daemon**（去 `--no-daemon`）+ `--console=plain` | 缓冲（证明与 single-use 无关） |
| E | 持久 daemon + rich console（去 `--console=plain`） | 缓冲 |

**健全性对照**：同一条 `script`+pipe 采集链路，`echo` 每 1s 一行 → 精确 1s 间隔实时到达 → 采集链路无辜，**缓冲 100% 在 Gradle 内部**。

**源码定因（gradle 8.10.2 `BuildActionsFactory.canUseCurrentProcess`）**：内联（in-process）需同时满足 `!isLowMemoryProcess()` + `DaemonCompatibilitySpec` + 客户端不可变 JVM 参数与 `getEffectiveSingleUseJvmArgs()` **逐字节相等**——官方文档称其为 "the rare case"，`--no-daemon` 下实测无法靠「匹配 jvmargs」触发。**故方案 A（内联实时流）不具可实施性。**

**结论**：Gradle 的 daemon（持久或 single-use）把任务输出经 socket 转发**周期性批量送达**（~100–500ms 粒度），不提供「逐行实时」；「无进度」观感的根治只能是**心跳/服务消息**，而非把 Gradle 自身输出变实时。

## 1. 结论速览（TL;DR）

- 我们的 android 构建输出「一次性突发、静默期无进度」的根因是 **Gradle 走了 single-use daemon（一次性守护进程）**：构建跑在 fork 出来的另一个 JVM 里，输出经 **local socket 连接转发**，转发链路把任务行攒到构建结束才送达客户端。
- 官方文档给了精确判定：`--no-daemon` 时，若 **客户端 JVM 的 JVM 参数与 `org.gradle.jvmargs` 匹配**，构建就在**客户端 JVM 内联执行**（「No Daemon」模式），输出是客户端自己的 `System.out`；**不匹配**则 fork 一次性 daemon（「single-use Daemon」模式），输出走 socket 转发 → 缓冲到结束。**但此「内联实时流」路径已被本次探针证伪（见 §0）：即便匹配 jvmargs 也仍 fork daemon，内联是官方所称 "the rare case"，不可靠触发。**
- 我们当前配置恰好落进 single-use daemon：gradlew 脚本给客户端 JVM 的是 `-Xmx64m`（`DEFAULT_JVM_OPTS`），而 `GRADLE_OPTS` 里的 `-Dorg.gradle.jvmargs=-Xmx4096m` 只是把「daemon 需要 4096m 堆」写成系统属性，**并不会把客户端 JVM 的堆调上去** → 客户端 64m ≠ 要求 4096m → 必然 fork 单次 daemon。
- **PTY 单独不够**：PTY 只让「客户端 JVM 自己的 `System.out`」行缓冲（前序探针已证 `System.console()` 非 null、逐行实时到达），但对 daemon 的 socket 转发无效——缓冲在 Gradle 自身的转发链路，不在 Java stdout。
- **最终结论见 §0**：缓冲在 Gradle daemon 内部（持久/single-use 皆然），根治只能是心跳（方案 C）或文件侧信道（方案 B），而非把 Gradle 自身输出变实时。

## 2. 问题定义：两代问题要分清

| | 问题① 输出丢失（已修复） | 问题② 非实时（本文对象） |
|---|---|---|
| 现象 | 每次编译只产出 1 个输出事件、无 Gradle 任务行 | 任务行在构建结束 ~44ms 内一次性吐完，长构建静默期看起来像卡死 |
| 根因 | 平台两处 flush 缺口（`_drain_queue` 只在新 chunk 到达时检查窗口 + `tool_step_service` 无最终 flush） | single-use daemon 的 socket 转发把输出缓冲到结束 |
| 状态 | ✅ `ea40eb43` + `6413a846` 已部署并 P0 真实编译验证 | 本文分析 + 待定修复方案 |

问题① 修完后「内容必达」已满足；问题② 是「到达的**时机**是否实时」，属于观感/体验层。

## 3. 官方文档依据：Gradle 双 JVM 架构

Gradle 官方 `gradle_daemon.html`（v9.7）明确区分两个进程：

| 进程 | 职责 | JVM 来源 |
|---|---|---|
| **Gradle Client JVM** | 启动即存在、贯穿整个构建调用；连接（或启动）daemon、发送构建请求、**把输出流回传给控制台** | 启动 wrapper 脚本的 JVM（`JAVA_HOME` / PATH / IDE） |
| **Gradle Daemon JVM** | 长驻进程，真正执行构建、跨构建缓存状态 | `org.gradle.java.home` / Tooling API / Daemon JVM toolchains |

关键原文：

> "Communication between the client and the Daemon happens via a **local socket connection**."
> （客户端与 Daemon 之间通过本地 socket 连接通信）

这解释了「缓冲到结束」的机制：构建输出由 Daemon 通过 socket 发给客户端，客户端再渲染到控制台。转发是批量的、非逐行的——它不是一条 `println` 就发一包。

## 4. 三种 Daemon 模式的精确语义（核心）

官方「Disable Daemon」一节给出两条互斥判定：

> **Single-use Daemon**：If the JVM args of the client process **don't match** what the build requires, a single-use Daemon (disposable JVM) is created. … it is created, used, and then stopped at the end of the build.
> **No Daemon**：If the **JAVA_OPTS and GRADLE_OPTS match org.gradle.jvmargs**, the Daemon will not be used at all since **the build happens in the client JVM**.

以及同节的醒目提示：

> "Don't forget to make sure your JVM arguments and GRADLE_OPTS / JAVA_OPTS **match** if you want to completely disable the Daemon and not simply invoke a single-use one."

外加兼容性章节的补充：

> "In the rare case where the daemon has been disabled (--no-daemon or -Dorg.gradle.daemon=false) **and the Client process is compatible**, the Gradle Daemon uses the JVM that launched the Gradle Client."

| 模式 | 触发条件 | 构建跑在哪 | 输出路径 | 是否实时 |
|---|---|---|---|---|
| Daemon（默认） | 未禁用 | 长驻 daemon JVM | socket 转发 | 否（转发批量） |
| **Single-use Daemon** | 禁用 daemon 但客户端参数 ≠ `org.gradle.jvmargs` | fork 的一次性 JVM | socket 转发 | **否**（缓冲到结束） |
| **No Daemon（内联）** | 禁用 daemon 且客户端参数 **==** `org.gradle.jvmargs` | **客户端 JVM 自身** | 客户端自己的 `System.out` | **是**（PTY 行缓冲） |

## 5. 我们当前配置为什么落到 single-use daemon

实锤证据链（来自现有代码与真实项目）：

1. `android_build_backend.py` 的命令行：
   ```bash
   ./gradlew --no-daemon --console=plain assembleDebug
   ```
   env 里 `GRADLE_OPTS` 含：
   ```
   -Dorg.gradle.daemon=false -Dorg.gradle.jvmargs=-Xmx4096m -XX:MaxMetaspaceSize=768m ...
   ```

2. 各项目 `gradlew` 脚本（mg2 / notepad / CalculatorApp 一致）：
   ```bash
   DEFAULT_JVM_OPTS='"-Xmx64m" "-Xms64m"'
   eval set -- $DEFAULT_JVM_OPTS $JAVA_OPTS $GRADLE_OPTS "-Dorg.gradle.appname=$APP_BASE_NAME" ... GradleWrapperMain
   ```

3. 项目 `gradle.properties` 的 `org.gradle.jvmargs`：
   - mg2：`-Xmx4096m -Dfile.encoding=UTF-8`
   - android-notepad / CalculatorApp：`-Xmx2048m -Dfile.encoding=UTF-8`

**结论链条**：
- 客户端 JVM（GradleWrapperMain）实际以 `-Xmx64m -Xms64m` 启动（来自 `DEFAULT_JVM_OPTS`）。
- `GRADLE_OPTS` 里的 `-Dorg.gradle.jvmargs=-Xmx4096m` 是**系统属性**，它设定的是「daemon 要求 4096m 堆」，**不改变客户端 JVM 的堆**。
- 客户端实际堆（64m）≠ 构建要求堆（4096m）→ Gradle 判定客户端不兼容 → fork single-use daemon（构建日志里那句 "single-use daemon process will be forked"）。
- 于是构建跑在一次性 JVM，输出经 socket 转发 → 攒到构建结束才吐。

一句话：**我们「禁用了 daemon」，却因为 jvmargs 不匹配而退化成 single-use daemon，正是官方文档警告的那个「not simply invoke a single-use one」的坑。**

## 6. 为什么 PTY 单独不够（证伪链）

前序实验已建立的事实：

- 五组探针（pipe / script PTY / PTY+stdbuf -oL / daemon 模式+PTY / 容器内逐行时间戳）全部显示：Gradle 任务行在构建结束 ~44ms 内一次性吐完。
- Java 控制台探针：`script` PTY 下 `System.console()` 非 null，`line-0/1/2` 以 700ms 间隔**实时逐行到达** → PTY 确实让 **Java 客户端 JVM** 行缓冲实时流。
- 二者结合 ⇒ 缓冲**不在 Java stdout 层**，而在 **Gradle 的 daemon socket 转发层**。

所以 PTY 解决的是「Java stdout 块缓冲」，但本次探针进一步证明：**即便给客户端 JVM 配了 PTY，Gradle daemon 的任务输出仍在 socket 转发层被周期性批量缓冲**（健全性对照已排除采集链路因素）。又因「No Daemon 内联」不可靠触发（§0），PTY 在本场景下**整体无济于事**。

## 7. 修复选项与收益/风险

### 方案 A：No Daemon 内联（真·实时流）——**已证伪，放弃**

~~让客户端 JVM 参数与 `org.gradle.jvmargs` 匹配，构建跑在客户端 JVM，再用 PTY 让 `System.out` 行缓冲。~~

探针实测（对照 B/C）证明：即便客户端 JVM 堆与 `org.gradle.jvmargs` 精确匹配、且直接以 `-Xmx512m` 启动客户端，Gradle 8.10.2 仍 fork single-use daemon。源码 `BuildActionsFactory.canUseCurrentProcess` 要求客户端不可变 JVM 参数与 `getEffectiveSingleUseJvmArgs()` **逐字节相等**（含 Gradle 自动追加的 `-Dfile.encoding`/`-Duser.*` 等），官方文档称其为 "the rare case"。**在容器化、非交互、`--no-daemon` 的生产形态下不可靠触发，方案作废。**（曾设想的 PTY 包装 + `\r\n` 归一也一并作废——缓冲不在客户端 stdout，PTY 解决不了。）

### 方案 B：init script 服务消息 + **文件侧信道**（绕过 daemon 缓冲）

保持 `--no-daemon` 不变，用 **init script**（`-I` 注入）在构建内挂 `TaskExecutionListener`/`BuildListener`，在每个 task 边界写结构化进度。**关键教训（本次探针）**：服务消息若 `println` 到 stdout，会与 Gradle 自身任务行（"> Task :probe"）走同一条 daemon socket 转发、同样被缓冲。**必须写到一个文件侧信道**（如 `/workspace/.gradle-progress`，项目已 bind-mount，宿主/后端可实时 tail），才能绕过 socket 缓冲拿到任务级实时进度。

依据：TeamCity / JetBrains 官方对 Gradle 的集成即通过注入 init script 上报任务进度；区别在于它走的是 CI 服务器能拉取的日志通道，而非依赖 daemon 的 stdout 转发实时性。

收益：任务级进度（「正在编译 :app:compileDebugKotlin」）实时可达，且**不碰 JVM 参数/PTY**。
风险/代价：需在每个 agent 项目注入 init script + 后端新增文件 tail 机制（当前只有 `container.logs` 流，无文件 tail）；协议层比方案 C 重。

### 方案 C：静默期心跳（观感修复，最低风险，可叠加）

用户痛点本质是「没进度」的观感。可在 drain 静默超时（如 >5s 无新帧）时发一条心跳事件「构建进行中… 已 Ns」。不碰 Gradle 内部，与 A/B 正交、可叠加。

## 8. 推荐路径（方案 A 已证伪后修订）

**推荐：方案 C（静默期心跳）为主，方案 B（init script + 文件侧信道）为「要真任务级进度」的升级项。**

- **方案 C** 零风险、当天可上：在 drain 静默超时（如 >5s 无新帧）发「构建进行中… 已 Ns」心跳，直接解决「看起来卡死」的观感，且与 Gradle 内部无关。
- **方案 B** 才是「真任务进度」的唯一可靠路径（Gradle 自身输出经 daemon socket 转发不可实时），但需新增文件 tail 机制，成本更高，作为 C 之后的增强。

**原「待实证项」已闭环**：方案 A 的内联实时流假设已被探针**证伪**（见 §0），无需再在真实构建上验证。附录保留最小复现配方作为本次证伪过程的原始记录：

```bash
# 最小 Gradle 项目：一个带 sleep 的 probe task
mkdir -p /tmp/gradle-stream-probe && cd /tmp/gradle-stream-probe
cat > settings.gradle <<'EOF'
rootProject.name = 'probe'
EOF
cat > build.gradle <<'EOF'
tasks.register('probe') {
    doLast {
        for (int i = 1; i <= 6; i++) {
            println("TASK_LINE_" + i)
            sleep(500)
        }
    }
}
EOF
cat > gradle.properties <<'EOF'
org.gradle.jvmargs=-Xmx512m
EOF

# 5 组对照（A~E）均缓冲；健全性对照（echo 每 1s）实时 → 缓冲在 Gradle 内部。
# 采集用 script -qefc "... /dev/null" + while-read 逐行 date 时间戳（见探针脚本 probe-inner.sh / probe-args.sh）。
```

## 9. 参考

- Gradle 官方：`docs.gradle.org/current/userguide/gradle_daemon.html`（Client vs Daemon、single-use / No Daemon 判定、socket 通信、兼容性）
- Gradle 官方：`docs.gradle.org/current/userguide/logging.html`（日志级别、`--console` 模式、`useLogger`/init script 自定义日志 UI）
- Gradle 8.10.2 源码（本次核对）：`BuildActionsFactory.canUseCurrentProcess`（in-process 判定：`!isLowMemoryProcess()` + `DaemonCompatibilitySpec` + 不可变 JVM 参数逐字节相等）、`SingleUseDaemonClient`（"single-use Daemon process will be forked" 消息）、`DaemonClientFactory`
- JetBrains TeamCity 官方：`jetbrains.com/help/teamcity/gradle.html`（Gradle build runner 集成，init script 注入任务进度服务消息的行业范式）
- 前序实验证据链：PTY 探针（`System.console()` 非 null + 逐行实时）、五组缓冲探针（任务行构建结束 44ms 内一次性吐完）
