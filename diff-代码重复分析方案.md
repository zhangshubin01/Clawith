# inlinediff vs ineditordiff 代码重复分析方案

> 分析日期：2026-05-14 | 对应问题 #12

## 一、现状

两个包共 36 个文件：

| 包 | 语言 | 文件数 | 在用 |
|----|------|--------|------|
| `inlinediff/` | Kotlin (8) + Java (1) | 9 | **0 外部调用** |
| `ineditordiff/` | Java (19) + Kotlin (5) | 27 | 6 个外部调用方 |

## 二、调用链梳理

### inlinediff/ — 完全无外部调用

```
grep 结果：src/main/java/ 下没有任何文件 import inlinediff 包
```

`inlinediff/` 的 `InlineDiffRender.kt` 在包内调用 Kotlin 版 `PrefixDiffingAlgorithm`，
但 `InlineDiffRender` 自身从未被外部实例化或调用。这是一个**完全孤立、未接入任何功能的代码岛**。

### ineditordiff/ — 生产代码路径

```
InlineChatPanel.java          ──→ InEditorDiffRenderer.create()  ──→ JGitDiffAlgorithmAdapter.compute()
CodeMarkdownHighlightComponent ──→ InEditorDiffRenderer.create()       ├── LinesSequence
InEditorDiffRenderBaseAction   ──→ InEditorDiffRenderer (get)    │     ├── ISequence
AcceptSingleInlineChatChangesAction                                  │     ├── DiffAlgorithmResult
RejectSingleInlineChatChangesAction                                 │     ├── SequenceDiff
                                                                    │     └── OffsetRange
                                                                    │
                                                                    └── InlineDiffSingleChangeComponent
                                                                         ├── InlineDiffActionPanel
                                                                         ├── ComponentInlaysContainer
                                                                         └── InlineDiffsActionTab
```

**实际 diff 算法路径**：`InEditorDiffRenderer.comparePrefixDiff()` → `JGitDiffAlgorithmAdapter.compute()` → JGit `HistogramDiff`

`ineditordiff/` 内也存在一个**未被生产代码使用的算法实现**：
- `PrefixDiffingAlgorithm.java`（176 行，自定义 DP 算法）— 仅被 `PrefixDiffingAlgorithMain.java`（测试 main）调用
- 连带未使用的配套类：`Array2D.java`、`ITimeout.java`、`InfiniteTimeout.java`

## 三、两个 PrefixDiffingAlgorithm 差异

| 维度 | Java 版 (ineditordiff) | Kotlin 版 (inlinediff) |
|------|----------------------|------------------------|
| 行数 | 176 | 114 |
| 空行过滤 | `ignoreEmptyLines` 配置 | 不支持 |
| 回溯起点 | 固定 `(2*N, M)` | 搜索最小成本行 `bestRow` |
| 回溯边界 | 处理 `row≤0` 和 `col≤0` 边界 | 仅 `row≥0 && col≥0` |
| 元素比较 | `elementsEqual()` 支持空行比较 | 直接 `==` |
| Diff 过滤 | `addDiffIfNotEmpty()` 过滤纯空行差异 | 无过滤 |
| 生产调用 | 无（死代码） | 无（死代码） |

两个版本的核心 DP 思路相同（`2n+1 × m+1` 矩阵、偶数行插入/奇数行匹配、成本模型），但回溯逻辑有显著差异。**两者均未被生产代码使用**，实际使用的是 `JGitDiffAlgorithmAdapter`（包装 JGit HistogramDiff）。

## 四、死代码清单

### 可安全删除：整个 inlinediff/ 包（9 文件，~337 行）

| 文件 | 行数 | 说明 |
|------|------|------|
| `inlinediff/InlineDiffRender.kt` | 139 | 无人调用的渲染器 |
| `inlinediff/InlineDiffMenu.java` | 23 | 无人使用的 Swing 面板 |
| `inlinediff/PrefixDiffingAlgorithm.kt` | 114 | Kotlin 版 DP 算法 |
| `inlinediff/DiffAlgorithmResult.kt` | 16 | 结果数据类 |
| `inlinediff/LinesSequence.kt` | 11 | 序列实现 |
| `inlinediff/Array2D.kt` | 14 | 二维数组 |
| `inlinediff/ISequence.kt` | 6 | 序列接口 |
| `inlinediff/ITimeout.kt` | 5 | 超时接口 |
| `inlinediff/InfiniteTimeout.kt` | 9 | 永不超时实现 |

### 可安全删除：ineditordiff/ 中的死代码（5 文件，~236 行）

| 文件 | 行数 | 说明 |
|------|------|------|
| `ineditordiff/PrefixDiffingAlgorithm.java` | 176 | 自定义 DP 算法，无人调用 |
| `ineditordiff/Array2D.java` | 17 | 仅被上述算法使用 |
| `ineditordiff/PrefixDiffingAlgorithMain.java` | 27 | 独立测试 main 方法 |
| `ineditordiff/ITimeout.java` | 5 | 仅被上述算法使用 |
| `ineditordiff/InfiniteTimeout.java` | 10 | 仅被测试 main 使用 |

### 保留（生产代码实际依赖）

| 文件 | 被谁使用 |
|------|----------|
| `JGitDiffAlgorithmAdapter.java` | `InEditorDiffRenderer.comparePrefixDiff()` |
| `ISequence.java` | `JGitDiffAlgorithmAdapter` + `InEditorDiffRenderer` |
| `LinesSequence.java` | `InEditorDiffRenderer.comparePrefixDiff()` |
| `DiffAlgorithmResult.java` | `JGitDiffAlgorithmAdapter` + `InEditorDiffRenderer` |
| `SequenceDiff.java` | `JGitDiffAlgorithmAdapter` + `DiffAlgorithmResult` |
| `OffsetRange.java` | `SequenceDiff` |
| 其余 16 个渲染/UI 文件 | 内联编辑渲染链 |

## 五、建议方案

### 推荐：直接删除死代码（零风险，立即生效）

```
删除 inlinediff/              9 文件，~337 行
删除 ineditordiff/ 中死代码    5 文件，~236 行
─────────────────────────────────────────
合计                          14 文件，~573 行
```

**理由**：
1. `inlinediff/` 9 个文件无任何外部 import，为孤立死代码
2. `ineditordiff/` 中的 `PrefixDiffingAlgorithm` 等 5 个文件不被生产代码调用
3. 生产路径唯一使用的 diff 算法是 `JGitDiffAlgorithmAdapter`（包装 JGit HistogramDiff）
4. 不存在"两个包之间共享算法"的情况 — 是没有调用关系的两份孤立副本

### 不做：合并两个 PrefixDiffingAlgorithm

两个自定义 DP 算法的回溯逻辑不同（Kotlin 版找最小成本行、Java 版固定起点），且**两者均未被生产代码使用**。合并无实际收益，反而增加维护负担。

### 不做：统一用 Kotlin 重写 ineditordiff/

`ineditordiff/` 的渲染层（InEditorDiffRenderer 570 行、InlineDiffSingleChangeComponent 516 行）依赖 IntelliJ 编辑器 API，逻辑复杂且有大量 EDT 线程交互。盲目重写风险高、收益低。可在日常迭代中逐步迁移新增文件到 Kotlin。

## 六、风险与验证

| 步骤 | 操作 |
|------|------|
| 1 | 删除 14 个文件 |
| 2 | `./gradlew compileJava compileKotlin` 确认编译通过 |
| 3 | `./gradlew build` 确认完整构建通过 |
| 4 | 可选：IDE 沙箱运行，触发内联编辑功能验证 diff 渲染正常 |

**风险等级：极低**。删除的文件均无外部调用方，编译即验证。
