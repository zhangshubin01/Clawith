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

    # 主下载尝试 1：清华 tuna Adoptium 镜像（国内直连快，动态解析最新版本）
    JDK_MIRROR_LISTING="https://mirrors.tuna.tsinghua.edu.cn/Adoptium/${JAVA_VERSION}/jdk/${ADOPTIUM_ARCH}/linux/"
    JDK_MIRROR_FILE=$(curl -fsSL --connect-timeout 15 --max-time 30 "${JDK_MIRROR_LISTING}" 2>/dev/null \
        | grep -oE "OpenJDK${JAVA_VERSION}U-jdk_${ADOPTIUM_ARCH}_linux_hotspot_[0-9._]+\.tar\.gz" \
        | sort -V | tail -1 || true)
    JDK_MIRROR_DOWNLOADED=false
    if [ -n "$JDK_MIRROR_FILE" ]; then
        JDK_MIRROR_URL="${JDK_MIRROR_LISTING}${JDK_MIRROR_FILE}"
        echo "[INFO] 主下载源(国内镜像): $JDK_MIRROR_URL"
        if curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 300 \
             "$JDK_MIRROR_URL" -o "${TMP_DIR}/jdk.tar.gz"; then
            JDK_MIRROR_DOWNLOADED=true
        fi
    fi

    # 主下载尝试 2：Adoptium CDN（tuna 不可用时回退）
    if [ "$JDK_MIRROR_DOWNLOADED" != "true" ]; then
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
    fi

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

# ─── 国内镜像注入（根治 fake-ip 代理劫持导致的依赖下载挂死） ───
# 背景: 宿主 Clash fake-ip 模式下 google()/mavenCentral() 被解析成 198.18.x.x 假 IP,
# TCP 能建立但代理节点不吐数据 → Gradle 下载线程无限挂起, checkDebugAarMetadata 卡死。
# 镜像仓库国内直连, 经 beforeSettings 先于 settings.gradle 求值注入到仓库列表最前,
# 外部仓库退化为兜底; 构建不再依赖代理出口。关闭: ANDROID_GRADLE_MIRRORS=off。
setup_gradle_mirrors() {
    mkdir -p "${GRADLE_USER_HOME:-/home/builduser/.gradle}/init.d"
    cat > "${GRADLE_USER_HOME:-/home/builduser/.gradle}/init.d/aliyun-mirrors.gradle" << 'GRADLE_SCRIPT'
    // 默认外部仓库 (google/mavenCentral/gradlePluginPortal) 重定向到阿里云镜像:
    // 镜像位于仓库列表最前, 命中的构件不再访问外部仓库, 自定义仓库保持原顺序兜底。
    beforeSettings { settings ->
        settings.pluginManagement {
            repositories {
                maven { url 'https://maven.aliyun.com/repository/google' }
                maven { url 'https://maven.aliyun.com/repository/public' }
                maven { url 'https://maven.aliyun.com/repository/gradle-plugin' }
            }
        }
        // dependencyResolutionManagement 自 Gradle 6.8 起提供, 旧 wrapper 项目跳过
        if (settings.metaClass.respondsTo(settings, 'getDependencyResolutionManagement')) {
            settings.dependencyResolutionManagement {
                repositories {
                    maven { url 'https://maven.aliyun.com/repository/google' }
                    maven { url 'https://maven.aliyun.com/repository/public' }
                }
            }
        }
    }
GRADLE_SCRIPT
}

remove_gradle_mirrors() {
    # 关闭镜像时同时清理持久卷中的残留脚本, 否则旧容器写入的 init.d 仍会生效
    rm -f "${GRADLE_USER_HOME:-/home/builduser/.gradle}/init.d/aliyun-mirrors.gradle"
}

if [ "${ANDROID_GRADLE_MIRRORS:-on}" != "off" ]; then
    setup_gradle_mirrors
else
    remove_gradle_mirrors
fi

# ─── 下载超时硬化 ───
# 任何仓库(含租户自定义)连接建立后 30s 连不上 / 60s 读不到一个字节即失败,
# 下载死连接快速报错而非无限挂起(此前的 500s+ 假 IP 挂死由 socketTimeout 兜底)。
setup_gradle_download_timeouts() {
    local props="${GRADLE_USER_HOME:-/home/builduser/.gradle}/gradle.properties"
    touch "$props"
    grep -q '^systemProp.org.gradle.internal.http.connectionTimeout=' "$props" || \
        echo 'systemProp.org.gradle.internal.http.connectionTimeout=30000' >> "$props"
    grep -q '^systemProp.org.gradle.internal.http.socketTimeout=' "$props" || \
        echo 'systemProp.org.gradle.internal.http.socketTimeout=60000' >> "$props"
}
setup_gradle_download_timeouts

# P5: select_java 统一设置 JAVA_HOME + PATH（带回退）
select_java

echo "[INFO] JAVA_HOME=$JAVA_HOME ($(java -version 2>&1 | head -1))"

# ─── P5 Fix 1: 每次容器启动写入 sqlite-jdbc 版本强制脚本 ───
# 解决 Room KSP DatabaseVerifier 在 aarch64 Linux 的原生库缺失
# 必须运行时写入（非镜像层），因为 ~/.gradle 被 Gradle 卷覆盖
mkdir -p /home/builduser/.gradle/init.d
cat > /home/builduser/.gradle/init.d/sqlite-jdbc-aarch64.gradle << 'GRADLE_SCRIPT'
allprojects {
    configurations.all {
        resolutionStrategy {
            force "org.xerial:sqlite-jdbc:3.53.2.0"
        }
    }
}
GRADLE_SCRIPT

# ─── tmpfs 加速编译热区 ───
# 将 app/build/intermediates 符号链接到 tmpfs（默认 /dev/shm/intermediates），
# 编译临时文件不落盘，减少 IO 并避免 read_only rootfs 冲突。
# 仅在路径完全不存在时才创建链接：断链（-L 为真）与真实目录/文件（-e 为真）
# 都不重建——容器内每次都会重建 tmpfs 目标，既有链接在容器内始终可解析。
TMPFS_INTERMEDIATES="${ANDROID_TMPFS_DIR:-/dev/shm/intermediates}"
CREATED_INTERMEDIATES_LINK=0

setup_intermediates_link() {
    mkdir -p "$TMPFS_INTERMEDIATES"
    if [ -d "app/build" ] && [ ! -L "app/build/intermediates" ] && [ ! -e "app/build/intermediates" ]; then
        ln -sf "$TMPFS_INTERMEDIATES" app/build/intermediates
        CREATED_INTERMEDIATES_LINK=1
    fi
}

cleanup_intermediates_link() {
    # 只移除本容器自己创建的链接，避免宿主持久化残留断链；
    # 不触碰构建产物或预先存在的链接。
    if [ "$CREATED_INTERMEDIATES_LINK" = "1" ] && [ -L "app/build/intermediates" ]; then
        rm -f app/build/intermediates
    fi
}

setup_intermediates_link
# bash 只保留最后注册的 EXIT trap：与 TMP_DIR 清理链式注册，两者都会执行
trap 'cleanup_intermediates_link; rm -rf "${TMP_DIR:-}"' EXIT

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

# ─── 执行用户命令（构建） ───
# 不用 exec：bash 作为容器 PID 1 需自行把 TERM/INT 转发给构建进程，
# 并把产物路径输出延后到构建完成之后（旧行为在构建前列出旧产物，误导排查）。
"$@" &
BUILD_PID=$!
trap 'kill -TERM "$BUILD_PID" 2>/dev/null || true' TERM INT
set +e
wait "$BUILD_PID"
BUILD_RC=$?
set -e
trap - TERM INT

# ─── 产物路径输出（纯信息，无代码消费方） ───
# 构建完成后输出本次 APK/AAB 路径列表，覆盖多模块和自定义 buildDir 项目
echo "=== APK_OUTPUT_PATHS ==="
APKS=$(find . -path "*/build/outputs/*" \( -name "*.apk" -o -name "*.aab" \) 2>/dev/null)
if [ -n "$APKS" ]; then
    echo "$APKS"
else
    echo "NO_APK_FOUND"
fi
echo "=== END_APK_OUTPUT_PATHS ==="

exit "$BUILD_RC"
