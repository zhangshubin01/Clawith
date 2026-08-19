# 根治方案：内置工具路径契约统一与结构化路径诊断

日期：2026-08-19
范围：backend（`agent_tools.py`、`workspace_paths.py`、`builtin_tool_definitions.py` 及其测试）
状态：实现中（L1/L2/L3 已落地，L4 待排期）

## 背景与事故

`android_compile` 工具调用 `{task: assembleDebug, java_version: 17, project_path: indonesia-loan-app}`
连续 6 次返回 `gradlew not found`（error_code `android_project_not_found`），模型空转约 10 轮：
重写 gradlew、chmod、四源下载 gradle-wrapper.jar、检查 git 状态——全部发生在工具函数的
前 10 行路径预检。实际根因只是**参数少传了 `workspace/` 前缀**（正确值
`workspace/indonesia-loan-app`）。

从 `agent_run_events` 的模型推理原文可见决定性证据：

> "也许需要把项目路径改为 `workspace/indonesia-loan-app`？工具描述说
> 'Android 项目在 workspace 中的相对路径'，所以 `indonesia-loan-app` 正确。"

模型拿到了正确答案，被工具描述劝退。

## 问题定性：路径契约缺失（一类系统性问题）

平台上并存四套路径框架，且没有任何一处向模型声明统一基准：

| 框架 | 约定 | 模型看到的证据 |
|---|---|---|
| 文件工具（list_files/read_file/write_file/find_files/search_files…） | 相对 **agent 工作区根**，回显带 `workspace/` 前缀 | `read_file('workspace/indonesia-loan-app/gradlew')` 成功且回显该路径 |
| `android_compile.project_path` | 实现同样相对 agent 根，**描述却写「在 workspace 中的相对路径」** | 模型照描述理解成相对 `workspace/` 目录 → 去掉前缀 |
| 沙箱 execute_code | agent 根映射为 `/workspace`，cwd=/workspace | 沙箱内看到 `/workspace/workspace/indonesia-loan-app` |
| ACP tool_bridge `_normalize_acp_project_path` | 主动剥离 `workspace/` 前缀（IDE 项目根约定） | 第三种约定 |

失败反馈零信息量：模型只收到 `content="gradlew not found"` + opaque `error_code`。
没有解析后路径、没有基准声明、没有「目录不存在 vs 有目录没 gradlew」的区分。
每个假设都不可证伪 → 只能脑补。

## 根治方案（四层）

### L0 契约单一化（治本）
所有内置工具的 path/pattern 参数一律相对 agent 工作区根目录（`WORKSPACE_ROOT/<agent_id>`）
解析、回显，与 read_file/list_files 展示的路径同形。绝对路径拒绝（现有行为，补文档）。

### L1 描述注入（堵住误导源头）
共享常量 `PATH_CONVENTION_TEXT`，在工具 schema 渲染时注入到所有路径类参数的
description 尾部；单独修正 `android_compile.project_path` 描述。
`tool_seeder.seed_builtin_tools()` 启动时会对既有行 UPDATE `parameters_schema`/`description`
（tool_seeder.py:173-175），改 seed 源 + 重启即全局生效。

### L2 结构化路径诊断（失败自带答案）
新 helper `describe_path_failure(root, rel_path)`，输出进 `result_summary`（模型只读该字段推理）：
- 解析后的绝对路径 + 基准根声明；
- 「目录不存在」与「存在但无目标文件」分开成不同错误码/文案；
- 最深存在祖先的目录列表（≤10 条）；
- 候选建议：若 `<根>/workspace/<rel>` 存在 → 明示「你是指 `workspace/<rel>` 吗？」；
  反向（多传前缀）对称提示。
接入点：android_compile、list_files（"Directory not found"）、read_file、find_files、search_files。

### L3 android_compile 专项（自动回退 + 边界明确）
- `<根>/<p>` 无 gradlew 且 `<根>/workspace/<p>` 有 → **自动采用**，结果注明实际解析路径。
  只在前缀缺失且候选命中时触发，不掩盖真拼写错误。
- 「目录存在但没 gradlew」独立文案，附带目录顶层条目；
- wrapper 完整性提示：`gradle/wrapper/gradle-wrapper.jar` 缺失时在失败输出中提醒补齐。
- 写工具**不做**自动回退（防歧义写入），只给 L2 建议。

### L4 回归防护（后置）
- Benchmark Case：「模型拿到不带前缀的 project_path 也能一次编译成功」（agent-evaluation 流程固化）。
- 单测已随 L2/L3 落地；收尾跑 `scripts/arch-guard.sh` + pytest + ruff。

## 实施顺序

1. ✅ L2：`describe_path_failure` helper + 单测（`tests/test_workspace_paths_diagnostics.py`，5 用例）
2. ✅ L1：描述注入 + android_compile 描述修正（`_PATH_CONVENTION_PARAMS` + `_inject_path_convention`）
3. ✅ L3：android_compile 回退与错误区分 + 测试（5 个新用例 + 1 个更新）
4. ✅ 验证：相关测试 138 通过、ruff 干净、`scripts/arch-guard.sh` 通过（仅存量前端行数告警）
5. ⏳ 部署：重启后端后 `seed_builtin_tools()` 自动把新描述同步进 `tools` 表；用一个真实 agent 会话复现验证
6. ⏳ L4：Benchmark Case（agent-evaluation 流程固化「无前缀路径一次编译成功」）

## 取舍记录

- 自动回退只对 android_compile 做（编译幂等、单次成本高）；写工具仅给建议。
- ACP 桥接的 `workspace/` 剥离逻辑不动（IDE 专用约定），只加注释防误改。
- 回退探测仅当 `<根>/workspace/<p>` 确实含 gradlew 时生效，避免掩盖真实路径错误。
