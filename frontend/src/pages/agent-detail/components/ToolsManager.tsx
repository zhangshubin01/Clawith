import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconBrowser, IconChevronDown, IconClock, IconFileText, IconMessageCircle, IconSearch, IconSettings, IconTerminal2, IconTools } from '@tabler/icons-react';

import { useDialog } from '../../../components/Dialog/DialogProvider';
import { useToast } from '../../../components/Toast/ToastProvider';
import { useAuthStore } from '../../../stores';
import { getCategoryLabels, CATEGORY_CONFIG_SCHEMAS, SENSITIVE_KEYS_BASE, getSensitiveKeys, applyConfigDefaults, switchTrack, switchKnob } from './toolConstants';
import { categoryDescriptions, renderCategoryIcon, getToolGroupMeta } from './toolCategoryUtils';
import ToolSourceTabs from './ToolSourceTabs';
import ToolSearchBar from './ToolSearchBar';

export default function ToolsManager({ agentId, canManage = false }: { agentId: string; canManage?: boolean }) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const [tools, setTools] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    const [configTool, setConfigTool] = useState<any | null>(null);
    const [configData, setConfigData] = useState<Record<string, any>>({});
    const [configJson, setConfigJson] = useState('');
    const [configSaving, setConfigSaving] = useState(false);
    const [toolTab, setToolTab] = useState<'company' | 'installed'>('company');
    const [deletingToolId, setDeletingToolId] = useState<string | null>(null);
    const [configCategory, setConfigCategory] = useState<string | null>(null);
    const [focusedField, setFocusedField] = useState<string | null>(null);
    const [showAdvancedToolConfig, setShowAdvancedToolConfig] = useState(false);
    const [expandedCategories, setExpandedCategories] = useState<Set<string>>(() => new Set());
    const [toolSearch, setToolSearch] = useState('');
    const [toolStatusFilter, setToolStatusFilter] = useState<'all' | 'enabled' | 'disabled' | 'configured'>('all');
    // Global (company-level) config for the currently open modal — used to show
    // lock hints and prevent agent from overriding company-set fields.
    const [configGlobalData, setConfigGlobalData] = useState<Record<string, any>>({});

    const loadTools = async () => {
        try {
            const token = localStorage.getItem('token');
            const res = await fetch(`/api/tools/agents/${agentId}/with-config`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (res.ok) setTools(await res.json());
            else {
                // Fallback to old endpoint
                const res2 = await fetch(`/api/tools/agents/${agentId}`, { headers: { Authorization: `Bearer ${token}` } });
                if (res2.ok) setTools(await res2.json());
            }
        } catch (e) { console.error('[ToolsManager] Failed to load tools:', String(e)); }
        setLoading(false);
    };

    useEffect(() => { loadTools(); }, [agentId]);

    const toggleTool = async (toolId: string, enabled: boolean) => {
        setTools(prev => prev.map(t => t.id === toolId ? { ...t, enabled } : t));
        try {
            const token = localStorage.getItem('token');
            await fetch(`/api/tools/agents/${agentId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                body: JSON.stringify([{ tool_id: toolId, enabled }]),
            });
        } catch (e) { console.error('[ToolsManager] Failed to toggle tool:', String(e)); }
    };

    const openConfig = (tool: any) => {
        setConfigTool(tool);
        setShowAdvancedToolConfig(false);
        // Build merged config: start with global defaults, overlay agent overrides.
        // For sensitive fields, only use agent_config values (global ones are masked
        // like "****xxxx" and should not pre-fill the input).
        const sensitiveKeys = getSensitiveKeys(tool.config_schema);
        const globalCfg = tool.global_config || {};
        const agentCfg = tool.agent_config || {};
        const merged: Record<string, any> = {};
        for (const [k, v] of Object.entries(globalCfg)) {
            if (!sensitiveKeys.has(k)) merged[k] = v;
        }
        Object.assign(merged, agentCfg);
        Object.assign(merged, applyConfigDefaults(tool.config_schema?.fields || [], merged));
        setConfigData(merged);
        setConfigJson(JSON.stringify(agentCfg, null, 2));
        setFocusedField(null);
    };

    const openCategoryConfig = async (category: string) => {
        setConfigCategory(category);
        setShowAdvancedToolConfig(false);
        setConfigData({});
        setConfigGlobalData({});
        setConfigSaving(true);
        setFocusedField(null);
        try {
            const token = localStorage.getItem('token');
            const res = await fetch(`/api/tools/agents/${agentId}/category-config/${category}`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (res.ok) {
                const data = await res.json();
                // global_config: company-level (masked sensitive fields like ****xxxx)
                // agent_config: agent-level overrides only
                const globalCfg = data.global_config || {};
                const agentCfg = data.agent_config || {};
                setConfigGlobalData(globalCfg);
                // Pre-fill only agent-level values; company fields show as hints
                const catSchema = CATEGORY_CONFIG_SCHEMAS[category];
                const sensitiveKeys = getSensitiveKeys(catSchema);
                const merged: Record<string, any> = {};
                for (const [k, v] of Object.entries(globalCfg)) {
                    // Non-sensitive global fields (e.g. os_type) pre-fill; sensitive ones don't
                    if (!sensitiveKeys.has(k)) merged[k] = v;
                }
                Object.assign(merged, agentCfg);
                setConfigData(merged);
            }
        } catch (e) { console.error('[ToolsManager] Failed to load category config:', String(e)); }
    };

    const saveConfig = async () => {
        if (!configTool && !configCategory) return;
        setConfigSaving(true);
        try {
            const token = localStorage.getItem('token');

            if (configCategory) {
                const raw = configData;
                // Strip empty sensitive fields so untouched password inputs
                // don't send empty values that would clear an inherited company key
                const catSchema = CATEGORY_CONFIG_SCHEMAS[configCategory!];
                const sensitiveKeys = getSensitiveKeys(catSchema);
                const payload: Record<string, any> = {};
                for (const [k, v] of Object.entries(raw)) {
                    if (sensitiveKeys.has(k) && (v === '' || v === undefined || v === null)) continue;
                    payload[k] = v;
                }
                await fetch(`/api/tools/agents/${agentId}/category-config/${configCategory}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                    body: JSON.stringify({ config: payload }),
                });
                setConfigCategory(null);
            } else {
                const hasSchema = configTool.config_schema?.fields?.length > 0;
                const raw = hasSchema ? configData : JSON.parse(configJson || '{}');
                // Strip empty sensitive fields only — agent CAN override company values
                const sensitiveKeys = getSensitiveKeys(configTool.config_schema);
                const payload: Record<string, any> = {};
                for (const [k, v] of Object.entries(raw)) {
                    if (sensitiveKeys.has(k) && (v === '' || v === undefined || v === null)) continue;
                    payload[k] = v;
                }
                await fetch(`/api/tools/agents/${agentId}/tool-config/${configTool.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                    body: JSON.stringify({ config: payload }),
                });
                setConfigTool(null);
            }
            loadTools();
        } catch (e: any) { toast.error(t('common.error.saveFailed'), { details: String(e?.message || e) }); }
        setConfigSaving(false);
    };

    if (loading) return <div style={{ color: 'var(--text-tertiary)', padding: '20px' }}>{t('common.loading')}</div>;

    // Company tools = platform presets (builtin) + company admin-added tools (admin)
    // Hide system-internal tools (e.g. finish) — they are protocol-level and not user-facing.
    const companyTools = tools.filter(t => (t.source === 'builtin' || t.source === 'admin') && t.category !== 'system');
    const agentInstalledTools = tools.filter(t => t.source === 'agent' && t.category !== 'system');

    const mcpGroupKey = (tool: any) => {
        const serverName = String(tool.mcp_server_name || '').trim();
        return tool.type === 'mcp' && serverName
            ? `mcp:${serverName.toLowerCase()}`
            : (tool.category || 'general');
    };

    const groupByCategory = (toolList: any[]) =>
        toolList.reduce((acc: Record<string, any[]>, t) => {
            const cat = mcpGroupKey(t);
            (acc[cat] = acc[cat] || []).push(t);
            return acc;
        }, {});

    const toggleCategoryExpanded = (category: string) => {
        setExpandedCategories(prev => {
            const next = new Set(prev);
            if (next.has(category)) next.delete(category);
            else next.add(category);
            return next;
        });
    };

    const bulkToggleCategory = async (catTools: any[], enabled: boolean) => {
        const catToolIds = new Set(catTools.map(t => t.id));
        setTools(prev => prev.map(t => catToolIds.has(t.id) ? { ...t, enabled } : t));
        try {
            const token = localStorage.getItem('token');
            const payload = Array.from(catToolIds).map(id => ({ tool_id: id, enabled }));
            await fetch(`/api/tools/agents/${agentId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                body: JSON.stringify(payload),
            });
        } catch (err: any) {
            console.error('[Tools] Bulk update failed', err);
            loadTools();
        }
    };

    const renderToolRow = (tool: any, category: string) => {
        const hasConfig = tool.config_schema?.fields?.length > 0 || tool.type === 'mcp';
        const hasAgentOverride = tool.agent_config && Object.keys(tool.agent_config).length > 0;
        const isGlobalCategoryConfig = category === 'agentbay' && tool.name === 'agentbay_browser_navigate';
        return (
            <div key={tool.id} style={{
                display: 'grid',
                gridTemplateColumns: 'minmax(0, 1fr) auto',
                alignItems: 'center',
                gap: '12px',
                padding: '10px 14px',
                borderTop: '1px solid var(--border-subtle)',
                background: 'var(--bg-primary)',
            }}>
                <div style={{ minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0 }}>
                        <span style={{ fontWeight: 500, fontSize: '13px', color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tool.display_name}</span>
                        {tool.type === 'mcp' && (
                            <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>MCP</span>
                        )}
                        {tool.type === 'builtin' && (
                            <span style={{ fontSize: '10px', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>Built-in</span>
                        )}
                        {hasAgentOverride && (
                            <span style={{ fontSize: '10px', background: 'rgba(99,102,241,0.15)', color: 'var(--accent-color)', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>{t('enterprise.tools.configured', 'Configured')}</span>
                        )}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {tool.description}
                        {tool.mcp_server_name && <span> · {tool.mcp_server_name}</span>}
                    </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                    {canManage && hasConfig && !isGlobalCategoryConfig && (
                        <button
                            onClick={() => openConfig(tool)}
                            style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                            title={t('agent.tools.configurePerAgent', 'Configure per-agent settings')}
                        ><IconSettings size={12} stroke={1.8} /> {t('agent.tools.config', 'Config')}</button>
                    )}
                    {canManage && tool.source === 'agent' && tool.agent_tool_id && (
                        <button
                            onClick={async () => {
                                const ok = await dialog.confirm(
                                    t('agent.tools.confirmDelete', `Remove "${tool.display_name}" from this agent?`),
                                    { danger: true, confirmLabel: t('common.confirmActions.removeLabel') },
                                );
                                if (!ok) return;
                                setDeletingToolId(tool.id);
                                try {
                                    const token = localStorage.getItem('token');
                                    const res = await fetch(`/api/tools/agent-tool/${tool.agent_tool_id}`, {
                                        method: 'DELETE',
                                        headers: { Authorization: `Bearer ${token}` },
                                    });
                                    if (res.ok) await loadTools();
                                    else toast.error(t('agent.tools.deleteFailed', 'Delete failed'));
                                } catch (e: any) { toast.error(t('agent.tools.deleteFailed', 'Delete failed'), { details: String(e?.message || e) }); }
                                setDeletingToolId(null);
                            }}
                            disabled={deletingToolId === tool.id}
                            style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-tertiary)', opacity: deletingToolId === tool.id ? 0.5 : 1 }}
                            title={t('agent.tools.removeTool', 'Remove from agent')}
                        >{deletingToolId === tool.id ? '...' : '✕'}</button>
                    )}
                    {canManage ? (
                        <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer', flexShrink: 0 }}>
                            <input
                                type="checkbox"
                                checked={tool.enabled}
                                onChange={e => toggleTool(tool.id, e.target.checked)}
                                style={{ opacity: 0, width: 0, height: 0 }}
                            />
                            <span style={switchTrack(tool.enabled)}>
                                <span style={switchKnob(tool.enabled)} />
                            </span>
                        </label>
                    ) : (
                        <span style={{ fontSize: '11px', color: tool.enabled ? 'var(--accent-primary)' : 'var(--text-tertiary)', fontWeight: 500 }}>
                            {tool.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                        </span>
                    )}
                </div>
            </div>
        );
    };

    const renderToolGroup = (groupedTools: Record<string, any[]>, allGroupedTools: Record<string, any[]>) =>
        Object.entries(groupedTools)
            .sort(([a, aTools], [b, bTools]) => {
                const aMeta = getToolGroupMeta(a, allGroupedTools[a] || aTools, t);
                const bMeta = getToolGroupMeta(b, allGroupedTools[b] || bTools, t);
                return aMeta.label.localeCompare(bMeta.label);
            })
            .map(([category, catTools]) => {
                const allCatTools = allGroupedTools[category] || catTools;
                const meta = getToolGroupMeta(category, allCatTools, t);
                const label = meta.label;
                const enabledCount = allCatTools.filter((tool: any) => tool.enabled).length;
                const configuredCount = allCatTools.filter((tool: any) => tool.agent_config && Object.keys(tool.agent_config).length > 0).length;
                const allEnabled = allCatTools.length > 0 && enabledCount === allCatTools.length;
                const mixed = enabledCount > 0 && enabledCount < allCatTools.length;
                const expanded = expandedCategories.has(category) || !!toolSearch.trim();
                const visibleCount = (catTools as any[]).length;
                return (
                    <div key={category} style={{
                        border: '1px solid var(--border-subtle)',
                        borderRadius: '8px',
                        overflow: 'hidden',
                        background: 'var(--bg-primary)',
                    }}>
                        <div
                            role="button"
                            tabIndex={0}
                            onClick={() => toggleCategoryExpanded(category)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') {
                                    e.preventDefault();
                                    toggleCategoryExpanded(category);
                                }
                            }}
                            style={{
                                width: '100%',
                                background: 'var(--bg-secondary)',
                                padding: '13px 16px',
                                display: 'grid',
                                gridTemplateColumns: '1fr auto',
                                gap: '14px',
                                alignItems: 'center',
                                cursor: 'pointer',
                                textAlign: 'left',
                                boxSizing: 'border-box',
                            }}
                        >
                            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}>
                                <IconChevronDown
                                    size={16}
                                    style={{
                                        transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)',
                                        transition: 'transform 120ms ease',
                                        color: 'var(--text-tertiary)',
                                        flexShrink: 0,
                                    }}
                                />
                                <span style={{
                                    width: '28px',
                                    height: '28px',
                                    borderRadius: '7px',
                                    border: '1px solid var(--border-subtle)',
                                    background: 'var(--bg-primary)',
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    flexShrink: 0,
                                }}>{renderCategoryIcon(meta.iconCategory, 16)}</span>
                                <div style={{ minWidth: 0 }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                                        <span style={{ fontSize: '13px', fontWeight: 650, color: 'var(--text-primary)' }}>{label}</span>
                                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {allCatTools.length} tools · {enabledCount} enabled
                                            {visibleCount !== allCatTools.length ? ` · ${visibleCount} shown` : ''}
                                        </span>
                                        {configuredCount > 0 && (
                                            <span style={{ fontSize: '10px', background: 'rgba(99,102,241,0.15)', color: 'var(--accent-color)', borderRadius: '4px', padding: '1px 5px' }}>
                                                {configuredCount} configured
                                            </span>
                                        )}
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {meta.description}
                                    </div>
                                </div>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }} onClick={(e) => e.stopPropagation()}>
                                {CATEGORY_CONFIG_SCHEMAS[meta.configCategory] && canManage && (
                                    <button
                                        type="button"
                                        onClick={() => openCategoryConfig(meta.configCategory)}
                                        style={{ background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '4px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                                        title={t('agent.tools.configureCategory', 'Configure {{category}}', { category: label })}
                                    ><IconSettings size={12} stroke={1.8} /> {t('agent.tools.config', 'Config')}</button>
                                )}
                                {canManage && (
                                    <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer', flexShrink: 0 }} title={t('agent.tools.enableDisableAll', 'Enable/Disable all {{category}} tools', { category: label })}>
                                        <input type="checkbox"
                                            checked={allEnabled}
                                            onChange={(e) => void bulkToggleCategory(allCatTools, e.target.checked)}
                                            style={{ opacity: 0, width: 0, height: 0 }} />
                                        <span style={switchTrack(allEnabled, mixed)}>
                                            <span style={switchKnob(allEnabled)} />
                                        </span>
                                    </label>
                                )}
                            </div>
                        </div>
                        {expanded && (
                            <div>
                                {(catTools as any[]).map((tool: any) => renderToolRow(tool, category))}
                            </div>
                        )}
                    </div>
                );
            });

    const activeTools = toolTab === 'company' ? companyTools : agentInstalledTools;
    const normalizedToolSearch = toolSearch.trim().toLowerCase();
    const matchesToolSearch = (tool: any) => {
        if (!normalizedToolSearch) return true;
        const category = tool.category || 'general';
        const haystack = [
            tool.name,
            tool.display_name,
            tool.description,
            tool.mcp_server_name,
            category,
            getCategoryLabels(t)[category],
        ].filter(Boolean).join(' ').toLowerCase();
        return haystack.includes(normalizedToolSearch);
    };
    const matchesStatusFilter = (tool: any) => {
        if (toolStatusFilter === 'enabled') return !!tool.enabled;
        if (toolStatusFilter === 'disabled') return !tool.enabled;
        if (toolStatusFilter === 'configured') return !!(tool.agent_config && Object.keys(tool.agent_config).length > 0);
        return true;
    };
    const filteredTools = activeTools.filter(tool => matchesToolSearch(tool) && matchesStatusFilter(tool));
    const groupedActiveTools = groupByCategory(activeTools);
    const groupedFilteredTools = groupByCategory(filteredTools);
    const hasFilters = !!normalizedToolSearch || toolStatusFilter !== 'all';

    return (
        <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <ToolSourceTabs toolTab={toolTab} setToolTab={setToolTab} companyCount={companyTools.length} installedCount={agentInstalledTools.length} />

                <ToolSearchBar toolSearch={toolSearch} setToolSearch={setToolSearch} toolStatusFilter={toolStatusFilter} setToolStatusFilter={setToolStatusFilter} expandedCount={expandedCategories.size} totalCategories={Object.keys(groupedActiveTools).length} onToggleExpand={() => { const categories = Object.keys(groupedActiveTools); setExpandedCategories(prev => prev.size >= categories.length ? new Set() : new Set(categories)); }} />

                {/* Tool List */}
                {filteredTools.length > 0 ? (
                    renderToolGroup(groupedFilteredTools, groupedActiveTools)
                ) : (
                    <div className="card" style={{ textAlign: 'center', padding: '30px', color: 'var(--text-tertiary)' }}>
                        {hasFilters ? t('agent.tools.noMatchingTools', 'No matching tools') : toolTab === 'installed' ? t('agent.tools.noInstalled', 'No agent-installed tools yet') : t('agent.tools.noCompany', 'No company-configured tools')}
                    </div>
                )}
            </div>
            {tools.length === 0 && (
                <div className="card" style={{ textAlign: 'center', padding: '30px', color: 'var(--text-tertiary)' }}>
                    {t('common.noData')}
                </div>
            )}

            {/* Tool Config Modal */}
            {(configTool || configCategory) && (() => {
                const target = configTool || CATEGORY_CONFIG_SCHEMAS[configCategory!];
                const fields = configTool ? (configTool.config_schema?.fields || []) : (target.fields || []);
                const title = configTool ? configTool.display_name : target.title;
                const isCat = !!configCategory;
                const visibleFields = fields.filter((field: any) => {
                    if (!field.depends_on) return true;
                    return Object.entries(field.depends_on).every(([depKey, depVals]: [string, any]) =>
                        (depVals as string[]).includes(configData[depKey] ?? '')
                    );
                });
                const primaryFields = visibleFields.filter((field: any) => !field.advanced);
                const advancedFields = visibleFields.filter((field: any) => field.advanced);
                return (
                    <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                        onClick={() => { setConfigTool(null); setConfigCategory(null); }}>
                        <div onClick={e => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                                <div>
                                    <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}><IconSettings size={20} stroke={1.8} /> {title}</h3>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{isCat ? 'Shared category configuration (affects all tools in this category)' : 'Per-agent configuration (overrides global defaults)'}</div>
                                </div>
                                <button onClick={() => { setConfigTool(null); setConfigCategory(null); }} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}>✕</button>
                            </div>

                            {fields.length > 0 ? (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                                    {primaryFields
                                        .map((field: any) => {
                                            // Get user role from store directly in the map function
                                            const userFromStore = useAuthStore.getState().user;
                                            const currentUserRole = userFromStore?.role;
                                            const isReadOnly = field.read_only_for_roles?.includes(currentUserRole);
                                            return (
                                                <div key={field.key}>
                                                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>
                                                        {field.label}
                                                        {isReadOnly && <span style={{ fontWeight: 400, color: 'var(--text-tertiary)', marginLeft: '4px' }}>(Admin only)</span>}
                                                        {/* Show company-configured value as a hint in the label */}
                                                        {(() => {
                                                            const globalVal = configTool?.global_config?.[field.key] ?? configGlobalData?.[field.key];
                                                            if (!globalVal) return null;
                                                            return (
                                                                <span style={{ fontWeight: 400, color: 'var(--accent-primary)', marginLeft: '4px', fontSize: '11px' }}>
                                                                    (company: {String(globalVal).slice(0, 20)}{String(globalVal).length > 20 ? '\u2026' : ''})
                                                                </span>
                                                            );
                                                        })()}
                                                    </label>
                                                    {field.type === 'checkbox' ? (
                                                        <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: isReadOnly ? 'not-allowed' : 'pointer' }}>
                                                            <input
                                                                type="checkbox"
                                                                checked={configData[field.key] ?? field.default ?? false}
                                                                disabled={isReadOnly}
                                                                onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.checked }))}
                                                                style={{ opacity: 0, width: 0, height: 0 }}
                                                            />
                                                            <span style={{
                                                                position: 'absolute', inset: 0,
                                                                background: (configData[field.key] ?? field.default) ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                                                                borderRadius: '11px', transition: 'background 0.2s', opacity: isReadOnly ? 0.6 : 1,
                                                            }}>
                                                                <span style={{
                                                                    position: 'absolute', left: (configData[field.key] ?? field.default) ? '20px' : '2px', top: '2px',
                                                                    width: '18px', height: '18px', background: '#fff',
                                                                    borderRadius: '50%', transition: 'left 0.2s',
                                                                }} />
                                                            </span>
                                                        </label>
                                                    ) : field.type === 'password' ? (
                                                        <>
                                                        {(() => {
                                                            const globalVal = configTool?.global_config?.[field.key] ?? configGlobalData?.[field.key];
                                                            const isUsingGlobal = globalVal && !configData[field.key];
                                                            
                                                            if (isUsingGlobal && focusedField !== field.key) {
                                                                return (
                                                                    <div 
                                                                        className="form-input" 
                                                                        style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'text', background: 'var(--bg-tertiary)', borderColor: 'var(--border)', overflow: 'hidden' }}
                                                                        onClick={() => setFocusedField(field.key)}
                                                                    >
                                                                        <span style={{ flex: 1, color: 'var(--text-tertiary)', fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t('agent.tools.usingCompanyKey', 'Using company key ({{val}})', { val: globalVal })}</span>
                                                                        <span style={{ fontSize: '12px', color: 'var(--accent-primary)', flexShrink: 0, cursor: 'pointer' }}>{t('common.edit', 'Edit')}</span>
                                                                    </div>
                                                                );
                                                            }

                                                            return (
                                                                <input type="password" autoComplete="new-password" className="form-input"
                                                                    autoFocus={focusedField === field.key}
                                                                    value={configData[field.key] ?? ''}
                                                                    placeholder={globalVal ? t('agent.tools.usingCompanyKey', 'Using company key ({{val}})', { val: globalVal }) : (field.placeholder || t('admin.leaveBlankDefault', 'Leave blank to use global default'))}
                                                                    onBlur={(e) => {
                                                                        if (!e.target.value) setFocusedField(null);
                                                                    }}
                                                                    onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))} />
                                                            );
                                                        })()}
                                                        {/* Per-provider help text for auth_code */}
                                                        {field.key === 'auth_code' && (() => {
                                                            const providerField = configTool?.config_schema?.fields?.find((f: any) => f.key === 'email_provider');
                                                            const selectedProvider = configData['email_provider'] || providerField?.default || '';
                                                            const providerOption = providerField?.options?.find((o: any) => o.value === selectedProvider);
                                                            if (!providerOption?.help_text) return null;
                                                            return (
                                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', lineHeight: '1.5' }}>
                                                                    {providerOption.help_text}
                                                                    {providerOption.help_url && (
                                                                        <> &middot; <a href={providerOption.help_url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent-primary)', textDecoration: 'none' }}>Setup guide</a></>
                                                                    )}
                                                                </div>
                                                            );
                                                        })()}

                                                        </>
                                                    ) : field.type === 'select' ? (
                                                        <select className="form-input" value={configData[field.key] ?? field.default ?? ''}
                                                            onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}>
                                                            {(field.options || []).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                                        </select>
                                                    ) : field.type === 'number' ? (
                                                        <input type="number" className="form-input" value={configData[field.key] ?? field.default ?? ''} placeholder={field.placeholder || ''} min={field.min} max={field.max} onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value ? Number(e.target.value) : '' }))} />
                                                    ) : field.type === 'textarea' ? (
                                                        <textarea
                                                            className="form-input"
                                                            value={configData[field.key] ?? ''}
                                                            placeholder={field.placeholder || t('admin.leaveBlankDefault', 'Leave blank to use global default')}
                                                            rows={Math.max(3, Math.min(10, String(configData[field.key] ?? field.default ?? field.placeholder ?? '').split('\n').length))}
                                                            style={{ minHeight: '88px', fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)', resize: 'vertical' }}
                                                            onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}
                                                        />
                                                    ) : (
                                                        <>
                                                        {(() => {
                                                            const globalVal = configTool?.global_config?.[field.key] ?? configGlobalData?.[field.key];
                                                            const isUsingGlobal = globalVal && !configData[field.key];
                                                            
                                                            if (isUsingGlobal && focusedField !== field.key) {
                                                                return (
                                                                    <div 
                                                                        className="form-input" 
                                                                        style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'text', background: 'var(--bg-tertiary)', borderColor: 'var(--border)', overflow: 'hidden' }}
                                                                        onClick={() => setFocusedField(field.key)}
                                                                    >
                                                                        <span style={{ flex: 1, color: 'var(--text-tertiary)', fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t('agent.tools.usingCompanyConfig', 'Using company config ({{val}})', { val: globalVal })}</span>
                                                                        <span style={{ fontSize: '12px', color: 'var(--accent-primary)', flexShrink: 0, cursor: 'pointer' }}>{t('common.edit', 'Edit')}</span>
                                                                    </div>
                                                                );
                                                            }

                                                            return (
                                                                <input type="text" className="form-input"
                                                                    autoFocus={focusedField === field.key}
                                                                    value={configData[field.key] ?? ''}
                                                                    placeholder={globalVal ? t('agent.tools.usingCompanyConfig', 'Using company config ({{val}})', { val: globalVal }) : (field.placeholder || t('admin.leaveBlankDefault', 'Leave blank to use global default'))}
                                                                    onBlur={(e) => {
                                                                        if (!e.target.value) setFocusedField(null);
                                                                    }}
                                                                    onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))} />
                                                            );
                                                        })()}
                                                        </>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    {advancedFields.length > 0 && (
                                        <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '10px', marginTop: '2px' }}>
                                            <button
                                                type="button"
                                                className="btn btn-ghost"
                                                onClick={() => setShowAdvancedToolConfig(v => !v)}
                                                style={{ padding: 0, minWidth: 'auto', fontSize: '12px', color: 'var(--text-secondary)' }}
                                            >
                                                {showAdvancedToolConfig ? 'Hide advanced settings' : 'Advanced settings'}
                                            </button>
                                            {showAdvancedToolConfig && (
                                                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '12px' }}>
                                                    {advancedFields.map((field: any) => {
                                                        const userFromStore = useAuthStore.getState().user;
                                                        const currentUserRole = userFromStore?.role;
                                                        const isReadOnly = field.read_only_for_roles?.includes(currentUserRole);
                                                        return (
                                                            <div key={field.key}>
                                                                <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>
                                                                    {field.label}
                                                                    {isReadOnly && <span style={{ fontWeight: 400, color: 'var(--text-tertiary)', marginLeft: '4px' }}>(Admin only)</span>}
                                                                </label>
                                                                {field.type === 'checkbox' ? (
                                                                    <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: isReadOnly ? 'not-allowed' : 'pointer' }}>
                                                                        <input
                                                                            type="checkbox"
                                                                            checked={configData[field.key] ?? field.default ?? false}
                                                                            disabled={isReadOnly}
                                                                            onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.checked }))}
                                                                            style={{ opacity: 0, width: 0, height: 0 }}
                                                                        />
                                                                        <span style={{ position: 'absolute', inset: 0, background: (configData[field.key] ?? field.default) ? 'var(--accent-primary)' : 'var(--bg-tertiary)', borderRadius: '11px', transition: 'background 0.2s', opacity: isReadOnly ? 0.6 : 1 }}>
                                                                            <span style={{ position: 'absolute', left: (configData[field.key] ?? field.default) ? '20px' : '2px', top: '2px', width: '18px', height: '18px', background: '#fff', borderRadius: '50%', transition: 'left 0.2s' }} />
                                                                        </span>
                                                                    </label>
                                                                ) : field.type === 'select' ? (
                                                                    <select className="form-input" value={configData[field.key] ?? field.default ?? ''} disabled={isReadOnly}
                                                                        onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}>
                                                                        {(field.options || []).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                                                    </select>
                                                                ) : field.type === 'number' ? (
                                                                    <input type="number" className="form-input" value={configData[field.key] ?? field.default ?? ''} disabled={isReadOnly} placeholder={field.placeholder || ''} min={field.min} max={field.max} onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value ? Number(e.target.value) : '' }))} />
                                                                ) : field.type === 'textarea' ? (
                                                                    <textarea
                                                                        className="form-input"
                                                                        value={configData[field.key] ?? field.default ?? ''}
                                                                        disabled={isReadOnly}
                                                                        placeholder={field.placeholder || t('admin.leaveBlankDefault', 'Leave blank to use global default')}
                                                                        rows={Math.max(3, Math.min(10, String(configData[field.key] ?? field.default ?? field.placeholder ?? '').split('\n').length))}
                                                                        style={{ minHeight: '88px', fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)', resize: 'vertical' }}
                                                                        onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))}
                                                                    />
                                                                ) : (
                                                                    <input type={field.type === 'password' ? 'password' : 'text'} autoComplete={field.type === 'password' ? 'new-password' : undefined} className="form-input"
                                                                        value={configData[field.key] ?? field.default ?? ''}
                                                                        disabled={isReadOnly}
                                                                        placeholder={field.placeholder || t('admin.leaveBlankDefault', 'Leave blank to use global default')}
                                                                        onChange={e => setConfigData(p => ({ ...p, [field.key]: e.target.value }))} />
                                                                )}
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            )}
                                        </div>
                                    )}
                                    {/* Email tool: test connection button + help text */}
                                    {configTool?.category === 'email' && (
                                        <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                                            <button
                                                className="btn btn-secondary"
                                                style={{ alignSelf: 'flex-start' }}
                                                onClick={async () => {
                                                    const btn = document.getElementById('email-test-btn');
                                                    const status = document.getElementById('email-test-status');
                                                    if (btn) btn.textContent = 'Testing...';
                                                    if (btn) (btn as HTMLButtonElement).disabled = true;
                                                    try {
                                                        const token = localStorage.getItem('token');
                                                        const res = await fetch('/api/tools/test-email', {
                                                            method: 'POST',
                                                            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                                                            body: JSON.stringify({ config: configData }),
                                                        });
                                                        const data = await res.json();
                                                        if (status) {
                                                            status.textContent = data.ok
                                                                ? `${data.imap}\n${data.smtp}`
                                                                : `${data.imap || ''}\n${data.smtp || ''}\n${data.error || ''}`;
                                                            status.style.color = data.ok ? 'var(--success)' : 'var(--error)';
                                                        }
                                                    } catch (e: any) {
                                                        if (status) { status.textContent = `Error: ${e.message}`; status.style.color = 'var(--error)'; }
                                                    } finally {
                                                        if (btn) { btn.textContent = 'Test Connection'; (btn as HTMLButtonElement).disabled = false; }
                                                    }
                                                }}
                                                id="email-test-btn"
                                            >Test Connection</button>
                                            <div id="email-test-status" style={{ fontSize: '11px', whiteSpace: 'pre-line', minHeight: '16px' }}></div>
                                        </div>
                                    )}
                                </div>
                            ) : (
                                <div>
                                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>Config JSON (Agent Override)</label>
                                    <textarea
                                        className="form-input"
                                        value={configJson}
                                        onChange={e => setConfigJson(e.target.value)}
                                        style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', minHeight: '120px', resize: 'vertical' }}
                                        placeholder='{}'
                                    />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        Global default: <code style={{ fontSize: '10px' }}>{JSON.stringify(configTool?.global_config || {}).slice(0, 80)}</code>
                                    </div>
                                </div>
                            )}

                            <div style={{ display: 'flex', gap: '8px', marginTop: '16px', justifyContent: 'flex-end' }}>
                                {configTool && configTool.agent_config && Object.keys(configTool.agent_config || {}).length > 0 && (
                                    <button className="btn btn-ghost" style={{ color: 'var(--error)', marginRight: 'auto' }} onClick={async () => {
                                        const token = localStorage.getItem('token');
                                        await fetch(`/api/tools/agents/${agentId}/tool-config/${configTool.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ config: {} }) });
                                        setConfigTool(null); loadTools();
                                    }}>Reset to Global</button>
                                )}
                                {isCat && (
                                    <button
                                        className="btn btn-secondary"
                                        style={{ marginRight: 'auto' }}
                                        onClick={async () => {
                                            const btn = document.getElementById('cat-test-btn');
                                            if (btn) btn.textContent = 'Testing...';
                                            try {
                                                const token = localStorage.getItem('token');
                                                const res = await fetch(`/api/tools/agents/${agentId}/category-config/${configCategory}/test`, {
                                                    method: 'POST',
                                                    headers: { Authorization: `Bearer ${token}` }
                                                });
                                                const data = await res.json();
                                                if (data.ok) {
                                                    await dialog.alert(data.message || t('common.error.testSuccess'), { type: 'success', title: t('common.model.connectivityTest') });
                                                } else {
                                                    await dialog.alert(t('common.error.testFailed'), { type: 'error', title: t('common.model.connectivityTest'), details: typeof data.error === 'string' ? data.error : JSON.stringify(data, null, 2) });
                                                }
                                            } catch (e: any) { await dialog.alert(t('common.error.testFailed'), { type: 'error', title: t('common.model.connectivityTest'), details: String(e?.message || e) }); }
                                            finally { if (btn) btn.textContent = 'Test Connection'; }
                                        }}
                                        id="cat-test-btn"
                                    >Test Connection</button>
                                )}
                                <button className="btn btn-secondary" onClick={() => { setConfigTool(null); setConfigCategory(null); }}>Cancel</button>
                                <button className="btn btn-primary" onClick={saveConfig} disabled={configSaving}>{configSaving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}</button>
                            </div>
                        </div>
                    </div>
                );
            })()}
        </>
    );
}
