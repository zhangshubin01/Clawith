# 修改默认响应语言为中文 Spec

## Why
目前系统在 [agent_context.py:581](file:///Users/shubinzhang/Documents/UGit/Clawith/backend/app/services/agent_context.py#L581) 规则是 "Reply in the same language the user uses."。用户需求是：**直接写死默认就是中文**，修改这条规则强制要求模型始终使用中文回复。

## What Changes
- 修改 `backend/app/services/agent_context.py` 中的系统提示词规则
- 将 "Reply in the same language the user uses." 改为强制中文回复指令
- 不需要数据库变更，不需要前端可配置选项，直接改写死

## Impact
- Affected specs: 修改默认响应语言为中文
- Affected code:
  - `backend/app/services/agent_context.py` - 修改系统提示词中的语言规则

## MODIFIED Requirements
### Requirement: 响应语言规则
原有规则：`10. Reply in the same language the user uses.`

修改后：`10. 你必须始终使用中文回复用户，即使用户使用其他语言提问。`

所有模型默认都强制使用中文输出，满足中文用户使用习惯。
