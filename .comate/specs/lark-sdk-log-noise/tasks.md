# lark-oapi SDK 日志降噪任务计划

- [x] Task 1: 修改 WS Client log_level 为 WARNING（feishu_ws.py）
    - 1.1: 将首次创建 client 的 `log_level=lark.LogLevel.INFO` 改为 `lark.LogLevel.WARNING`（L282）
    - 1.2: 将重连重建 client 的 `log_level=lark.LogLevel.INFO` 改为 `lark.LogLevel.WARNING`（L341）

- [x] Task 2: 补充连接成功日志（feishu_ws.py）
    - 2.1: 在 `await client._connect()` 成功后添加 `logger.info(f"[Feishu WS] Connected for agent {agent_id}")`，位于 retry 循环内、ping_task 创建前

- [x] Task 3: 修复 Docker 模式双重日志（logging_config.py）
    - 3.1: 在 `configure_file_logging()` 的 Docker 早返回分支中，return 前调用 `_intercept_standard_logging()`

- [x] Task 4: 部署验证
    - 4.1: 重启服务，确认飞书 WS 连接成功日志出现
    - 4.2: 确认不再有 `connected to wss://...access_key=` 凭据泄露日志
    - 4.3: 确认不再有 `trying to reconnect` / `disconnected to` 噪音日志
    - 4.4: 确认异常日志（ping failed / connect failed）仍可见
