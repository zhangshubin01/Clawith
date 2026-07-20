#!/bin/bash
# Android Docker 构建环境定期清理
# 部署: sudo cp this /usr/local/bin/android_docker_cleanup.sh && sudo chmod +x "$_"
# cron: 0 3 * * 0 /bin/bash /usr/local/bin/android_docker_cleanup.sh

set -euo pipefail
LOG_FILE="/var/log/android_docker_cleanup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== $(date) 开始 Docker 构建环境清理 ==="

# 1. 清理 Docker 镜像和停止的容器（不碰卷，避免误删）
echo "[1/4] 清理 Docker 悬空资源（7 天以上）..."
docker system prune -a -f --filter "until=168h"

# 2. 清理 SDK 临时文件（sdkmanager 下载缓存，非运行时目录）
echo "[2/4] 清理 Android SDK 临时文件..."
docker run --rm \
    -v global_android_sdk:/opt/android-sdk:ro \
    -e ANDROID_HOME=/opt/android-sdk \
    alpine:latest \
    sh -c 'find /opt/android-sdk \( -name ".temp" -o -name "*.download" \) -type d -exec rm -rf {} + 2>/dev/null; find /opt/android-sdk -type d -name "tmp" -empty -delete 2>/dev/null; echo "SDK temp cleaned"'

# 3. 报告卷创建时间（Gradle 内置驱逐管理缓存生命周期，外部不手动删除）
echo "[3/4] Gradle 缓存卷状态（依赖 Gradle 内置驱逐，不手动清理）..."
for vol in $(docker volume ls -q | grep '^gradle_cache_'); do
    project_name="${vol#gradle_cache_}"
    volume_created=$(docker volume inspect "$vol" --format '{{.CreatedAt}}' 2>/dev/null || echo "unknown")
    echo "  项目 $project_name (卷: $vol, 创建于: $volume_created)"
done

# 4. 报告磁盘使用情况
echo "[4/4] 磁盘使用报告："
echo "--- Docker 磁盘使用 ---"
docker system df
echo "--- 卷列表 ---"
docker volume ls | grep -E 'global_|gradle_cache_'
echo "=== $(date) 清理完成 ==="
