-- 飞书渠道 encrypt_key 数据迁移检查脚本
-- 背景：encrypt_key 是飞书事件订阅的安全配置项，新渠道必填，
--       历史渠道可能缺少此字段（NULL 或空字符串），需手动从飞书开放平台获取后填入。
-- 使用方式: psql -d <database> -f backfill_feishu_encrypt_key.sql
-- 或直接连接 PostgreSQL 执行

SELECT id, agent_id, app_id, created_at,
       CASE
           WHEN encrypt_key IS NULL OR encrypt_key = '' THEN 'MISSING'
           ELSE 'OK'
       END AS encrypt_key_status,
       CASE
           WHEN encrypt_key IS NULL OR encrypt_key = '' THEN '需要手动从飞书开放平台获取 encrypt_key 并更新'
           ELSE '已配置，无需操作'
       END AS action_required
FROM channel_configs
WHERE channel_type = 'feishu' AND is_configured = true
ORDER BY encrypt_key_status, created_at;
