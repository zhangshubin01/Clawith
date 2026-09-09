import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconUser, IconUserPlus } from '@tabler/icons-react';

import ConfirmModal from '../../../components/ConfirmModal';
import { agentApi } from '../../../services/api';
import { fetchAuth } from '../utils/fetchAuth';

type Maintainer = {
    id: string;
    user_id: string;
    name: string | null;
    username: string | null;
    email: string | null;
    created_by: string | null;
    created_at: string | null;
};

type Candidate = { id: string; name: string; username?: string; email?: string };

/**
 * Maintainer management (view / add / remove).
 *
 * Only rendered when the caller is the agent creator or an admin (see
 * AgentDetailPage gating): a narrow `isOwner || isAdmin` gate, NOT `canManage`,
 * because the latter also matches custom-mode users who would get a 403 from
 * the backend.
 */
export default function MaintainersPanel({
    agentId,
    isOwner,
    isAdmin,
    queryClient,
}: {
    agentId: string;
    isOwner: boolean;
    isAdmin: boolean;
    queryClient: any;
}) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const canManage = isOwner || isAdmin;

    const [userSearch, setUserSearch] = useState('');
    const [showDropdown, setShowDropdown] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [removeTarget, setRemoveTarget] = useState<Maintainer | null>(null);
    const searchRef = useRef<HTMLDivElement | null>(null);

    const { data } = useQuery({
        queryKey: ['agent-maintainers', agentId],
        queryFn: () => agentApi.maintainers.list(agentId),
        enabled: !!agentId && canManage,
    });

    const { data: candidateData } = useQuery({
        queryKey: ['agent-maintainer-candidates', agentId, userSearch],
        queryFn: () =>
            fetchAuth<{ users: Candidate[]; agents: any[] }>(
                `/agents/${agentId}/permissions/candidates${
                    userSearch.trim() ? `?search=${encodeURIComponent(userSearch.trim())}` : ''
                }`
            ),
        enabled: !!agentId && canManage && userSearch.trim().length > 0,
    });

    const maintainers = data?.maintainers ?? [];
    const creator = data?.creator ?? null;
    const candidates = candidateData?.users ?? [];
    const existingIds = useMemo(
        () => new Set(maintainers.map((m) => m.user_id)),
        [maintainers]
    );

    const refresh = () =>
        queryClient.invalidateQueries({ queryKey: ['agent-maintainers', agentId] });

    const addMaintainer = async (user: Candidate) => {
        setBusy(true);
        setError(null);
        try {
            await agentApi.maintainers.add(agentId, user.id);
            setUserSearch('');
            setShowDropdown(false);
            refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const removeMaintainer = async () => {
        if (!removeTarget) return;
        setBusy(true);
        setError(null);
        try {
            await agentApi.maintainers.remove(agentId, removeTarget.user_id);
            setRemoveTarget(null);
            refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const labelFor = (name: string | null, username: string | null, email: string | null) => {
        const subtitle = [username, email].filter(Boolean).join(' · ');
        return { name: name || username || '—', subtitle };
    };

    return (
        <div className="card" style={{ marginBottom: '12px' }}>
            <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <IconUserPlus size={16} stroke={1.8} />{' '}
                {t('agent.settings.maintainers.title', 'Maintainers')}
            </h4>
            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                {t(
                    'agent.settings.maintainers.description',
                    "Maintainers can edit this agent's workspace and skill files."
                )}
            </p>

            {creator && (
                <div
                    style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                        padding: '8px 12px',
                        border: '1px solid var(--border-subtle)',
                        borderRadius: '8px',
                        marginBottom: '8px',
                    }}
                >
                    <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '13px', fontWeight: 500 }}>
                            {labelFor(creator.name, creator.username, creator.email).name}
                        </div>
                        {labelFor(creator.name, creator.username, creator.email).subtitle && (
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                {labelFor(creator.name, creator.username, creator.email).subtitle}
                            </div>
                        )}
                    </div>
                    <span className="badge" style={{ fontSize: '10px', flexShrink: 0 }}>
                        {t('agent.settings.maintainers.creatorLabel', 'Creator · implicit maintainer')}
                    </span>
                </div>
            )}

            {maintainers.length === 0 ? (
                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                    {t('agent.settings.maintainers.empty', 'No maintainers yet')}
                </div>
            ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '8px' }}>
                    {maintainers.map((m) => (
                        <div
                            key={m.id}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '8px',
                                padding: '8px 12px',
                                border: '1px solid var(--border-subtle)',
                                borderRadius: '8px',
                            }}
                        >
                            <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: '13px', fontWeight: 500 }}>
                                    {labelFor(m.name, m.username, m.email).name}
                                </div>
                                {labelFor(m.name, m.username, m.email).subtitle && (
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                        {labelFor(m.name, m.username, m.email).subtitle}
                                    </div>
                                )}
                            </div>
                            <button
                                className="btn btn-ghost"
                                style={{ padding: '2px 6px', fontSize: '11px', color: 'var(--error)' }}
                                onClick={() => setRemoveTarget(m)}
                            >
                                {t('agent.settings.maintainers.remove', 'Remove')}
                            </button>
                        </div>
                    ))}
                </div>
            )}

            <div ref={searchRef} style={{ position: 'relative', maxWidth: '520px' }}>
                <input
                    className="input"
                    value={userSearch}
                    onChange={(e) => {
                        setUserSearch(e.target.value);
                        setShowDropdown(true);
                    }}
                    onFocus={() => setShowDropdown(true)}
                    placeholder={t('agent.settings.maintainers.searchPlaceholder', 'Search name or email to add...')}
                    style={{ fontSize: '12px', width: '100%' }}
                />
                {showDropdown && userSearch.trim() && (
                    <div
                        style={{
                            position: 'absolute',
                            top: '100%',
                            left: 0,
                            right: 0,
                            background: 'var(--bg-primary)',
                            border: '1px solid var(--border-subtle)',
                            borderRadius: '6px',
                            marginTop: '4px',
                            maxHeight: '220px',
                            overflowY: 'auto',
                            zIndex: 20,
                            boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                        }}
                    >
                        {candidates.length === 0 ? (
                            <div style={{ padding: '8px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                {t('agent.settings.maintainers.noResults', 'No available results')}
                            </div>
                        ) : (
                            candidates.map((u) => {
                                const already = existingIds.has(u.id) || (creator && u.id === creator.user_id);
                                return (
                                    <div
                                        key={u.id}
                                        style={{
                                            padding: '8px 12px',
                                            cursor: already ? 'default' : 'pointer',
                                            fontSize: '13px',
                                            borderBottom: '1px solid var(--border-subtle)',
                                            display: 'flex',
                                            alignItems: 'flex-start',
                                            gap: '8px',
                                            opacity: already ? 0.6 : 1,
                                        }}
                                        onClick={() => {
                                            if (!already) void addMaintainer(u);
                                        }}
                                        onMouseEnter={(e) => {
                                            if (!already) e.currentTarget.style.background = 'var(--bg-elevated)';
                                        }}
                                        onMouseLeave={(e) => {
                                            e.currentTarget.style.background = 'transparent';
                                        }}
                                    >
                                        <IconUser size={14} stroke={1.8} style={{ marginTop: '2px', flexShrink: 0 }} />
                                        <div style={{ minWidth: 0, flex: 1 }}>
                                            <div style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                {u.name}
                                            </div>
                                            {(u.username || u.email) && (
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                    {[u.username, u.email].filter(Boolean).join(' · ')}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                );
                            })
                        )}
                    </div>
                )}
            </div>

            {error && (
                <div style={{ marginTop: '8px', fontSize: '12px', color: 'var(--error)' }}>{error}</div>
            )}
            {busy && (
                <div style={{ marginTop: '8px', fontSize: '12px', color: 'var(--text-tertiary)' }}>
                    {isChinese ? '处理中...' : 'Working...'}
                </div>
            )}

            <ConfirmModal
                open={!!removeTarget}
                title={t('agent.settings.maintainers.removeConfirmTitle', 'Remove maintainer')}
                message={t(
                    'agent.settings.maintainers.removeConfirmMessage',
                    "Remove this user's maintainer role? They will lose the ability to edit workspace and skill files."
                )}
                danger
                confirmLabel={t('agent.settings.maintainers.remove', 'Remove')}
                onConfirm={() => void removeMaintainer()}
                onCancel={() => setRemoveTarget(null)}
            />
        </div>
    );
}
