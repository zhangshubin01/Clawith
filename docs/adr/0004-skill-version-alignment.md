# ADR-0004: 内置 Skill 文件层版本对齐（白名单式覆盖）

- **状态**: 已接受（2026-08-27）

## 背景

内置 Skill（web-research、skill-creator 等 `is_default` 注册表条目）在 Clawith 中有**两层副本**：DB 注册表（`skills` + `skill_files`，启动时 `seed_skills` 更新）与每个 agent 自己的文件层（`<agent_prefix>/skills/<folder>/*`）。历史机制（`push_default_skills_to_existing_agents`）只在**文件缺失时补写**，因此产生了一个部署缺口：

- skill-creator 等 skill 在早期版本部署过旧版文件；此后官方内容升级，DB 注册表随之更新，但已存在文件的 agent 文件层永远停留在旧版——**缺失补写永远不触发，旧版内容永久滞留**。
- 2026-08-27 全量排查确认：17 个活跃 agent 的 6 个 skill-creator 文件停留在历史版本（与 DB 内容不符，且非人为定制）。
- 修复不能「一律覆盖」：agent 文件层允许租户定制，覆盖会摧毁定制内容。需要**只覆盖可识别的历史内置版本**。

## 决策

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| 1 | 修复形态 | **版本对齐式覆盖（白名单）**：DB 启动更新时把被替换的旧版 md5 记入持久化白名单；对齐时文件内容 md5 ∈ 白名单 ⇒ 视为历史内置版本，覆盖为 DB 现行版 | 「补缺失」与「一律覆盖」之间取可识别性：旧版 md5 可精确枚举，定制内容不受损 |
| 2 | 白名单内容来源 | **静态种子 + 持久化追加双源**（`BUILTIN_SKILL_VERSION_SEED` 记录本次排查发现的 6 个历史版本；此后每次 `seed_skills` 替换 DB 文件内容时自动把旧 md5 追加进 `SystemSetting(key="builtin_skill_version_whitelist")`） | 已发生的滞留用种子兜底；未来升级靠运行时自动记录，白名单随版本演进自我维持 |
| 3 | 非白名单文件 | **保留并告警（skipped）**，视为租户定制、永久退出自动升级 | 语义写进 docstring：定制即自维护；覆盖定制是数据丢失，不可做 |
| 4 | 对齐触发门 | 原 `default_skills_sync_hash` 摘要门保留，**白名单内容并入摘要** | 白名单变化（新种子/新记录）本身会改变「什么算可安全覆盖」，必须触发一次对齐 |
| 5 | 部分失败语义 | **任一 agent 对齐失败则不持久化 hash**，下次启动对全部 agent 重跑（幂等）；失败明细记 `failed_agents` 并以 `{exc!r}` 告警 | 若失败仍持久化 hash，失败 agent 会被摘要门永久跳过——比「重跑一遍幂等操作」代价高得多 |
| 6 | 兼容清理 | 保留 legacy `MCP_INSTALLER.md` 删除逻辑（内层窄 try/except，失败只跳过清理不影响该 agent 对齐） | 既有行为，不扩大本次改动面 |

## 实现形态

- `backend/app/services/skill_seeder.py`：
  - 常量 `BUILTIN_SKILL_VERSION_WHITELIST_KEY` / `DEFAULT_SKILLS_SYNC_HASH_KEY`；`BUILTIN_SKILL_VERSION_SEED`（6 个 skill-creator 历史 md5）。
  - `_load_version_whitelist`（种子∪持久化）、`_record_replaced_versions`（去重追加）、`_persist_replaced_versions`（SystemSetting create/update）。
  - `_default_skills_sync_digest(skills, whitelist)`：白名单并入 sha256。
  - `_align_default_skill_files`：四分支——缺失补写 / 与 DB 相同不动 / md5 ∈ 白名单覆盖 / 否则保留+告警（含 agent、文件路径、md5）。
  - `seed_skills`：DB 更新分支收集 `replaced_versions[path]=md5(旧内容)`，commit 前持久化。
  - `push_default_skills_to_existing_agents`：白名单加载 → 摘要门（含白名单）→ 逐 agent 对齐 → 无失败才持久化 hash。
- 测试：`backend/tests/test_skill_version_whitelist.py`、`backend/tests/test_skill_seeder_sync.py`（对齐四分支、push 全链路、失败不持久化、seed 记录接线）。

## 回滚

机制对既有数据无损（只多写一个 SystemSetting、多覆盖白名单内文件）。回滚 = revert 上述两个文件改动 + 删除 SystemSetting 行 `builtin_skill_version_whitelist`（可选；残留不影响既有行为，因为旧代码不读它）。已对齐的文件不回退（内容已是 DB 现行版，与 DB 一致即无告警）。

## 后果

- 正：历史内置版本滞留被一次性修复并可持续自愈（未来升级自动记录旧版）；租户定制文件显式保留且告警可观测；失败重试取代永久跳过。
- 负：摘要门重开一次全量对齐（一次性成本）；白名单是信任名单，若未来某 agent 文件恰好与历史内置版本同 md5 又属定制（概率可忽略），会被覆盖——此即「可识别性」代价。
- 中性：skipped 文件自维护语义固化在 docstring，运维若想强制刷新某 agent 需人工介入。
