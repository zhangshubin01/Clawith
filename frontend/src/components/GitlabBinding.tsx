import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { gitlabBindingApi } from '../services/api';
import { useDialog } from './Dialog/DialogProvider';
import {
    POLL_INTERVAL_MS,
    POLL_MAX_TRIES,
    badgeFor,
    displayProjectPath,
    shouldKeepPolling,
    shouldPoll,
} from '../lib/gitlabBindingState';

interface GitlabBindingProps {
    agentId: string;
    canManage?: boolean;
}

function StatusBadge({ status, timedOut }: { status: string; timedOut: boolean }) {
    const { t } = useTranslation();
    if (timedOut) {
        return <span className="badge" style={{ background: 'var(--warning, #d97706)' }}>{t('agent.settings.gitlab.statusTimeout', '初始化超时，请刷新页面查看')}</span>;
    }
    const badge = badgeFor(status);
    if (badge === 'initializing') {
        return <span className="badge" style={{ background: 'var(--info, #1677ff)' }}>{t('agent.settings.gitlab.statusInitializing', '初始化中…')}</span>;
    }
    if (badge === 'done') {
        return <span className="badge" style={{ background: 'var(--success, #16a34a)' }}>{t('agent.settings.gitlab.statusDone', '✓ 仓库就绪')}</span>;
    }
    if (badge === 'failed') {
        return <span className="badge" style={{ background: 'var(--error, #dc2626)' }}>{t('agent.settings.gitlab.statusFailed', '✗ 失败')}</span>;
    }
    return null;
}

export default function GitlabBinding({ agentId, canManage = true }: GitlabBindingProps) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const [binding, setBinding] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [token, setToken] = useState('');
    const [projectPath, setProjectPath] = useState('');
    const [defaultBranch, setDefaultBranch] = useState('f_android_ai');
    const [error, setError] = useState<string | null>(null);
    const [confirmUnbind, setConfirmUnbind] = useState(false);
    const [pollTimedOut, setPollTimedOut] = useState(false);
    const pollTimer = useRef<number | null>(null);

    const load = async () => {
        const data = await gitlabBindingApi.get(agentId);
        setBinding(data);
        setPollTimedOut(false);
        if (data) {
            setProjectPath(displayProjectPath(data.base_url, data.project_path || ''));
            setDefaultBranch(data.default_branch || 'f_android_ai');
        }
        setLoading(false);
    };

    useEffect(() => {
        load();
        return () => {
            if (pollTimer.current) window.clearInterval(pollTimer.current);
        };
    }, [agentId]);

    useEffect(() => {
        // 超时态不轮询；保存/重新加载会复位 pollTimedOut 并让本 effect 重启轮询
        if (!shouldPoll(binding?.init_status) || pollTimedOut) return;
        let tries = 0;
        pollTimer.current = window.setInterval(async () => {
            tries += 1;
            const data = await gitlabBindingApi.get(agentId);
            if (!data) return; // 瞬时错误：保持轮询，等下次 tick
            setBinding(data);
            // pending/initializing 都继续轮询；只在终态或超限时停
            if (!shouldKeepPolling(data.init_status)) {
                if (pollTimer.current) window.clearInterval(pollTimer.current);
                return;
            }
            if (tries >= POLL_MAX_TRIES) {
                if (pollTimer.current) window.clearInterval(pollTimer.current);
                setPollTimedOut(true);
            }
        }, POLL_INTERVAL_MS);
        return () => {
            if (pollTimer.current) window.clearInterval(pollTimer.current);
        };
    }, [binding?.init_status, pollTimedOut]);

    const save = async () => {
        if (!canManage) return;
        setError(null);
        setSaving(true);
        try {
            const currentDisplay = displayProjectPath(binding?.base_url, binding?.project_path || '');
            if (currentDisplay && currentDisplay !== projectPath && binding?.init_status === 'done') {
                const ok = await dialog.confirm(
                    t('agent.settings.gitlab.confirmChangeProject', '项目路径变更需要手动清空工作区后重新保存才会重新初始化。继续保存？'),
                    { title: t('agent.settings.gitlab.title', 'GitLab 绑定') }
                );
                if (!ok) {
                    setSaving(false);
                    return;
                }
            }
            const payload: any = { project_path: projectPath, default_branch: defaultBranch || 'f_android_ai' };
            if (token) payload.token = token;
            else if (!binding?.has_token) {
                setError(t('agent.settings.gitlab.tokenRequired', '首次绑定必须填写 GitLab Token'));
                setSaving(false);
                return;
            }
            await gitlabBindingApi.put(agentId, payload);
            setToken('');
            // 乐观置「进行中」：立即出徽标反馈，避免保存后 UI 无响应
            setBinding((b: any) => ({ ...(b || {}), init_status: 'pending', init_error: null }));
            setPollTimedOut(false);
            await load();
        } catch (e: any) {
            setError(e?.message || String(e));
        } finally {
            setSaving(false);
        }
    };

    const unbind = async () => {
        if (!canManage) return;
        setSaving(true);
        try {
            await gitlabBindingApi.del(agentId);
            setConfirmUnbind(false);
            setToken('');
            await load();
        } catch (e: any) {
            setError(e?.message || String(e));
        } finally {
            setSaving(false);
        }
    };

    if (loading) return null;

    return (
        <div className="card" style={{ marginBottom: '12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
                <h4 style={{ margin: 0 }}>{t('agent.settings.gitlab.title', 'GitLab 绑定')}</h4>
                {binding && <StatusBadge status={binding.init_status} timedOut={pollTimedOut} />}
            </div>
            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                {t('agent.settings.gitlab.desc', '绑定后自动拉取/初始化代码仓库（纯 git 方式）。每个数字员工绑定一个 GitLab Token 与一个项目。')}
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <div>
                    <label style={{ display: 'block', fontSize: '12px', marginBottom: '4px' }}>{t('agent.settings.gitlab.token', 'GitLab Token')}</label>
                    <input
                        className="form-input"
                        type="password"
                        autoComplete="new-password"
                        disabled={!canManage}
                        value={token}
                        onChange={(e) => setToken(e.target.value)}
                        placeholder={binding?.has_token
                            ? t('agent.settings.gitlab.tokenKeepHint', '已配置（留空则保留现有 Token）')
                            : t('agent.settings.gitlab.tokenPlaceholder', 'glpat-…')}
                    />
                </div>
                <div>
                    <label style={{ display: 'block', fontSize: '12px', marginBottom: '4px' }}>{t('agent.settings.gitlab.project', '项目路径')}</label>
                    <input
                        className="form-input"
                        disabled={!canManage}
                        value={projectPath}
                        onChange={(e) => setProjectPath(e.target.value)}
                        placeholder="zhangshubin/mydome1 或 http://192.168.5.254/zhangshubin/mydome1"
                    />
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {t('agent.settings.gitlab.urlHint', '支持 group/repo 形式，或完整 URL（http(s)://host/group/repo），可指向任意 GitLab 实例')}
                    </div>
                </div>
                <div>
                    <label style={{ display: 'block', fontSize: '12px', marginBottom: '4px' }}>{t('agent.settings.gitlab.branch', '默认分支')}</label>
                    <input
                        className="form-input"
                        disabled={!canManage}
                        value={defaultBranch}
                        onChange={(e) => setDefaultBranch(e.target.value)}
                        placeholder="f_android_ai"
                    />
                </div>
            </div>

            {error && (
                <div style={{ color: 'var(--error)', fontSize: '12px', marginTop: '8px' }}>{error}</div>
            )}

            <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                <button className="btn btn-primary" disabled={!canManage || saving} onClick={save}>
                    {saving ? '…' : t('common.save', '保存')}
                </button>
                {binding?.configured && canManage && (
                    confirmUnbind ? (
                        <>
                            <button className="btn btn-danger" disabled={saving} onClick={unbind}>
                                {t('agent.settings.gitlab.confirmUnbind', '确认解绑')}
                            </button>
                            <button className="btn btn-secondary" onClick={() => setConfirmUnbind(false)}>
                                {t('common.cancel', '取消')}
                            </button>
                        </>
                    ) : (
                        <button className="btn btn-secondary" disabled={saving} onClick={() => setConfirmUnbind(true)}>
                            {t('agent.settings.gitlab.unbind', '解绑')}
                        </button>
                    )
                )}
            </div>

            {binding?.init_status === 'failed' && binding.init_error && (
                <div className="card" style={{ background: 'rgba(220,38,38,0.08)', marginTop: '12px', padding: '10px', fontSize: '12px' }}>
                    <div style={{ color: 'var(--error)', marginBottom: '6px', fontWeight: 600 }}>{binding.init_error}</div>
                    <button className="btn btn-secondary" disabled={saving} onClick={save}>
                        {t('agent.settings.gitlab.retry', '重试')}
                    </button>
                </div>
            )}
            {binding?.configured && binding.init_status === 'done' && binding.has_token && (
                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '8px' }}>
                    {t('agent.settings.gitlab.unbindHint', '解绑后仓库文件保留，但数字员工无法再推送。')}
                </div>
            )}
        </div>
    );
}
