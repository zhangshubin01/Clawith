# ADR-0009: execute_code 终态 = Docker 会话沙箱（DockerSessionBackend）

- **状态**: 已接受（2026-08-29）
- **前置**: 2026-08-28 bwrap setuid 0.12 事故（部署 b222006b，execute_code 0成功/11失败）
- **废止**: `SandboxType.DOCKER` 原指向的一次性 `DockerBackend`（半成品占位：无 workspace 挂载、
  无安全预检、无 staging/网关发布——从未启用，无调用方无测试）
- **调研**: 业界对照（Codex bwrap-userns+Landlock / gptme docker / SWE-agent swerex / OpenHands
  Runtime Service / E2B Firecracker 自托管 / gVisor 在 OrbStack 的可行性）

## 背景

bwrap 0.12 移除 setuid 支持后，过渡态（自编译 0.11.2+setuid）可止血但不可作终态：
上游不再维护 setuid 路线，新 CVE 无人修；OrbStack 的嵌套 userns 残缺（mount proc/bind 被拒）
使业界标准路线（Codex 式 bwrap-userns）在本平台不可行。终态需要一条**平台可控、隔离更强、
生命周期完整**的执行链路。

## 决策

`execute_code` 迁移到 **每 Run 长驻沙箱容器**（`docker run -d sleep infinity` + `docker exec`），
即 `DockerSessionBackend`（`backend/app/services/sandbox/local/docker_backend.py`），
替换 `SandboxType.DOCKER` 的旧实现。要点：

1. **复用后端无关逻辑**：staging 克隆、网关发布（`verify_and_merge_outputs`）、host 侧 pip 代理、
   venv 生命周期抽取到 `sandbox/local/shared.py`，subprocess 后端留薄委托——两后端同一行为权威。
2. **镜像同源**：`clawith-code-sandbox`（`backend/Dockerfile.sandbox`）FROM 与后端相同的
   python:3.12-slim base（+nodejs/git/curl 等常用工具），保证宿主 uv 创建的 per-agent venv
   （symlink → /usr/local/bin/python3.12）与 glibc ABI 在容器内原样可用。
3. **隔离参数**：`--user 1000:1000`（staging 属主对齐，防 root 污染）、`network_mode=none`
   （allow_network 时才 bridge）、256m/0.5cpu、pids-limit 64、cap-drop ALL、
   no-new-privileges、只读 rootfs + tmpfs /tmp、auto_remove。
4. **生命周期**：run 结束由 command_worker 同时调 subprocess/docker 两路 close（各为 no-op），
   超时=kill 容器+会话重置（exit 124，语义对齐 bwrap）；一次性调用（无 run_id）用临时会话。
5. **GHSA-pxhw-h44j-8pfx 缓解**：pip 响应文件改 O_EXCL+O_NOFOLLOW 排他写入；受保护文件恢复
   拒绝穿越 symlink 组件（staging 侧宿主写路径全部加固）。
6. **DooD 挂载源翻译**：bind-mount 源由宿主 daemon 解析，容器私有路径（/tmp）在 daemon
   视角不存在——socket 级探针实测（2026-08-29）证明源路径会**静默变成空目录**（无任何报错）。
   因此 staging 落在共享 bind mount `/data/agents/.sandbox-staging`，且所有 `/data/agents`
   挂载源经 `detect_host_agent_data_root()`（自 inspect 本容器 Mounts，与 AndroidBuildBackend
   同一模式，抽到 shared.py 单权威）翻译为宿主机路径；检测失败时回退直传（宿主直跑 dev 场景）。
7. **灰度开关现成**：`SandboxConfig.from_dict` 已支持 per-agent `sandbox_type`；全局
   `SANDBOX_TYPE=docker` 切换。setuid bwrap 0.11.2 过渡态保持到灰度通过后从镜像删除。

## 业界对照（决策依据）

| 路线 | 结论 |
|---|---|
| Docker 容器沙箱（SWE-agent v2/swerex、gptme、OpenHands Runtime Service） | **采纳**——业界事实标准，且本仓库已有 DooD 基础设施（docker.sock+socat 代理+AndroidBuildBackend 先例） |
| bwrap userns（Codex 路线，无 setuid） | 否决——OrbStack 内核 userns 残缺（实测），Codex 的"正常 Linux 内核"前提不成立 |
| gVisor/runsc | 否决——OrbStack 不支持自定义 runtime（unmodified engine，无法进其 VM） |
| E2B 自托管（Firecracker） | 否决——需云厂商+嵌套虚拟化+Terraform，无单机路径 |

## 关键证据

- bwrap 事故根因链与过渡态实测：workspace memory `bwrap-setuid-0.12-execute-code-broken`。
- 旧 `DockerBackend` 全仓库 0 调用方、0 测试、0 生产配置（`.env.example` 仅注释）——
  替换不构成行为变更。
- 后端镜像 `FROM python:3.12-slim`（backend/Dockerfile:4,30）与沙箱镜像同源，venv 兼容性成立。

## 后果

- bash 脚本可见工具集从「bind /usr 全量」收窄为沙箱镜像白名单（git/curl/node/jq/zip/unzip）
  ——隔离换来的有意行为变化，灰度时向受影响 agent 说明。
- 每次 execute_code 首调用有 ~1-2s 容器启动成本（session 复用后为 exec 延迟）。
- 旧一次性 `DockerBackend` 类删除；`SandboxType.DOCKER` 枚举与配置键不变。
- **孤儿容器是已知边界**：正常路径由 `close_run` 删除（command_worker 兜底 + 超时 kill），
  但 backend 进程被 SIGKILL/OOM、或 docker.sock 瞬时故障时，运行中的沙箱容器可能残留。
  没有宿主侧 GC 守护（与 bwrap 过渡态同一水平）。缓解：残留容器是 `sleep infinity`
  空转（exec 随主进程死亡而结束），资源上限已钳制（256m/0.5cpu/64 pid），且
  `auto_remove` 保证容器停止即自动移除；容器带 `clawith.sandbox=execute-code` /
  `clawith.run_id` 标签，运营可 `docker ps --filter label=clawith.sandbox` 巡检。
  若线上观测到堆积，再补常驻 GC 任务（标签为识别基础，无需迁移数据格式）。
- **staging 目录同属孤儿边界**：`/data/agents/.sandbox-staging/` 下的会话目录在硬崩溃时
  同样残留（无 GC）。缓解：目录前缀唯一且 0o700、每会话 < 工作区副本大小；巡检命令
  `ls /data/agents/.sandbox-staging | wc -l`，堆积时与容器 GC 一并清理。
