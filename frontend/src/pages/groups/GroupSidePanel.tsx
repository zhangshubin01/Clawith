import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconPlus, IconRobot, IconSettings, IconUser, IconX } from '@tabler/icons-react';
import { groupApi } from '../../services/groupApi';
import GroupTextFileEditor from './GroupTextFileEditor';
import GroupWorkspaceTab from './GroupWorkspaceTab';
import GroupMemoryTab from './GroupMemoryTab';
import type { GroupMember } from '../../types/group';

type PanelTab = 'members' | 'announcement' | 'workspace' | 'memory';

interface GroupSidePanelProps {
    groupId: string;
    groupName: string;
    members: GroupMember[];
    onInvite: () => void;
    onOpenSettings: () => void;
    onClose: () => void;
}

/**
 * The group-level side panel: a fixed header naming the group (so it reads as group-scoped and does
 * not change when the session switches), then tabs for members, announcement, files and memory. It
 * is view-and-invite only — renaming, removing members and dissolving live in the settings modal
 * behind the gear.
 */
export default function GroupSidePanel({
    groupId,
    groupName,
    members,
    onInvite,
    onOpenSettings,
    onClose,
}: GroupSidePanelProps) {
    const { t } = useTranslation();
    const [tab, setTab] = useState<PanelTab>('members');

    const people = members.filter((member) => member.participant_type === 'user');
    const agents = members.filter((member) => member.participant_type === 'agent');

    const renderMember = (member: GroupMember) => (
        <div key={member.id} className="group-member-row">
            <span className={`group-avatar sm ${member.participant_type === 'agent' ? 'agent' : ''}`}>
                {member.participant_type === 'agent'
                    ? <IconRobot size={14} stroke={1.6} />
                    : member.display_name.slice(0, 1).toUpperCase()}
            </span>
            <div className="group-member-body">
                <div className="group-member-name">
                    {member.display_name}
                    {member.role === 'manager' && (
                        <span className="group-badge-manager">{t('groups.manager', '群管理')}</span>
                    )}
                </div>
                {(member.role_description || member.title) && (
                    <div className="group-member-hint">{member.role_description || member.title}</div>
                )}
            </div>
        </div>
    );

    const TABS: { key: PanelTab; label: string }[] = [
        { key: 'members', label: `${t('groups.members', '成员')} · ${members.length}` },
        { key: 'announcement', label: t('groups.announcement', '群公告') },
        { key: 'workspace', label: t('groups.workspace', '文件') },
        { key: 'memory', label: t('groups.memory', '记忆') },
    ];

    return (
        <aside className="group-side-panel">
            <div className="group-panel-topbar">
                <span className="group-panel-groupname" title={groupName}>{groupName}</span>
                <div className="group-column-actions">
                    <button
                        type="button"
                        className="group-icon-btn"
                        title={t('groups.settings', '群设置')}
                        onClick={onOpenSettings}
                    >
                        <IconSettings size={16} stroke={1.7} />
                    </button>
                    <button type="button" className="group-icon-btn" onClick={onClose}>
                        <IconX size={16} stroke={1.7} />
                    </button>
                </div>
            </div>

            <div className="group-panel-header">
                <div className="group-tabs scrollable">
                    {TABS.map(({ key, label }) => (
                        <button
                            key={key}
                            type="button"
                            className={`group-tab ${tab === key ? 'active' : ''}`}
                            onClick={() => setTab(key)}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            </div>

            <div className="group-panel-body">
                {tab === 'members' && (
                    <>
                        <button type="button" className="group-invite-btn" onClick={onInvite}>
                            <IconPlus size={14} stroke={1.8} />
                            {t('groups.inviteTitle', '邀请成员')}
                        </button>

                        {agents.length > 0 && (
                            <>
                                <div className="group-panel-label">
                                    <IconRobot size={12} stroke={1.7} />
                                    {t('groups.tabAgents', '智能体')} · {agents.length}
                                </div>
                                {agents.map(renderMember)}
                            </>
                        )}

                        <div className="group-panel-label">
                            <IconUser size={12} stroke={1.7} />
                            {t('groups.tabPeople', '成员')} · {people.length}
                        </div>
                        {people.map(renderMember)}
                    </>
                )}

                {tab === 'announcement' && (
                    <GroupTextFileEditor
                        queryKey={['group-announcement', groupId]}
                        note={t('groups.announcementNote', '群公告会注入被 @ 智能体的上下文，用于约定群目标和协作规则。')}
                        placeholder={t('groups.announcementPlaceholder', '写下群目标、协作规则和对智能体的要求...')}
                        load={() => groupApi.announcement(groupId)}
                        save={(content, token) => groupApi.saveAnnouncement(groupId, content, token)}
                    />
                )}

                {tab === 'workspace' && <GroupWorkspaceTab groupId={groupId} />}

                {tab === 'memory' && <GroupMemoryTab groupId={groupId} members={members} />}
            </div>
        </aside>
    );
}
