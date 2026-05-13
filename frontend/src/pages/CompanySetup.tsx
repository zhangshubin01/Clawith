import { useState, useEffect } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconWorld, IconX } from '@tabler/icons-react';
import { useAuthStore } from '../stores';
import { tenantApi, authApi } from '../services/api';
import { LoneStar, HairlineInput, Button, MonoLabel } from '../components/atlas';

export default function CompanySetup() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const location = useLocation();
    const { user, setAuth } = useAuthStore();
    const [allowCreate, setAllowCreate] = useState(true);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');

    // Check if coming from registration flow.
    // Primary: location.state.fromRegister (set by Login page).
    // Fallback: if user exists but is not active, they're in the registration flow
    // (the Navigate in ProtectedRoute may strip location.state).
    const fromRegister = (location.state as any)?.fromRegister || (user && !user.is_active);

    // Join company form
    const [inviteCode, setInviteCode] = useState('');
    const [showJoinModal, setShowJoinModal] = useState(false);
    const [joinError, setJoinError] = useState('');
    // Create company form
    const [companyName, setCompanyName] = useState('');

    useEffect(() => {
        // Check if self-creation is allowed
        tenantApi.registrationConfig().then((d: any) => {
            setAllowCreate(d.allow_self_create_company);
        }).catch(() => {});
    }, []);

    // Allow access from login tenant selection dialog ("Create or Join Organization")
    // Use URL param instead of location.state for robustness (survives refresh)
    const [searchParams] = useSearchParams();
    const fromTenantSelection = searchParams.get('from') === 'tenant-selection';

    // If user already has a company and not from registration/tenant-selection, redirect home
    useEffect(() => {
        if (user?.tenant_id && !fromRegister && !fromTenantSelection) {
            navigate('/');
        }
    }, [user, navigate, fromRegister, fromTenantSelection]);

    const refreshUser = async () => {
        try {
            const me = await authApi.me();
            const token = useAuthStore.getState().token;
            if (token) setAuth(me, token);
            return me;
        } catch { return null; }
    };

    const applyTenantSetupResult = async (result: any) => {
        const nextTenantId = result?.tenant?.id ? String(result.tenant.id) : '';
        if (result?.access_token) {
            localStorage.setItem('token', result.access_token);
            if (nextTenantId) {
                localStorage.setItem('current_tenant_id', nextTenantId);
                window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: nextTenantId }));
            }
            const me = await authApi.me();
            setAuth(me, result.access_token);
            return me;
        }
        if (nextTenantId) {
            localStorage.setItem('current_tenant_id', nextTenantId);
            window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: nextTenantId }));
        }
        return refreshUser();
    };

    const handleJoin = async (e: React.FormEvent) => {
        e.preventDefault();
        setJoinError('');
        setError('');
        setLoading(true);
        try {
            const result = await tenantApi.join(inviteCode);
            await applyTenantSetupResult(result);
            navigate('/onboarding?mode=join');
        } catch (err: any) {
            const msg = err.message || 'Failed to join company';
            if (showJoinModal) setJoinError(msg);
            else setError(msg);
        } finally {
            setLoading(false);
        }
    };

    const handleCreate = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setLoading(true);
        try {
            const result = await tenantApi.selfCreate({ name: companyName });
            await applyTenantSetupResult(result);
            if (fromRegister) {
                navigate('/onboarding?mode=create');
            } else {
                navigate('/onboarding?mode=create');
            }
        } catch (err: any) {
            setError(err.message || 'Failed to create company');
        } finally {
            setLoading(false);
        }
    };

    const toggleLang = () => {
        i18n.changeLanguage(i18n.language === 'zh' ? 'en' : 'zh');
    };

    // If not from registration/tenant-selection and user already has tenant, don't show
    if (!fromRegister && !fromTenantSelection && user?.tenant_id) {
        return null;
    }

    const isZh = i18n.language.startsWith('zh');

    return (
        <div className="company-setup-page company-setup-page--atlas">
            <LoneStar className="company-setup-bg" />

            <button type="button" className="company-setup-lang-switcher" onClick={toggleLang} aria-label="Toggle language">
                <IconWorld size={18} stroke={1.8} />
            </button>

            <div className="company-setup-container">
                <h1 className="atlas-display company-setup-title">
                    {isZh ? '开始吧。给你的公司起个名字。' : "Let's begin. Name your Company."}
                </h1>

                {error && (
                    <div className="atlas-error">
                        <IconAlertTriangle size={14} stroke={1.8} /> {error}
                    </div>
                )}

                {allowCreate ? (
                    <form className="atlas-form" onSubmit={handleCreate}>
                        <HairlineInput
                            value={companyName}
                            onChange={(e) => setCompanyName(e.target.value)}
                            required
                            autoFocus
                            placeholder={isZh ? '在这里写下名字' : 'Write the name here'}
                            serif="md"
                        />
                        <MonoLabel>{isZh ? '名称 · 必填' : 'DESIGNATION · REQUIRED'}</MonoLabel>
                        <Button variant="outline" type="submit" disabled={loading || !companyName.trim()}>
                            {loading ? '…' : (isZh ? '继续 →' : 'CONTINUE →')}
                        </Button>
                    </form>
                ) : (
                    <form className="atlas-form" onSubmit={handleJoin}>
                        <HairlineInput
                            value={inviteCode}
                            onChange={(e) => setInviteCode(e.target.value)}
                            required
                            autoFocus
                            placeholder={t('companySetup.inviteCodePlaceholder', 'e.g. ABC12345')}
                            style={{ textTransform: 'uppercase', letterSpacing: '2px', fontFamily: 'var(--font-mono)' }}
                        />
                        <MonoLabel>{isZh ? '邀请码 · 必填' : 'INVITATION CODE · REQUIRED'}</MonoLabel>
                        <Button variant="primary" type="submit" disabled={loading || !inviteCode.trim()}>
                            {loading ? '…' : t('companySetup.joinBtn', 'JOIN COMPANY')}
                        </Button>
                    </form>
                )}

                {allowCreate && (
                    <button
                        type="button"
                        className="atlas-mono atlas-text-link company-setup-join-link"
                        onClick={() => { setJoinError(''); setShowJoinModal(true); }}
                    >
                        {isZh ? '加入已有团队？' : 'Joining an existing team?'}
                    </button>
                )}
            </div>

            {showJoinModal && (
                <div
                    className="atlas-modal-overlay"
                    onClick={() => !loading && setShowJoinModal(false)}
                >
                    <div className="atlas-modal" onClick={(e) => e.stopPropagation()}>
                        <button
                            type="button"
                            className="atlas-modal-close"
                            onClick={() => setShowJoinModal(false)}
                            disabled={loading}
                            aria-label="Close"
                        >
                            <IconX size={18} stroke={1.8} />
                        </button>
                        <h2 className="atlas-h1 atlas-modal-title">
                            {isZh ? '加入已有团队' : 'Join an existing team'}
                        </h2>
                        <p className="atlas-body atlas-body--muted atlas-modal-desc">
                            {isZh
                                ? '输入团队管理员发给你的邀请码。'
                                : 'Enter the invitation code your team admin shared with you.'}
                        </p>
                        <form onSubmit={handleJoin} className="atlas-form">
                            <HairlineInput
                                value={inviteCode}
                                onChange={(e) => setInviteCode(e.target.value)}
                                required
                                autoFocus
                                placeholder={t('companySetup.inviteCodePlaceholder', 'e.g. ABC12345')}
                                style={{ textTransform: 'uppercase', letterSpacing: '2px', fontFamily: 'var(--font-mono)' }}
                            />
                            {joinError && (
                                <div className="atlas-error">
                                    <IconAlertTriangle size={14} stroke={1.8} /> {joinError}
                                </div>
                            )}
                            <Button variant="primary" type="submit" disabled={loading || !inviteCode.trim()}>
                                {loading ? '…' : (isZh ? '加入' : 'JOIN')}
                            </Button>
                        </form>
                    </div>
                </div>
            )}
        </div>
    );
}
