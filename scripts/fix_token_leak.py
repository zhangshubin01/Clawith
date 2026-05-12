#!/usr/bin/env python3
"""修复 CosyWebSocketConnectClient.java 中的 Token 明文日志泄露"""

filepath = "/Users/shubinzhang/Downloads/demo-new/src/main/java/com/alibabacloud/intellij/cosy/core/websocket/CosyWebSocketConnectClient.java"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: DeploymentException — 不把原始异常传给 log.warn
old_deploy = '} catch (DeploymentException var9) {\n         log.warn(String.format("Cosy connect failed, cost %d ms", System.currentTimeMillis() - connectSt), var9);\n         throw new DeploymentException("Deploying websocket encountered error");'

new_deploy = ('} catch (DeploymentException var9) {\n'
    '         // Token 脱敏：Jakarta WebSocket 库抛出的异常消息包含完整 URL（含 token），\n'
    '         // 直接传给 log.warn 会将 token 明文写入 idea.log。对 URL 中的 token 参数脱敏后再输出。\n'
    '         String maskedDeployMsg = "Deploying websocket encountered error: " + maskUrl(var9.getMessage());\n'
    '         log.warn(String.format("Cosy connect failed, cost %d ms", System.currentTimeMillis() - connectSt) + ": " + maskedDeployMsg);\n'
    '         throw new DeploymentException(maskedDeployMsg);')

if old_deploy in content:
    content = content.replace(old_deploy, new_deploy)
    print("OK: DeploymentException catch 块已修复")
else:
    print("SKIP: DeploymentException 模式未匹配")

# Fix 2: IOException — 同样脱敏
old_io = ('} catch (IOException var10) {\n'
    '         log.error("连接 WebSocket 时发生 IO 异常", var10);\n'
    '         throw new IOException("连接 WebSocket 时发生 IO 异常", var10);')

new_io = ('} catch (IOException var10) {\n'
    '         // Token 脱敏：IOException 消息可能包含完整 URL，脱敏后再输出\n'
    '         String maskedIoMsg = "连接 WebSocket 时发生 IO 异常: " + maskUrl(var10.getMessage());\n'
    '         log.error(maskedIoMsg);\n'
    '         throw new IOException(maskedIoMsg);')

if old_io in content:
    content = content.replace(old_io, new_io)
    print("OK: IOException catch 块已修复")
else:
    print("SKIP: IOException 模式未匹配")

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("OK: 文件已保存")
