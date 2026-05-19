import {
    IconBrowser, IconClock, IconFileText, IconMessageCircle,
    IconSearch, IconSettings, IconTerminal2, IconTools,
} from '@tabler/icons-react';
import { getCategoryLabels } from './toolConstants';

export const categoryDescriptions: Record<string, string> = {
    agentbay: 'Browser and cloud computer automation',
    file: 'Read, write, convert, and manage workspace files',
    communication: 'Messages and cross-channel collaboration',
    search: 'Web and knowledge search tools',
    code: 'Code execution and development utilities',
    aware: 'Triggers, reminders, and awareness workflows',
    email: 'Email reading and sending tools',
    feishu: 'Feishu / Lark messaging and collaboration',
    okr: 'Objectives, key results, and progress reporting',
    social: 'Social publishing and community workflows',
    discovery: 'Tool and capability discovery',
    custom: 'Company-added or MCP tools',
    general: 'General purpose tools',
};

export const renderCategoryIcon = (category: string, size = 15) => {
    const style = { color: 'var(--text-tertiary)' };
    switch (category) {
        case 'agentbay': return <IconBrowser size={size} stroke={1.8} style={style} />;
        case 'file': return <IconFileText size={size} stroke={1.8} style={style} />;
        case 'communication': case 'feishu': case 'email': case 'social':
            return <IconMessageCircle size={size} stroke={1.8} style={style} />;
        case 'search': case 'discovery':
            return <IconSearch size={size} stroke={1.8} style={style} />;
        case 'code': return <IconTerminal2 size={size} stroke={1.8} style={style} />;
        case 'aware': return <IconClock size={size} stroke={1.8} style={style} />;
        case 'custom': return <IconSettings size={size} stroke={1.8} style={style} />;
        default: return <IconTools size={size} stroke={1.8} style={style} />;
    }
};

export const getToolGroupMeta = (groupKey: string, toolsInGroup: any[], t: any) => {
    const labels = getCategoryLabels(t);
    const first = toolsInGroup.find((tool: any) => tool.type === 'mcp' && tool.mcp_server_name) || toolsInGroup[0];
    if (groupKey.startsWith('mcp:') && first?.mcp_server_name) {
        return {
            label: first.mcp_server_name,
            description: t('agent.tools.mcpGroupDescription', 'Tools from {{name}}', { name: first.mcp_server_name }),
            iconCategory: 'custom',
            configCategory: first.category || 'custom',
        };
    }
    return {
        label: labels[groupKey] || groupKey,
        description: categoryDescriptions[groupKey] || 'Tools in this category',
        iconCategory: groupKey,
        configCategory: groupKey,
    };
};
