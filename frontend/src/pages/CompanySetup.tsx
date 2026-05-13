import { useState, useEffect } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconWorld, IconX } from '@tabler/icons-react';
import { useAuthStore } from '../stores';
import { tenantApi, authApi } from '../services/api';
import CosmicBackground from '../components/CosmicBackground';

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

    return (
        <div className="company-setup-page company-setup-page--dark">
            {/* Particle starfield background */}
            <CosmicBackground />

            {/* The "new company" — pulsing star, positioned in upper-right */}
            <svg
                className="company-setup-newstar"
                viewBox="0 0 100 100"
                aria-hidden="true"
            >
                <defs>
                    <radialGradient id="newstar-halo" cx="50%" cy="50%" r="50%">
                        <stop offset="0%" stopColor="#ffffff" stopOpacity="1" />
                        <stop offset="18%" stopColor="#ffffff" stopOpacity="0.55" />
                        <stop offset="50%" stopColor="#cdd8ff" stopOpacity="0.14" />
                        <stop offset="100%" stopColor="#a8b8ff" stopOpacity="0" />
                    </radialGradient>
                    <linearGradient id="newstar-ray" x1="0%" y1="50%" x2="100%" y2="50%">
                        <stop offset="0%" stopColor="#ffffff" stopOpacity="0" />
                        <stop offset="50%" stopColor="#ffffff" stopOpacity="1" />
                        <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
                    </linearGradient>
                    <filter id="newstar-shimmer" x="-30%" y="-30%" width="160%" height="160%">
                        <feTurbulence type="fractalNoise" baseFrequency="0.45" numOctaves="2" seed="7" result="noise" />
                        <feDisplacementMap in="SourceGraphic" in2="noise" scale="3" />
                    </filter>
                </defs>

                <g transform="translate(50 50)">
                    {/* outer hazy halo with irregular edges */}
                    <circle r="26" fill="url(#newstar-halo)" filter="url(#newstar-shimmer)" opacity="0.32">
                        <animate attributeName="opacity" values="0.28;0.36;0.28" dur="4.7s" repeatCount="indefinite" />
                    </circle>

                    {/* mid glow — slightly irregular */}
                    <circle r="9" fill="url(#newstar-halo)" filter="url(#newstar-shimmer)" opacity="0.5">
                        <animate attributeName="opacity" values="0.42;0.56;0.46" dur="3.2s" repeatCount="indefinite" />
                    </circle>

                    {/* diffraction spikes — short, soft */}
                    <rect x="-26" y="-0.22" width="52" height="0.45" fill="url(#newstar-ray)" opacity="0.55">
                        <animate attributeName="opacity" values="0.42;0.62;0.46" dur="5.3s" repeatCount="indefinite" />
                    </rect>
                    <rect x="-26" y="-0.22" width="52" height="0.45" fill="url(#newstar-ray)" opacity="0.55" transform="rotate(90)">
                        <animate attributeName="opacity" values="0.46;0.6;0.42" dur="4.1s" repeatCount="indefinite" />
                    </rect>

                    {/* very faint diagonal hints */}
                    <rect x="-14" y="-0.16" width="28" height="0.3" fill="url(#newstar-ray)" opacity="0.22" transform="rotate(38)" />
                    <rect x="-14" y="-0.16" width="28" height="0.3" fill="url(#newstar-ray)" opacity="0.22" transform="rotate(-38)" />

                    {/* tiny core */}
                    <circle r="1.4" fill="#ffffff">
                        <animate attributeName="opacity" values="0.88;1;0.88" dur="3.7s" repeatCount="indefinite" />
                    </circle>
                </g>
            </svg>

            {/* Language Switcher */}
            <button type="button" className="company-setup-lang-switcher" onClick={toggleLang} aria-label="Toggle language">
                <IconWorld size={18} stroke={1.8} />
            </button>

            <div className="company-setup-container">
                <div className="company-setup-header company-setup-header--genesis">
                    <h1>{i18n.language.startsWith('zh')
                        ? '开始吧。给你的公司起个名字。'
                        : "Let's begin. Name your Company."}</h1>
                </div>

                {error && (
                    <div className="login-error" style={{ marginBottom: 16 }}>
                        <span><IconAlertTriangle size={14} stroke={1.8} /></span> {error}
                    </div>
                )}

                {allowCreate ? (
                    <form className="company-name-form" onSubmit={handleCreate}>
                        <input
                            value={companyName}
                            onChange={(e) => setCompanyName(e.target.value)}
                            required
                            autoFocus
                            placeholder={i18n.language.startsWith('zh') ? '在这里写下名字' : 'Write the name here'}
                        />
                        <button className="onboarding-primary-btn" type="submit" disabled={loading || !companyName.trim()}>
                            {loading ? <span className="login-spinner" /> : (i18n.language.startsWith('zh') ? '继续' : 'Continue')}
                        </button>
                    </form>
                ) : (
                    <form className="company-name-form" onSubmit={handleJoin}>
                        <input
                            value={inviteCode}
                            onChange={(e) => setInviteCode(e.target.value)}
                            required
                            autoFocus
                            placeholder={t('companySetup.inviteCodePlaceholder', 'e.g. ABC12345')}
                            style={{ textTransform: 'uppercase', letterSpacing: '2px', fontFamily: 'monospace' }}
                        />
                        <button className="onboarding-primary-btn" type="submit" disabled={loading || !inviteCode.trim()}>
                            {loading ? <span className="login-spinner" /> : t('companySetup.joinBtn', 'Join Company')}
                        </button>
                    </form>
                )}

                {allowCreate && (
                    <button
                        type="button"
                        className="company-setup-join-link"
                        onClick={() => { setJoinError(''); setShowJoinModal(true); }}
                    >
                        {i18n.language.startsWith('zh') ? '加入已有团队？' : 'Joining an existing team?'}
                    </button>
                )}
            </div>

            {showJoinModal && (
                <div
                    className="join-modal-overlay"
                    onClick={() => !loading && setShowJoinModal(false)}
                >
                    <div className="join-modal" onClick={(e) => e.stopPropagation()}>
                        <button
                            type="button"
                            className="join-modal-close"
                            onClick={() => setShowJoinModal(false)}
                            disabled={loading}
                            aria-label="Close"
                        >
                            <IconX size={18} stroke={1.8} />
                        </button>
                        <h2 className="join-modal-title">
                            {i18n.language.startsWith('zh') ? '加入已有团队' : 'Join an existing team'}
                        </h2>
                        <p className="join-modal-desc">
                            {i18n.language.startsWith('zh')
                                ? '输入团队管理员发给你的邀请码。'
                                : 'Enter the invitation code your team admin shared with you.'}
                        </p>
                        <form onSubmit={handleJoin} className="join-modal-form">
                            <input
                                value={inviteCode}
                                onChange={(e) => setInviteCode(e.target.value)}
                                required
                                autoFocus
                                placeholder={t('companySetup.inviteCodePlaceholder', 'e.g. ABC12345')}
                                className="join-modal-input"
                            />
                            {joinError && (
                                <div className="login-error" style={{ marginTop: 4 }}>
                                    <span><IconAlertTriangle size={14} stroke={1.8} /></span> {joinError}
                                </div>
                            )}
                            <button
                                className="onboarding-primary-btn"
                                type="submit"
                                disabled={loading || !inviteCode.trim()}
                            >
                                {loading
                                    ? <span className="login-spinner" />
                                    : (i18n.language.startsWith('zh') ? '加入' : 'Join')}
                            </button>
                        </form>
                    </div>
                </div>
            )}
        </div>
    );
}
