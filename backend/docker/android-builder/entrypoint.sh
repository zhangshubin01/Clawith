#!/bin/bash
# entrypoint.sh — Android 构建容器入口
# 职责：
#   1. 检查共享卷中是否有对应 JDK，没有则自动从 Adoptium 下载
#   2. 根据 JAVA_VERSION 环境变量设置 JAVA_HOME
#   3. 注入 Git 凭证（CI_JOB_TOKEN 或 PAT）
#   4. 执行用户命令

set -euo pipefail
trap 'rm -rf "${TMP_DIR:-}"' EXIT

JDK_CACHE_DIR="${JDK_CACHE_DIR:-/opt/jdks}"
JAVA_VERSION="${JAVA_VERSION:-17}"

# ─── JDK 多版本回退（P4） ───
# 如果指定版本的 JDK 目录不存在，回退到最新可用 JDK
select_java() {
    local target="${JDK_CACHE_DIR}/jdk-${JAVA_VERSION}"
    if [ -d "$target" ]; then
        export JAVA_HOME="$target"
    else
        local fallback=$(ls -d "${JDK_CACHE_DIR}"/jdk-* 2>/dev/null | sort -t- -k2 -n | tail -1)
        if [ -n "$fallback" ]; then
            echo "[entrypoint] JDK ${JAVA_VERSION} 不可用，回退到: $fallback"
            export JAVA_HOME="$fallback"
        else
            echo "[entrypoint] 错误: 无可用 JDK"
            exit 1
        fi
    fi
    export PATH="${JAVA_HOME}/bin:${PATH}"
}

# ─── 自动检测容器架构，ARM64 原生镜像用 aarch64 JDK ───
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64) ADOPTIUM_ARCH="aarch64" ;;
    *)             ADOPTIUM_ARCH="x64" ;;
esac

# Adoptium JDK 映射表 — 新增版本只需加一行
declare -A JDK_MAP=(
    ["11"]="https://api.adoptium.net/v3/binary/latest/11/ga/linux/${ADOPTIUM_ARCH}/jdk/hotspot/normal/adoptium"
    ["17"]="https://api.adoptium.net/v3/binary/latest/17/ga/linux/${ADOPTIUM_ARCH}/jdk/hotspot/normal/adoptium"
    ["21"]="https://api.adoptium.net/v3/binary/latest/21/ga/linux/${ADOPTIUM_ARCH}/jdk/hotspot/normal/adoptium"
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

    # 主下载尝试：Adoptium CDN（强重试参数）
    echo "[INFO] 主下载源: $JDK_URL"
    curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 300 \
         "$JDK_URL" -o "${TMP_DIR}/jdk.tar.gz" || {

        # 回退链：Adoptium → Amazon Corretto（轻量重试）
        # Adoptium CDN 在国内偶尔不可达，Corretto 作为二级源
        JDK_FALLBACK_URLS=(
            "https://corretto.aws/downloads/latest/amazon-corretto-${JAVA_VERSION}-${ADOPTIUM_ARCH}-linux-jdk.tar.gz"
        )
        DOWNLOADED=false
        for url in "${JDK_FALLBACK_URLS[@]}"; do
            echo "[INFO] 尝试 JDK 回退下载: $url"
            if curl -fsSL --retry 2 --retry-delay 3 --connect-timeout 15 --max-time 180 "$url" -o "${TMP_DIR}/jdk.tar.gz"; then
                echo "[INFO] JDK 下载成功"
                DOWNLOADED=true
                break
            fi
            echo "[WARN] 下载失败，尝试下一个源..."
        done

        if [ "$DOWNLOADED" != "true" ]; then
            echo "[ERROR] 所有 JDK 下载源均不可达"
            exit 1
        fi
    }

    # sha256 校验（tar 之前，非阻塞：校验和下载失败时跳过）
    curl -fsSL "${JDK_URL}.sha256.txt" -o "${TMP_DIR}/jdk.sha256" 2>/dev/null || true
    if [ -f "${TMP_DIR}/jdk.sha256" ]; then
        EXPECTED=$(awk '{print $1}' "${TMP_DIR}/jdk.sha256")
        ACTUAL=$(sha256sum "${TMP_DIR}/jdk.tar.gz" | awk '{print $1}')
        if [ "$EXPECTED" = "$ACTUAL" ] && [ -n "$EXPECTED" ]; then
            echo "[INFO] JDK sha256 校验通过"
        else
            echo "[ERROR] JDK sha256 校验不匹配，文件可能损坏" >&2
            exit 1
        fi
    fi

    tar -xzf "${TMP_DIR}/jdk.tar.gz" -C "${TMP_DIR}"
    EXTRACTED_DIR=$(ls -d "${TMP_DIR}"/jdk-* 2>/dev/null | head -1)
    if [ -z "$EXTRACTED_DIR" ]; then
        # Corretto 包名格式不同：amazon-corretto-XX-xxx-linux-x64.tar.gz
        EXTRACTED_DIR=$(ls -d "${TMP_DIR}"/amazon-corretto-* 2>/dev/null | head -1)
    fi
    if [ -z "$EXTRACTED_DIR" ]; then
        echo "[ERROR] JDK 解压失败：找不到 jdk-* 或 amazon-corretto-* 目录"
        exit 1
    fi
    # 原子重命名防并发：先检查目录是否已被其他容器创建
    if [ -d "${JDK_HOME}" ]; then
        echo "[INFO] JDK $JAVA_VERSION 已被并发容器缓存，复用即可"
        rm -rf "$EXTRACTED_DIR" 2>/dev/null || true
    else
        mv "$EXTRACTED_DIR" "${JDK_HOME}" 2>/dev/null || {
            echo "[ERROR] JDK 安装失败" >&2; exit 1
        }
        echo "[INFO] JDK $JAVA_VERSION 下载完成 → ${JDK_HOME}"
    fi
else
    echo "[INFO] JDK $JAVA_VERSION 命中共享缓存 → ${JDK_HOME}"
fi

# P4: 使用 select_java 回退逻辑
select_java

echo "[INFO] JAVA_HOME=$JAVA_HOME ($(java -version 2>&1 | head -1))"

# ─── tmpfs 加速编译热区 ───
# 将 app/build/intermediates 符号链接到 /dev/shm/intermediates（tmpfs 挂载点）
# 编译过程中临时文件不落盘，减少 IO 开销并避免 read_only rootfs 冲突
mkdir -p /dev/shm/intermediates
if [ -d "app/build" ] && [ ! -L "app/build/intermediates" ]; then
    mkdir -p app/build
    ln -sf /dev/shm/intermediates app/build/intermediates
fi

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

# ─── 签名密钥写入 tmpfs（/dev/shm 已挂载 tmpfs，容器销毁自动释放） ───
# 环境变量在 docker inspect 中可见，因此使用 tmpfs 文件传递密钥
if [ -n "${KEY_STORE_PASSWORD:-}" ]; then
    echo "$KEY_STORE_PASSWORD" > /dev/shm/keystore_password && chmod 600 /dev/shm/keystore_password
    echo "$KEY_PASSWORD" > /dev/shm/key_password && chmod 600 /dev/shm/key_password
    export ORG_GRADLE_PROJECT_android.injected.signing.store.password=$(cat /dev/shm/keystore_password)
    export ORG_GRADLE_PROJECT_android.injected.signing.key.password=$(cat /dev/shm/key_password)
fi

# ─── 产物路径输出（供 agent_tools.py 解析） ───
# 编译完成后输出 APK/AAB 路径列表，覆盖多模块和自定义 buildDir 项目
echo "=== APK_OUTPUT_PATHS ==="
APKS=$(find . -path "*/build/outputs/*" \( -name "*.apk" -o -name "*.aab" \) 2>/dev/null)
if [ -n "$APKS" ]; then
    echo "$APKS"
else
    echo "NO_APK_FOUND"
fi
echo "=== END_APK_OUTPUT_PATHS ==="

exec "$@"
