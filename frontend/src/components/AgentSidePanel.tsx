import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import TakeControlPanel from './TakeControlPanel';
import WorkspaceOperationPanel, { WorkspaceActivity, WorkspaceLiveDraft } from './WorkspaceOperationPanel';
import type { LivePreviewState } from './AgentBayLivePanel';

interface Props {
    liveState: LivePreviewState;
    workspaceActivePath?: string | null;
    workspaceActivities: WorkspaceActivity[];
    workspaceLiveDraft?: WorkspaceLiveDraft | null;
    workspaceLocked?: boolean;
    visible: boolean;
    onToggle: () => void;
    activeTab: SidePanelTab;
    onTabChange: (tab: SidePanelTab) => void;
    onWorkspaceSelectPath: (path: string) => void;
    onWorkspaceToggleLock?: () => void;
    onWorkspaceEditingChange?: (editing: boolean) => void;
    onWorkspacePathDeleted?: (path: string) => void;
    awareContent?: ReactNode;
    agentId?: string;
    sessionId?: string;
    onLiveUpdate?: (env: 'browser' | 'desktop', screenshotDataUri: string) => void;
}

export type SidePanelTab = 'workspace' | 'aware' | 'browser' | 'desktop' | 'code';

const MIN_WIDTH = 340;
const MAX_WIDTH_VW = 0.68;

function calcInitialWidth(): number {
    const container = (document.querySelector('.agent-chat-area') as HTMLElement | null)
        || (document.querySelector('.chat-container') as HTMLElement | null);
    if (container) return Math.max(MIN_WIDTH, Math.floor(container.clientWidth / 2));
    return Math.max(MIN_WIDTH, Math.floor((window.innerWidth - 60) / 2));
}

const labels: Record<SidePanelTab, string> = {
    workspace: '工作区',
    aware: '自我意识',
    browser: 'Browser',
    desktop: 'Desktop',
    code: 'Code',
};

export default function AgentSidePanel({
    liveState,
    workspaceActivePath,
    workspaceActivities,
    workspaceLiveDraft,
    workspaceLocked = false,
    visible,
    onToggle,
    activeTab,
    onTabChange,
    onWorkspaceSelectPath,
    onWorkspaceToggleLock,
    onWorkspaceEditingChange,
    onWorkspacePathDeleted,
    awareContent,
    agentId,
    sessionId,
    onLiveUpdate,
}: Props) {
    const [panelWidth, setPanelWidth] = useState(() => calcInitialWidth());
    const [showTakeControl, setShowTakeControl] = useState(false);
    const [workspaceActivityOpen, setWorkspaceActivityOpen] = useState(false);
    const isDragging = useRef(false);
    const userResized = useRef(false);
    const dragStartX = useRef(0);
    const dragStartWidth = useRef(0);
    const codeEndRef = useRef<HTMLDivElement>(null);
    const onLiveUpdateRef = useRef(onLiveUpdate);

    useEffect(() => {
        onLiveUpdateRef.current = onLiveUpdate;
    });

    const availableTabs: SidePanelTab[] = ['workspace'];
    if (awareContent) availableTabs.push('aware');
    if (liveState.browser) availableTabs.push('browser');
    if (liveState.desktop) availableTabs.push('desktop');
    if (liveState.code) availableTabs.push('code');

    useEffect(() => {
        const onResize = () => {
            if (!isDragging.current && !userResized.current) setPanelWidth(calcInitialWidth());
        };
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
    }, []);

    useEffect(() => {
        if (activeTab === 'code') codeEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [activeTab, liveState.code?.output]);

    useEffect(() => {
        if (availableTabs.length > 0 && !availableTabs.includes(activeTab)) onTabChange(availableTabs[0]);
    }, [availableTabs.join('|'), activeTab]);

    const handleDragMouseDown = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        isDragging.current = true;
        dragStartX.current = e.clientX;
        dragStartWidth.current = panelWidth;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    }, [panelWidth]);

    useEffect(() => {
        const onMouseMove = (e: MouseEvent) => {
            if (!isDragging.current) return;
            const delta = dragStartX.current - e.clientX;
            const maxWidth = window.innerWidth * MAX_WIDTH_VW;
            setPanelWidth(Math.min(maxWidth, Math.max(MIN_WIDTH, dragStartWidth.current + delta)));
        };
        const onMouseUp = () => {
            if (!isDragging.current) return;
            isDragging.current = false;
            userResized.current = true;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        };
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        return () => {
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };
    }, []);

    if (!visible) return null;

    return (
        <div className="live-panel agent-side-panel" style={{ width: `${panelWidth}px`, flexShrink: 0 }}>
            <div className="live-panel-resize-handle" onMouseDown={handleDragMouseDown} title="Drag to resize" />
            <div className="live-panel-header">
                <span className="live-panel-title">{labels[activeTab]}</span>
                {agentId && sessionId && (activeTab === 'browser' || activeTab === 'desktop') && (
                    <button className="live-panel-take-control" onClick={() => setShowTakeControl(true)} title="Take control">
                        <span>Control</span>
                    </button>
                )}
                <div className="live-panel-header-right">
                    {activeTab === 'workspace' && (
                        <>
                            <button
                                className={`live-panel-icon-btn ${workspaceActivityOpen ? 'active' : ''}`}
                                onClick={() => setWorkspaceActivityOpen(o => !o)}
                                title="Version history"
                            >
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
                            </button>
                            <span className="live-panel-header-sep" />
                        </>
                    )}
                    <button className="live-panel-collapse" onClick={onToggle} title="Close">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
                    </button>
                </div>
            </div>
            <div className="live-panel-content">
                {activeTab === 'workspace' && agentId && (
                    <WorkspaceOperationPanel
                        agentId={agentId}
                        sessionId={sessionId}
                        activePath={workspaceActivePath}
                        activities={workspaceActivities}
                        liveDraft={workspaceLiveDraft}
                        locked={workspaceLocked}
                        onSelectPath={onWorkspaceSelectPath}
                        onToggleLock={onWorkspaceToggleLock}
                        onEditingChange={onWorkspaceEditingChange}
                        onPathDeleted={onWorkspacePathDeleted}
                        activityOpen={workspaceActivityOpen}
                        onActivityToggle={setWorkspaceActivityOpen}
                    />
                )}
                {activeTab === 'aware' && awareContent}
                {activeTab === 'desktop' && liveState.desktop && (
                    <div className="live-panel-browser">
                        <img src={liveState.desktop.screenshotUrl} alt="Desktop preview" className="live-panel-screenshot" />
                        <div className="live-panel-badge"><span className="live-dot" />Live</div>
                    </div>
                )}
                {activeTab === 'browser' && liveState.browser && (
                    <div className="live-panel-browser">
                        <img src={liveState.browser.screenshotUrl} alt="Browser preview" className="live-panel-screenshot" />
                        <div className="live-panel-badge"><span className="live-dot" />Live</div>
                    </div>
                )}
                {activeTab === 'code' && liveState.code && (
                    <div className="live-panel-code">
                        <pre>{liveState.code.output}</pre>
                        <div ref={codeEndRef} />
                    </div>
                )}
            </div>
            {showTakeControl && agentId && sessionId && (activeTab === 'browser' || activeTab === 'desktop') && (
                <TakeControlPanel
                    agentId={agentId}
                    sessionId={sessionId}
                    envType={activeTab === 'desktop' ? 'computer' : 'browser'}
                    onClose={() => setShowTakeControl(false)}
                    onLastScreenshot={(dataUri) => {
                        const env = activeTab === 'desktop' ? 'desktop' : 'browser';
                        onLiveUpdateRef.current?.(env, dataUri);
                    }}
                />
            )}
        </div>
    );
}
