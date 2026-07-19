#!/bin/bash
# entrypoint.sh — Android 构建容器入口
# 职责：
#   1. 检查共享卷中是否有对应 JDK，没有则自动从 Adoptium 下载
#   2. 根据 JAVA_VERSION 环境变量设置 JAVA_HOME
#   3. 注入 Git 凭证（CI_JOB_TOKEN 或 PAT）
#   4. 执行用户命令

set -euo pipefail

JDK_CACHE_DIR="${JDK_CACHE_DIR:-/opt/jdks}"
JAVA_VERSION="${JAVA_VERSION:-17}"

# ─── 自动检测容器架构，ARM64 原生镜像用 aarch64 JDK ───
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64) ADOPTIUM_ARCH="aarch64" ;;
    *)             ADOPTIUM_ARCH="x64" ;;
esac

# Adoptium JDK 映射表 — 新增版本只需加一行
declare -A JDK_MAP=(
    ["11"]="https://api.adoptium.net/v3/binary/latest/11/ga/linux/${ADOPTIUM_ARCH}/jdk/hotspot/normal/eclipse"
    ["17"]="https://api.adoptium.net/v3/binary/latest/17/ga/linux/${ADOPTIUM_ARCH}/jdk/hotspot/normal/eclipse"
    ["21"]="https://api.adoptium.net/v3/binary/latest/21/ga/linux/${ADOPTIUM_ARCH}/jdk/hotspot/normal/eclipse"
)

# ─── JDK 按需下载（原子 rename 防并发） ───
JDK_HOME="${JDK_CACHE_DIR}/jdk-${JAVA_VERSION}"

if [ ! -d "${JDK_HOME}/bin" ]; then
    JDK_URL="${JDK_MAP[$JAVA_VERSION]:-}"
    if [ -z "$JDK_URL" ]; then
        echo "[ERROR] 不支持的 JAVA_VERSION=$JAVA_VERSION，可用: ${!JDK_MAP[*]}"
        exit 1
    fi

    echo "[INFO] JDK $JAVA_VERSION 未缓存，正在下载到共享卷 $JDK_CACHE_DIR ..."
    mkdir -p "${JDK_CACHE_DIR}"
    TMP_DIR=$(mktemp -d)
    curl -fsSL "$JDK_URL" -o "${TMP_DIR}/jdk.tar.gz"
    tar -xzf "${TMP_DIR}/jdk.tar.gz" -C "${TMP_DIR}"
    EXTRACTED_DIR=$(ls -d "${TMP_DIR}"/jdk-* 2>/dev/null | head -1)
    if [ -z "$EXTRACTED_DIR" ]; then
        echo "[ERROR] JDK 解压失败：找不到 jdk-* 目录"
        exit 1
    fi
    # 原子重命名防并发：先 mv 到 .tmp，再 rename；失败说明被抢写
    mv "$EXTRACTED_DIR" "${JDK_HOME}.tmp" 2>/dev/null || true
    if mv "${JDK_HOME}.tmp" "${JDK_HOME}" 2>/dev/null; then
        echo "[INFO] JDK $JAVA_VERSION 下载完成 → ${JDK_HOME}"
    else
        rm -rf "${JDK_HOME}.tmp" 2>/dev/null || true
        echo "[INFO] JDK $JAVA_VERSION 已被并发容器缓存，复用即可"
    fi
    rm -rf "${TMP_DIR}"
else
    echo "[INFO] JDK $JAVA_VERSION 命中共享缓存 → ${JDK_HOME}"
fi

export JAVA_HOME="${JDK_HOME}"
export PATH="${JAVA_HOME}/bin:${PATH}"

echo "[INFO] JAVA_HOME=$JAVA_HOME ($(java -version 2>&1 | head -1))"

# ─── Git 凭证注入 ───
# CI_JOB_TOKEN：GitLab CI 自动注入的短效令牌，随 Pipeline 结束自动销毁
# 注：跨项目访问需在目标项目 Settings > CI/CD > Token Access 中添加 allowlist
if [ -n "${CI_JOB_TOKEN:-}" ]; then
    # 方案 A: GitLab 原生 CI_JOB_TOKEN（推荐，低泄漏风险——令牌随 Pipeline 自动销毁）
    git config --global url."https://gitlab-ci-token:${CI_JOB_TOKEN}@gitlab.company.com/".insteadOf "https://gitlab.company.com/"
    echo "[INFO] Git credentials configured via CI_JOB_TOKEN (auto-expire)"
elif [ -n "${GIT_TOKEN:-}" ]; then
    # 方案 B: 通用 PAT 回退（非 GitLab CI 环境）
    git config --global url."https://gitlab-ci-token:${GIT_TOKEN}@${GIT_HOST:-gitlab.com}/".insteadOf "https://${GIT_HOST:-gitlab.com}/"
    echo "[INFO] Git credentials configured via PAT"
fi

# ─── Gradle 配置 ───
export GRADLE_OPTS="${GRADLE_OPTS:--Dorg.gradle.daemon=false -Dorg.gradle.jvmargs=-Xmx4096m -XX:+HeapDumpOnOutOfMemoryError}"

exec "$@"
