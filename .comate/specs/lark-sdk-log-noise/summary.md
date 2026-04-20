# lark-oapi SDK 日志降噪 — 完成总结

## 完成情况

4 个任务全部完成，已部署验证。

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/services/feishu_ws.py` | `log_level=lark.LogLevel.INFO` → `lark.LogLevel.WARNING`（2 处）；`await client._connect()` 后补充 `logger.info("[Feishu WS] Connected for agent ...")` |
| `backend/app/core/logging_config.py` | Docker 早返回分支添加 `_intercept_standard_logging()` 调用，修复 lark-oapi "Lark" logger 双重日志 |

## 验证结果

| 检查项 | 结果 |
|--------|------|
| 飞书 WS 连接成功日志 | 新增 `[Feishu WS] Connected for agent ...` 出现 |
| SDK 凭据泄露 | 重启后 0 条 `access_key=` 日志 |
| SDK 连接噪音 | 重启后 0 条 `connected to` / `disconnected to` / `trying to reconnect` |
| 异常日志保留 | `ping failed` (WARN) / `connect failed` (ERROR) / `receive message loop exit` (ERROR) 仍可见 |
| 服务启动正常 | 无报错 |

## 核心改动

1. **log_level WARNING** — SDK 默认就是 WARNING，之前 Clawith 显式设为 INFO 导致连接状态日志全部泄露。改回 WARNING 后，仅异常（ping/connect/message loop 错误）输出
2. **Connected 日志** — SDK 静默后 Clawith 侧补充连接确认，保留可追踪性
3. **Docker 双重日志修复** — Docker 模式下 lark-oapi import 顺序导致 "Lark" logger 保留 SDK 自己的 StreamHandler，通过在 Docker 分支重新调用 `_intercept_standard_logging()` 替换
