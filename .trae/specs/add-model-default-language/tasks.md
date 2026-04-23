# Tasks

- [x] 任务 1: 修改 agent_context.py，将语言规则从 "Reply in the same language the user uses." 改为强制中文回复
  - 直接修改系统提示词中的规则
  - 改为："10. 你必须始终使用中文回复用户，即使用户使用其他语言提问。"
  - 不需要数据库变更，不需要前端配置，直接改写死
