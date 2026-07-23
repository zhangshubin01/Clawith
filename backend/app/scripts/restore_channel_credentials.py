"""回滚脚本：将已加密的渠道凭据恢复为明文。

查找 credential_encrypted_version >= 1 的渠道配置，
使用 decrypt_data() 解密存储在 app_secret / encrypt_key / verification_token 中的密文，
然后置 credential_encrypted_version = 0。

使用方式：
  --dry-run: 仅预览受影响的行
  无参数: 实际执行回滚

  Docker:  docker exec clawith-backend-1 python3 -m app.scripts.restore_channel_credentials
  Docker(dry-run): docker exec clawith-backend-1 python3 -m app.scripts.restore_channel_credentials --dry-run
  源码:  cd backend && python3 -m app.scripts.restore_channel_credentials
"""

import argparse
import asyncio

from loguru import logger

# 需加密的敏感字段列表（与加密阶段保持一致）
_SENSITIVE_FIELDS = ["app_secret", "encrypt_key", "verification_token"]


async def main():
    parser = argparse.ArgumentParser(description="将已加密的渠道凭据恢复为明文")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    args = parser.parse_args()

    # 导入所有模型以确保 SQLAlchemy 正确解析关系
    from app.models import (  # noqa: F401
        agent, channel_config, chat_session, audit, org, user, tenant,  # 核心模型
    )
    from app.core.security import decrypt_data
    from app.config import get_settings
    from app.database import async_session
    from app.models.channel_config import ChannelConfig

    settings = get_settings()
    key = settings.SECRET_KEY

    async with async_session() as db:
        # 查询所有已加密的渠道（credential_encrypted_version >= 1）
        from sqlalchemy import select

        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.credential_encrypted_version >= 1)
        )
        configs = result.scalars().all()

        if not configs:
            logger.info("未找到已加密的渠道凭据，无需回滚。")
            return

        logger.info(f"找到 {len(configs)} 个已加密的渠道配置")

        if args.dry_run:
            for cfg in configs:
                encrypted_fields = {
                    f: getattr(cfg, f, None)
                    for f in _SENSITIVE_FIELDS
                    if getattr(cfg, f, None)
                }
                logger.info(
                    "[DRY-RUN] Agent {} ({}): 待解密字段={}, credential_encrypted_version={}",
                    cfg.agent_id,
                    cfg.channel_type,
                    list(encrypted_fields.keys()),
                    cfg.credential_encrypted_version,
                )
            return

        # 实际执行：解密并恢复为明文
        success_count = 0
        for cfg in configs:
            try:
                # 逐个解密敏感字段
                for field in _SENSITIVE_FIELDS:
                    encrypted_value = getattr(cfg, field, None)
                    if not encrypted_value:
                        continue
                    try:
                        plaintext = decrypt_data(encrypted_value, key)
                        setattr(cfg, field, plaintext)
                    except ValueError:
                        logger.warning(
                            "[SKIP] Agent {} field '{}' 解密失败（可能已是明文），跳过",
                            cfg.agent_id,
                            field,
                        )

                cfg.credential_encrypted_version = 0
                success_count += 1
                logger.info(
                    "[OK] Agent {} ({}) 凭据已恢复为明文",
                    cfg.agent_id,
                    cfg.channel_type,
                )
            except Exception as e:
                logger.error(
                    "[FAIL] Agent {} ({}) 回滚失败: {}",
                    cfg.agent_id,
                    cfg.channel_type,
                    e,
                )

        await db.commit()
        logger.info("回滚完成：{}/{} 个渠道凭据已恢复为明文", success_count, len(configs))


if __name__ == "__main__":
    asyncio.run(main())
