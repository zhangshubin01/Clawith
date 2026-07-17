#!/bin/bash
# Android SDK 许可证月度更新
# 部署: sudo cp this /usr/local/bin/android_sdk_license_update.sh && sudo chmod +x "$_"
# cron: 0 2 1 * * /bin/bash /usr/local/bin/android_sdk_license_update.sh

set -euo pipefail
echo "=== $(date) 更新 Android SDK 许可证 ==="
docker run --rm \
    -v global_android_sdk:/opt/android-sdk:ro \
    -e ANDROID_HOME=/opt/android-sdk \
    clawith-android-builder:latest \
    bash -c 'yes | sdkmanager --licenses > /dev/null 2>&1 && echo "许可证更新成功"'
