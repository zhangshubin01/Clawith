import { useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { IconDownload, IconFolder, IconTools } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { useDialog } from '../../../components/Dialog/DialogProvider';
import type { FileBrowserApi } from '../../../components/FileBrowser';
import FileBrowser from '../../../components/FileBrowser';
import { useToast } from '../../../components/Toast/ToastProvider';
import { fileApi, skillApi } from '../../../services/api';

type SafeDisplayIcon = (icon?: string | null, fallback?: ReactNode) => ReactNode;

interface Props {
    agentId: string;
    canManage: boolean;
    safeDisplayIcon: SafeDisplayIcon;
    showAgentClawhub: boolean;
    setShowAgentClawhub: Dispatch<SetStateAction<boolean>>;
    agentClawhubQuery: string;
    setAgentClawhubQuery: Dispatch<SetStateAction<string>>;
    agentClawhubResults: any[];
    setAgentClawhubResults: Dispatch<SetStateAction<any[]>>;
    agentClawhubSearching: boolean;
    setAgentClawhubSearching: Dispatch<SetStateAction<boolean>>;
    agentClawhubInstalling: string | null;
    setAgentClawhubInstalling: Dispatch<SetStateAction<string | null>>;
    showAgentUrlImport: boolean;
    setShowAgentUrlImport: Dispatch<SetStateAction<boolean>>;
    agentUrlInput: string;
    setAgentUrlInput: Dispatch<SetStateAction<string>>;
    agentUrlImporting: boolean;
    setAgentUrlImporting: Dispatch<SetStateAction<boolean>>;
    showImportSkillModal: boolean;
    setShowImportSkillModal: Dispatch<SetStateAction<boolean>>;
    globalSkillsForImport: any[] | undefined;
    importingSkillId: string | null;
    setImportingSkillId: Dispatch<SetStateAction<string | null>>;
}

export default function SkillsTab(props: Props) {
    const {
        agentId,
        canManage,
        safeDisplayIcon,
        showAgentClawhub,
        setShowAgentClawhub,
        agentClawhubQuery,
        setAgentClawhubQuery,
        agentClawhubResults,
        setAgentClawhubResults,
        agentClawhubSearching,
        setAgentClawhubSearching,
        agentClawhubInstalling,
        setAgentClawhubInstalling,
        showAgentUrlImport,
        setShowAgentUrlImport,
        agentUrlInput,
        setAgentUrlInput,
        agentUrlImporting,
        setAgentUrlImporting,
        showImportSkillModal,
        setShowImportSkillModal,
        globalSkillsForImport,
        importingSkillId,
        setImportingSkillId,
    } = props;
    const [showGitlabSettings, setShowGitlabSettings] = useState(false);
    const [myGitlabStatus, setMyGitlabStatus] = useState<{ configured: boolean; masked: string; username: string } | null>(null);
    const [myGitlabToken, setMyGitlabToken] = useState('');
    const [myGitlabUser, setMyGitlabUser] = useState('');
    const [myGitlabPass, setMyGitlabPass] = useState('');
    const [savingMyGitlab, setSavingMyGitlab] = useState(false);
    const [importProgress, setImportProgress] = useState<{ current?: number; total?: number; file?: string; message?: string } | null>(null);
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const queryClient = useQueryClient();
    const [skillsCurrentPath, setSkillsCurrentPath] = useState('skills');
    const adapter: FileBrowserApi = {
        list: (path) => { setSkillsCurrentPath(path || 'skills'); return fileApi.list(agentId, path); },
        read: (path) => fileApi.read(agentId, path),
        write: (path, content) => fileApi.write(agentId, path, content),
        delete: (path) => fileApi.delete(agentId, path),
        upload: (file, path, onProgress) => fileApi.upload(agentId, file, path, onProgress),
        downloadUrl: (path) => fileApi.downloadUrl(agentId, path),
    };

    const searchClawHub = () => {
        setAgentClawhubSearching(true);
        skillApi.clawhub.search(agentClawhubQuery)
            .then((results) => {
                setAgentClawhubResults(results);
                setAgentClawhubSearching(false);
            })
            .catch(() => setAgentClawhubSearching(false));
    };

    return (
        <div>
            <div style={{ marginBottom: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                        <h3 style={{ marginBottom: '4px' }}>{t('agent.skills.title')}</h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.skills.description')}</p>
                    </div>
                    {canManage && <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                        <button
                            className="btn btn-ghost"
                            style={{ fontSize: '13px', padding: '6px 8px', minWidth: 'auto' }}
                            title="GitLab Settings"
                            onClick={async () => {
                                setShowGitlabSettings(true);
                                if (!myGitlabStatus) {
                                    try { setMyGitlabStatus(await skillApi.settings.getMyGitlab()); } catch { /* */ }
                                }
                            }}
                        >⚙</button>
                        <button
                            className="btn btn-secondary"
                            style={{ fontSize: '13px' }}
                            onClick={() => { setShowAgentUrlImport(true); setAgentUrlInput(''); }}
                        >
                            Import from URL
                        </button>
                        <button
                            className="btn btn-secondary"
                            style={{ fontSize: '13px' }}
                            onClick={() => { setShowAgentClawhub(true); setAgentClawhubQuery(''); setAgentClawhubResults([]); }}
                        >
                            Browse ClawHub
                        </button>
                        <button
                            className="btn btn-primary"
                            style={{ display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap' }}
                            onClick={() => setShowImportSkillModal(true)}
                        >
                            Import from Presets
                        </button>
                    </div>}
                </div>
                <div style={{ marginTop: '8px', padding: '10px 14px', background: 'var(--bg-secondary)', borderRadius: '8px', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                    <strong>Skill Format:</strong><br />
                    • <code>skills/my-skill/SKILL.md</code> — {t('agent.skills.folderFormat', 'Each skill is a folder with a SKILL.md file and optional auxiliary files (scripts/, examples/)')}
                </div>
            </div>

            <FileBrowser api={adapter} rootPath="skills" features={{ newFile: canManage, edit: canManage, delete: canManage, newFolder: canManage, upload: canManage, directoryNavigation: true }} title={t('agent.skills.skillFiles')} />

            {showAgentClawhub && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowAgentClawhub(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '600px', width: '90%', maxHeight: '70vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <h3>Browse ClawHub</h3>
                            <button onClick={() => setShowAgentClawhub(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>x</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px' }}>
                            Search and install skills from ClawHub directly into this agent&apos;s workspace.
                        </p>
                        <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
                            <input
                                className="input"
                                placeholder="Search skills..."
                                value={agentClawhubQuery}
                                onChange={(e) => setAgentClawhubQuery(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && agentClawhubQuery.trim()) searchClawHub();
                                }}
                                style={{ flex: 1, fontSize: '13px' }}
                            />
                            <button
                                className="btn btn-primary"
                                style={{ fontSize: '13px' }}
                                disabled={!agentClawhubQuery.trim() || agentClawhubSearching}
                                onClick={searchClawHub}
                            >
                                {agentClawhubSearching ? 'Searching...' : 'Search'}
                            </button>
                        </div>
                        <div style={{ flex: 1, overflowY: 'auto' }}>
                            {agentClawhubResults.length === 0 && !agentClawhubSearching && (
                                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>Search ClawHub to find skills</div>
                            )}
                            {agentClawhubResults.map((result: any) => (
                                <div key={result.slug} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px', borderRadius: '8px', marginBottom: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)' }}>
                                    <div style={{ flex: 1 }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                            <span style={{ fontWeight: 600, fontSize: '13px' }}>{result.displayName || result.slug}</span>
                                            {result.version && <span style={{ fontSize: '10px', color: 'var(--accent-text)', background: 'var(--accent-subtle)', padding: '1px 5px', borderRadius: '4px' }}>v{result.version}</span>}
                                        </div>
                                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{result.summary?.substring(0, 100)}{result.summary?.length > 100 ? '...' : ''}</div>
                                        {result.updatedAt && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px', opacity: 0.7 }}>Updated {new Date(result.updatedAt).toLocaleDateString()}</div>}
                                    </div>
                                    <button
                                        className="btn btn-secondary"
                                        style={{ fontSize: '12px', padding: '5px 12px', marginLeft: '12px' }}
                                        disabled={agentClawhubInstalling === result.slug}
                                        onClick={async () => {
                                            if (!canManage) return;
                                            setAgentClawhubInstalling(result.slug);
                                            try {
                                                const response = await skillApi.agentImport.fromClawhub(agentId, result.slug);
                                                toast.success(t('common.file.skillInstalled', { name: result.displayName || result.slug }));
                                                queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                                            } catch (err: any) {
                                                await dialog.alert(t('common.error.installFailed'), { type: 'error', details: String(err?.message || err) });
                                            } finally {
                                                setAgentClawhubInstalling(null);
                                            }
                                        }}
                                    >
                                        {agentClawhubInstalling === result.slug ? 'Installing...' : 'Install'}
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}

            {showAgentUrlImport && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowAgentUrlImport(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '500px', width: '90%', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <h3>Import from URL</h3>
                            <button onClick={() => setShowAgentUrlImport(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>x</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px' }}>
                            Paste a GitHub or GitLab URL pointing to a skill repository or directory.
                        </p>
                        <input
                            className="input"
                            placeholder="https://github.com/owner/repo or https://gitlab.com/owner/repo/-/tree/main/path"
                            value={agentUrlInput}
                            onChange={(e) => setAgentUrlInput(e.target.value)}
                            style={{ width: '100%', fontSize: '13px', marginBottom: '12px', boxSizing: 'border-box' }}
                        />
                        {importProgress && (
                            <div style={{ marginBottom: '12px', padding: '10px 14px', background: 'var(--bg-secondary)', borderRadius: '8px' }}>
                                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px', fontSize: '12px' }}>
                                    <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, marginRight: '8px' }}>
                                        {importProgress.message || importProgress.file}
                                    </span>
                                    {(importProgress.current != null && importProgress.total != null && importProgress.total > 0) && (
                                        <span style={{ color: 'var(--text-tertiary)', flexShrink: 0 }}>{importProgress.current}/{importProgress.total}</span>
                                    )}
                                </div>
                                <div style={{ height: '4px', borderRadius: '2px', background: 'var(--bg-tertiary)', overflow: 'hidden' }}>
                                    {importProgress.message && !importProgress.file ? (
                                        <div style={{
                                            height: '100%', width: '30%', borderRadius: '2px',
                                            background: 'linear-gradient(90deg, transparent, var(--accent-primary), transparent)',
                                            animation: 'indeterminateProgress 1.2s ease-in-out infinite',
                                        }} />
                                    ) : (
                                        <div style={{ height: '100%', borderRadius: '2px', background: 'var(--accent-primary)', width: `${Math.round((importProgress.current || 0) / Math.max(importProgress.total || 1, 1) * 100)}%`, transition: 'width 0.15s ease' }} />
                                    )}
                                </div>
                            </div>
                        )}
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                            <button className="btn btn-secondary" onClick={() => { setShowAgentUrlImport(false); setImportProgress(null); }}>Cancel</button>
                            <button
                                className="btn btn-primary"
                                disabled={!agentUrlInput.trim() || agentUrlImporting}
                                style={{ minWidth: '80px' }}
                                onClick={async () => {
                                    if (!canManage) return;
                                    setAgentUrlImporting(true);
                                    setImportProgress(null);
                                    try {
                                        const response = await skillApi.agentImport.fromUrl(
                                            agentId, agentUrlInput.trim(), skillsCurrentPath,
                                            (current, total, file) => setImportProgress(
                                                current === 0 && total === 0
                                                    ? { message: file }
                                                    : { current, total, file, message: undefined }
                                            )
                                        );
                                        toast.success(t('common.file.filesImported', { count: response.files_written }));
                                        queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                                        // Brief delay so user sees the completed bar
                                        await new Promise(r => setTimeout(r, 600));
                                        setShowAgentUrlImport(false);
                                        setImportProgress(null);
                                    } catch (err: any) {
                                        setImportProgress(null);
                                        await dialog.alert(t('common.error.importFailed'), { type: 'error', details: String(err?.message || err) });
                                    } finally {
                                        setAgentUrlImporting(false);
                                    }
                                }}
                            >
                                {agentUrlImporting ? 'Importing...' : 'Import'}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {showImportSkillModal && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowImportSkillModal(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '600px', width: '90%', maxHeight: '70vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <h3>{t('agent.skills.importPreset', 'Import from Presets')}</h3>
                            <button onClick={() => setShowImportSkillModal(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>✕</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 16px' }}>
                            {t('agent.skills.importDesc', 'Select a preset skill to import into this agent. All skill files will be copied to the agent\'s skills folder.')}
                        </p>
                        <div style={{ flex: 1, overflowY: 'auto' }}>
                            {!globalSkillsForImport ? (
                                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>Loading...</div>
                            ) : globalSkillsForImport.length === 0 ? (
                                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>No preset skills available</div>
                            ) : (
                                globalSkillsForImport.map((skill: any) => (
                                    <div
                                        key={skill.id}
                                        style={{
                                            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                            padding: '12px 14px', borderRadius: '8px', marginBottom: '8px',
                                            border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)',
                                            transition: 'border-color 0.15s',
                                        }}
                                        onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--accent-primary)')}
                                        onMouseLeave={(e) => (e.currentTarget.style.borderColor = 'var(--border-subtle)')}
                                    >
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1 }}>
                                            <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-tertiary)' }}>
                                                {safeDisplayIcon(skill.icon, <IconTools size={20} stroke={1.8} />)}
                                            </span>
                                            <div>
                                                <div style={{ fontWeight: 600, fontSize: '14px' }}>{skill.name}</div>
                                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                                                    {skill.description?.substring(0, 100)}{skill.description?.length > 100 ? '...' : ''}
                                                </div>
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                                                    <IconFolder size={12} stroke={1.8} /> {skill.folder_name}
                                                    {skill.is_default && <span style={{ marginLeft: '8px', color: 'var(--accent-primary)', fontWeight: 600 }}>✓ Default</span>}
                                                </div>
                                            </div>
                                        </div>
                                        <button
                                            className="btn btn-secondary"
                                            style={{ whiteSpace: 'nowrap', fontSize: '12px', padding: '6px 14px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}
                                            disabled={importingSkillId === skill.id}
                                            onClick={async () => {
                                                if (!canManage) return;
                                                setImportingSkillId(skill.id);
                                                try {
                                                    const response = await fileApi.importSkill(agentId, skill.id);
                                                    toast.success(t('common.file.skillImported', { name: skill.name }));
                                                    queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                                                    setShowImportSkillModal(false);
                                                } catch (err: any) {
                                                    await dialog.alert(t('common.error.importFailed'), { type: 'error', details: String(err?.message || err) });
                                                } finally {
                                                    setImportingSkillId(null);
                                                }
                                            }}
                                        >
                                            {importingSkillId === skill.id ? 'Importing...' : <><IconDownload size={13} stroke={1.8} /> Import</>}
                                        </button>
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* ── GitLab Credentials Modal ── */}
            {showGitlabSettings && (
                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowGitlabSettings(false)}>
                    <div onClick={(e) => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '480px', width: '90%', maxHeight: '90vh', overflowY: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                            <h3 style={{ margin: 0 }}>My GitLab Credentials</h3>
                            <button onClick={() => setShowGitlabSettings(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>✕</button>
                        </div>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 16px' }}>
                            Personal credentials for importing skills from your own GitLab repositories.
                        </p>

                        {/* PAT */}
                        <div style={{ marginBottom: '14px' }}>
                            <div style={{ fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>Personal Access Token</div>
                            {myGitlabStatus?.masked && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Current: <code style={{ fontSize: '11px', background: 'var(--bg-tertiary)', padding: '1px 6px', borderRadius: '4px' }}>{myGitlabStatus.masked}</code></div>}
                            <input className="input" autoComplete="off" data-form-type="other" placeholder="glpat-xxxxxxxxxxxx"
                                value={myGitlabToken} onChange={e => setMyGitlabToken(e.target.value)}
                                style={{ width: '100%', fontSize: '13px', fontFamily: 'monospace', marginBottom: '6px', boxSizing: 'border-box', WebkitTextSecurity: 'disc' } as React.CSSProperties} />
                            <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                                {myGitlabStatus?.masked && <button className="btn btn-secondary" style={{ fontSize: '12px' }}
                                    onClick={async () => {
                                        await skillApi.settings.setMyGitlabToken('');
                                        setMyGitlabStatus(await skillApi.settings.getMyGitlab());
                                        toast.success('Token cleared');
                                    }}>Clear</button>}
                                <button className="btn btn-primary" style={{ fontSize: '12px' }}
                                    disabled={!myGitlabToken.trim() || savingMyGitlab}
                                    onClick={async () => {
                                        setSavingMyGitlab(true);
                                        try {
                                            await skillApi.settings.setMyGitlabToken(myGitlabToken.trim());
                                            setMyGitlabStatus(await skillApi.settings.getMyGitlab());
                                            setMyGitlabToken('');
                                            toast.success('Token saved');
                                        } catch (e: any) { toast.error(e.message || 'Failed'); }
                                        setSavingMyGitlab(false);
                                    }}>Save</button>
                            </div>
                        </div>

                        {/* Username + Password */}
                        <div>
                            <div style={{ fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>Username & Password</div>
                            {myGitlabStatus?.username && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Current user: <code style={{ fontSize: '11px', background: 'var(--bg-tertiary)', padding: '1px 6px', borderRadius: '4px' }}>{myGitlabStatus.username}</code></div>}
                            <input className="input" autoComplete="off" data-form-type="other" placeholder="Username"
                                value={myGitlabUser} onChange={e => setMyGitlabUser(e.target.value)}
                                style={{ width: '100%', fontSize: '13px', marginBottom: '6px', boxSizing: 'border-box' }} />
                            <input className="input" type="password" autoComplete="off" data-form-type="other" placeholder="Password"
                                value={myGitlabPass} onChange={e => setMyGitlabPass(e.target.value)}
                                style={{ width: '100%', fontSize: '13px', marginBottom: '6px', boxSizing: 'border-box' }} />
                            <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                                {myGitlabStatus?.username && <button className="btn btn-secondary" style={{ fontSize: '12px' }}
                                    onClick={async () => {
                                        await skillApi.settings.setMyGitlabCredentials('', '');
                                        setMyGitlabStatus(await skillApi.settings.getMyGitlab());
                                        toast.success('Credentials cleared');
                                    }}>Clear</button>}
                                <button className="btn btn-primary" style={{ fontSize: '12px' }}
                                    disabled={!myGitlabUser.trim() || !myGitlabPass.trim() || savingMyGitlab}
                                    onClick={async () => {
                                        setSavingMyGitlab(true);
                                        try {
                                            await skillApi.settings.setMyGitlabCredentials(myGitlabUser.trim(), myGitlabPass.trim());
                                            setMyGitlabStatus(await skillApi.settings.getMyGitlab());
                                            setMyGitlabUser(''); setMyGitlabPass('');
                                            toast.success('Credentials saved');
                                        } catch (e: any) { toast.error(e.message || 'Failed'); }
                                        setSavingMyGitlab(false);
                                    }}>Save</button>
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
