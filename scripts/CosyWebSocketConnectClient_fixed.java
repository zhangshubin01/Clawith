package com.alibabacloud.intellij.cosy.core.websocket;


import com.alibabacloud.intellij.cosy.core.Cosy;
import com.alibabacloud.intellij.cosy.core.lsp.LanguageClientImpl;
import com.alibabacloud.intellij.cosy.core.lsp.LanguageConnectClient;
import com.alibabacloud.intellij.cosy.core.lsp.model.LanguageClient;
import com.alibabacloud.intellij.cosy.core.lsp.model.LanguageServer;
import com.alibabacloud.intellij.cosy.util.ThreadUtil;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import java.io.IOException;
import java.net.URI;
import java.util.Collections;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import jakarta.websocket.ClientEndpoint;
import jakarta.websocket.CloseReason;
import jakarta.websocket.ContainerProvider;
import jakarta.websocket.DeploymentException;
import jakarta.websocket.OnClose;
import jakarta.websocket.OnError;
import jakarta.websocket.Session;
import lombok.Generated;
import org.eclipse.lsp4j.jsonrpc.Launcher;

/**
 * 基于 WebSocket 的 Cosy 语言服务连接客户端。
 * <p>
 * 通过 WebSocket 协议与远程语言服务端建立连接，进行 LSP（Language Server Protocol）通信。
 * 使用 {@link ClientEndpoint} 注解标记为 WebSocket 客户端端点，支持连接生命周期管理
 * （连接、断开、错误处理）以及消息收发功能。
 * </p>
 *
 * @see LanguageConnectClient
 * @see ClientEndpoint
 * <p>研究文档见 Clawith 仓库：{@code docs/plugin-analysis/phase1-1-websocket-layer.md}。</p>
 */
@ClientEndpoint
public class CosyWebSocketConnectClient implements LanguageConnectClient {
   /** 日志记录器 */
   private static final Logger log = Logger.getInstance(CosyWebSocketConnectClient.class);

   /** WebSocket 会话实例，用于管理连接状态和消息收发 */
   private Session session;

   /** 远程语言服务端代理对象，用于向服务端发送 LSP 请求 */
   LanguageServer server;

   /** 本地语言客户端实现，用于接收服务端的通知和回调 */
   LanguageClient client;

   /** 远程语言服务端的 WebSocket URI 地址 */
   URI uri;

   /** 当前 IntelliJ 项目实例 */
   Project project;
   private static final int LOG_STATUS_PREVIEW_MAX = 120;

   /** 异常断开后重连冷却时间（毫秒），防止短时间内重复触发重启 */
   private static final long RECONNECT_COOLDOWN_MS = 2000;
   /** 上次异常断开触发重启的时间戳（毫秒） */
   private final AtomicLong lastAbnormalRestartTime = new AtomicLong(0);
   /** 连续重连失败次数，成功连接后重置 */
   private final AtomicInteger consecutiveReconnectAttempts = new AtomicInteger(0);
   /** 最大连续重连次数 */
   private static final int MAX_CONSECUTIVE_RECONNECTS = 5;
   /** 指数退避基础间隔（毫秒） */
   private static final long RECONNECT_BACKOFF_BASE_MS = 2000;
   /** 指数退避最大间隔（毫秒） */
   private static final long RECONNECT_BACKOFF_MAX_MS = 60000;

   /**
    * 构造一个基于 WebSocket 的 Cosy 连接客户端。
    *
    * @param project 当前 IntelliJ 项目实例，用于初始化语言客户端
    * @param uri     远程语言服务端的 WebSocket URI 地址
    */
   public CosyWebSocketConnectClient(Project project, URI uri) {
      this.project = project;
      this.uri = uri;
      // 创建本地语言客户端实现，绑定到当前项目
      this.client = new LanguageClientImpl(project);
   }

   /**
    * 建立 WebSocket 连接，并与语言服务端进行通信。
    * <p>
    * 该方法会执行以下操作：
    * <ol>
    *   <li>切换当前线程的类加载器为该类的类加载器</li>
    *   <li>通过 WebSocket 容器连接到远程服务端</li>
    *   <li>使用自定义的 {@link CosyWebSocketLauncherBuilder} 创建 LSP Launcher</li>
    *   <li>获取远程服务端代理对象</li>
    * </ol>
    * </p>
    *
    * @throws DeploymentException 如果 WebSocket 部署过程中出现错误
    * @throws IOException          如果连接 I/O 操作出现异常
    */
   @Override
   public void connect() throws DeploymentException, IOException {
      // 记录连接开始时间，用于统计耗时
      long connectSt = System.currentTimeMillis();
      // 记录连接目标（URL 脱敏，适用于 Lingma 和 Clawith）
      log.info("WebSocket connecting: uri=" + maskUrl(this.uri.toString()));
      log.debug("WebSocket connect lifecycle: status=CONNECTING, thread=" + Thread.currentThread().getName());
      // 保存原始类加载器，以便在 finally 中恢复
      ClassLoader originalClassLoader = Thread.currentThread().getContextClassLoader();

      try {
         // 将当前线程的类加载器切换为该类的类加载器，确保 WebSocket 依赖正确加载
         Thread.currentThread().setContextClassLoader(CosyWebSocketConnectClient.class.getClassLoader());
         // 通过 WebSocket 容器连接到远程服务端，返回会话
         this.session = ContainerProvider.getWebSocketContainer().connectToServer(this, this.uri);
         log.debug("WebSocket connect lifecycle: status=CONNECTED, sessionId=" + safeText(this.session != null ? this.session.getId() : null));
         // 使用自定义的 Launcher 构建器创建 LSP Launcher
         Launcher<LanguageServer> launcher = new CosyWebSocketLauncherBuilder()
            .setSession(this.session)
            .setLocalService(this.client)
            .setRemoteInterface(LanguageServer.class)
            .validateMessages(true)
            .create();
         // 获取远程服务端代理对象
         this.server = (LanguageServer)launcher.getRemoteProxy();
         log.debug("WebSocket connect lifecycle: status=LSP_PROXY_READY, uri=" + maskUrl(this.uri.toString()));
         log.info(String.format("Cosy websocket startup succeed, cost %d ms", System.currentTimeMillis() - connectSt));
         consecutiveReconnectAttempts.set(0);
      } catch (DeploymentException var9) {
         String maskedDeployMsg = "Deploying websocket encountered error: " + maskUrl(var9.getMessage());
         log.warn(String.format("Cosy connect failed, cost %d ms", System.currentTimeMillis() - connectSt) + ": " + maskedDeployMsg);
         throw new DeploymentException(maskedDeployMsg);
      } catch (IOException var10) {
         String maskedIoMsg = "连接 WebSocket 时发生 IO 异常: " + maskUrl(var10.getMessage());
         log.error(maskedIoMsg);
         throw new IOException(maskedIoMsg);
      } finally {
         // 恢复原始类加载器
         Thread.currentThread().setContextClassLoader(originalClassLoader);
      }
   }

   /**
    * 判断当前 WebSocket 会话是否处于打开状态。
    * <p>
    * 通过检查会话是否非空且 isOpen() 为 true 来判断。
    * </p>
    *
    * @return 如果会话打开且不为空，返回 {@code true}；否则返回 {@code false}
    */
   @Override
   public boolean isSessionOpen() {
      // 检查会话是否存在且处于打开状态
      return this.session != null && this.session.isOpen();
   }

   /**
    * 关闭当前 WebSocket 会话。
    * <p>
    * 如果会话处于打开状态，则关闭它并记录日志；
    * 如果会话已经关闭，则仅输出警告日志。
    * </p>
    */
   @Override
   public void closeSession() {
      if (this.isSessionOpen()) {
         try {
            // 关闭 WebSocket 会话
            log.info("WebSocket closeSession requested: sessionId=" + safeText(this.session.getId()) + ", status=OPEN");
            this.session.close();
            log.info("Session closed");
         } catch (IOException var2) {
            log.warn("Session close encountered error" + var2.getMessage());
         }
      } else {
         // 会话已经处于关闭状态
         log.info("Session is already closed");
      }
   }

   /**
    * 通过异步方式发送文本消息到远程服务端。
    *
    * @param str 要发送的文本消息内容
    */
   public void sendMessage(String str) {
      // 使用异步远程端点发送文本消息
      log.debug(
         "WebSocket sendMessage: sessionOpen="
            + this.isSessionOpen()
            + ", payloadPreview="
            + safeText(str)
      );
      this.session.getAsyncRemote().sendText(str);
   }

   /**
    * 获取远程语言服务端代理对象。
    *
    * @return 远程语言服务端代理，可用于向服务端发送 LSP 请求
    */
   @Override
   public LanguageServer getServer() {
      return this.server;
   }

   /**
    * 获取本地语言客户端实例。
    *
    * @return 本地语言客户端实现，用于接收服务端的通知和回调
    */
   public LanguageClient getClient() {
      return this.client;
   }

   /**
    * WebSocket 错误回调方法。
    * <p>
    * 当 WebSocket 连接发生错误时由容器自动调用，记录警告日志。
    * </p>
    *
    * @param session 发生错误的 WebSocket 会话
    * @param t       错误异常对象
    */
   @OnError
   public void error(Session session, Throwable t) {
      log.warn(
         "WebSocket encountered error: sessionId="
            + safeText(session != null ? session.getId() : null)
            + ", sessionOpen="
            + (session != null && session.isOpen())
            + ", uri="
            + maskUrl(this.uri != null ? this.uri.toString() : null),
         t
      );
   }

   /**
    * WebSocket 连接关闭回调方法。
    * <p>
    * 当 WebSocket 连接被关闭时由容器自动调用，记录关闭原因和状态码。
    * </p>
    *
    * @param session 被关闭的 WebSocket 会话
    * @param reason  连接关闭的原因信息
    */
   @OnClose
   public void close(Session session, CloseReason reason) {
      // 判断是否为 Clawith 连接（通过 URI 中是否包含 agent_id 参数）
      boolean isClawith = this.uri.toString().contains("agent_id=");
      String prefix = isClawith ? "[Clawith] " : "";
      // 记录 WebSocket 关闭的原因和状态码
      log.info(
         prefix + "WebSocket closed, reason: " + reason.getReasonPhrase() + " code:" + (reason.getCloseCode() != null ? reason.getCloseCode().getCode() : "unknown")
      );
      log.info(prefix + "WebSocket close detail: sessionOpen=" + (session != null && session.isOpen())
         + ", project=" + (this.project != null ? this.project.getName() : "null")
         + ", uri=" + maskUrl(this.uri != null ? this.uri.toString() : "null"));
      // Clawith 模式下，非正常关闭后自动重连，避免消息长期停留在"待发送"。
      // 排除认证失败码（4001/4002），这些是永久性错误，重连无意义。
      int closeCode = reason != null && reason.getCloseCode() != null ? reason.getCloseCode().getCode() : -1;
      boolean abnormalClose = closeCode != 1000 && closeCode != 4001 && closeCode != 4002;
      log.info(prefix + "WebSocket lifecycle transition: status=" + (abnormalClose ? "ABNORMAL_CLOSED" : "NORMAL_CLOSED") + ", closeCode=" + closeCode);
      if (isClawith && abnormalClose && this.project != null && !this.project.isDisposed()) {
         if (Cosy.INSTANCE.isStarting(this.project)) {
            log.info(prefix + "Restart already in progress, skip scheduling. code=" + closeCode);
         } else {
            int attempts = consecutiveReconnectAttempts.incrementAndGet();
            if (attempts > MAX_CONSECUTIVE_RECONNECTS) {
               log.warn(prefix + "Max consecutive reconnect attempts (" + MAX_CONSECUTIVE_RECONNECTS
                  + ") reached, giving up. code=" + closeCode);
               return;
            }
            long now = System.currentTimeMillis();
            long lastRestart = lastAbnormalRestartTime.get();
            long backoff = Math.min(RECONNECT_BACKOFF_BASE_MS * (1L << Math.min(attempts - 1, 5)), RECONNECT_BACKOFF_MAX_MS);
            long effectiveCooldown = Math.max(RECONNECT_COOLDOWN_MS, backoff);
            if (now - lastRestart < effectiveCooldown) {
               log.warn(prefix + "WebSocket abnormal close within cooldown window, skip duplicate restart. "
                  + "code=" + closeCode + ", elapsedMs=" + (now - lastRestart)
                  + ", backoffMs=" + effectiveCooldown + ", attempt=" + attempts);
               return;
            }
            if (lastAbnormalRestartTime.compareAndSet(lastRestart, now)) {
               log.warn(prefix + "WebSocket abnormal close detected, scheduling auto-restart. "
                  + "code=" + closeCode + ", attempt=" + attempts + "/" + MAX_CONSECUTIVE_RECONNECTS
                  + ", backoffMs=" + effectiveCooldown);
               ThreadUtil.execute(() -> Cosy.INSTANCE.restart(this.project, Collections.emptyList()));
            } else {
               log.debug(prefix + "WebSocket abnormal close CAS failed (restart already scheduled by concurrent event). code=" + closeCode);
            }
         }
      }
   }

   /**
    * 获取当前 WebSocket 会话实例。
    *
    * @return 当前 WebSocket 会话
    */
   @Generated
   public Session getSession() {
      return this.session;
   }

   /** 对 URL 中的 token 参数进行脱敏处理 */
   private static String maskUrl(String url) {
      if (url == null) {
         return "null";
      }
      return url.replaceAll("token=[^&]*", "token=***");
   }

   private static String safeText(String text) {
      if (text == null) {
         return "null";
      }
      String truncated = com.alibabacloud.intellij.cosy.util.StringUtils.truncateLast(text, LOG_STATUS_PREVIEW_MAX);
      return truncated == null ? "null" : truncated;
   }
}
