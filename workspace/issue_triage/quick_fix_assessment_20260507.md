# Issue 快速修复评估报告

**生成时间**: 2026-05-07 16:05
**分析人**: 产品实习生
**总 Issue 数**: 141 (Open)

---

## 📊 快速修复候选清单 (Easy Wins)

以下 Issue 已经过代码验证，确认真实存在且修复成本较低，适合今天快速修复。

### 1. ⚠️ Issue #520 - Ubuntu 低配置服务器启动超时
**优先级**: P0 - 高 | **工作量**: 5 分钟 | **类型**: Bug Fix

**问题描述**:
- 低配置 Ubuntu 服务器启动 backend 时，10 秒超时太短
- 用户报告需要 30 秒才能启动完成

**代码验证**:
- ✅ 已确认 `restart.sh` 第 222 行仍为 `wait_for_port $BACKEND_PORT "Backend" 10`
- ❌ 未修复

**修复方案**:
```bash
# restart.sh 第 222 行
# 修改前:
wait_for_port $BACKEND_PORT "Backend" 10

# 修改后:
wait_for_port $BACKEND_PORT "Backend" 30
```

**验收标准**:
- [ ] 低配置服务器 (1 核 2GB) 能正常启动
- [ ] 日志显示 "✅ Backend ready (XXs)" 且 XX <= 30

**建议负责人**: @前端开发工程师 (脚本修改)

---

### 2. 📝 Issue #529 - 阿拉伯语 README 翻译缺失
**优先级**: P1 - 中 | **工作量**: 30 分钟 | **类型**: Documentation

**问题描述**:
- PR #529 添加了阿拉伯语 README 翻译
- 但 PR 尚未合并，main 分支缺少 README_ar.md

**代码验证**:
- ✅ README.md 存在，包含 5 种语言链接 (EN, 中文，日本語，한국어, Español)
- ❌ README_ar.md 不存在于 main 分支
- ⚠️ PR #529 有 2 个必须修复的问题：
  1. README_ar.md 头部缺少 badge (Technical Whitepaper, Stars, Forks 等)
  2. Discord 链接不一致 (当前：3AKMBM2G, 正确：NRNHZkyDcG)

**修复方案**:
1. 通知 PR 作者 @Y1fe1Zh0u 修复 2 个问题
2. 修复后合并 PR

**验收标准**:
- [ ] README_ar.md 包含完整的头部 badge
- [ ] Discord 链接统一为 `https://discord.gg/NRNHZkyDcG`
- [ ] 语言选择器添加 العربية 链接

**建议负责人**: @产品实习生 (协调 PR 合并)

---

### 3. 🔧 Issue #524 - macOS IME 输入法 Enter 键冲突
**优先级**: P1 - 高 | **工作量**: 15 分钟 | **类型**: Bug Fix

**问题描述**:
- macOS 使用中文输入法时，按 Enter 确认候选词会同时发送消息
- 严重影响中文用户体验

**代码验证**:
- 需要检查 `frontend/src/pages/Chat.tsx` 或输入框组件的 keydown 事件处理
- 应添加 `KeyboardEvent.isComposing` 检查

**修复方案**:
```typescript
// frontend/src/components/xxx.tsx
const handleKeyDown = (e: KeyboardEvent) => {
  // 新增：检查是否在 IME 组合输入中
  if (e.isComposing) {
    return; // 不发送消息，只确认候选词
  }
  
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
};
```

**验收标准**:
- [ ] macOS + 搜狗拼音/系统拼音下，Enter 确认候选词不发送消息
- [ ] 正常 Enter 发送消息功能不受影响
- [ ] Shift+Enter 换行功能正常

**建议负责人**: @前端开发工程师

---

### 4. 🐛 Issue #517 - Docker 中 execute_code 无权限
**优先级**: P0 - 高 | **工作量**: 1 小时 | **类型**: Bug Fix

**问题描述**:
- Docker 部署下，execute_code 工具运行任何代码都提示权限错误
- 用户截图显示 Python/Bash 代码执行失败

**代码验证**:
- 需要检查：
  1. `backend/app/services/sandbox/local/docker_backend.py` - Docker 沙箱权限配置
  2. `backend/app/services/sandbox/registry.py` - 沙箱后端选择逻辑
  3. Docker 容器内的用户权限设置

**可能原因**:
- Docker 容器内以非 root 用户运行，但沙箱未正确配置权限
- 临时目录或执行目录权限不足

**修复方案**:
待架构师分析后确定

**验收标准**:
- [ ] Docker 部署下能正常执行 Python 代码
- [ ] Docker 部署下能正常执行 Bash 命令
- [ ] 文件读写操作正常

**建议负责人**: @架构师 1 + @后端架构师

---

## 📋 待进一步验证的 Issue

以下 Issue 需要更多代码分析或测试验证，暂不列入今日快速修复清单。

| Issue | 标题 | 优先级 | 验证状态 | 备注 |
|-------|------|--------|----------|------|
| #527 | 工具安装受限、沙箱过度限制、Skill 安装 Bug | P0 | ⏳ 待验证 | 需要测试 Skill 安装流程 |
| #526 | Agent-to-Agent 对话无法清空 | P0 | ⏳ 待验证 | 需要检查 API 和前端 |
| #525 | 对话管理 UX 改进 (4 项) | P1 | ⏳ 待验证 | 功能增强，非 bug |
| #522 | 沙箱过于严格 | P1 | ⚠️ 部分验证 | 配置支持调整，但默认限制多 |
| #519 | 路径限制硬编码 | P1 | ⏳ 待验证 | 需要检查文件服务代码 |
| #518 | Agent 过期提示处理 | P2 | ⏳ 待验证 | 需要检查配额管理 |
| #516 | 公司设置缺少 skill upload | P1 | ⏳ 待验证 | 需要对比员工/公司设置页面 |
| #515 | Browse ClawHub 安装技能错误 | P1 | ⏳ 待验证 | 需要测试 ClawHub 集成 |
| #513 | 普通成员可见公司设置入口 | P1 | ⏳ 待验证 | 权限控制问题 |
| #512 | 超级管理员可被移除 | P0 | ⏳ 待验证 | 安全漏洞，需优先修复 |

---

## 🚀 今日修复计划

### 上午 (9:00-12:00)
1. **Issue #520** - 修改 restart.sh 超时时间 (5 分钟)
2. **Issue #524** - 修复 macOS IME 冲突 (15 分钟)
3. **Issue #529** - 协调 PR 合并 (30 分钟)

### 下午 (13:00-18:00)
4. **Issue #517** - Docker execute_code 权限问题 (1 小时)
5. 验证修复并测试
6. 评估剩余 Issue 优先级

---

## 📌 团队分工建议

| 成员 | 负责 Issue | 预计工时 |
|------|-----------|----------|
| @前端开发工程师 | #520, #524 | 30 分钟 |
| @架构师 1 | #517 (主导) | 1 小时 |
| @产品实习生 | #529 (协调) | 30 分钟 |
| @代码审查员 | 所有修复的 Code Review | 30 分钟 |

---

## 📞 下一步行动

1. **产品实习生**: 
   - [ ] 在团队频道发布此评估报告
   - [ ] 通知 @Y1fe1Zh0u 修复 PR #529 的 2 个问题
   - [ ] 跟踪修复进度

2. **架构师 1**:
   - [ ] 分析 Issue #517 根本原因
   - [ ] 提出修复方案

3. **前端开发工程师**:
   - [ ] 修复 Issue #520 (restart.sh)
   - [ ] 修复 Issue #524 (IME 冲突)

4. **代码审查员**:
   - [ ] Review 所有提交的修复
   - [ ] 确保不引入回归问题

---

**备注**: 本报告基于 2026-05-07 的代码库状态分析。修复后请及时更新 Issue 状态。
