#!/bin/bash
# sdk-provision.sh — 构建前从国内镜像补齐 Android SDK 组件（供 entrypoint.sh source）
#
# 背景: AGP 构建期自动补全缺失 SDK 组件时直连 dl.google.com, 在宿主 Clash fake-ip
# 代理环境下连接挂死 → 构建卡住（与 checkDebugAarMetadata 同类故障）。
# 本脚本在构建前主动检测项目声明的 SDK 需求, 缺失组件从腾讯云镜像
# ($ANDROID_SDK_MIRROR) 下载解压进共享 SDK 卷, 使构建全程零 Google 依赖。
#
# 特性:
#   - 幂等: 已安装组件仅做目录存在性检查（毫秒级），热构建零开销
#   - 并发安全: flock 串行化补齐; 解压到临时目录后原子 mv, 无半成品状态
#   - 尽力而为: 任何失败仅告警不阻塞构建
#   - 安全集: 未显式声明 buildToolsVersion 时 AGP 按版本取默认值, 常用默认
#     版本(30.0.3~36.0.0)与常用平台(android-33~36)预置兜底, 可用
#     ANDROID_SDK_SAFETY_PACKAGES 覆盖
#
# 环境变量:
#   ANDROID_HOME              SDK 根目录 (默认 /opt/android-sdk)
#   ANDROID_SDK_MIRROR        镜像基地址 (默认腾讯云)
#   ANDROID_SDK_SAFETY_PACKAGES 空格分隔的安全集包名 (默认 build-tools 常用默认版本 + platforms 33-36)
#
# 输出: 每次调用后 SDK_PROVISION_INSTALLED_COUNT 为本次新安装组件数。

SDK_ROOT="${ANDROID_HOME:-/opt/android-sdk}"
SDK_MIRROR="${ANDROID_SDK_MIRROR:-https://mirrors.cloud.tencent.com/AndroidSDK}"
SDK_SAFETY_PACKAGES="${ANDROID_SDK_SAFETY_PACKAGES:-build-tools;30.0.3 build-tools;31.0.0 build-tools;32.0.0 build-tools;33.0.0 build-tools;33.0.1 build-tools;34.0.0 build-tools;35.0.0 build-tools;36.0.0 platforms;android-33 platforms;android-34 platforms;android-35 platforms;android-36}"
SDK_PROVISION_INSTALLED_COUNT=0

# 解析项目文件里声明的 SDK 组件 → 输出包名 (platforms;android-N / build-tools;X.Y.Z / ndk;V / cmake;V)
# 覆盖: kts (compileSdk = 34) / groovy (compileSdk 34, "34") / version catalog (libs.versions.toml) /
#       gradle.properties (android.compileSdk=34) / buildToolsVersion / ndkVersion / cmake { version }
detect_required_sdk_packages() {
    local files
    files=$(find . -maxdepth 6 \( -name '*.gradle' -o -name '*.gradle.kts' -o -name 'gradle.properties' -o -name '*.versions.toml' \) \
        -not -path './.gradle/*' -not -path './build/*' -not -path './app/build/*' -not -path './*/build/*' 2>/dev/null)
    [ -n "$files" ] || return 0

    # 平台: compileSdk / compileSdkVersion / targetSdk / targetSdkVersion
    # 形式: = 34 | 34 | "34" | android.compileSdk=34 | 目录里任意值
    grep -hoE '(compileSdk|compileSdkVersion|targetSdk|targetSdkVersion)[[:space:]]*=[[:space:]]*["'"'"']?[0-9]+' $files 2>/dev/null \
        | grep -oE '[0-9]+$' | sort -u | while read -r ver; do echo "platforms;android-$ver"; done || true
    grep -hoE '(compileSdk|compileSdkVersion|targetSdk|targetSdkVersion)[[:space:]]+["'"'"']?[0-9]+' $files 2>/dev/null \
        | grep -oE '[0-9]+$' | sort -u | while read -r ver; do echo "platforms;android-$ver"; done || true
    grep -hoE 'android\.(compileSdk|targetSdk)[[:space:]]*=[[:space:]]*[0-9]+' $files 2>/dev/null \
        | grep -oE '[0-9]+$' | sort -u | while read -r ver; do echo "platforms;android-$ver"; done || true
    # version catalog: [versions] compileSdk = "34" / targetSdk = "34"
    grep -hoE '^[[:space:]]*(compileSdk|targetSdk)[[:space:]]*=[[:space:]]*"[0-9]+"' $files 2>/dev/null \
        | grep -oE '[0-9]+' | sort -u | while read -r ver; do echo "platforms;android-$ver"; done || true

    # build-tools
    grep -hoE 'buildToolsVersion[[:space:]]*=[[:space:]]*["'"'"']?[0-9]+\.[0-9]+\.[0-9]+' $files 2>/dev/null \
        | grep -oE '[0-9.]+$' | sort -u | while read -r ver; do echo "build-tools;$ver"; done || true
    grep -hoE 'buildToolsVersion[[:space:]]+["'"'"']?[0-9]+\.[0-9]+\.[0-9]+' $files 2>/dev/null \
        | grep -oE '[0-9.]+$' | sort -u | while read -r ver; do echo "build-tools;$ver"; done || true

    # ndk: ndkVersion = "26.1.10909125" / ndkVersion "26.1.10909125"
    # 注意: 不能用 [=([:space:]]+ 这类含 [= 的字符类 — 会被按等价类解析导致永不匹配
    grep -hoE 'ndkVersion[[:space:]]*(=[[:space:]]*)?["'"'"']?[0-9]+\.[0-9]+\.[0-9]+' $files 2>/dev/null \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | sort -u | while read -r ver; do echo "ndk;$ver"; done || true

    # cmake: cmake { version = "3.22.1" } 块级提取（跨行）
    awk 'BEGIN{RS="}"} index($0,"cmake") && index($0,"version") {
        s=$0; sub(/^.*cmake[[:space:]]*\{/, "", s)
        if (match(s, /version[[:space:]]*=[[:space:]]*["'"'"']?[0-9]+\.[0-9]+\.[0-9]+/)) {
            v=substr(s, RSTART, RLENGTH)
            sub(/^[^0-9]*/, "", v)
            print v
        }
    }' $files 2>/dev/null | sort -u | while read -r ver; do echo "cmake;$ver"; done || true
}

sdk_package_installed() {
    local pkg="$1" kind="${1%%;*}" ver="${1#*;}"
    [ -n "$ver" ] || return 1
    case "$kind" in
        platforms)   [ -d "$SDK_ROOT/platforms/$ver" ] ;;
        build-tools) [ -d "$SDK_ROOT/build-tools/$ver" ] ;;
        ndk)         [ -d "$SDK_ROOT/ndk/$ver" ] ;;
        cmake)       [ -d "$SDK_ROOT/cmake/$ver" ] ;;
        *) return 1 ;;
    esac
}

# 包名 → 腾讯镜像候选文件名（按优先级输出多行）
# 平台修订号取自镜像 repository XML 快照; build-tools 命名规则实测:
#   ≤34 用连字符 (build-tools_r34-linux.zip), ≥35 用下划线 (build-tools_r36_linux.zip)
mirror_candidates_for_package() {
    local kind="${1%%;*}" ver="${1#*;}"
    case "$kind" in
        platforms)
            ver="${ver#android-}"
            case "$ver" in
                23) echo "platform-23_r03.zip" ;;
                24) echo "platform-24_r02.zip" ;;
                25) echo "platform-25_r03.zip" ;;
                26) echo "platform-26_r02.zip" ;;
                27) echo "platform-27_r03.zip" ;;
                28) echo "platform-28_r06.zip" ;;
                29) echo "platform-29_r05.zip" ;;
                30) echo "platform-30_r03.zip" ;;
                31) echo "platform-31_r01.zip" ;;
                32) echo "platform-32_r01.zip" ;;
                33) echo "platform-33_r03.zip"; echo "platform-33_r02.zip"; echo "platform-33_r01.zip" ;;
                34) echo "platform-34_r02.zip"; echo "platform-34_r01.zip" ;;
                35) echo "platform-35_r02.zip" ;;
                36) echo "platform-36_r02.zip" ;;
                *) echo "platform-${ver}_r03.zip"; echo "platform-${ver}_r02.zip"; echo "platform-${ver}_r01.zip" ;;
            esac
            ;;
        build-tools)
            local major="${ver%%.*}" full="$ver"
            [ "$full" = "$major.0.0" ] && full="$major"
            echo "build-tools_r${full}-linux.zip"
            echo "build-tools_r${full}_linux.zip"
            echo "build-tools_r${major}-linux.zip"
            echo "build-tools_r${major}_linux.zip"
            ;;
        ndk)
            case "$ver" in
                23.1.7779620) echo "android-ndk-r23b-linux.zip" ;;
                24.0.8215888) echo "android-ndk-r24-linux.zip" ;;
                25.1.8937393) echo "android-ndk-r25b-linux.zip" ;;
                25.2.9519653) echo "android-ndk-r25c-linux.zip" ;;
                26.0.10404224) echo "android-ndk-r26-linux.zip" ;;
                26.1.10909125) echo "android-ndk-r26d-linux.zip" ;;
                27.0.12077973) echo "android-ndk-r27d-linux.zip" ;;
                28.0.12916984) echo "android-ndk-r28-linux.zip" ;;
            esac
            ;;
        cmake)
            echo "cmake-${ver}-linux.zip"
            echo "cmake-${ver}-linux-x86_64.zip"
            ;;
    esac
}

# 未知 NDK 版本: 从镜像的 repository2-3.xml 查长版本 → linux zip 名
ndk_zip_from_mirror_xml() {
    local ver="$1" xml="${SDK_MIRROR_INDEX_CACHE:-}"
    if [ ! -f "$xml" ]; then
        xml="/tmp/sdk-repository2-3.xml"
        curl -fsSL --connect-timeout 20 --max-time 120 "$SDK_MIRROR/repository2-3.xml" -o "$xml" 2>/dev/null || return 1
    fi
    awk -v v="$ver" '
        /<remotePackage/ { inblk = 0 }
        index($0, "path=\"ndk;" v "\"") { inblk = 1 }
        /<\/remotePackage/ { inblk = 0 }
        inblk && /<url>/ && /-linux\.zip/ {
            sub(/^.*<url>/, "")
            sub(/<\/url>.*$/, "")
            print
            exit
        }
    ' "$xml"
}

# 安装单个包到 $SDK_ROOT（需调用方持有 flock）
# 依赖全局: SDK_TMP_DIR（解压中转，须与 SDK_ROOT 同文件系统以保证 mv 原子性）
install_sdk_package() {
    local pkg="$1" kind="${1%%;*}" ver="${1#*;}"
    sdk_package_installed "$pkg" && return 0

    local cand url zip=""
    for cand in $(mirror_candidates_for_package "$pkg"); do
        [ -n "$cand" ] || continue
        url="$SDK_MIRROR/$cand"
        echo "[sdk-provision] 下载 $url"
        if curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 600 \
                "$url" -o "$SDK_TMP_DIR/component.zip" 2>/dev/null; then
            zip="$SDK_TMP_DIR/component.zip"
            break
        fi
    done
    if [ -z "$zip" ] && [ "$kind" = "ndk" ]; then
        cand=$(ndk_zip_from_mirror_xml "$ver" 2>/dev/null) || cand=""
        if [ -n "$cand" ]; then
            echo "[sdk-provision] 下载 $SDK_MIRROR/$cand"
            curl -fsSL --retry 3 --connect-timeout 30 --max-time 900 \
                "$SDK_MIRROR/$cand" -o "$SDK_TMP_DIR/component.zip" 2>/dev/null && zip="$SDK_TMP_DIR/component.zip"
        fi
    fi
    if [ -z "$zip" ]; then
        echo "[sdk-provision] 警告: $pkg 镜像无可用文件, 跳过 (若 AGP 自动补全将直连 dl.google.com)" >&2
        return 1
    fi

    local dest
    case "$kind" in
        platforms)   dest="$SDK_ROOT/platforms/$ver" ;;
        build-tools) dest="$SDK_ROOT/build-tools/$ver" ;;
        ndk)         dest="$SDK_ROOT/ndk/$ver" ;;
        cmake)       dest="$SDK_ROOT/cmake/$ver" ;;
    esac

    local topdir
    topdir=$(unzip -q -l "$zip" | awk 'NR==4 {print $4; exit}' | cut -d/ -f1)
    [ -n "$topdir" ] || { echo "[sdk-provision] 警告: $pkg 压缩包结构异常, 跳过" >&2; rm -f "$zip"; return 1; }

    mkdir -p "$SDK_TMP_DIR/extract" "$(dirname "$dest")"
    if unzip -q "$zip" -d "$SDK_TMP_DIR/extract" 2>/dev/null && [ -d "$SDK_TMP_DIR/extract/$topdir" ]; then
        mv "$SDK_TMP_DIR/extract/$topdir" "$dest" && {
            SDK_PROVISION_INSTALLED_COUNT=$((SDK_PROVISION_INSTALLED_COUNT + 1))
            echo "[sdk-provision] 已安装 $pkg → $dest"
        }
    else
        echo "[sdk-provision] 警告: $pkg 解压失败, 跳过" >&2
    fi
    rm -rf "$SDK_TMP_DIR/extract" "$zip"
}

# 主入口: 检测项目声明的组件 + 安全集, 补齐缺失项。
# 返回 0；SDK_PROVISION_INSTALLED_COUNT = 本次新安装数（供 entrypoint 决定是否重试构建）。
sync_sdk_components() {
    SDK_PROVISION_INSTALLED_COUNT=0
    [ -d "$SDK_ROOT" ] || { echo "[sdk-provision] $SDK_ROOT 不存在, 跳过补齐" >&2; return 0; }

    # 清扫历史残留的临时目录（仅本脚本命名空间，安全）
    rm -rf "$SDK_ROOT"/.provision-tmp-* 2>/dev/null || true

    SDK_TMP_DIR="$SDK_ROOT/.provision-tmp-$$"
    mkdir -p "$SDK_TMP_DIR"

    # 共享卷级互斥: 并发容器同时补齐时串行化
    local lockfile="$SDK_ROOT/.provision.lock" list="$SDK_TMP_DIR/packages.txt"
    exec 9>"$lockfile" 2>/dev/null || { echo "[sdk-provision] 警告: 无法创建锁文件, 跳过补齐" >&2; rm -rf "$SDK_TMP_DIR"; return 0; }
    if ! flock -w 120 9; then
        echo "[sdk-provision] 警告: 等锁超时(另一容器正在补齐), 跳过" >&2
        exec 9>&-
        rm -rf "$SDK_TMP_DIR"
        return 0
    fi

    { detect_required_sdk_packages; printf '%s\n' $SDK_SAFETY_PACKAGES; } | sort -u > "$list"
    while read -r pkg; do
        [ -n "$pkg" ] || continue
        if ! sdk_package_installed "$pkg"; then
            echo "[sdk-provision] 缺失组件: $pkg"
            install_sdk_package "$pkg" || true
        fi
    done < "$list"

    flock -u 9
    exec 9>&-
    rm -rf "$SDK_TMP_DIR"
    echo "[sdk-provision] 补齐完成, 本次新安装 ${SDK_PROVISION_INSTALLED_COUNT} 个组件"
}
