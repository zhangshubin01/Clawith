import { useTranslation } from 'react-i18next';
import { IconSearch } from '@tabler/icons-react';

interface ToolSearchBarProps {
    toolSearch: string;
    setToolSearch: (v: string) => void;
    toolStatusFilter: 'all' | 'enabled' | 'disabled' | 'configured';
    setToolStatusFilter: (v: 'all' | 'enabled' | 'disabled' | 'configured') => void;
    expandedCount: number;
    totalCategories: number;
    onToggleExpand: () => void;
}

export default function ToolSearchBar({ toolSearch, setToolSearch, toolStatusFilter, setToolStatusFilter, expandedCount, totalCategories, onToggleExpand }: ToolSearchBarProps) {
    const { t } = useTranslation();
    return (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <div style={{ position: 'relative', flex: '1 1 260px', minWidth: '220px' }}>
                <IconSearch size={15} style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-tertiary)' }} />
                <input
                    value={toolSearch}
                    onChange={(e) => setToolSearch(e.target.value)}
                    placeholder={t('agent.tools.searchTools', 'Search tools...')}
                    style={{
                        width: '100%', boxSizing: 'border-box',
                        border: '1px solid var(--border-subtle)', borderRadius: '8px',
                        background: 'var(--bg-primary)', color: 'var(--text-primary)',
                        padding: '8px 10px 8px 32px', fontSize: '13px', outline: 'none',
                    }}
                />
            </div>
            {(['all', 'enabled', 'disabled', 'configured'] as const).map(filter => (
                <button
                    key={filter} type="button"
                    onClick={() => setToolStatusFilter(filter)}
                    style={{
                        border: '1px solid var(--border-subtle)', borderRadius: '999px',
                        background: toolStatusFilter === filter ? 'var(--text-primary)' : 'var(--bg-primary)',
                        color: toolStatusFilter === filter ? 'var(--bg-primary)' : 'var(--text-secondary)',
                        padding: '6px 10px', fontSize: '11px', cursor: 'pointer',
                    }}
                >
                    {filter === 'all' ? t('common.all', 'All')
                        : filter === 'enabled' ? t('common.enabled', 'Enabled')
                            : filter === 'disabled' ? t('common.disabled', 'Disabled')
                                : t('agent.tools.configured', 'Configured')}
                </button>
            ))}
            <button type="button" onClick={onToggleExpand} style={{
                border: '1px solid var(--border-subtle)', borderRadius: '8px',
                background: 'var(--bg-primary)', color: 'var(--text-secondary)',
                padding: '6px 10px', fontSize: '11px', cursor: 'pointer',
            }}>
                {expandedCount >= totalCategories ? t('agent.tools.collapseAll', 'Collapse all') : t('agent.tools.expandAll', 'Expand all')}
            </button>
        </div>
    );
}
