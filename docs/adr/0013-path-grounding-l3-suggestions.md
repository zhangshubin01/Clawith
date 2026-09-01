# ADR-0013: L3 路径接地——read_file 路径幻觉的模糊定位建议

- **状态**: 已接受（2026-09-01，grill-with-docs 决策；code-review 双轴评审后修订 v2）
- 关联：`docs/technical-plans/20260901-file-path-grounding.md`（实施细节）
- 背景：L1/L2 契约出自 `docs/technical-plans/20260819-builtin-tool-path-contract-plan.md`

## 背景

run 6a1c0eab 中模型凭 Java 包名约定猜路径 `com/example/mydome1/`（实际 `com/example/calculator/`），连续 4 次 `workspace_file_not_found`。现有 L2 诊断的「Did you mean」只处理 workspace/ 前缀增删，对中间段猜错不生效。模型最终靠 L2 的「Entries under it」4 秒自纠——证明错误侧信息有效，缺的是把「自纠线索」升级为「工具直接给答案」。

## 决策

1. **三级路径接地契约**：
   - L1：路径契约注入（`PATH_CONVENTION_TEXT`，经 `_PATH_CONVENTION_PARAMS` 注入所列 path/pattern 参数）；
   - L2：失败诊断（`describe_path_failure`：解析路径/最深祖先/条目/前缀型 Did you mean）；
   - **L3（新增）**：存储侧有界 basename 定位——失败时在 StorageBackend 中搜索同名文件/目录，回「Did you mean (verified in workspace storage)」。
2. **真相源 = StorageBackend**：本地 FS 只保留现状渲染；建议必须来自工具实际读取的存储层（Local/S3/Fallback 三后端一致正确）；企业路径（is_enterprise）限定在 enterprise 根内同界搜索，不跨根。
3. **只建议、不自动重读**：工具不做内容代偿；模型决策保留（实证：给线索即可 4 秒自纠，代价仅一个工具轮次）。
4. **匹配保守**：basename 大小写敏感精确匹配，深度 ≤6、节点 ≤150、建议 ≤3、最近优先；零命中不输出。difflib 模糊留待数据判据。
5. **范围**：只覆盖读侧 5 处调用点（read_file/list_files/find_files/search/android_compile project_path）；edit/write 不加（误写歧义风险不对称）；glob pattern base 两处排除 storage 建议（改 `include_storage=False`，仍随 `_path_failure_details` async 化一并 `await`）。
6. **search-first 注入**：L1 文本追加一句「未验证路径先用 list_files/find_files 发现，禁止按约定猜路径」（gemini-cli 同款，snippets.ts:720）。
7. **升级判据（数据驱动）**：A+B 上线后以 Langfuse 周指标裁决——`workspace_file_not_found` 占 `tool_failure=0` 评分比例 >5% 上 C-lite（list_files 受限递归），>15% 评估 C-full（aider 式 repo-map）。

## 后果

- 正向：猜错路径的失败从「模型自己悟」变「工具直接给答案」，预期减少无效失败轮次与 token；零常态开销（仅失败路径有界搜索）。
- 负向：错误消息变长（每次失败 +0~1 行）；S3 后端失败路径多几次 list_dir 往返；建议若失真可能引导模型读错文件（以「精确匹配+storage 验证+只建议」三重约束对冲）。
- 中立：edit/write 的路径幻觉未治理，靠读侧诊断与编译/后续读错误间接暴露——记录为已知缺口。
