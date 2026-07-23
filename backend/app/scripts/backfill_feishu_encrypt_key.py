"""encrypt_key 数据迁移检查脚本：扫描飞书渠道，输出需要手动配置 encrypt_key 的渠道列表。

背景：encrypt_key 是飞书事件订阅的安全配置项，新渠道必填，
      历史渠道可能缺少此字段（NULL 或空字符串），需手动从飞书开放平台获取后填入。

使用方式:
  Docker:  docker exec clawith-backend-1 python3 -m app.scripts.backfill_feishu_encrypt_key
  Source:  cd backend && python3 -m app.scripts.backfill_feishu_encrypt_key

输出说明：
  - encrypt_key=OK      → 已配置，无需操作
  - encrypt_key=MISSING → 需要登录飞书开放平台，进入对应应用的安全设置中获取 encrypt_key，
                           然后通过 API 管理后台或直接 UPDATE SQL 填入。
"""

import asyncio

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig


async def main():
    """读取所有 feishu 渠道，输出 encrypt_key 状态审计报告。"""
    logger.info("开始扫描飞书渠道 encrypt_key 配置状态...")

    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == "feishu",
                ChannelConfig.is_configured.is_(True),
            )
        )
        configs = result.scalars().all()

    if not configs:
        logger.info("无已配置的飞书渠道，无需处理。")
        return

    ok_count = 0
    missing_count = 0

    print()
    print("=" * 78)
    print(f"{'ID':<8} {'Agent ID':<36} {'App ID':<32} {'encrypt_key':<12}")
    print("=" * 78)

    for cfg in configs:
        agent_id_str = str(cfg.agent_id)
        app_id_str = cfg.app_id or "(空)"
        has_encrypt = bool(cfg.encrypt_key)
        status = "OK" if has_encrypt else "MISSING"

        print(f"{cfg.id:<8} {agent_id_str:<36} {app_id_str:<32} {status:<12}")

        if has_encrypt:
            ok_count += 1
        else:
            missing_count += 1

    print("=" * 78)
    print()
    logger.info(f"扫描完成：共 {len(configs)} 个飞书渠道，"
                f"encrypt_key 已配置 {ok_count} 个，缺失 {missing_count} 个。")

    if missing_count > 0:
        print()
        print("!")
        print("! 以下渠道 encrypt_key 缺失，需要手动处理：")
        print("!   1. 登录飞书开放平台 (https://open.feishu.cn)")
        print("!   2. 进入对应应用的「安全设置」页面")
        print("!   3. 获取 Encrypt Key 值")
        print("!   4. 通过管理后台更新或执行 SQL:")
        print("!      UPDATE channel_configs")
        print("!      SET encrypt_key = '<你的加密密钥>'")
        print("!      WHERE id IN (SELECT id FROM channel_configs")
        print("!        WHERE channel_type='feishu' AND is_configured")
        print("!        AND (encrypt_key IS NULL OR encrypt_key = ''));")
        print()

    logger.info("encrypt_key 状态审计完成。")


if __name__ == "__main__":
    asyncio.run(main())
