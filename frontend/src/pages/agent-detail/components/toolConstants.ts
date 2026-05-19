// 工具分类标签、敏感字段集合等纯常量定义。
// 从 ToolsManager.tsx 提取以减小组件体积。

export const getCategoryLabels = (t: any): Record<string, string> => ({
    file: t('agent.toolCategories.file'),
    task: t('agent.toolCategories.task'),
    communication: t('agent.toolCategories.communication'),
    search: t('agent.toolCategories.search'),
    aware: t('agent.toolCategories.aware', 'Aware & Triggers'),
    social: t('agent.toolCategories.social', 'Social'),
    code: t('agent.toolCategories.code', 'Code & Execution'),
    discovery: t('agent.toolCategories.discovery', 'Discovery'),
    email: t('agent.toolCategories.email', 'Email'),
    feishu: t('agent.toolCategories.feishu', 'Feishu / Lark'),
    custom: t('agent.toolCategories.custom'),
    general: t('agent.toolCategories.general'),
    agentbay: t('agent.toolCategories.agentbay', 'AgentBay'),
});

export const CATEGORY_CONFIG_SCHEMAS: Record<string, any> = {
    agentbay: {
        title: 'AgentBay Settings',
        fields: [
            { key: 'api_key', label: 'API Key (from AgentBay)', type: 'password', placeholder: 'Enter your AgentBay API key' },
            { key: 'os_type', label: 'Cloud Computer OS', type: 'select', default: 'windows', options: [{ value: 'linux', label: 'Linux' }, { value: 'windows', label: 'Windows' }] },
        ]
    },
    atlassian: {
        title: 'Atlassian Connectivity Settings',
        fields: [
            { key: 'api_key', label: 'API Key (Atlassian API Token)', type: 'password', placeholder: 'Enter your Atlassian API key' },
            { key: 'cloud_id', label: 'Cloud ID (Optional)', type: 'text', placeholder: 'e.g. bcc01-abc-123' }
        ]
    }
};

/** 敏感字段名集合：不应从 masked 全局配置预填充 */
export const SENSITIVE_KEYS_BASE = new Set(['api_key', 'private_key', 'auth_code', 'password', 'secret']);

/** 从 config_schema 动态提取 password 类型字段，与 SENSITIVE_KEYS_BASE 合并 */
export const getSensitiveKeys = (schema: any): Set<string> => {
    const keys = new Set(SENSITIVE_KEYS_BASE);
    if (schema?.fields) {
        for (const field of schema.fields) {
            if (field.type === 'password') keys.add(field.key);
        }
    }
    return keys;
};

/** Switch 开关轨道样式 */
export const switchTrack = (enabled: boolean, mixed = false) => ({
    position: 'absolute' as const, inset: 0,
    background: enabled ? 'var(--accent-primary)' : mixed ? 'var(--border-default)' : 'var(--bg-tertiary)',
    borderRadius: '11px', transition: 'background 0.2s',
});

/** Switch 开关滑块样式 */
export const switchKnob = (enabled: boolean) => ({
    position: 'absolute' as const, left: enabled ? '20px' : '2px', top: '2px',
    width: '18px', height: '18px', background: '#fff',
    borderRadius: '50%', transition: 'left 0.2s',
    boxShadow: '0 1px 3px rgba(0,0,0,0.12)',
});

/** 将 schema 字段默认值合并到现有配置对象，跳过已有值（含 null/空串） */
export const applyConfigDefaults = (fields: any[] = [], config: Record<string, any> = {}) => {
    const next = { ...config };
    for (const field of fields) {
        if (field.default !== undefined && (next[field.key] === undefined || next[field.key] === null || next[field.key] === '')) {
            next[field.key] = field.default;
        }
    }
    return next;
};
