# Clawith IDE 集成（ACP 瘦客户端）

本目录为**开发者本机**所需的全部文件：通过 JetBrains **Agent Client Protocol (ACP)** 把 Android Studio / IntelliJ IDEA 接到云端或局域网 **Clawith**，由云端智能体（如 WL4）推理，在本地 IDE 内读写文件、执行终端命令。

> 服务端需启用 `clawith-acp` 插件，WebSocket 路径为：`/api/plugins/clawith-acp/ws`。  
> 瘦客户端与后端 JSON 消息含 `**schemaVersion: 3`**（升级任一侧时需对齐 `CLOUD_WS_SCHEMA_VERSION` / `ACP_WS_SCHEMA_VERSION`）。

扩展能力与 Clawith 对齐的分阶段计划见 **[FULL_ACP_INTEGRATION_PLAN.md](./FULL_ACP_INTEGRATION_PLAN.md)**。

**多台电脑统一装机（内网 zip、锁定依赖）** 见 **[ENTERPRISE.md](./ENTERPRISE.md)**。

自动化测试（后端 + 瘦客户端逻辑，无需 IDE）：在仓库 `backend` 目录执行  
`pytest tests/plugins/test_clawith_acp.py -v`（需安装 `pip install -e ".[dev]"` 或等价 dev 依赖）。

---

## 目录说明


| 文件 / 目录                              | 说明                                               |
| ------------------------------------ | ------------------------------------------------ |
| `server.py`                          | ACP 瘦客户端（stdio），由 IDE 拉起                         |
| `requirements.txt`                   | Python 直连依赖（`==` 固定）                             |
| `requirements.lock.txt`              | 完整锁定（**企业装机推荐**）                                 |
| `VERSION`                            | 分发包版本号                                           |
| `ENTERPRISE.md`                      | 内网 zip 制作与装机流程                                   |
| `scripts/package-release.sh`         | 打 `releases/*.zip`（不含 venv）                      |
| `env.example`                        | 环境变量模板，复制为 `.env`                                |
| `scripts/setup-mac.sh`               | macOS 一键创建 venv 并安装依赖                            |
| `scripts/run-mac.sh`                 | macOS 读取 `.env` 后启动 `server.py`（调试用）             |
| `scripts/setup-win.ps1`              | Windows 同上                                       |
| `scripts/run-win.ps1`                | Windows 读取 `.env` 后启动                            |
| `jetbrains/acp.json.example`         | macOS / Linux 路径风格                               |
| `jetbrains/acp.windows.json.example` | Windows 路径风格                                     |
| `FULL_ACP_INTEGRATION_PLAN.md`       | ACP 全能集成路线图（与框架能力对照）                             |
| `MANUAL_TEST_IDE_ACP.md`             | **手工测试用例**（本机逐项验收 ACP / P2）                      |
| `ACP_SDK_COVERAGE.md`                | **AGENT_METHODS / CLIENT_METHODS** 与代码 ✅/⚠️/❌ 对照 |


将整个 `clawith-ide-acp` 文件夹复制到团队共享盘或打包 zip 分发即可，**无需**携带完整 Clawith 源码。

---

## 前置条件

1. **Python 3.10+**（macOS 可用 Homebrew；Windows 官网安装包，建议勾选 *Add python.exe to PATH*）。
2. **JetBrains AI Assistant** 支持 ACP（Android Studio / IDEA 等，版本以 [官方文档](https://www.jetbrains.com/help/ai-assistant/acp.html) 为准）。
3. 可访问的 **Clawith 后端**（已部署且加载 `clawith-acp` 插件）。
4. Clawith 用户 **API Key**（`cw-` 前缀，在 Web 端生成）。

---

## 快速开始

### macOS

```bash
cd /path/to/clawith-ide-acp
bash scripts/setup-mac.sh
# 编辑 .env：CLAWITH_URL、CLAWITH_API_KEY、CLAWITH_DEFAULT_AGENT_ID

# 可选：验证能否启动（JetBrains 会自行拉进程，此步仅排错）
bash scripts/run-mac.sh
```

### Windows（PowerShell）

```powershell
cd C:\path\to\clawith-ide-acp\scripts
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force   # 若脚本被策略拦截
.\setup-win.ps1
# 编辑上级目录中的 .env

.\run-win.ps1   # 可选排错
```

---

## 配置 JetBrains（Android Studio / IDEA）

1. 打开 **AI Chat**，右上角选择 **Add Custom Agent**（或 **Settings → Tools → AI Assistant → Agents**），会生成或编辑用户级配置：
  - macOS / Linux：`~/.jetbrains/acp.json`
  - Windows：`%USERPROFILE%\.jetbrains\acp.json`
2. 参考本目录下的 `**jetbrains/acp.json.example`**（或 Windows 用 `**acp.windows.json.example`**）：
  - `**command**`：指向本目录 `.venv` 里的 **python** 可执行文件（**绝对路径**）。
  - `**args`**：一个元素，为 `**server.py` 的绝对路径**。
  - `**env`**：至少设置 `CLAWITH_URL`、`CLAWITH_API_KEY`；`CLAWITH_DEFAULT_AGENT_ID` 默认为智能体名称（如 `WL4`）。
3. 保存后重启 IDE 或重新加载 Agents；在 AI Chat 的模式列表中选择 **Clawith (ACP)**（与你在 `agent_servers` 里写的名称一致）。
4. 官方格式说明：[Agent Client Protocol - Add a custom agent](https://www.jetbrains.com/help/ai-assistant/acp.html)。

---

## 环境变量


| 变量                         | 必填  | 说明                                                                                            |
| -------------------------- | --- | --------------------------------------------------------------------------------------------- |
| `CLAWITH_URL`              | 是   | 后端根 URL，如 `https://clawith.example.com` 或 `http://127.0.0.1:8008`，**不要**末尾 `/`                |
| `CLAWITH_API_KEY`          | 是   | `cw-...`                                                                                      |
| `CLAWITH_DEFAULT_AGENT_ID` | 否   | 智能体名称或 UUID，默认 `WL4`                                                                          |
| `CLAWITH_WS_PROXY`         | 否   | 留空或 `direct`：WebSocket **直连** `CLAWITH_URL`（推荐，避免全局 SOCKS 需额外安装 `python-socks`）。`auto`：跟随系统代理 |


---

## 与 Continue / OpenAI 兼容接口

- **Continue**、Android Studio 内置「OpenAI 兼容」等走的是 **HTTP** `chat/completions`，**不是**本 ACP 流程。
- 若仅需对话、不要求 IDE 原生 ACP 工具链，可继续用 OpenAI Base URL + API Key；**需要 IDE 侧读文件 / 跑终端** 时使用本瘦客户端 + JetBrains ACP。

---

## 故障排除


| 现象                               | 处理                                                                                                 |
| -------------------------------- | -------------------------------------------------------------------------------------------------- |
| `python-socks is required`       | 使用默认直连（不设 `CLAWITH_WS_PROXY` 或设为 `direct`），或 `pip install python-socks` 且 `CLAWITH_WS_PROXY=auto`  |
| WebSocket **403**                | 确认服务端已加载 `clawith-acp` 插件；反向代理需放行 `Upgrade` / `Connection`；URL 含正确路径 `/api/plugins/clawith-acp/ws` |
| **Command executable not found** | `acp.json` 中 `command` / `args` 必须为**绝对路径**，且 venv 已用对应系统的 setup 脚本创建                              |
| Windows 下终端工具无输出                 | 部分环境需使用 `cmd.exe /c`；本客户端已在 Windows 上使用 `cmd.exe /c` 执行 `ide_execute_command`                      |
| WSL                              | JetBrains 当前对 ACP 在 WSL 的支持有限，见官方说明；建议在 **原生 Windows** 或 **macOS** 上跑 IDE                          |


---

## English summary

- **One folder** to copy to each developer machine: `clawith-ide-acp/`.
- Run `**scripts/setup-mac.sh`** or `**scripts/setup-win.ps1`** to create `.venv` and install deps; configure `**.env`**.
- Point **JetBrains `~/.jetbrains/acp.json`** `command` + `args` to that **venv Python** and `**server.py`** (absolute paths). Use `**acp.windows.json.example`** on Windows.
- `**server.py`** connects to `**CLAWITH_URL`** over WebSocket at `**/api/plugins/clawith-acp/ws**`; set `**CLAWITH_WS_PROXY**` only if you need system proxy (SOCKS may need `**python-socks**`).

---

## 仓库内开发说明

若你从 **Clawith 源码仓库** 打开项目，也可执行仓库根目录下的 `clawith_acp/server.py`：它会**转调**本目录的 `server.py`，便于与历史 `acp.json` 路径兼容。