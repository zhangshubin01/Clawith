# 企业内网分发（固定版本安装包）

面向 **多台开发机统一装机**（例如 100+ 台）：用 **zip + 锁定依赖**，避免每人 `pip` 拉到不同子版本。

---

## 包内容（zip 里应包含）

从本目录打出的 zip **只含** `clawith-ide-acp/` 以下内容（**不要**包含 `.venv`、`.env`）：


| 路径                                       | 说明                            |
| ---------------------------------------- | ----------------------------- |
| `VERSION`                                | 分发包版本号，工单与排障时先报此号             |
| `requirements.txt`                       | 直连依赖（== 固定）                   |
| `requirements.lock.txt`                  | **推荐装机用**：完整 `pip freeze`，可复现 |
| `server.py`                              | ACP 瘦客户端                      |
| `env.example`                            | 环境变量模板                        |
| `scripts/setup-mac.sh` / `setup-win.ps1` | 安装脚本                          |
| `scripts/run-mac.sh` / `run-win.ps1`     | 本地调试启动                        |
| `scripts/package-release.sh`             | 在**有本仓库时**重新打 zip（可选）         |
| `jetbrains/*.json.example`               | IDE 配置示例                      |


---

## 装机流程（每台电脑）

### 前置

- **Python 3.10～3.12**（建议全公司统一一个次版本；3.13+ 需自行验证 lock）
- 可访问 **PyPI** 或 **内网 pip 镜像**（离线见下文）

### macOS / Linux

```bash
unzip clawith-ide-acp-0.2.0.zip
cd clawith-ide-acp-0.2.0   # zip 内顶层目录名与 VERSION 一致
bash scripts/setup-mac.sh
# 编辑 .env：CLAWITH_URL、CLAWITH_API_KEY、CLAWITH_DEFAULT_AGENT_ID
# 按 README 配置 ~/.jetbrains/acp.json（绝对路径指向本目录 .venv 与 server.py）
```

### Windows（PowerShell）

```powershell
Expand-Archive clawith-ide-acp-0.2.0.zip -DestinationPath C:\Tools
cd C:\Tools\clawith-ide-acp-0.2.0
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force   # 若脚本被拦截，仅需一次
.\scripts\setup-win.ps1
# 编辑 .env，再配置 %USERPROFILE%\.jetbrains\acp.json
```

安装脚本会 **优先使用 `requirements.lock.txt`**（若存在），否则回退 `requirements.txt`。

---

## 制作发布 zip（发布负责人）

在 **Clawith 仓库** 内执行：

```bash
cd integrations/clawith-ide-acp
bash scripts/package-release.sh
```

产物默认在同级目录 `**releases/clawith-ide-acp-<VERSION>.zip**`。将 zip 上传到 **内网文件站 / MDM**，不要改 zip 内文件名与 `VERSION` 不一致。

更新依赖时：

1. 修改 `requirements.txt` 中的 `==` 版本。
2. 新建临时 venv：`pip install -r requirements.txt && pip freeze > requirements.lock.txt`（去掉与项目无关的包）。
3. 递增 `VERSION`。
4. 重新执行 `package-release.sh`。

---

## 完全离线（无 PyPI）

1. 在一台可联网机器上：用 **目标相同 Python 版本** 建 venv，执行
  `pip download -r requirements.lock.txt -d wheels/`
2. 将 `wheels/` 与 zip 一并放到内网。
3. 装机脚本改为（示例）：
  `pip install --no-index --find-links=wheels -r requirements.lock.txt`  
   可在内网文档中写死路径或提供 `setup-offline-mac.sh` / `setup-offline-win.ps1`（按需再加）。

---

## 与后端的版本关系

- 瘦客户端与云端 WebSocket 使用 `**schemaVersion: 3`**（见 README）。升级 Clawith 后端若 bump 协议，需 **同步发新 zip** 并更新 `VERSION` / 发布说明。

---

## 支持台帐建议字段

- 机器：`VERSION`、OS、Python `python3 --version`
- `CLAWITH_URL`（勿在工单贴完整 API Key）
- JetBrains 版本、Android Studio / IDEA 版本
- `acp.json` 中 `command` / `args` 是否为本机绝对路径

