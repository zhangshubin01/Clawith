# android_compile「编译失败但零错误输出」—— 容器 stderr 丢失分析

> 2026-08-21。现象：run `bd534c6a`（Android 工程师05 重构 credito-mx）连续多次
> `android_compile` 失败，但 `Parsed: 0 errors, 0 warnings`，summary 的「原始编译输出」
> 里没有任何 Kotlin `e:` 行与 FAILURE 段——模型陷入「换任务名/加参数」的盲修循环
> （clean assembleDebug → assembleDebug --stacktrace → compileDebugKotlin --quiet），
> 最后整个 run 失败。用户报告「输出被截断（2966 字符），没有 Kotlin 错误详情」。

## 1. 表面假象与第一轮排查

- 「原始编译输出 (2966 字符)」并非截断：`_format_android_build_failure` 的
  head/tail 阈值为 1200+10800，2966 ≤ 阈值是全量显示。截断不是问题。
- 收集层（`android_build_backend._drain_queue`）`container.logs(stream=True,
  follow=True, stdout=True, stderr=True)` 参数完整，且 result_metadata 显示
  `summary_truncated=false`、`archive_status=inline`。
- 关键事实：**Gradle 把 Kotlin 错误与 FAILURE 段全部写到 stderr**（stdout 只有
  任务行），而收集到的 output 恰好只有 stdout 内容。

## 2. 受控探针复现（backend 容器内 docker SDK，与生产同参）

| 实验 | 结果 |
|---|---|
| 容器内文件重定向 `> gout.log 2> gerr.log` | **stderr 文件里有全部 4 条 `e:` 行 + FAILURE + What went wrong** |
| python SDK `logs(stdout=True, stderr=True)` 非流式全量 | 只有 stdout（3185 字节，零错误行） |
| docker CLI `docker logs` | 同样只有 stdout |
| 前台 attach（docker run 2>&1）走 entrypoint | 同样只有 stdout |
| `--entrypoint sh` 绕过 entrypoint，`echo ERR >&2` | stderr 正常到达（attach 与 logs 都行） |
| 走 entrypoint + chain 内 `exec 2>&1` | ERR1 经 stdout 到达 ✓ |
| 走 entrypoint + 命令级 `echo ERR >&2 2>&1` | 仍丢（`>&2` 先求值，输出仍进 stderr 管道） |

结论：**entrypoint（bash 作为 PID1，`"$@" &` 后台执行构建命令）组合下，容器的
stderr 管道写入在此环境（OrbStack）整体丢失**；`docker CLI logs` / python SDK /
前台 attach 三通道都收不到。绕过 entrypoint 时 stderr 正常，但 entrypoint 源码
与镜像内版本均无任何 exec/2> 重定向（已逐段二分：后台 job、sdk-provision source、
卷挂载、select_java 均单独复现不出丢 stderr）。机制未完全实锤（疑 OrbStack 对
PID1+后台 job 的 stderr copy 缺陷），但不影响修复方向：**让 stderr 内容走 stdout
管道**。

## 3. 修复

`android_build_backend.py` 容器 command 的 gradle 行尾加 `2>&1`（一行）：

```python
f"./gradlew --no-daemon --console=plain -I /tmp/gradle-progress.gradle {shlex.quote(str(gradle_task))} 2>&1 ",
```

- 重定向作用于 gradle 进程的 fd2 → fd1（容器 stdout 管道），不改变 chain 中其它
  命令（sdkmanager 授权、local.properties）的 stderr 语义。
- entrypoint 的 SDK 自愈重试段执行的是同一个 `"$@"`，重试构建同样受益。
- 同步修复实时流：`on_output` 消费的 docker logs 流从此能收到错误行。

## 4. 端到端验证（与生产同参数探针）

- 修复前：logs 收集 3185 字节、0 错误行。
- 修复后：收集到 `e: file:///workspace/app/src/main/java/com/credito/mx/MainActivity.kt:7:37
  Unresolved reference 'AppNavHost'` 等 4 条错误 + FAILURE 段，`_parse_android_build_errors`
  可正常结构化（`Parsed: 4 errors`）。

## 5. 影响面与遗留

- 影响面：所有经 android-builder 容器的构建（成功构建无 stderr 输出，不受影响；
  历史失败构建的 FAILURE 段一直丢失，模型此前一直在盲修）。
- 遗留：OrbStack stderr 丢失的精确机制未实锤（探针已定位到 entrypoint 组合条件，
  未定位到 OrbStack 内部原因）；若未来直接读容器 stderr 的其它路径（如 attach 调试）
  仍会丢，需要注意。
