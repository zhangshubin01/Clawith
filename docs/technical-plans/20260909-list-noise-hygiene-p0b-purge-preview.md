# P0-b 清淤「整体清空」只读预览（agent 950a1943）

> **只读预览，未写库**。生成依据：`agent_list_items` 实时只读查询（2026-09-09）。
> 数据口径（PG 实读核实）：agent `950a1943-6ad6-4139-842e-8bde89ca823c` 共 **139 行** = `mydome1` 81 行（**76 pending + 5 completed**）+ `legacy:*` **12 作用域 58 行**（全 pending）。
> **五审修正后的结论**：`mydome1` 项目**已闭环**（MR !1~!11 连续全合并、main 头 `b9240bd`、agent 2026-09-09 自陈「无在途、平台清单为空」）。这 139 行是**交付过程中未同步 complete 的过时快照**，不是「活跃待办 + 噪音」。
> 因此清淤目标 = **整体清空**（不再逐 key 分类保留）：
> - `mydome1` **76 行 pending → `completed`**（收口，保留历史，不删除）；
> - `legacy:*` **58 行 → 删除**（死数据，原始 `memory/清单.md` 已归档 `workspace/archived/memory-历史清单-2026-09-09.md`）。
> **六审扩围提示**：本预览只覆盖**当前 DB 里已存在**的 950a1943 一家（唯一有行者）。`fix-plan.md` 六审已把 P0-b 扩为「全平台清淤 + 先归档 10 个 agent 的源 `清单.md`」——其余 10 个 agent（8 个 `project: -` + 2 个 real-project）尚未迁移、DB 无行，故不在本预览列出；它们迁移后的清淤按 `fix-plan.md` 的「归档源文件 → 清库」顺序另行执行。

## 汇总

| 去向 | 作用域 | 行数 | 说明 |
|---|---|---|---|
| `mydome1` pending → completed | 1 | 76 | 项目已闭环，逐条 `complete_list_item` 收口（保留历史） |
| `mydome1` 保持 completed 不动 | 1 | 5 | 已是 completed，无需变更 |
| `legacy:*` → 删除 | 12 | 58 | 死数据（`extract_workspace_project` 永不派生 `legacy:*`，永不注入/显示） |
| **合计** | 14 | **139** | |

---

## 一、`mydome1` 76 行 pending → `completed`（逐条收口）

> 收口方式：对以下 76 项逐条 `complete_list_item`（或等价 UPDATE `status='completed'`）。保留历史，不删除。
> 判定依据：项目已闭环（MR !1~!11 全合并），这 76 行是「已交付/已推进但未标 completed」的过时快照，无一属「活跃待办」。

| # | sort_order | key | title |
|---|---|---|---|
| 1 | 3 | `四个改动文件仍在且内容命中` | **四个改动文件仍在且内容命中** |
| 2 | 4 | `reflections_已更新` | **reflections 已更新** |
| 3 | 5 | `复制结果可见入口` | 复制结果可见入口 |
| 4 | 6 | `按键防抖` | 按键防抖 |
| 5 | 7 | `无障碍适配` | 无障碍适配 |
| 6 | 8 | `键盘输入` | 键盘输入 |
| 7 | 9 | `横屏适配` | 横屏适配 |
| 8 | 10 | `设置项` | 设置项 |
| 9 | 11 | `深色模式` | 深色模式 |
| 10 | 12 | `大数_科学计数法显示` | 大数/科学计数法显示 |
| 11 | 13 | `历史记录` | 历史记录 |
| 12 | 14 | `错误提示_ui_化` | 错误提示 UI 化 |
| 13 | 15 | `边界测试补充` | 边界测试补充 |
| 14 | 16 | `中英文资源` | 中英文资源 |
| 15 | 17 | `13_16_13_17_13_18_13_19_43` | 13:16 / 13:17 / 13:18 / 13:19:43 |
| 16 | 18 | `13_17_13_18_13_19_43` | 13:17 / 13:18 / 13:19:43 |
| 17 | 19 | `宿主侧只读核验` | 宿主侧只读核验 |
| 18 | 20 | `p1_文档_readme_与代码脱节` | [P1·文档] README 与代码脱节 |
| 19 | 21 | `p1_展示_循环小数退化` | [P1·展示] 循环小数退化 |
| 20 | 22 | `p1_正确性_超大指数错误文案错位` | [P1·正确性] 超大指数错误文案错位 |
| 21 | 23 | `p1_体验_出错后输入不重置` | [P1·体验] 出错后输入不重置 |
| 22 | 24 | `p2_测试_无_androidtest_源集` | [P2·测试] 无 androidTest 源集 |
| 23 | 25 | `p2_输入_缺键盘_粘贴支持` | [P2·输入] 缺键盘/粘贴支持 |
| 24 | 26 | `p2_无障碍_结果变化无播报` | [P2·无障碍] 结果变化无播报 |
| 25 | 27 | `p2_持久化_历史仅存内存` | [P2·持久化] 历史仅存内存 |
| 26 | 28 | `p2_i18n_缺英文资源` | [P2·i18n] 缺英文资源 |
| 27 | 29 | `p2_适配_横屏布局未验证` | [P2·适配] 横屏布局未验证 |
| 28 | 30 | `p3_工程_release_未配置` | [P3·工程] release 未配置 |
| 29 | 31 | `p3_细节_按键无触觉反馈` | [P3·细节] 按键无触觉反馈 |
| 30 | 32 | `git_credentials` | `~/.git-credentials` |
| 31 | 33 | `环境变量` | 环境变量 |
| 32 | 34 | `git_remote_v` | `git remote -v` |
| 33 | 35 | `宿主_workspace_mydome1_git_config` | 宿主 `workspace/mydome1/.git/config` |
| 34 | 36 | `环境变量_env_grep_i_token_gitlab` | 环境变量 `env | grep -i token|gitlab` |
| 35 | 39 | `附加核查_config_gh_netrc_inbox_目录_全局_find_token_glpat_文件` | 附加核查：`~/.config/gh`、`~/.netrc`、inbox 目录、全局 find token/glpat 文件 |
| 36 | 40 | `env_中_token_gitlab_相关变量` | `env` 中 token/gitlab 相关变量 |
| 37 | 43 | `环境变量_token_gitlab_gl` | 环境变量 token/gitlab/GL |
| 38 | 44 | `oauth2_insteadof_规则` | oauth2/insteadOf 规则 |
| 39 | 45 | `补充扫描_netrc_gitconfig_global_config_中的_glpat_credentials` | 补充扫描 `~/.netrc`、`~/.gitconfig`、global config 中的 glpat/credentials |
| 40 | 46 | `沙箱_git_credentials` | 沙箱 `~/.git-credentials` |
| 41 | 47 | `沙箱环境变量_token_gitlab` | 沙箱环境变量 token/gitlab |
| 42 | 48 | `git_config` | `git config` |
| 43 | 50 | `push` | **Push** |
| 44 | 51 | `mr_5` | **MR !5** |
| 45 | 52 | `merge_成功` | **Merge 成功** |
| 46 | 53 | `main_树回读` | **main 树回读** |
| 47 | 54 | `编译_单测复跑通过` | **编译/单测复跑通过** |
| 48 | 55 | `收尾` | **收尾** |
| 49 | 60 | `等待新需求` | **等待新需求** |
| 50 | 66 | `git_logs_head_末行仍为_9087bee_e212a4b_1788508193` | `.git/logs/HEAD` 末行仍为 `9087bee → e212a4b @1788508193` |
| 51 | 67 | `git_refs_remotes_origin_f_android_ai_仍为_9087bee` | `.git/refs/remotes/origin/f_android_ai` 仍为 `9087bee` |
| 52 | 68 | `calculatoraction_kt_主代码` | **CalculatorAction.kt（主代码）** |
| 53 | 69 | `keyboardmapper_kt_主代码` | **KeyboardMapper.kt（主代码）** |
| 54 | 70 | `tapdebouncer_kt_主代码` | **TapDebouncer.kt（主代码）** |
| 55 | 71 | `keyboardmappertest_kt_测试` | **KeyboardMapperTest.kt（测试）** |
| 56 | 72 | `tapdebouncertest_kt_测试` | **TapDebouncerTest.kt（测试）** |
| 57 | 73 | `activity_main_xml_中_btnsettings_段落` | **activity_main.xml 中 btnSettings 段落** |
| 58 | 74 | `弃用删除` | **弃用删除** |
| 59 | 75 | `保留待续` | **保留待续** |
| 60 | 76 | `补齐提交` | **补齐提交** |
| 61 | 77 | `编译_单测` | **编译 + 单测** |
| 62 | 78 | `提交` | **提交** |
| 63 | 80 | `mr_7_另开_供你_review` | **MR !7（另开、供你 review）** |
| 64 | 81 | `不并入_mr_6` | **不并入 MR !6** |
| 65 | 82 | `mr_7_review_合并` | **MR !7 review 合并** |
| 66 | 86 | `p2_工程侧优化立项` | **[P2] 工程侧优化立项** |
| 67 | 87 | `已授权未实施项` | **已授权未实施项** |
| 68 | 88 | `验证前置` | **验证前置** |
| 69 | 89 | `合并执行` | **合并执行** |
| 70 | 90 | `合并后双确认` | **合并后双确认** |
| 71 | 91 | `归档完成` | **归档完成** |
| 72 | 95 | `p2_设置_设置项只有振动开关` | **[P2·设置] 设置项只有振动开关** |
| 73 | 96 | `p2_发布_release_仍是_可安装验证_态` | **[P2·发布] release 仍是「可安装验证」态** |
| 74 | 97 | `p2_体验_历史列表全量重建` | **[P2·体验] 历史列表全量重建** |
| 75 | 98 | `p2_测试_androidtest_仅启动冒烟` | **[P2·测试] androidTest 仅启动冒烟** |
| 76 | 99 | `p2_隐私_allowbackup_true_且无备份规则` | **[P2·隐私] allowBackup=true 且无备份规则** |

---

## 二、`legacy:*` 12 作用域 58 行 → 删除

> 判定依据：`extract_workspace_project` 永远派生 `mydome1`，`legacy:*` 永不注入、永不显示，属**死数据**。
> 原始 `memory/清单.md` 已归档 `workspace/archived/memory-历史清单-2026-09-09.md`，无信息损失。
> 删除方式：按 `project LIKE 'legacy:%'`（12 作用域）整段 DELETE。

### 汇总（12 作用域）

| # | project（作用域） | 行数 | distinct key |
|---|---|---|---|
| 1 | `legacy:1a2c1102-f753-41be-8c4a-1ce478ab2b2f` | 2 | 2 |
| 2 | `legacy:351f1006-9527-41fa-84b4-b4acc7e1ec5c` | 5 | 5 |
| 3 | `legacy:38b3d752-663a-4d35-98e5-14a9ea88f490` | 10 | 10 |
| 4 | `legacy:3f289908-1aa3-491c-9fa0-eaefefd65d7e` | 4 | 4 |
| 5 | `legacy:7ffbd09b-000f-4a6f-9601-21763e2d1ff2` | 9 | 9 |
| 6 | `legacy:805a414b-fd25-4b6f-9c76-a409f693bccd` | 7 | 7 |
| 7 | `legacy:9ff1d153-8b1f-4c51-ad32-5d678ee3c883` | 4 | 4 |
| 8 | `legacy:c8bd4ebf-6b12-4ff8-9efb-08d541217aa5` | 5 | 5 |
| 9 | `legacy:d965b040-107a-4932-bb29-ef4fb19ce27d` | 2 | 2 |
| 10 | `legacy:dbb3825f-1e15-4b3c-b9f0-767b279cdb23` | 2 | 2 |
| 11 | `legacy:e7eb596a-88cd-4552-95b4-8e88b4ed751a` | 2 | 2 |
| 12 | `legacy:fdcf534a-66d5-4928-9fde-b8d608d144ac` | 6 | 6 |
| | **合计** | **58** | |

### 逐作用域明细（key）

**1. `legacy:1a2c1102`（2 行）**：`gitlab_交付链路已闭环`、`无在途执行`

**2. `legacy:351f1006`（5 行）**：`无新增用户需求`、`交付链路已闭环`、`触发器全部关闭`、`focus_空闲`、`无探索必要`

**3. `legacy:38b3d752`（10 行）**：`push_mr_本地_2b6077f_网络阻塞_已闭环_2026_09_05`、`push_前核验_远端_40b83d9_与本地链关系`、`优化项授权清单_已授权_未实施`、`p1_文档_readme_与代码脱节`、`p1_展示_循环小数退化`、`p1_体验_出错后输入不重置`、`p2_工程侧优化_待拍板`、`mr_6_仍_open`、`宿主_6_个未提交草稿`、`待办存量`

**4. `legacy:3f289908`（4 行）**：`p2_工程侧优化立项`、`p1_文档_readme_与代码脱节`、`p1_展示_循环小数退化`、`p1_体验_出错后输入不重置`

**5. `legacy:7ffbd09b`（9 行）**：`p1_1_4_交付_9087bee_push_mr_merge_已闭环_移出待办`、`优化项授权清单_按键防抖_无障碍适配_键盘输入_横屏适配_设置项_未实施`、`剩余未实施优化_错误提示_ui_化_边界测试补全_中英文资源_未实施`、`p1_文档_readme_与代码脱节`、`p1_展示_循环小数退化`、`p1_正确性_超大指数错误文案错位`、`p1_体验_出错后输入不重置`、`p2_工程侧优化_待拍板`、`等待新需求`

**6. `legacy:805a414b`（7 行）**：`mr_6_review_合并`、`宿主_6_个未提交草稿去留`、`p2_工程侧优化立项`、`p1_文档_readme_与代码脱节`、`p1_展示_循环小数退化`、`p1_体验_出错后输入不重置`、`优化项授权清单_已授权_未实施`

**7. `legacy:9ff1d153`（4 行）**：`p2_工程侧优化立项`、`p1_文档_readme_与代码脱节`、`p1_展示_循环小数退化`、`p1_体验_出错后输入不重置`

**8. `legacy:c8bd4ebf`（5 行）**：`无新增用户需求`、`交付链路已闭环`、`触发器全部关闭`、`focus_空闲`、`无探索必要`

**9. `legacy:d965b040`（2 行）**：`幽灵_fetch_第_5_次确认`、`40b83d9_对象仍未入宿主可见库`

**10. `legacy:dbb3825f`（2 行）**：`list_triggers`、`list_focus_items_include_completed_true`

**11. `legacy:e7eb596a`（2 行）**：`幽灵_fetch_第_5_次确认`、`40b83d9_对象仍未入宿主可见库`

**12. `legacy:fdcf534a`（6 行）**：`p0_紧急项`、`p1_唯一在途状态`、`p2_可选的待确认项`、`代码风格约定`、`代码评审流程`、`周报形式`

---

## 执行前提（写库前）

1. 本清单**只读预览**，未动任何数据。
2. 写库前：**备份 `agent_list_items`**（agent 950a1943，对齐 heartbeat 清淤先例 `reflections-backup-*`），再执行最终 UPDATE/DELETE，并给你逐句核对最终 SQL。
3. 写库需你**明确说「授权清淤」**（当前红线：DB 只读）。
4. `mydome1` 收口用 `status='completed'`（保留历史），`legacy:*` 用 DELETE；两者都不触碰 `memory.md` / `reflections.md` / `workspace/archived/` 原文（清单表与 reflections 分属两个 owner）。
5. 收口后 `session_task_state` 的 `has_open_list_items=bool(pending)` 翻转为 False、phase 判为 complete——**这是正确终态**（项目已闭环），非副作用。

---

## 执行记录（2026-09-09 10:21 UTC，已写库）

> 执行前实读发现本预览**已过时**：`agent_list_items` 实为 **151 行 / 2 个 agent**，而非预览时的 139 行 / 1 家。`08a739c1` 已在预览生成后（当日）用**旧迁移代码**首次迁移，产出 3 个 `legacy:*` 作用域 12 行。**据此收窄清淤范围**，未执行「全平台 `legacy:%` 兜底删除」的原文。

**实际执行（仅 950a1943，`08a739c1` 一行未动）：**

| 操作 | 作用域 | 结果 | SQL |
|---|---|---|---|
| 收口 | `950a1943` / `mydome1` / pending | **76 → completed** | `UPDATE ... SET status='completed', completed_at=now(), updated_at=now() WHERE agent_id='950a1943-…' AND project='mydome1' AND status='pending'` |
| 删除 | `950a1943` / `legacy:*`（12 作用域） | **58 行删除** | `DELETE ... WHERE agent_id='950a1943-…' AND project LIKE 'legacy:%'` |

**备份**：`agent_list_items_p0b_backup_20260909`（`CREATE TABLE AS SELECT`，全表 151 行，含 08a739c1 的 12 行）。

**执行后终态**：`agent_list_items` 剩 **93 行** = `950a1943/mydome1` 81 行全 completed（completed_at 非空，最新 2026-09-09 10:21:41）+ `08a739c1` 12 行 legacy pending（保留）。

**`08a739c1` 收窄原因（决定性）**：实读其 12 行内容，非死数据——含**真实在途待办** `mg2_决策回复`、`09_07_决策触发器`（方案七审明示 `08a739c1`「mg2（ColDinero）挂起等待授权」），混合 8 条观察快照（`mg2_coldinero_状态无变化`/`无新输入`/`触发器状态正常`/`无待办焦点`/`预案已写入_reflections`/`清理重复_focus`/`探索判断`/`下一节点`）。删除即掩埋真待办。**处置**：按方案「5 活跃 agent」路径——P1 部署后 `08a739c1` 下次碰清单工具会重迁移入 LIVE，届时这 12 行成死重复，再走对账 + legacy 清淤。

**遗留（需后续，非本次写库）**：
1. **P1 + P0-a（commit `53b46e34`）尚未部署到生产**——`08a739c1` 今早正是被旧迁移代码产出 legacy 分叉；剩余 7 个 `project: -` agent + 2 个 real-project agent 的源 `清单.md` 仍在，部署 P1 前它们随时可能复刻同类 legacy 分叉。
2. 10 个 agent 的源 `清单.md` 归档（方案七审三分）与 2 个 real-project agent 的 LIVE 对账，属 filesystem + 迁移动作，待 P1 部署后执行。
