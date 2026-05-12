#!/bin/bash
# 修复 CosyWebSocketConnectClient.java Token 明文泄露
set -e

F="/Users/shubinzhang/Downloads/demo-new/src/main/java/com/alibabacloud/intellij/cosy/core/websocket/CosyWebSocketConnectClient.java"
python3 -c "
import re
c = open('$F', 'r').read()
# DeploymentException fix
old = '} catch (DeploymentException var9) {\n         log.warn(String.format(\"Cosy connect failed, cost %d ms\", System.currentTimeMillis() - connectSt), var9);\n         throw new DeploymentException(\"Deploying websocket encountered error\");'
new = '} catch (DeploymentException var9) {\n         String maskedDeployMsg = \"Deploying websocket encountered error: \" + maskUrl(var9.getMessage());\n         log.warn(String.format(\"Cosy connect failed, cost %d ms\", System.currentTimeMillis() - connectSt) + \": \" + maskedDeployMsg);\n         throw new DeploymentException(maskedDeployMsg);'
c = c.replace(old, new)
# IOException fix
old2 = '} catch (IOException var10) {\n         log.error(\"连接 WebSocket 时发生 IO 异常\", var10);\n         throw new IOException(\"连接 WebSocket 时发生 IO 异常\", var10);'
new2 = '} catch (IOException var10) {\n         String maskedIoMsg = \"连接 WebSocket 时发生 IO 异常: \" + maskUrl(var10.getMessage());\n         log.error(maskedIoMsg);\n         throw new IOException(maskedIoMsg);'
c = c.replace(old2, new2)
open('$F', 'w').write(c)
print('OK: Token leak fixed in CosyWebSocketConnectClient.java')
"
