"""clawith-lsp4j - LSP4J WebSocket bridge for Tongyi Lingma IDE plugin.

将开源通义灵码 JetBrains IDE 插件适配连接到 Clawith AI 后端。
基于 LSP Base Protocol + JSON-RPC 2.0，与现有 ACP 插件共存。

已实现的协议方法：
- 生命周期: initialize, shutdown, exit
- 聊天: chat/ask, chat/stop, chat/codeChange/apply
- 工具: tool/call/approve, tool/invokeResult, tool/call/results
- 配置: config/getEndpoint, config/updateEndpoint
- 图片: image/upload
- Commit: commitMsg/generate
- 会话: session/title/update (通知)
- 进度: chat/process_step_callback (通知), tool/call/sync (通知)
- 步骤确认: agents/testAgent/stepProcessConfirm

Stub 方法（返回空响应，避免 Method not found）：
- chat/: systemEvent, getStage, replyRequest, like, stopSession, receive/notice,
  quota/doNotRemindAgain, listAllSessions, getSessionById, deleteSessionById,
  clearAllSessions, deleteChatById
- LanguageServer: config/getGlobal, config/queryModels, ping, ide/update,
  dataPolicy/query, dataPolicy/sign, dataPolicy/cancel, auth/profile/getUrl,
  auth/profile/update, extension/query, extension/contextProvider/loadComboBoxItems,
  codebase/recommendation, kb/list, model/queryClasses, model/getByokConfig,
  model/checkByokConfig, user/plan, webview/command/list
- AuthService: auth/login, auth/status, auth/logout, auth/grantInfos,
  auth/grantInfosWrap, auth/switchAccount
- LoginService: login/generateUrl
- FeedbackService: feedback/submit
- SnapshotService: snapshot/listBySession, snapshot/operate
- WorkingSpaceFileService: workingSpaceFile/operate, listBySnapshot,
  getLastStableContent, getFullContent, updateContent
- SessionService: session/getCurrent
- SystemService: system/reportDiagnosisLog
- SnippetService: snippet/search, snippet/report
- TextDocumentService: textDocument/inlineEdit, textDocument/editPredict
"""

from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI
from app.plugins.base import ClawithPlugin

from .router import router
from .tool_hooks import install_lsp4j_tool_hooks


class ClawithLsp4jPlugin(ClawithPlugin):
    name: ClassVar[str] = "clawith-lsp4j"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = "LSP4J WebSocket bridge for Tongyi Lingma IDE plugin"

    def register(self, app: FastAPI) -> None:
        """注册 WebSocket 路由和工具钩子。

        工具钩子必须在 register() 中安装（晚于 ACP 的模块级导入安装），
        以保证获取到 ACP 已安装的 _custom_execute_tool / _custom_get_tools 引用。
        """
        # 安装 LSP4J 工具钩子（扩展 ACP 的执行路径和注册路径）
        install_lsp4j_tool_hooks()

        # 注册 WebSocket 路由
        app.include_router(router, prefix="/api/plugins/clawith-lsp4j", tags=["lsp4j"])


plugin = ClawithLsp4jPlugin()
