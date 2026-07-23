"""渠道凭据加密迁移脚本：将飞书渠道的明文凭据加密存储。

查找 credential_encrypted_version=0 的飞书渠道配置，
使用 encrypt_data()（AES-256-CBC）加密 app_secret / encrypt_key / verification_token，
然后置 credential_encrypted_version=1。

使用方式：
  --dry-run: 仅预览受影响的行
  无参数: 实际执行加密

  Docker:  docker exec clawith-backend-1 python3 -m app.scripts.credential_migration
  Docker(dry-run): docker exec clawith-backend-1 python3 -m app.scripts.credential_migration --dry-run
  源码:  cd backend && python3 -m app.scripts.credential_migration
"""

import argparse
import asyncio

from loguru import logger

# 需加密的敏感字段列表
_SENSITIVE_FIELDS = ["app_secret", "encrypt_key", "verification_token"]


async def main():
    parser = argparse.ArgumentParser(description="将飞书渠道的明文凭据加密存储")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    args = parser.parse_args()

    # 导入所有模型以确保 SQLAlchemy 正确解析关系
    from app.models import (  # noqa: F401
        agent,
        channel_config,
        chat_session,
        audit,
        org,
        user,
        tenant,  # 核心模型
    )
    from app.core.security import decrypt_data, encrypt_data
    from app.config import get_settings
    from app.database import async_session
    from app.models.channel_config import ChannelConfig

    settings = get_settings()
    key = settings.SECRET_KEY

    async with async_session() as db:
        # 查询所有未加密的飞书渠道
        from sqlalchemy import select

        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == "feishu",
                ChannelConfig.credential_encrypted_version == 0,
            )
        )
        configs = result.scalars().all()

        if not configs:
            logger.info("未找到未加密的飞书渠道凭据，无需处理。")
            return

        logger.info(f"找到 {len(configs)} 个待加密的飞书渠道配置")

        # 检查哪些配置有敏感字段需要加密
        configs_to_encrypt = []
        for cfg in configs:
            has_sensitive = any(getattr(cfg, f, None) for f in _SENSITIVE_FIELDS)
            if has_sensitive:
                configs_to_encrypt.append(cfg)

        if not configs_to_encrypt:
            logger.info("所有飞书渠道均无敏感字段需要加密，无需处理。")
            return

        if args.dry_run:
            # dry-run 模式：仅打印待加密的渠道，不写入
            for cfg in configs_to_encrypt:
                plain_fields = {f: getattr(cfg, f, None) for f in _SENSITIVE_FIELDS if getattr(cfg, f, None)}
                logger.info(
                    "[DRY-RUN] Agent {}: 待加密字段={}, credential_encrypted_version={}",
                    cfg.agent_id,
                    list(plain_fields.keys()),
                    cfg.credential_encrypted_version,
                )
            return

        # 实际执行：加密敏感字段并设置版本号
        success_count = 0
        fail_count = 0

        for cfg in configs_to_encrypt:
            # 逐个加密敏感字段
            all_encrypted = True
            for field in _SENSITIVE_FIELDS:
                value = getattr(cfg, field, None)
                if not value:
                    continue

                # 检查是否已是密文：尝试解密，若成功则跳过
                try:
                    decrypt_data(value, key)
                    # 解密成功 → 已是密文，无需重复加密
                    continue
                except ValueError:
                    pass

                # 明文 → 加密
                try:
                    setattr(cfg, field, encrypt_data(value, key))
                except Exception as e:
                    logger.warning(
                        "[SKIP] Agent {} field '{}' 加密失败: {}",
                        cfg.agent_id,
                        field,
                        e,
                    )
                    all_encrypted = False

            if not all_encrypted:
                fail_count += 1
                logger.error(
                    "[FAIL] Agent {} ({}) 部分敏感字段加密失败",
                    cfg.agent_id,
                    cfg.channel_type,
                )
                continue

            cfg.credential_encrypted_version = 1
            success_count += 1
            logger.info(
                "[OK] Agent {} ({}) 凭据已加密",
                cfg.agent_id,
                cfg.channel_type,
            )

        await db.commit()
        logger.info(
            "加密完成：成功 {}/{}，失败 {}",
            success_count,
            len(configs_to_encrypt),
            fail_count,
        )


if __name__ == "__main__":
    asyncio.run(main())
