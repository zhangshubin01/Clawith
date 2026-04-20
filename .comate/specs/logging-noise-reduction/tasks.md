# 日志噪音消除任务计划

- [x] Task 1: 添加请求噪音过滤器（logging_config.py）
    - 1.1: 定义 `_SILENT_PATH_PREFIXES` 常量和 `_request_noise_filter()` 函数
    - 1.2: 修改 `_ensure_trace_id()` 内部调用 `_request_noise_filter()`，形成 filter 链
    - 1.3: 在 `_intercept_standard_logging()` 末尾添加第三方库 setLevel(WARNING) 控制
    - 1.4: 在 `_add_stdout_handler()` / `_add_file_handler()` 中使用更新后的 filter

- [x] Task 2: 中间件响应日志附加 request_info（middleware.py）
    - 2.1: 在响应日志 `logger.info()` 中添加 `extra={"request_info": {...}}` 字段
    - 2.2: 确认请求入口日志不变，仅响应日志带 extra

- [x] Task 3: Gateway poll/report 降级 DEBUG（gateway.py）
    - 3.1: 将 poll 的 `logger.info` 降级为 `logger.debug`
    - 3.2: 将 report 正常路径的 `logger.info` 降级为 `logger.debug`
    - 3.3: 确认 report 错误路径保留 `logger.error/warning`

- [x] Task 4: 飞书事件日志降级 DEBUG（feishu_ws.py）
    - 4.1: 将 "EVENT RECEIVED" 的 `logger.info` 降级为 `logger.debug`
    - 4.2: 将 event data 日志的 `logger.info` 降级为 `logger.debug`

- [x] Task 5: AgentBay "expected" 降级 DEBUG（agent_tools.py）
    - 5.1: 将 "No DB config found" 的 `logger.warning` 降级为 `logger.debug`

- [x] Task 6: 部署验证
    - 6.1: 重启服务，确认启动无报错
    - 6.2: 检查日志输出：健康检查不再刷屏，正常请求仍记录，第三方库仅 WARNING+
