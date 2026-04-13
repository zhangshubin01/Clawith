# Clawith IDEA 插件实现方案

## Context

### 背景与需求
用户需要开发一个对接 Clawith 智能体的 IDEA 自定义插件,用于辅助 Android 开发。该插件需要:
1. 支持选择和切换智能体、LLM 模型
2. 提供完整的聊天界面,支持文本、图片多模态消息
3. 通过 @ 符号引用本地文件和文件夹,自动注入上下文
4. 显示智能体生成代码的 Diff,支持用户审查和接受/拒绝
5. 执行 ADB logcat 读取 Android 设备日志并发送给智能体分析
6. 感知当前打开的 Android 组件(Activity/Fragment/ViewModel)作为上下文
7. 直连 Clawith 后端 API (非 ACP 协议),支持 API Key 配置
8. 参考通义灵码插件的交互体验

### 技术基础
- **Clawith 后端**: FastAPI + WebSocket,提供 `/api/agents/`, `/ws/chat/{agent_id}`, `/api/llm-models/` 等接口
- **现有插件**: androidefficiencyplugin 已具备 ToolWindow、文件操作、网络请求(Retrofit+OkHttp)、配置持久化等基础设施
- **目标平台**: Android Studio 2025.1.3.7 (Kotlin 2.3, Java 21)

---

## Architecture

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Clawith IDEA Plugin                       │
├─────────────────────────────────────────────────────────────┤
│  UI Layer                                                    │
│  ├─ ClawithToolWindowFactory (ToolWindow 入口)              │
│  ├─ ClawithChatPanel (主聊天界面)                           │
│  ├─ CodeDiffDialog (Diff Review 对话框)                     │
│  ├─ MentionCompletionProvider (@ 文件自动补全)              │
│  └─ ClawithSettingsConfigurable (设置页面)                  │
├─────────────────────────────────────────────────────────────┤
│  Service Layer                                               │
│  ├─ AuthService (API Key 管理, PasswordSafe)                │
│  ├─ ClawithApiService (HTTP + WebSocket 客户端)             │
│  ├─ AgentModelService (智能体/模型选择与缓存)               │
│  ├─ ChatService (消息处理、流式响应、工具调用)              │
│  ├─ FileSystemService (VFS 集成,@ 引用解析)                 │
│  ├─ DiffReviewService (IntelliJ Diff API 封装)              │
│  ├─ CommandLineService (ADB/Gradle 命令执行)                │
│  └─ AndroidContextService (Android 项目上下文提取)          │
├─────────────────────────────────────────────────────────────┤
│  Data Layer                                                  │
│  ├─ Models (ChatMessage, AgentInfo, LLMModel, CodeDiff)     │
│  ├─ State Management (PersistentStateComponent + StateFlow) │
│  └─ Cache (Agents, Models, Session history)                 │
└─────────────────────────────────────────────────────────────┘
```

### 数据流向

```
用户输入 → ClawithChatPanel
    ↓
ChatService.processMessage()
    ↓
FileSystemService.parseAndResolveMentions()  // 解析 @ 文件引用
    ↓
AndroidContextService.buildAndroidContextPrompt()  // 注入 Android 上下文
    ↓
ClawithApiService.sendWebSocket()  // 发送到后端
    ↓
┌──────────────────────────────────────┐
│  Clawith Backend (FastAPI)           │
│  WS /ws/chat/{agent_id}              │
│  - JWT Auth (Bearer cw-xxx)          │
│  - Stream response chunks            │
└──────────────────────────────────────┘
    ↓
ChatService.handleStreamChunk()  // 处理流式响应
    ↓
┌──────────────────────────────────────┐
│  响应类型判断                          │
│  ├─ Text chunk → 打字机效果更新 UI    │
│  ├─ Tool call → 显示工具调用状态      │
│  └─ Code diff → 弹出 Diff Review 对话框│
└──────────────────────────────────────┘
    ↓
UI 更新 (消息列表 / Diff 对话框)
```

---

## Implementation Plan

### Phase 1: 基础架构与认证 (优先级: P0)

#### 1.1 项目结构搭建
**关键文件**:
- `src/main/kotlin/com/clawith/plugin/ClawithPlugin.kt` - 插件入口点
- `src/main/resources/META-INF/plugin.xml` - 插件描述文件
- `build.gradle.kts` - 构建配置

**任务**:
- 创建包结构: `service/`, `ui/`, `model/`, `util/`
- 配置依赖: OkHttp 4.12+, kotlinx-serialization 1.6+, IntelliJ Platform SDK
- 注册 ToolWindow: `<toolWindow id="Clawith" .../>`

#### 1.2 认证服务
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/AuthService.kt`

**核心功能**:
```kotlin
@State(name = "ClawithAuth", storages = [Storage("clawith-auth.xml")])
class AuthService : PersistentStateComponent<AuthService.State> {
    data class State(
        var serverUrl: String = "http://localhost:8000",
        var apiKey: String? = null
    )
    
    fun getApiKey(): String?  // 从 PasswordSafe 读取
    fun saveApiKey(apiKey: String)  // 保存到安全存储
    suspend fun validateApiKey(): Boolean  // 测试连接
}
```

**设置页面**: `src/main/kotlin/com/clawith/plugin/ui/ClawithSettingsConfigurable.kt`
- UI: TextField (server URL), PasswordField (API Key), "Test Connection" Button
- 保存时调用 `AuthService.saveApiKey()`

---

### Phase 2: 网络层与智能体管理 (优先级: P0)

#### 2.1 API 客户端
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/ClawithApiService.kt`

**核心功能**:
```kotlin
class ClawithApiService(private val authService: AuthService) {
    private val httpClient = OkHttpClient.Builder()
        .addInterceptor { chain ->
            chain.request().newBuilder()
                .addHeader("Authorization", "Bearer ${authService.getApiKey()}")
                .build()
        }
        .build()
    
    // REST APIs
    suspend fun getAgents(): List<AgentInfo>  // GET /api/agents/
    suspend fun getLLMModels(): List<LLMModelInfo>  // GET /api/enterprise/llm-models
    
    // WebSocket
    fun connectWebSocket(
        agentId: String,
        onMessage: (ServerMessage) -> Unit,
        onError: (Throwable) -> Unit
    ): WebSocket
}
```

**数据模型**:
- `src/main/kotlin/com/clawith/plugin/model/AgentInfo.kt`
- `src/main/kotlin/com/clawith/plugin/model/LLMModel.kt`
- `src/main/kotlin/com/clawith/plugin/model/WebSocketMessages.kt` (ClientMessage, ServerMessage sealed class)

#### 2.2 智能体与模型选择服务
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/AgentModelService.kt`

**核心功能**:
```kotlin
class AgentModelService(private val apiService: ClawithApiService) {
    private var cachedAgents: List<AgentInfo>? = null
    private var cachedModels: List<LLMModelInfo>? = null
    
    suspend fun fetchAgents(): List<AgentInfo>  // 带 5 分钟缓存
    suspend fun fetchModels(): List<LLMModelInfo>
    
    fun getCurrentAgentId(): String?
    fun setCurrentAgent(agentId: String)
    fun getCurrentModelId(): String?
    fun setCurrentModel(modelId: String)
}
```

---

### Phase 3: 聊天界面核心 (优先级: P0)

#### 3.1 主聊天面板
**关键文件**: `src/main/kotlin/com/clawith/plugin/ui/ClawithChatPanel.kt`

**布局结构**:
```
┌──────────────────────────────────────────┐
│  [Agent: 🤖 Assistant ▼] [Model: GPT-4▼]│
├──────────────────────────────────────────┤
│  [Scrollable Message List (JBList)]      │
│  - User messages (right-aligned)         │
│  - Assistant messages (left-aligned)     │
│  - Tool call indicators                   │
├──────────────────────────────────────────┤
│  [@ Mention] [📎 Attach] [Type message...]│
│  [Send Button]                            │
└──────────────────────────────────────────┘
```

**核心逻辑**:
```kotlin
class ClawithChatPanel(
    private val chatService: ChatService,
    private val agentModelService: AgentModelService
) : JPanel() {
    private val agentComboBox: JComboBox<AgentInfo>
    private val modelComboBox: JComboBox<LLMModelInfo>
    private val messageListModel: DefaultListModel<ChatMessageItem>
    private val inputTextArea: JBTextArea
    private val sendButton: JButton
    
    init {
        setupUI()
        loadAgentsAndModels()
        setupEventListeners()
    }
    
    private fun onSendMessage() {
        val content = inputTextArea.text
        val mentions = fileSystemService.parseMentions(content)
        
        launch {
            chatService.sendMessage(
                content = content,
                mentions = mentions,
                onChunk = { chunk -> updateLastMessage(chunk) },
                onToolCall = { toolCall -> showToolCallIndicator(toolCall) },
                onComplete = { response -> handleCodeDiffs(response) }
            )
        }
    }
}
```

#### 3.2 聊天服务
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/ChatService.kt`

**核心功能**:
```kotlin
class ChatService(
    private val apiService: ClawithApiService,
    private val fileSystemService: FileSystemService,
    private val androidContextService: AndroidContextService
) {
    private var webSocket: WebSocket? = null
    private val _messages = MutableStateFlow<List<ChatMessageItem>>(emptyList())
    val messages: StateFlow<List<ChatMessageItem>> = _messages.asStateFlow()
    
    suspend fun sendMessage(
        content: String,
        mentions: List<FileReference>,
        onChunk: (String) -> Unit,
        onToolCall: (ToolCallInfo) -> Unit,
        onComplete: (String) -> Unit
    ) {
        // 1. Enrich message with file contents and Android context
        val enrichedContent = buildEnrichedMessage(content, mentions)
        
        // 2. Connect WebSocket if not connected
        ensureWebSocketConnected()
        
        // 3. Send message
        val clientMsg = ClientMessage(content = enrichedContent)
        webSocket?.send(Json.encodeToString(clientMsg))
        
        // 4. Handle streaming response in WebSocket listener
    }
    
    private fun buildEnrichedMessage(content: String, mentions: List<FileReference>): String {
        val androidContext = androidContextService.buildAndroidContextPrompt()
        val fileRefs = fileSystemService.formatFileReferences(mentions)
        
        return buildString {
            appendLine(androidContext)
            appendLine(fileRefs)
            appendLine("--- USER MESSAGE ---")
            appendLine(content)
        }
    }
}
```

**消息数据模型**: `src/main/kotlin/com/clawith/plugin/model/ChatMessage.kt`
```kotlin
sealed class ChatMessageItem {
    abstract val timestamp: Long
    abstract val role: String
    
    data class User(
        val content: String,
        val attachments: List<MessageAttachment> = emptyList(),
        override val timestamp: Long = System.currentTimeMillis(),
        override val role: String = "user"
    ) : ChatMessageItem()
    
    data class Assistant(
        val content: StringBuilder = StringBuilder(),
        val thinking: String? = null,
        val toolCalls: MutableList<ToolCallInfo> = mutableListOf(),
        override val timestamp: Long = System.currentTimeMillis(),
        override val role: String = "assistant"
    ) : ChatMessageItem()
}
```

---

### Phase 4: 文件系统与 @ 引用 (优先级: P1)

#### 4.1 文件系统服务
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/FileSystemService.kt`

**核心功能**:
```kotlin
class FileSystemService {
    fun readFileContent(filePath: String): String? {
        val virtualFile = LocalFileSystem.getInstance().findFileByPath(filePath)
            ?: return null
        
        return try {
            virtualFile.contentsToByteArray().toString(Charsets.UTF_8)
        } catch (e: Exception) {
            Logger.getInstance(FileSystemService::class.java).warn("Failed to read: $filePath", e)
            null
        }
    }
    
    fun parseMentions(message: String): List<FileReference> {
        val pattern = Regex("@([\\w./-]+)")
        return pattern.findAll(message).mapNotNull { match ->
            val filePath = match.groupValues[1]
            val content = readFileContent(resolveFilePath(filePath))
            if (content != null) FileReference(filePath, content) else null
        }.toList()
    }
    
    fun formatFileReferences(files: List<FileReference>): String {
        return files.joinToString("\n\n") { ref ->
            """
            --- BEGIN FILE: ${ref.path} ---
            ${ref.content}
            --- END FILE: ${ref.path} ---
            """.trimIndent()
        }
    }
}
```

#### 4.2 @ 提及自动补全
**关键文件**: `src/main/kotlin/com/clawith/plugin/ui/MentionCompletionProvider.kt`

**核心功能**:
```kotlin
class MentionCompletionProvider(private val project: Project) : CompletionContributor() {
    init {
        extend(
            CompletionType.BASIC,
            PlatformPatterns.psiElement(),
            object : CompletionProvider<CompletionParameters>() {
                override fun addCompletions(
                    parameters: CompletionParameters,
                    context: ProcessingContext,
                    result: CompletionResultSet
                ) {
                    val text = parameters.position.text
                    val atIndex = text.lastIndexOf('@')
                    if (atIndex == -1) return
                    
                    val query = text.substring(atIndex + 1)
                    val files = findMatchingFiles(query)
                    
                    files.forEach { file ->
                        result.addElement(
                            LookupElementBuilder.create(file.path)
                                .withIcon(file.icon)
                                .withTypeText(file.typeText)
                        )
                    }
                }
            }
        )
    }
}
```

---

### Phase 5: Diff Review 系统 (优先级: P1)

#### 5.1 Diff 管理服务
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/DiffReviewService.kt`

**核心功能**:
```kotlin
class DiffReviewService(private val project: Project) {
    private val pendingDiffs = mutableMapOf<String, DiffBatch>()
    
    fun parseCodeBlocksFromResponse(response: String): List<CodeDiff> {
        val pattern = Regex("```(?:diff|code):(.+?)\n(.*?)```", RegexOption.DOT_MATCHES_ALL)
        return pattern.findAll(response).mapNotNull { match ->
            val filePath = match.groupValues[1].trim()
            val newContent = match.groupValues[2].trim()
            val originalContent = FileSystemService.readFileContent(filePath)
            val changeType = if (originalContent == null) ChangeType.CREATE else ChangeType.MODIFY
            
            CodeDiff(filePath, originalContent, newContent, changeType)
        }.toList()
    }
    
    fun showBatchDiffReview(batch: DiffBatch) {
        pendingDiffs[batch.id] = batch
        ApplicationManager.getApplication().invokeLater {
            val dialog = CodeDiffDialog(project, batch)
            dialog.show()
        }
    }
    
    suspend fun applySelectedChanges(batchId: String, selectedFiles: List<String>): Boolean {
        val batch = pendingDiffs[batchId] ?: return false
        var successCount = 0
        
        for (filePath in selectedFiles) {
            val diff = batch.diffs.find { it.filePath == filePath } ?: continue
            val success = writeFile(filePath, diff.newContent)
            if (success) successCount++
        }
        
        batch.status = if (successCount == batch.diffs.size) {
            DiffBatchStatus.APPLIED
        } else if (successCount > 0) {
            DiffBatchStatus.PARTIALLY_APPLIED
        } else {
            DiffBatchStatus.REJECTED
        }
        
        return successCount > 0
    }
}
```

**Diff 数据模型**:
```kotlin
data class CodeDiff(
    val filePath: String,
    val originalContent: String?,
    val newContent: String,
    val changeType: ChangeType = ChangeType.MODIFY
)

enum class ChangeType { CREATE, MODIFY, DELETE }

data class DiffBatch(
    val id: String = UUID.randomUUID().toString(),
    val diffs: List<CodeDiff>,
    val timestamp: Long = System.currentTimeMillis(),
    var status: DiffBatchStatus = DiffBatchStatus.PENDING
)
```

#### 5.2 Diff 对话框
**关键文件**: `src/main/kotlin/com/clawith/plugin/ui/CodeDiffDialog.kt`

**布局**:
```
┌────────────────────────────────────────────────┐
│  Clawith - Code Changes Review          [×]    │
├──────────────┬─────────────────────────────────┤
│ ☑ MainActivity.java  │  << IntelliJ Diff Viewer >> │
│ ☑ fragment_home.xml  │  Left: Original              │
│ ☐ build.gradle       │  Right: Proposed             │
│                      │                               │
│ [Select All]         │                               │
├──────────────┴─────────────────────────────────┤
│  [Accept Selected] [Reject Selected] [Cancel]  │
└────────────────────────────────────────────────┘
```

**核心实现**: 使用 IntelliJ Platform 的 `DiffViewer` API
```kotlin
class CodeDiffDialog(private val project: Project, private val batch: DiffBatch) : DialogWrapper(project) {
    override fun createCenterPanel(): JComponent {
        val diffRequest = SimpleDiffRequest(
            "Clawith - Code Review",
            DiffContentFactory.getInstance().create(diff.originalContent ?: ""),
            DiffContentFactory.getInstance().create(diff.newContent),
            "Original",
            "Proposed by Clawith"
        )
        
        return DiffViewer(diffRequest).component
    }
}
```

---

### Phase 6: Android 开发辅助 (优先级: P2)

#### 6.1 Android 上下文服务
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/AndroidContextService.kt`

**核心功能**:
```kotlin
class AndroidContextService(private val project: Project) {
    fun getCurrentAndroidContext(): AndroidContext {
        val editor = FileEditorManager.getInstance(project).selectedTextEditor ?: return AndroidContext.Unknown
        val virtualFile = editor.virtualFile ?: return AndroidContext.Unknown
        
        return when {
            isActivityFile(virtualFile) -> {
                val className = extractClassName(editor.document.text)
                AndroidContext.Activity(className, virtualFile.path)
            }
            isFragmentFile(virtualFile) -> AndroidContext.Fragment(...)
            isViewModelFile(virtualFile) -> AndroidContext.ViewModel(...)
            else -> AndroidContext.Unknown
        }
    }
    
    fun buildAndroidContextPrompt(): String {
        val context = getCurrentAndroidContext()
        val buildInfo = parseBuildGradle()
        val manifestInfo = parseAndroidManifest()
        
        return buildString {
            appendLine("## Android Project Context")
            appendLine("- Current Component: $context")
            if (buildInfo != null) {
                appendLine("- Compile SDK: ${buildInfo.compileSdkVersion}")
                appendLine("- Min SDK: ${buildInfo.minSdkVersion}")
            }
        }
    }
}
```

#### 6.2 命令行服务 (ADB logcat)
**关键文件**: `src/main/kotlin/com/clawith/plugin/service/CommandLineService.kt`

**核心功能**:
```kotlin
class CommandLineService(private val project: Project) {
    suspend fun executeAdbLogcat(filter: String = "", maxLines: Int = 100): String {
        val command = listOf("adb", "logcat", "-d", filter)
        
        return try {
            val output = executeCommand(command, timeout = 5000)
            output.lines().takeLast(maxLines).joinToString("\n")
        } catch (e: Exception) {
            "Error executing adb logcat: ${e.message}"
        }
    }
    
    private suspend fun executeCommand(command: List<String>, timeout: Long): String = withContext(Dispatchers.IO) {
        val process = ProcessBuilder(command)
            .directory(project.basePath?.let { File(it) })
            .redirectErrorStream(true)
            .start()
        
        val output = process.inputStream.bufferedReader().readText()
        process.waitFor(timeout, TimeUnit.MILLISECONDS)
        output.trim()
    }
}
```

**集成到聊天**: 添加 "📱 Get Logcat" 按钮,点击后执行 ADB 命令并将结果作为上下文发送给智能体。

---

### Phase 7: 优化与错误处理 (优先级: P2)

#### 7.1 WebSocket 断线重连
```kotlin
class ResilientWebSocket(private val apiService: ClawithApiService, private val agentId: String) {
    private var reconnectAttempts = 0
    private val maxReconnectAttempts = 5
    
    fun connect(onMessage: (ServerMessage) -> Unit) {
        apiService.connectWebSocket(
            agentId = agentId,
            onMessage = onMessage,
            onError = { error ->
                if (reconnectAttempts < maxReconnectAttempts) {
                    val delay = (2L.pow(reconnectAttempts) * 1000).coerceAtMost(30000)
                    reconnectAttempts++
                    GlobalScope.launch { delay(delay); connect(onMessage) }
                } else {
                    notifyUser("Connection lost. Please check your network.")
                }
            }
        )
    }
}
```

#### 7.2 大文件保护
```kotlin
fun readFileContentSafe(filePath: String, maxSize: Int = 500 * 1024): String? {
    val file = File(filePath)
    if (file.length() > maxSize) {
        NotificationUtils.showWarning("File too large (${file.length() / 1024}KB). Max: 500KB")
        return null
    }
    return FileSystemService.readFileContent(filePath)
}
```

#### 7.3 错误处理中间件
```kotlin
class ApiErrorHandler {
    suspend fun <T> executeWithRetry(
        operation: suspend () -> T,
        maxRetries: Int = 3
    ): T {
        repeat(maxRetries) { attempt ->
            try {
                return operation()
            } catch (e: IOException) {
                if (attempt == maxRetries - 1) throw e
                delay(1000L * (attempt + 1)) // Exponential backoff
            } catch (e: HttpException) {
                when (e.code()) {
                    401 -> throw AuthenticationException("Invalid API key")
                    429 -> { delay(5000); continue }
                    in 500..599 -> { if (attempt < maxRetries - 1) delay(1000L * (attempt + 1)); else throw e }
                    else -> throw e
                }
            }
        }
        throw RuntimeException("Max retries exceeded")
    }
}
```

---

## File Structure

```
src/main/kotlin/com/clawith/plugin/
├── ClawithPlugin.kt                          # Plugin entry point, register services
├── service/
│   ├── AuthService.kt                        # API Key management (PasswordSafe)
│   ├── ClawithApiService.kt                  # HTTP + WebSocket client (OkHttp)
│   ├── AgentModelService.kt                  # Agent & Model selection with cache
│   ├── ChatService.kt                        # Message handling, streaming, tool calls
│   ├── FileSystemService.kt                  # VFS integration, @ mention parsing
│   ├── DiffReviewService.kt                  # Code diff management, IntelliJ Diff API
│   ├── CommandLineService.kt                 # ADB/Gradle command execution
│   └── AndroidContextService.kt              # Android project context extraction
├── ui/
│   ├── ClawithToolWindowFactory.kt           # ToolWindow factory registration
│   ├── ClawithChatPanel.kt                   # Main chat UI (Swing DSL)
│   ├── CodeDiffDialog.kt                     # Diff review dialog with accept/reject
│   ├── MentionCompletionProvider.kt          # @ file autocomplete popup
│   └── ClawithSettingsConfigurable.kt        # Settings page (server URL, API Key)
├── model/
│   ├── ChatMessage.kt                        # ChatMessageItem sealed class
│   ├── AgentInfo.kt                          # Agent data class
│   ├── LLMModel.kt                           # LLM model data class
│   ├── CodeDiff.kt                           # CodeDiff, DiffBatch, ChangeType
│   └── WebSocketMessages.kt                  # ClientMessage, ServerMessage sealed class
├── util/
│   ├── JsonUtils.kt                          # kotlinx.serialization helpers
│   ├── ImageUtils.kt                         # Base64 image encoding
│   └── LoggerExtensions.kt                   # Logging utilities
└── resources/
    ├── META-INF/
    │   └── plugin.xml                        # Plugin descriptor (toolWindow, extensions)
    └── icons/
        └── clawith.svg                       # Plugin icon
```

---

## Dependencies

### Gradle Dependencies (`build.gradle.kts`)
```kotlin
dependencies {
    // HTTP Client
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    
    // JSON Serialization
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.0")
    
    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-swing:1.7.3")
}

plugins {
    kotlin("plugin.serialization") version "1.9.0"
}
```

### IntelliJ Platform APIs Used
- `VirtualFileSystem` - Local file access
- `FileEditorManager` - Detect current editor file
- `DiffViewer` / `SimpleDiffRequest` - Code diff display
- `ProgressManager` - Background task execution
- `PasswordSafe` - Secure API Key storage
- `ToolWindowFactory` - Sidebar integration
- `CompletionContributor` - @ mention autocomplete

---

## Verification

### 测试场景

#### 1. 认证与配置
- [ ] 在设置页面输入 API Key 和服务器地址
- [ ] 点击 "Test Connection" 验证连接成功
- [ ] 重启 IDE 后配置仍然保留

#### 2. 智能体与模型选择
- [ ] ToolWindow 头部显示智能体和模型下拉框
- [ ] 从 `/api/agents/` 获取智能体列表并填充
- [ ] 从 `/api/llm-models/` 获取模型列表并填充
- [ ] 切换智能体后,新对话使用正确的 agent_id

#### 3. 聊天功能
- [ ] 输入文本消息并发送
- [ ] 看到打字机效果的流式响应
- [ ] 发送包含图片的消息 (拖拽或粘贴截图)
- [ ] 历史消息正确显示在列表中

#### 4. @ 文件引用
- [ ] 在输入框输入 `@` 触发自动补全弹窗
- [ ] 选择文件后,文件路径插入输入框
- [ ] 发送消息时,文件内容自动注入到上下文中
- [ ] 智能体能正确回答关于文件内容的问题

#### 5. Diff Review
- [ ] 智能体生成代码修改建议 (格式: ```diff:path/to/file.java)
- [ ] 自动弹出 Diff Review 对话框
- [ ] 左侧显示原始代码,右侧显示建议代码
- [ ] 勾选文件后点击 "Accept Selected",文件被正确修改
- [ ] 点击 "Reject Selected",文件保持不变

#### 6. Android 辅助
- [ ] 打开 Activity 文件时,聊天上下文包含 "Currently editing Activity: XXX"
- [ ] 点击 "📱 Get Logcat" 按钮,执行 `adb logcat -d`
- [ ] Logcat 输出作为上下文发送给智能体
- [ ] 智能体能分析日志并给出建议

#### 7. 错误处理
- [ ] API Key 无效时,显示友好错误提示
- [ ] WebSocket 断开后,自动重试连接 (最多 5 次)
- [ ] 读取超过 500KB 的文件时,提示用户文件过大
- [ ] ADB 未安装时,提示用户安装 ADB

### 性能指标
- [ ] 启动时间: ToolWindow 打开后,1 秒内加载完成
- [ ] 消息延迟: 发送消息到收到第一个 chunk < 2 秒
- [ ] 内存占用: 连续聊天 100 条消息后,内存增长 < 50MB
- [ ] 文件大小限制: 单次读取文件不超过 500KB

---

## Risks & Mitigations

### 风险 1: WebSocket 稳定性
**问题**: 网络波动导致频繁断线  
**缓解**: 
- 实现指数退避重连 (1s, 2s, 4s, 8s, 16s, max 30s)
- 断线时缓存用户消息,重连后自动重发
- 显示连接状态指示器 (🟢 Connected / 🔴 Disconnected)

### 风险 2: Diff 合并冲突
**问题**: 用户在智能体生成代码后手动修改了文件  
**缓解**:
- 应用更改前检查文件 MD5 是否与原始内容一致
- 如果检测到冲突,显示三向合并界面 (Original / Current / Proposed)
- 允许用户手动编辑最终版本

### 风险 3: ADB 跨平台兼容性
**问题**: Windows/macOS/Linux 上 ADB 路径和行为差异  
**缓解**:
- 检测操作系统并自动调整命令
- 提供 "ADB Path" 配置项,允许用户自定义
- 如果 ADB 不可用,优雅降级并提示用户安装

### 风险 4: 大文件性能
**问题**: 读取超大文件 (>1MB) 导致 UI 卡顿  
**缓解**:
- 强制限制单次读取文件大小为 500KB
- 异步读取文件,显示加载指示器
- 对超大文件提供"分块读取"选项 (只读取前 100 行)

### 风险 5: 智能体生成的代码格式不规范
**问题**: 代码块标记不完整,无法正确解析  
**缓解**:
- 要求用户使用标准格式: ```diff:path/to/file.java
- 提供多种解析策略 (尝试匹配 ```java, ```kotlin, ```xml 等)
- 如果解析失败,显示原始响应并提示用户手动复制

---

## Future Enhancements

### Phase 8+ (后续迭代)
1. **会话历史持久化**: 将聊天记录保存到本地 SQLite,支持搜索和历史回顾
2. **多轮对话上下文管理**: 维护 conversation_id,支持长期对话记忆
3. **技能市场集成**: 从 Clawith 后端获取可用技能列表,一键启用
4. **快捷键支持**: Cmd/Ctrl+Shift+C 快速打开 Clawith ToolWindow
5. **代码片段收藏**: 收藏智能体生成的优质代码片段
6. **团队协作**: 共享智能体配置和常用 Prompt 模板
7. **离线模式**: 缓存智能体响应,网络恢复后同步

---

## References

### Clawith 后端 API 文档
- `backend/app/api/agents.py` - 智能体管理 API
- `backend/app/api/websocket.py` - WebSocket 聊天端点
- `backend/app/plugins/clawith_acp/router.py` - ACP 协议参考实现

### IntelliJ Platform SDK
- [Tool Windows](https://plugins.jetbrains.com/docs/intellij/tool-windows.html)
- [Virtual File System](https://plugins.jetbrains.com/docs/intellij/virtual-file-system.html)
- [Diff Viewer](https://plugins.jetbrains.com/docs/intellij/diff-viewer.html)
- [Completion Contributors](https://plugins.jetbrains.com/docs/intellij/completion-contributors.html)

### 通义灵码插件参考
- 侧边栏聊天界面布局
- @ 文件引用交互模式
- 流式响应打字机效果
- Diff Review 工作流程
