import { useTranslation } from 'react-i18next';

interface ToolSourceTabsProps {
    toolTab: 'company' | 'installed';
    setToolTab: (tab: 'company' | 'installed') => void;
    companyCount: number;
    installedCount: number;
}

export default function ToolSourceTabs({ toolTab, setToolTab, companyCount, installedCount }: ToolSourceTabsProps) {
    const { t } = useTranslation();
    return (
        <div className="tool-source-tabs" role="tablist" aria-label={t('agent.tools.sourceTabs', 'Tool sources')}>
            <button
                type="button"
                role="tab"
                aria-selected={toolTab === 'company'}
                className={toolTab === 'company' ? 'active' : ''}
                onClick={() => setToolTab('company')}
            >
                <span>{t('agent.tools.companyTools', 'Company Tools')}</span>
                <span className="tool-source-tab-count">{companyCount}</span>
            </button>
            <button
                type="button"
                role="tab"
                aria-selected={toolTab === 'installed'}
                className={toolTab === 'installed' ? 'active' : ''}
                onClick={() => setToolTab('installed')}
            >
                <span>{t('agent.tools.agentInstalled', 'Agent Self-Installed Tools')}</span>
                <span className="tool-source-tab-count">{installedCount}</span>
            </button>
        </div>
    );
}
