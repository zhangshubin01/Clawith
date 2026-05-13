import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { IconArrowLeft, IconArrowRight, IconWorld } from '@tabler/icons-react';
import { onboardingApi } from '../services/api';
import { useAuthStore } from '../stores';
import { Button, HairlineInput, MonoLabel, ConstellationFigure, CompassMedallion } from '../components/atlas';

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
        <div className="onboarding-page onboarding-page--atlas">
            <button className="onboarding-lang atlas-icon-btn" type="button" onClick={toggleLang} aria-label="Language">
                <IconWorld size={18} stroke={1.8} />
            </button>

            {step === 'assistant' && (
                <>
                    <button className="onboarding-back atlas-btn atlas-btn--ghost atlas-back-link" onClick={() => navigate(-1)}>
                        <IconArrowLeft size={14} stroke={1.5} /> {isZh ? '返回' : 'BACK'}
                    </button>
                    <div className="assistant-stage assistant-stage--atlas">
                        <div className="assistant-plate-left">
                            <ConstellationFigure width={280} />
                            <div className="assistant-meta">
                                <MonoLabel as="div">[ EMP-001 ]</MonoLabel>
                                <MonoLabel as="div">{isZh ? '首位居民' : 'First inhabitant'}</MonoLabel>
                                <MonoLabel as="div">{isZh ? '代号：' : 'Designation: '}{(assistantName || 'Clawiee').toUpperCase()}</MonoLabel>
                            </div>
                        </div>
                        <div className="assistant-panel assistant-panel--atlas">
                            <h1 className="atlas-display">{isZh ? '见见你的第一位员工。' : 'Meet your first employee.'}</h1>
                            <p className="atlas-body atlas-body--muted">{isZh
                                ? '你的私人助理 —— 打理日程、备忘、和你不愿亲自处理的事。'
                                : "Your personal assistant — for your calendar, your memory, and the things you'd rather hand off."}</p>
                            {error && <div className="atlas-error">{error}</div>}
                            <HairlineInput
                                label={isZh ? '助理名字' : 'ASSISTANT NAME'}
                                value={assistantName}
                                onChange={(e) => setAssistantName(e.target.value)}
                                placeholder={isZh ? '助理的名字' : 'Assistant name'}
                                serif="lg"
                            />
                            <button className="assistant-expand assistant-expand--atlas" type="button" onClick={() => setExpanded(v => !v)}>
                                <span className="atlas-body">{isZh ? '定制你的助理' : 'Customize your assistant'}</span>
                                <span className="atlas-mono">{expanded ? (isZh ? '收起' : 'COLLAPSE') : (isZh ? '展开' : 'EXPAND')}</span>
                            </button>
                            {expanded && (
                                <div className="assistant-options assistant-options--atlas">
                                    <div>
                                        <MonoLabel as="div">{isZh ? '性格' : 'Personality'}</MonoLabel>
                                        <div className="assistant-chips">
                                            {personalityOptions.map((item) => (
                                                <button key={item.id} type="button" className={personality === item.id ? 'active' : ''} onClick={() => setPersonality(item.id)}>
                                                    {isZh ? item.zh : item.en}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                    <div>
                                        <MonoLabel as="div">{isZh ? '办事风格' : 'Work style'}</MonoLabel>
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
                            <div className="assistant-footer assistant-footer--atlas">
                                <Button variant="primary" onClick={createAssistant} disabled={loading || !assistantName.trim()}>
                                    {loading ? '…' : (isZh ? '欢迎入职 →' : 'WELCOME ABOARD →')}
                                </Button>
                                <Button variant="ghost" type="button" onClick={createAssistant} disabled={loading}>
                                    {isZh ? '暂时跳过' : 'Skip for now'}
                                </Button>
                            </div>
                        </div>
                    </div>
                </>
            )}

            {step === 'opening' && (
                <div className="opening-stage opening-stage--atlas">
                    <div className="opening-grid-bg" aria-hidden="true" />
                    <div className="opening-stack">
                        <CompassMedallion size={160} />
                        <h1 className="atlas-display opening-stage-title">{isZh ? '灯，亮了。' : 'The lights are on.'}</h1>
                        {error && <div className="atlas-error">{error}</div>}
                        <Button variant="primary" onClick={enterOffice} disabled={!assistantId}>
                            {isZh ? '进入办公室 →' : 'ENTER OFFICE →'} <IconArrowRight size={14} stroke={1.5} />
                        </Button>
                        <MonoLabel as="div" className="opening-fig-caption">FIG. IV · THRESHOLD</MonoLabel>
                    </div>
                </div>
            )}
        </div>
    );
}
