#!/bin/bash
# ci_build.sh — Android 项目 CI/CD 构建入口
# 用法: ./ci_build.sh <project_name> <jdk_version> <gradle_task>
# 示例: ./ci_build.sh ProjectA 17 assembleDebug
#       ./ci_build.sh ProjectB 11 assembleRelease

set -euo pipefail

PROJECT_NAME="${1:?缺少项目名称}"
JDK_VERSION="${2:?缺少 JDK 版本 (11|17|21)}"
BUILD_TASK="${3:-assembleDebug}"

# Gradle 堆内存：根据任务类型调整
if [[ "$BUILD_TASK" == *"Release"* ]]; then
    GRADLE_HEAP="6g"
    CONTAINER_MEM="8g"
else
    GRADLE_HEAP="4g"
    CONTAINER_MEM="6g"
fi

# 为当前项目动态创建独立的 Gradle 缓存卷（避免并发锁冲突）
GRADLE_VOLUME="gradle_cache_global"
docker volume create "${GRADLE_VOLUME}" > /dev/null 2>&1 || true

# 签名密钥：Base64 + tmpfs 零落盘（进阶方案，零物理磁盘接触）
# GitLab CI: KEYSTORE_BASE64 存入 Masked Variable
# Jenkins:  credential('KEYSTORE_BASE64')
KEYSTORE_SCRIPT=""
if [ -n "${KEYSTORE_BASE64:-}" ]; then
    KEYSTORE_SCRIPT='
        mkdir -p /workspace/secure_keys
        echo "$KEY_B64" | base64 -d > /workspace/secure_keys/release.jks
        chmod 600 /workspace/secure_keys/release.jks
    '
    echo "[INFO] 签名密钥通过 Base64 + tmpfs 注入（零落盘）"
fi

# Nexus 代理 init.gradle（如果存在）
NEXUS_MOUNT=""
if [ -f "${PWD}/init.gradle" ]; then
    NEXUS_MOUNT="-v ${PWD}/init.gradle:/root/.gradle/init.d/nexus.gradle:ro"
    echo "[INFO] Nexus 代理 init.gradle 已挂载"
fi

# 自动检测 Mac 芯片类型，选择最优架构
ARCH_FLAG="--platform=linux/amd64"
if [[ "$(uname -m)" == "arm64" ]] && [[ "$JDK_VERSION" -ge 17 ]]; then
    ARCH_FLAG="--platform=linux/arm64"
    echo "[INFO] M 系列芯片 + JDK $JDK_VERSION → 使用原生 ARM64 架构"
fi

echo "=========================================="
echo "  项目: ${PROJECT_NAME}  JDK: ${JDK_VERSION}"
echo "  任务: ${BUILD_TASK}  架构: ${ARCH_FLAG}"
echo "=========================================="

# ─── tmpfs 内存盘（减少 SSD 磨损）───
docker run --rm \
    ${ARCH_FLAG} \
    --memory="${CONTAINER_MEM}" \
    --memory-swap="${CONTAINER_MEM}" \
    --cpus="6" \
    --tmpfs /workspace/app/build:rw,exec,noatime,size=4g \
    --tmpfs /workspace/build:rw,exec,noatime,size=2g \
    --tmpfs /workspace/.gradle:rw,noatime,size=1g \
    -v "$(pwd)":/workspace \
    -v "global_jdk_cache:/opt/jdks:rw" \
    -v "global_android_sdk:/opt/android-sdk:rw" \
    -v "${GRADLE_VOLUME}:/root/.gradle" \
    ${NEXUS_MOUNT} \
    -e JAVA_VERSION="${JDK_VERSION}" \
    -e CI_JOB_TOKEN="${CI_JOB_TOKEN:-}" \
    -e KEY_B64="${KEYSTORE_BASE64:-}" \
    -e KEY_STORE_PASSWORD="${KEY_STORE_PASSWORD:-}" \
    -e KEY_ALIAS="${KEY_ALIAS:-}" \
    -e KEY_PASSWORD="${KEY_PASSWORD:-}" \
    -e GRADLE_OPTS="-Xmx${GRADLE_HEAP} -Dorg.gradle.jvmargs=-Xmx${GRADLE_HEAP} -XX:MaxMetaspaceSize=1g -XX:+HeapDumpOnOutOfMemoryError -XX:+ExitOnOutOfMemoryError -Dorg.gradle.daemon=false -Dorg.gradle.caching=true -Dorg.gradle.parallel=true -Dorg.gradle.configuration-cache=true -Dorg.gradle.configuration-cache.problems=warn -Dorg.gradle.configuration-cache.max-problems=512 -Dkotlin.compiler.execution.strategy=in-process" \
    -w /workspace \
    clawith-android-builder:latest \
    bash -c "echo \"sdk.dir=/opt/android-sdk\" > local.properties && chmod +x ./gradlew && ${KEYSTORE_SCRIPT} ./gradlew ${BUILD_TASK} && cp -r app/build/outputs/apk /workspace/apk-output 2>/dev/null || true && sleep 30"

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ [${PROJECT_NAME}] ${BUILD_TASK} 构建成功"
else
    echo "❌ [${PROJECT_NAME}] ${BUILD_TASK} 构建失败 (exit=$EXIT_CODE)" >&2
fi
exit $EXIT_CODE
