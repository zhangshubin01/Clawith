# Clawith IDEA 插件集成方案 (双端适配计划)

## 概述

本方案包含两个独立但相关的实施计划:
1. **Clawith 服务端适配计划** - 修改后端以支持 IDEA 插件直连
2. **IDEA 插件实现计划** - 在 androidefficiencyplugin 项目中添加 Clawith 插件模块

---

# Part 1: Clawith 服务端适配计划

## Context

### 当前状态
- Clawith 后端已有完整的 REST API 和 WebSocket 聊天接口
- 已有 ACP 协议实现 (`backend/app/plugins/clawith_acp/`),但这是为 JetBrains ACP 瘦客户端设计的
- 用户希望 IDEA 插件**直连后端**,不走 ACP 协议层

### 需要解决的问题
1. **认证方式**: IDEA 插件使用 API Key (`cw-xxx`) 直接认证,无需 JWT
2. **文件操作**: 插件需要读取**本地项目文件**,而非 Agent 工作空间文件
3. **工具调用**: 插件需要执行**本地命令** (ADB, Gradle),而非云端工具
4. **代码 Diff**: 插件需要在**本地 IDE** 中展示 Diff,而非云端审批

### 目标
提供一套轻量级的 API 扩展,使 IDEA 插件能够:
- 通过 API Key 认证并获取智能体列表
- 建立 WebSocket 连接进行流式对话
- 上报本地文件内容和命令执行结果
- 接收代码生成建议并在本地展示 Diff

---

## Implementation Plan

### Phase 1: 新增 IDEA 插件专用 API 路由

#### 1.1 创建新的路由模块

**文件**: `backend/app/api/ide_plugin.py`

**职责**: 提供 IDEA 插件专用的简化 API

**核心端点**:

```python
from fastapi import APIRouter, Depends, HTTPException, Header
from app.core.security import verify_api_key
from app.models.user import User
from app.models.agent import Agent

router = APIRouter(prefix="/api/ide-plugin", tags=["ide-plugin"])

@router.get("/agents")
async def list_agents_for_ide(
    x_api_key: str = Header(..., description="API Key (cw-xxx)")
):
    """获取用户可访问的智能体列表 (简化版,仅返回必要字段)"""
    user = await verify_api_key(x_api_key)
    # 返回精简的 Agent 列表
    return [
        {
            "id": str(agent.id),
            "name": agent.name,
            "avatar_url": agent.avatar_url,
            "role_description": agent.role_description,
            "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None
        }
        for agent in user_accessible_agents
    ]

@router.get("/models")
async def list_models_for_ide(
    x_api_key: str = Header(...)
):
    """获取可用的 LLM 模型列表"""
    user = await verify_api_key(x_api_key)
    # 返回启用的模型列表
    return [
        {
            "id": str(model.id),
            "provider": model.provider,
            "model": model.model,
            "label": model.label,
            "supports_vision": model.supports_vision
        }
        for model in enabled_models
    ]
```

#### 1.2 增强现有 WebSocket 端点

**文件**: `backend/app/api/websocket.py` (修改)

**新增功能**: 支持 IDEA 插件特有的消息类型

```python
# 新增消息类型
class IdePluginMessage(BaseModel):
    type: Literal["text", "image", "file_content", "command_output"]
    content: str
    metadata: dict | None = None

# 在 call_llm 函数中处理特殊消息
async def handle_ide_plugin_message(message: IdePluginMessage):
    if message.type == "file_content":
        # 将文件内容注入上下文
        inject_file_context(message.content, message.metadata)
    elif message.type == "command_output":
        # 将命令输出 (如 logcat) 注入上下文
        inject_command_output(message.content, message.metadata)
```

### Phase 2: 新增工具注册机制

#### 2.1 定义 IDEA 插件可用工具

**文件**: `backend/app/services/ide_plugin_tools.py` (新建)

**职责**: 注册 IDEA 插件可调用的工具 schema

```python
IDE_PLUGIN_TOOLS = [
    {
        "name": "read_local_file",
        "description": "读取用户本地项目中的文件内容",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件相对路径"},
                "limit": {"type": "integer", "description": "最大行数,默认100"}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "execute_adb_command",
        "description": "在用户本地执行 ADB 命令",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "ADB 命令,如 'logcat -d'"},
                "timeout": {"type": "integer", "description": "超时秒数,默认10"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "execute_gradle_task",
        "description": "在用户本地执行 Gradle 任务",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Gradle 任务名,如 'assembleDebug'"}
            },
            "required": ["task"]
        }
    },
    {
        "name": "show_code_diff",
        "description": "在 IDE 中展示代码 Diff 供用户审查",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "original_content": {"type": "string"},
                "new_content": {"type": "string"},
                "change_type": {"type": "string", "enum": ["create", "modify", "delete"]}
            },
            "required": ["file_path", "new_content"]
        }
    }
]
```

#### 2.2 工具调用回调机制

**文件**: `backend/app/api/websocket.py` (修改)

**逻辑**: 当 LLM 调用工具时,通过 WebSocket 发送工具调用请求给 IDEA 插件

```python
# 在 call_llm 的工具循环中
for tool_call in response.tool_calls:
    if tool_call.name in ["read_local_file", "execute_adb_command", ...]:
        # 发送工具调用请求到 IDEA 插件
        await websocket.send_json({
            "type": "tool_call_request",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "arguments": tool_call.arguments
        })
        
        # 等待 IDEA 插件执行并返回结果
        result = await wait_for_tool_result(tool_call.id, timeout=30)
        
        # 将结果传回 LLM
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result)
        })
```

### Phase 3: 会话管理增强

#### 3.1 支持多会话隔离

**文件**: `backend/app/models/chat_session.py` (增强)

**新增字段**:
```python
class ChatSession(Base):
    # ... 现有字段
    
    client_type: str = Field(default="web")  # "web" | "ide_plugin" | "acp"
    project_path: str | None = None  # IDEA 插件上报的项目路径
    current_file: str | None = None  # 当前打开的文件路径
```

#### 3.2 会话上下文持久化

**文件**: `backend/app/services/session_context.py` (新建)

**职责**: 管理 IDEA 插件会话的上下文信息

```python
class SessionContextManager:
    async def update_ide_context(
        self,
        session_id: str,
        project_path: str | None = None,
        current_file: str | None = None,
        android_component: dict | None = None
    ):
        """更新 IDEA 插件会话的上下文"""
        # 存储到 Redis 或数据库
        pass
    
    async def get_ide_context(self, session_id: str) -> dict:
        """获取会话上下文,用于构建 prompt"""
        pass
```

### Phase 4: 权限与安全

#### 4.1 API Key 权限控制

**文件**: `backend/app/core/security.py` (增强)

**新增函数**:
```python
async def verify_ide_plugin_api_key(api_key: str) -> User:
    """验证 IDEA 插件的 API Key"""
    # 检查 API Key 格式 (cw-xxx)
    # 查询数据库验证有效性
    # 返回关联的用户
    pass
```

#### 4.2 工具调用权限审批

**文件**: `backend/app/models/tool_permission.py` (新建)

**表结构**:
```python
class ToolPermission(Base):
    __tablename__ = "tool_permissions"
    
    id: UUID
    user_id: UUID
    tool_name: str  # "execute_adb_command", "read_local_file", etc.
    allowed: bool
    requires_approval: bool  # 是否需要每次确认
```

---

## Verification

### 服务端测试场景

1. **API Key 认证**
   - [ ] 使用有效 API Key 调用 `/api/ide-plugin/agents` 返回 200
   - [ ] 使用无效 API Key 返回 401

2. **WebSocket 连接**
   - [ ] IDEA 插件通过 `ws://host/ws/chat/{agent_id}?token=cw-xxx` 成功连接
   - [ ] 发送文本消息后收到流式响应

3. **工具调用**
   - [ ] LLM 调用 `read_local_file` 时,WebSocket 发送 `tool_call_request`
   - [ ] 收到 IDEA 插件的 `tool_call_result` 后继续对话

4. **会话管理**
   - [ ] 不同 IDEA 插件实例的会话相互隔离
   - [ ] 重启插件后可恢复之前的会话

---

# Part 2: IDEA 插件实现计划

## Context

### 目标项目
`/Users/shubinzhang/Documents/UGit/androidefficiencyplugin`

### 技术栈
- Kotlin 2.3.10 + Java 21
- Gradle 8.9 + IntelliJ Platform Gradle Plugin 2.10.5
- OkHttp 4.12.0 (已存在)
- 目标平台: Android Studio 2025.1.3.7

### 现有基础设施
- 多模块架构 (HttpLibs, UtilsLibs, AntiAssociationPlugin 等)
- 成熟的 ToolWindow 实现模式
- Retrofit + OkHttp 网络层
- PasswordSafe 安全存储
- IntelliJ Platform DSL Builder UI

---

## Architecture

### 模块结构

```
androidefficiencyplugin/
├── ClawithPlugin/                    # 新建独立模块
│   ├── build.gradle.kts
│   └── src/main/kotlin/com/clawith/plugin/
│       ├── ClawithPluginInitializer.kt    # 插件入口 (可选,主模块已注册)
│       ├── service/
│       │   ├── AuthService.kt             # API Key 管理
│       │   ├── ClawithApiService.kt       # HTTP + WebSocket 客户端
│       │   ├── ChatService.kt             # 聊天业务逻辑
│       │   ├── FileSystemService.kt       # 本地文件系统操作
│       │   ├── DiffReviewService.kt       # Diff 审查管理
│       │   ├── CommandLineService.kt      # ADB/Gradle 命令执行
│       │   └── AndroidContextService.kt   # Android 上下文提取
│       ├── ui/
│       │   ├── ClawithToolWindowFactory.kt  # ToolWindow 工厂
│       │   ├── ClawithChatPanel.kt          # 主聊天界面
│       │   ├── CodeDiffDialog.kt            # Diff 审查对话框
│       │   ├── MentionCompletionProvider.kt # @ 文件自动补全
│       │   └── ClawithSettingsConfigurable.kt # 设置页面
│       ├── model/
│       │   ├── ChatMessage.kt               # 消息数据模型
│       │   ├── AgentInfo.kt                 # 智能体信息
│       │   ├── LLMModel.kt                  # 模型信息
│       │   ├── CodeDiff.kt                  # Diff 数据模型
│       │   └── WebSocketMessages.kt         # WS 消息协议
│       └── util/
│           ├── JsonUtils.kt                 # JSON 序列化辅助
│           └── ImageUtils.kt                # 图片编码工具
```

---

## Implementation Plan

### Phase 1: 模块基础搭建 (Week 1)

#### 1.1 创建模块结构

**步骤**:
1. 在项目根目录创建 `ClawithPlugin/` 文件夹
2. 创建 `src/main/kotlin/com/clawith/plugin/` 目录结构
3. 创建 `build.gradle.kts`

**文件**: `ClawithPlugin/build.gradle.kts`

```kotlin
plugins {
    kotlin("jvm")
}

dependencies {
    // 复用现有模块
    implementation(project(":HttpLibs"))
    implementation(project(":UtilsLibs"))
    
    // WebSocket 支持 (OkHttp 已包含)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    
    // JSON 序列化
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.0")
    
    // 协程
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-swing:1.7.3")
    
    // IntelliJ Platform API (由主项目提供)
    compileOnly(intellijPlatform.androidStudio(providers.gradleProperty("AndroidStudioVersion")))
}

kotlin {
    jvmToolchain(21)
}
```

#### 1.2 更新构建配置

**文件**: `settings.gradle.kts` (修改)

```kotlin
include("ClawithPlugin")  // 新增这一行
```

**文件**: `build.gradle.kts` (根目录,修改)

```kotlin
dependencies {
    // ... 现有依赖
    implementation(project(":ClawithPlugin"))  // 新增
}
```

#### 1.3 注册 ToolWindow

**文件**: `src/main/resources/META-INF/plugin.xml` (修改)

```xml
<extensions defaultExtensionNs="com.intellij">
    <!-- 现有扩展... -->
    
    <!-- 新增 Clawith ToolWindow -->
    <toolWindow 
        factoryClass="com.clawith.plugin.ui.ClawithToolWindowFactory"
        id="Clawith AI助手"
        icon="/icons/clawith.svg"
        anchor="right"/>
    
    <!-- 新增设置页面 -->
    <applicationConfigurable 
        displayName="Clawith 配置"
        id="com.clawith.plugin.ui.ClawithSettingsConfigurable"
        instance="com.clawith.plugin.ui.ClawithSettingsConfigurable"/>
</extensions>
```

---

### Phase 2: 认证与配置 (Week 1-2)

#### 2.1 认证服务

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/service/AuthService.kt`

```kotlin
package com.clawith.plugin.service

import com.intellij.credentialStore.CredentialAttributes
import com.intellij.credentialStore.Credentials
import com.intellij.ide.passwordSafe.PasswordSafe
import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.project.Project

@State(name = "ClawithAuth", storages = [Storage("clawith-auth.xml")])
class AuthService : PersistentStateComponent<AuthService.State> {
    
    data class State(
        var serverUrl: String = "http://localhost:8000",
        var apiKey: String? = null,
        var lastValidated: Long = 0
    )
    
    private var state = State()
    
    companion object {
        private const val CREDENTIAL_KEY = "clawith_api_key"
    }
    
    fun getState(): State = state
    
    fun loadState(state: State) {
        this.state = state
    }
    
    /**
     * 获取 API Key (从 PasswordSafe)
     */
    fun getApiKey(): String? {
        return state.apiKey ?: run {
            val credentialAttributes = createCredentialAttributes()
            val credentials = PasswordSafe.instance.get(credentialAttributes)
            credentials?.getPasswordAsString()
        }
    }
    
    /**
     * 保存 API Key (到 PasswordSafe)
     */
    fun saveApiKey(apiKey: String) {
        state.apiKey = apiKey
        val credentialAttributes = createCredentialAttributes()
        val credentials = Credentials(null, apiKey)
        PasswordSafe.instance.set(credentialAttributes, credentials)
    }
    
    /**
     * 获取服务器 URL
     */
    fun getServerUrl(): String = state.serverUrl
    
    /**
     * 更新服务器配置
     */
    fun updateServerUrl(url: String) {
        state.serverUrl = url
    }
    
    /**
     * 验证 API Key 有效性
     */
    suspend fun validateApiKey(): Boolean {
        return try {
            val apiService = ClawithApiService(this)
            val agents = apiService.getAgents()
            state.lastValidated = System.currentTimeMillis()
            agents.isNotEmpty()
        } catch (e: Exception) {
            false
        }
    }
    
    private fun createCredentialAttributes(): CredentialAttributes {
        return CredentialAttributes(CREDENTIAL_KEY)
    }
}
```

#### 2.2 设置页面

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/ui/ClawithSettingsConfigurable.kt`

```kotlin
package com.clawith.plugin.ui

import com.clawith.plugin.service.AuthService
import com.intellij.openapi.options.Configurable
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.panel
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch
import javax.swing.JComponent
import javax.swing.JPanel

class ClawithSettingsConfigurable(private val project: Project) : Configurable {
    
    private val authService = project.getService(AuthService::class.java)
    
    private lateinit var serverUrlField: JBTextField
    private lateinit var apiKeyField: JBTextField
    
    override fun getDisplayName(): String = "Clawith 配置"
    
    override fun createComponent(): JComponent {
        return panel {
            row("服务器地址:") {
                serverUrlField = textField()
                    .align(AlignX.FILL)
                    .component
                serverUrlField.text = authService.getServerUrl()
            }
            
            row("API Key:") {
                apiKeyField = passwordField()
                    .align(AlignX.FILL)
                    .component
                apiKeyField.text = authService.getApiKey() ?: ""
            }
            
            row {
                button("测试连接") {
                    testConnection()
                }
            }
            
            row {
                label("提示: API Key 格式为 cw-xxx,可在 Clawith Web 端生成")
                    .comment()
            }
        }
    }
    
    private fun testConnection() {
        val serverUrl = serverUrlField.text
        val apiKey = apiKeyField.text
        
        if (serverUrl.isBlank() || apiKey.isBlank()) {
            Messages.showErrorDialog(project, "请填写服务器地址和 API Key", "配置错误")
            return
        }
        
        authService.updateServerUrl(serverUrl)
        
        GlobalScope.launch {
            val success = authService.validateApiKey()
            
            com.intellij.openapi.application.ApplicationManager.getApplication().invokeLater {
                if (success) {
                    Messages.showInfoMessage(project, "连接成功!", "测试成功")
                } else {
                    Messages.showErrorDialog(project, "连接失败,请检查服务器地址和 API Key", "测试失败")
                }
            }
        }
    }
    
    override fun isModified(): Boolean {
        return serverUrlField.text != authService.getServerUrl() ||
               apiKeyField.text != (authService.getApiKey() ?: "")
    }
    
    override fun apply() {
        authService.updateServerUrl(serverUrlField.text)
        authService.saveApiKey(apiKeyField.text)
    }
    
    override fun reset() {
        serverUrlField.text = authService.getServerUrl()
        apiKeyField.text = authService.getApiKey() ?: ""
    }
}
```

---

### Phase 3: 网络层实现 (Week 2)

#### 3.1 API 服务客户端

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/service/ClawithApiService.kt`

```kotlin
package com.clawith.plugin.service

import com.clawith.plugin.model.AgentInfo
import com.clawith.plugin.model.LLMModelInfo
import kotlinx.serialization.json.Json
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

class ClawithApiService(private val authService: AuthService) {
    
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .addInterceptor { chain ->
            val request = chain.request().newBuilder()
                .addHeader("X-API-Key", authService.getApiKey() ?: "")
                .addHeader("Content-Type", "application/json")
                .build()
            chain.proceed(request)
        }
        .build()
    
    private val webSocketClient = httpClient.newBuilder()
        .pingInterval(30, TimeUnit.SECONDS)
        .build()
    
    private val json = Json { ignoreUnknownKeys = true }
    
    /**
     * 获取智能体列表
     */
    suspend fun getAgents(): List<AgentInfo> {
        val url = "${authService.getServerUrl()}/api/ide-plugin/agents"
        val request = Request.Builder().url(url).get().build()
        
        return httpClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IOException("Unexpected code $response")
            }
            val body = response.body?.string() ?: "[]"
            json.decodeFromString<List<AgentInfo>>(body)
        }
    }
    
    /**
     * 获取 LLM 模型列表
     */
    suspend fun getLLMModels(): List<LLMModelInfo> {
        val url = "${authService.getServerUrl()}/api/ide-plugin/models"
        val request = Request.Builder().url(url).get().build()
        
        return httpClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IOException("Unexpected code $response")
            }
            val body = response.body?.string() ?: "[]"
            json.decodeFromString<List<LLMModelInfo>>(body)
        }
    }
    
    /**
     * 连接 WebSocket 聊天
     */
    fun connectWebSocket(
        agentId: String,
        onMessage: (String) -> Unit,
        onError: (Throwable) -> Unit,
        onClosed: (Int, String) -> Unit
    ): WebSocket {
        val wsUrl = authService.getServerUrl()
            .replace("http://", "ws://")
            .replace("https://", "wss://")
        val fullUrl = "$wsUrl/ws/chat/$agentId?token=${authService.getApiKey()}"
        
        val request = Request.Builder().url(fullUrl).build()
        
        return webSocketClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                println("[Clawith] WebSocket connected")
            }
            
            override fun onMessage(webSocket: WebSocket, text: String) {
                onMessage(text)
            }
            
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                println("[Clawith] WebSocket error: ${t.message}")
                onError(t)
            }
            
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                println("[Clawith] WebSocket closed: $reason")
                onClosed(code, reason)
            }
        })
    }
    
    /**
     * 发送消息到 WebSocket
     */
    fun sendMessage(webSocket: WebSocket, content: String, sessionId: String? = null) {
        val message = mapOf(
            "content" to content,
            "session_id" to (sessionId ?: "")
        )
        val jsonStr = json.encodeToString(MapSerializer(String.serializer(), AnySerializer()), message)
        webSocket.send(jsonStr)
    }
}
```

#### 3.2 数据模型

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/model/AgentInfo.kt`

```kotlin
package com.clawith.plugin.model

import kotlinx.serialization.Serializable

@Serializable
data class AgentInfo(
    val id: String,
    val name: String,
    val avatar_url: String? = null,
    val role_description: String = "",
    val primary_model_id: String? = null
)
```

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/model/LLMModel.kt`

```kotlin
package com.clawith.plugin.model

import kotlinx.serialization.Serializable

@Serializable
data class LLMModelInfo(
    val id: String,
    val provider: String,
    val model: String,
    val label: String,
    val supports_vision: Boolean = false
)
```

---

### Phase 4: 聊天界面核心 (Week 3-4)

#### 4.1 ToolWindow 工厂

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/ui/ClawithToolWindowFactory.kt`

```kotlin
package com.clawith.plugin.ui

import com.clawith.plugin.service.AuthService
import com.clawith.plugin.service.ClawithApiService
import com.clawith.plugin.service.ChatService
import com.clawith.plugin.service.FileSystemService
import com.clawith.plugin.service.AndroidContextService
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory

class ClawithToolWindowFactory : ToolWindowFactory {
    
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val authService = project.getService(AuthService::class.java)
        val apiService = ClawithApiService(authService)
        val fileSystemService = FileSystemService(project)
        val androidContextService = AndroidContextService(project)
        val chatService = ChatService(apiService, fileSystemService, androidContextService)
        
        val chatPanel = ClawithChatPanel(project, chatService, apiService)
        
        val contentFactory = ContentFactory.getInstance()
        val content = contentFactory.createContent(chatPanel, "", false)
        toolWindow.contentManager.addContent(content)
    }
}
```

#### 4.2 主聊天面板

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/ui/ClawithChatPanel.kt`

```kotlin
package com.clawith.plugin.ui

import com.clawith.plugin.model.AgentInfo
import com.clawith.plugin.model.LLMModelInfo
import com.clawith.plugin.service.ClawithApiService
import com.clawith.plugin.service.ChatService
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.ComboBox
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.dsl.builder.Align
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.panel
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch
import javax.swing.JButton
import javax.swing.JPanel

class ClawithChatPanel(
    private val project: Project,
    private val chatService: ChatService,
    private val apiService: ClawithApiService
) : JPanel() {
    
    private val agentComboBox = ComboBox<AgentInfo>()
    private val modelComboBox = ComboBox<LLMModelInfo>()
    private val messageListPanel = JPanel()  // TODO: 使用 JBList
    private val inputTextArea = JBTextArea()
    private val sendButton = JButton("发送")
    private val attachImageButton = JButton("📎")
    
    init {
        setupUI()
        loadAgentsAndModels()
        setupEventListeners()
    }
    
    private fun setupUI() {
        layout = null  // 使用绝对布局或 MigLayout
        
        add(panel {
            row {
                cell(agentComboBox).align(AlignX.FILL)
                cell(modelComboBox).align(AlignX.FILL)
            }
            
            row {
                scrollCell(JBScrollPane(messageListPanel)).align(Align.FILL)
            }
            
            row {
                cell(inputTextArea).align(AlignX.FILL)
                cell(sendButton)
                cell(attachImageButton)
            }
        })
    }
    
    private fun loadAgentsAndModels() {
        GlobalScope.launch {
            try {
                val agents = apiService.getAgents()
                val models = apiService.getLLMModels()
                
                com.intellij.openapi.application.ApplicationManager.getApplication().invokeLater {
                    agents.forEach { agentComboBox.addItem(it) }
                    models.forEach { modelComboBox.addItem(it) }
                }
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }
    }
    
    private fun setupEventListeners() {
        sendButton.addActionListener {
            val content = inputTextArea.text
            if (content.isNotBlank()) {
                inputTextArea.text = ""
                
                GlobalScope.launch {
                    chatService.sendMessage(
                        content = content,
                        agentId = (agentComboBox.selectedItem as? AgentInfo)?.id ?: "",
                        onChunk = { chunk ->
                            // 更新 UI: 打字机效果
                        },
                        onComplete = { fullResponse ->
                            // 处理完整响应
                        }
                    )
                }
            }
        }
    }
}
```

---

### Phase 5: 文件系统与 @ 引用 (Week 5)

#### 5.1 文件系统服务

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/service/FileSystemService.kt`

```kotlin
package com.clawith.plugin.service

import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile

class FileSystemService(private val project: Project) {
    
    /**
     * 读取文件内容
     */
    fun readFileContent(filePath: String): String? {
        val virtualFile = LocalFileSystem.getInstance().findFileByPath(filePath)
            ?: return null
        
        return try {
            virtualFile.contentsToByteArray().toString(Charsets.UTF_8)
        } catch (e: Exception) {
            e.printStackTrace()
            null
        }
    }
    
    /**
     * 解析 @ 引用
     */
    fun parseMentions(message: String): List<FileReference> {
        val pattern = Regex("@([\\w./-]+)")
        return pattern.findAll(message).mapNotNull { match ->
            val filePath = match.groupValues[1]
            val content = readFileContent(resolveFilePath(filePath))
            if (content != null) FileReference(filePath, content) else null
        }.toList()
    }
    
    /**
     * 格式化文件引用为 Prompt
     */
    fun formatFileReferences(files: List<FileReference>): String {
        return files.joinToString("\n\n") { ref ->
            """
            --- BEGIN FILE: ${ref.path} ---
            ${ref.content}
            --- END FILE: ${ref.path} ---
            """.trimIndent()
        }
    }
    
    private fun resolveFilePath(relativePath: String): String {
        val basePath = project.basePath ?: return relativePath
        return "$basePath/$relativePath"
    }
}

data class FileReference(val path: String, val content: String)
```

---

### Phase 6: Diff Review 系统 (Week 6-7)

#### 6.1 Diff 管理服务

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/service/DiffReviewService.kt`

```kotlin
package com.clawith.plugin.service

import com.clawith.plugin.model.CodeDiff
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VfsUtil

class DiffReviewService(private val project: Project) {
    
    /**
     * 解析响应中的代码块
     */
    fun parseCodeBlocksFromResponse(response: String): List<CodeDiff> {
        val pattern = Regex("```(?:diff|code):(.+?)\n(.*?)```", RegexOption.DOT_MATCHES_ALL)
        return pattern.findAll(response).mapNotNull { match ->
            val filePath = match.groupValues[1].trim()
            val newContent = match.groupValues[2].trim()
            val originalContent = FileSystemService(project).readFileContent(filePath)
            val changeType = if (originalContent == null) ChangeType.CREATE else ChangeType.MODIFY
            
            CodeDiff(filePath, originalContent, newContent, changeType)
        }.toList()
    }
    
    /**
     * 应用更改
     */
    fun applyChanges(diffs: List<CodeDiff>): Boolean {
        return try {
            WriteCommandAction.runWriteCommandAction(project) {
                diffs.forEach { diff ->
                    when (diff.changeType) {
                        ChangeType.CREATE, ChangeType.MODIFY -> {
                            val file = LocalFileSystem.getInstance().findFileByPath(diff.filePath)
                            if (file != null) {
                                VfsUtil.saveText(file, diff.newContent)
                            }
                        }
                        ChangeType.DELETE -> {
                            val file = LocalFileSystem.getInstance().findFileByPath(diff.filePath)
                            file?.delete(this)
                        }
                    }
                }
            }
            true
        } catch (e: Exception) {
            e.printStackTrace()
            false
        }
    }
}

enum class ChangeType { CREATE, MODIFY, DELETE }
```

---

### Phase 7: Android 辅助功能 (Week 8)

#### 7.1 Android 上下文服务

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/service/AndroidContextService.kt`

```kotlin
package com.clawith.plugin.service

import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.project.Project

class AndroidContextService(private val project: Project) {
    
    /**
     * 获取当前 Android 上下文
     */
    fun getCurrentAndroidContext(): String {
        val editor = FileEditorManager.getInstance(project).selectedTextEditor ?: return ""
        val file = editor.virtualFile ?: return ""
        
        return when {
            file.name.endsWith("Activity.kt") || file.name.endsWith("Activity.java") -> {
                "Currently editing Activity: ${file.nameWithoutExtension}"
            }
            file.name.endsWith("Fragment.kt") || file.name.endsWith("Fragment.java") -> {
                "Currently editing Fragment: ${file.nameWithoutExtension}"
            }
            else -> ""
        }
    }
    
    /**
     * 构建 Android 上下文 Prompt
     */
    fun buildAndroidContextPrompt(): String {
        val context = getCurrentAndroidContext()
        return if (context.isNotEmpty()) {
            "## Android Context\n$context\n"
        } else {
            ""
        }
    }
}
```

#### 7.2 命令行服务

**文件**: `ClawithPlugin/src/main/kotlin/com/clawith/plugin/service/CommandLineService.kt`

```kotlin
package com.clawith.plugin.service

import com.intellij.openapi.project.Project
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.TimeUnit

class CommandLineService(private val project: Project) {
    
    /**
     * 执行 ADB logcat
     */
    suspend fun executeAdbLogcat(filter: String = "", maxLines: Int = 100): String {
        return withContext(Dispatchers.IO) {
            try {
                val command = listOf("adb", "logcat", "-d", filter)
                val process = ProcessBuilder(command)
                    .directory(project.basePath?.let { File(it) })
                    .redirectErrorStream(true)
                    .start()
                
                val output = process.inputStream.bufferedReader().readText()
                process.waitFor(5, TimeUnit.SECONDS)
                
                output.lines().takeLast(maxLines).joinToString("\n")
            } catch (e: Exception) {
                "Error executing adb: ${e.message}"
            }
        }
    }
}
```

---

## File Structure Summary

```
ClawithPlugin/
├── build.gradle.kts
└── src/main/kotlin/com/clawith/plugin/
    ├── service/
    │   ├── AuthService.kt              ✅ Phase 2
    │   ├── ClawithApiService.kt        ✅ Phase 3
    │   ├── ChatService.kt              ⏳ Phase 4
    │   ├── FileSystemService.kt        ✅ Phase 5
    │   ├── DiffReviewService.kt        ✅ Phase 6
    │   ├── CommandLineService.kt       ✅ Phase 7
    │   └── AndroidContextService.kt    ✅ Phase 7
    ├── ui/
    │   ├── ClawithToolWindowFactory.kt ✅ Phase 4
    │   ├── ClawithChatPanel.kt         ⏳ Phase 4
    │   ├── CodeDiffDialog.kt           ⏳ Phase 6
    │   ├── MentionCompletionProvider.kt ⏳ Phase 5
    │   └── ClawithSettingsConfigurable.kt ✅ Phase 2
    ├── model/
    │   ├── ChatMessage.kt              ⏳ Phase 4
    │   ├── AgentInfo.kt                ✅ Phase 3
    │   ├── LLMModel.kt                 ✅ Phase 3
    │   ├── CodeDiff.kt                 ⏳ Phase 6
    │   └── WebSocketMessages.kt        ⏳ Phase 4
    └── util/
        ├── JsonUtils.kt                ⏳ Phase 3
        └── ImageUtils.kt               ⏳ Phase 4
```

---

## Verification

### 插件测试场景

1. **配置与认证**
   - [ ] 在设置页面输入 API Key 和服务器地址
   - [ ] 点击"测试连接"显示成功
   - [ ] 重启 IDE 后配置保留

2. **聊天功能**
   - [ ] ToolWindow 显示智能体和模型下拉框
   - [ ] 发送文本消息后收到流式响应
   - [ ] 输入 `@` 触发文件自动补全

3. **文件操作**
   - [ ] @ 文件后,文件内容注入到消息中
   - [ ] 智能体能正确回答关于文件的问题

4. **Diff Review**
   - [ ] 智能体生成代码修改建议
   - [ ] 弹出 Diff 对话框显示变更
   - [ ] 点击"接受"后文件被修改

5. **Android 辅助**
   - [ ] 打开 Activity 文件时,上下文包含组件信息
   - [ ] 点击"获取 Logcat"按钮,执行 ADB 命令

---

## Timeline

| Phase | 周次 | 主要任务 | 交付物 |
|-------|------|---------|--------|
| Phase 1 | Week 1 | 模块基础搭建 | 模块结构、构建配置 |
| Phase 2 | Week 1-2 | 认证与配置 | AuthService, Settings UI |
| Phase 3 | Week 2 | 网络层实现 | ClawithApiService, 数据模型 |
| Phase 4 | Week 3-4 | 聊天界面核心 | ToolWindow, ChatPanel, ChatService |
| Phase 5 | Week 5 | 文件系统与 @ 引用 | FileSystemService, MentionCompletion |
| Phase 6 | Week 6-7 | Diff Review 系统 | DiffReviewService, CodeDiffDialog |
| Phase 7 | Week 8 | Android 辅助功能 | AndroidContextService, CommandLineService |

**总计**: 8 周完成 MVP 版本

---

## Next Steps

1. **立即开始**: 创建 `ClawithPlugin/` 模块结构和 `build.gradle.kts`
2. **优先实现**: Phase 2 (认证) + Phase 3 (网络层) - 这是后续所有功能的基础
3. **并行开发**: Phase 4 (UI) 可以在 Phase 3 完成后立即开始
4. **服务端配合**: 同步进行 Part 1 的服务端适配工作
