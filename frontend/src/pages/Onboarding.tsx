import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { IconArrowLeft, IconArrowRight, IconWorld } from '@tabler/icons-react';
import { onboardingApi } from '../services/api';
import { useAuthStore } from '../stores';

type Step = 'assistant' | 'opening';

export default function Onboarding() {
    const { i18n } = useTranslation();
    const isZh = i18n.language.startsWith('zh');
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const user = useAuthStore((s) => s.user);
    const mode = (searchParams.get('mode') === 'join' ? 'join' : 'create') as 'create' | 'join';
    const [step, setStep] = useState<Step>('assistant');
    const [assistantId, setAssistantId] = useState<string | null>(null);
    const [assistantName, setAssistantName] = useState('Clawiee');
    const [personality, setPersonality] = useState('warm');
    const [workStyle, setWorkStyle] = useState('concise');
    const [boundaries, setBoundaries] = useState('');
    const [expanded, setExpanded] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');
    }, []);

    useEffect(() => {
        let cancelled = false;
        onboardingApi.start(mode)
            .then((status) => {
                if (cancelled) return;
                if (status?.status === 'completed' && status.personal_assistant_agent_id) {
                    navigate(`/agents/${status.personal_assistant_agent_id}/chat`, { replace: true });
                    return;
                }
                if (status?.personal_assistant_agent_id) {
                    setAssistantId(status.personal_assistant_agent_id);
                    setStep('opening');
                }
            })
            .catch((err) => setError(err.message || 'Failed to start onboarding'));
        return () => { cancelled = true; };
    }, [mode, navigate]);

    const personalityOptions = useMemo(() => [
        { id: 'warm', zh: '温和', en: 'Warm' },
        { id: 'precise', zh: '严谨', en: 'Precise' },
        { id: 'quiet', zh: '幽默', en: 'Witty' },
        { id: 'direct', zh: '直接', en: 'Direct' },
    ], []);
    const workStyleOptions = useMemo(() => [
        { id: 'concise', zh: '简洁', en: 'Concise' },
        { id: 'efficient', zh: '高效', en: 'Efficient' },
        { id: 'detailed', zh: '详尽', en: 'Detailed' },
        { id: 'steady', zh: '保守', en: 'Steady' },
    ], []);

    const createAssistant = async () => {
        setError('');
        setLoading(true);
        try {
            const result = await onboardingApi.createPersonalAssistant({
                name: assistantName.trim(),
                personality,
                work_style: workStyle,
                boundaries,
            });
            const nextId = result?.agent?.id || result?.onboarding?.personal_assistant_agent_id;
            setAssistantId(nextId);
            setStep('opening');
        } catch (err: any) {
            setError(err.message || 'Failed to create personal assistant');
        } finally {
            setLoading(false);
        }
    };

    const enterOffice = () => {
        if (!assistantId) return;
        navigate(`/plaza?tour=company&assistantId=${assistantId}`);
    };

    const toggleLang = () => i18n.changeLanguage(isZh ? 'en' : 'zh');

    if (!user?.tenant_id) {
        return (
            <div className="onboarding-page onboarding-page--light">
                <div className="onboarding-center-card">
                    <h1>{isZh ? '先创建或加入一家公司' : 'Create or join a company first'}</h1>
                    <button className="onboarding-primary-btn" onClick={() => navigate('/setup-company')}>
                        {isZh ? '去设置公司' : 'Set up company'}
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div className={step === 'assistant' ? 'onboarding-page onboarding-page--dark' : 'onboarding-page onboarding-page--light'}>
            <button className="onboarding-lang" type="button" onClick={toggleLang} aria-label="Language">
                <IconWorld size={18} stroke={1.8} />
            </button>

            {step === 'assistant' && (
                <>
                    <button className="onboarding-back" onClick={() => navigate(-1)}>
                        <IconArrowLeft size={15} /> {isZh ? '返回' : 'Back'}
                    </button>
                    <div className="assistant-stage">
                        <div className="assistant-spotlight" aria-hidden="true">
                            <div className="assistant-light-beam" />
                            <div className="assistant-silhouette">
                                <span />
                                <strong />
                            </div>
                        </div>
                        <div className="assistant-panel">
                            <h1>{isZh ? '见见你的第一位员工。' : 'Meet your first employee.'}</h1>
                            <p>{isZh
                                ? '你的私人助理 —— 打理日程、备忘、和你不愿亲自处理的事。'
                                : "Your personal assistant — for your calendar, your memory, and the things you'd rather hand off."}</p>
                            {error && <div className="onboarding-error">{error}</div>}
                            <input
                                className="assistant-name-input"
                                value={assistantName}
                                onChange={(e) => setAssistantName(e.target.value)}
                                placeholder={isZh ? '助理的名字' : 'Assistant name'}
                            />
                            <button className="assistant-expand" type="button" onClick={() => setExpanded(v => !v)}>
                                {isZh ? '定制你的助理' : 'Customize your assistant'}
                                <span>{expanded ? (isZh ? '收起' : 'Collapse') : (isZh ? '展开' : 'Expand')}</span>
                            </button>
                            {expanded && (
                                <div className="assistant-options">
                                    <div>
                                        <label>{isZh ? '性格' : 'Personality'}</label>
                                        <div className="assistant-chips">
                                            {personalityOptions.map((item) => (
                                                <button key={item.id} type="button" className={personality === item.id ? 'active' : ''} onClick={() => setPersonality(item.id)}>
                                                    {isZh ? item.zh : item.en}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                    <div>
                                        <label>{isZh ? '办事风格' : 'Work style'}</label>
                                        <div className="assistant-chips">
                                            {workStyleOptions.map((item) => (
                                                <button key={item.id} type="button" className={workStyle === item.id ? 'active' : ''} onClick={() => setWorkStyle(item.id)}>
                                                    {isZh ? item.zh : item.en}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                    <textarea
                                        value={boundaries}
                                        onChange={(e) => setBoundaries(e.target.value)}
                                        placeholder={isZh ? '绝对不要做的事情（可留空）' : 'Things they should never do (optional)'}
                                    />
                                </div>
                            )}
                            <div className="assistant-footer">
                                <button className="onboarding-primary-btn" onClick={createAssistant} disabled={loading || !assistantName.trim()}>
                                    {loading ? <span className="login-spinner" /> : (isZh ? '欢迎入职' : 'Welcome aboard')} <IconArrowRight size={16} />
                                </button>
                                <button className="assistant-footer-skip" type="button" onClick={createAssistant} disabled={loading}>
                                    {isZh ? '暂时跳过' : 'Skip for now'}
                                </button>
                            </div>
                        </div>
                    </div>
                </>
            )}

            {step === 'opening' && (
                <div className="opening-stage opening-stage--minimal">
                    <svg className="opening-newstar" viewBox="0 0 100 100" aria-hidden="true">
                        <defs>
                            <radialGradient id="opening-halo" cx="50%" cy="50%" r="50%">
                                <stop offset="0%" stopColor="#111116" stopOpacity="0.95" />
                                <stop offset="18%" stopColor="#111116" stopOpacity="0.5" />
                                <stop offset="50%" stopColor="#3a3a55" stopOpacity="0.14" />
                                <stop offset="100%" stopColor="#3a3a55" stopOpacity="0" />
                            </radialGradient>
                            <linearGradient id="opening-ray" x1="0%" y1="50%" x2="100%" y2="50%">
                                <stop offset="0%" stopColor="#111116" stopOpacity="0" />
                                <stop offset="50%" stopColor="#111116" stopOpacity="0.85" />
                                <stop offset="100%" stopColor="#111116" stopOpacity="0" />
                            </linearGradient>
                            <filter id="opening-shimmer" x="-30%" y="-30%" width="160%" height="160%">
                                <feTurbulence type="fractalNoise" baseFrequency="0.45" numOctaves="2" seed="11" result="noise" />
                                <feDisplacementMap in="SourceGraphic" in2="noise" scale="3" />
                            </filter>
                        </defs>
                        <g transform="translate(50 50)">
                            <circle r="26" fill="url(#opening-halo)" filter="url(#opening-shimmer)" opacity="0.3" />
                            <circle r="9" fill="url(#opening-halo)" filter="url(#opening-shimmer)" opacity="0.55" />
                            <rect x="-26" y="-0.22" width="52" height="0.45" fill="url(#opening-ray)" opacity="0.7" />
                            <rect x="-26" y="-0.22" width="52" height="0.45" fill="url(#opening-ray)" opacity="0.7" transform="rotate(90)" />
                            <rect x="-14" y="-0.16" width="28" height="0.3" fill="url(#opening-ray)" opacity="0.3" transform="rotate(38)" />
                            <rect x="-14" y="-0.16" width="28" height="0.3" fill="url(#opening-ray)" opacity="0.3" transform="rotate(-38)" />
                            <circle r="1.6" fill="#111116" />
                        </g>
                    </svg>
                    <h1>{isZh ? '灯，亮了。' : 'The lights are on.'}</h1>
                    {error && <div className="onboarding-error">{error}</div>}
                    <button className="onboarding-primary-btn" onClick={enterOffice} disabled={!assistantId}>
                        {isZh ? '进入办公室' : 'Enter office'} <IconArrowRight size={16} />
                    </button>
                </div>
            )}
        </div>
    );
}
