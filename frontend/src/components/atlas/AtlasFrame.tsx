import type { ReactNode } from 'react';
import { IconWorld } from '@tabler/icons-react';

interface Props {
    /** When provided, shows "01 — 03" pagination next to the brand */
    step?: number;
    totalSteps?: number;
    /** When provided, replaces the Clawith brand with a "← BACK" pill button */
    onBack?: () => void;
    /** When provided, renders the language toggle in the top-right corner */
    onToggleLang?: () => void;
    /** Optional footer line on the left (uppercase mono). Defaults to brand line */
    footerLeft?: string;
    /** Optional footer line on the right. Defaults to MMXXVI */
    footerRight?: string;
    /** Page contents */
    children: ReactNode;
    className?: string;
}

function BrandMark() {
    // Renders the official Clawith logo, swapped by theme via CSS
    return (
        <span className="atlas-brand-mark-img" aria-hidden="true">
            <img src="/logo-black.png" alt="" className="atlas-brand-mark-light" />
            <img src="/logo-white.png" alt="" className="atlas-brand-mark-dark" />
        </span>
    );
}

export default function AtlasFrame({
    step,
    totalSteps = 3,
    onBack,
    onToggleLang,
    footerLeft = 'CLW · 2026 · YOUR AGENT COMPANY',
    footerRight = 'MMXXVI',
    className,
    children,
}: Props) {
    const pageClass = ['atlas-page', className].filter(Boolean).join(' ');
    return (
        <div className={pageClass}>
            <header className="atlas-frame-top">
                <div className="atlas-frame-top-left">
                    {onBack ? (
                        <button type="button" className="atlas-back-btn" onClick={onBack}>
                            <span aria-hidden="true">←</span> Back
                        </button>
                    ) : (
                        <div className="atlas-brand">
                            <BrandMark />
                            <span className="atlas-brand-name">Clawith</span>
                        </div>
                    )}
                    {step !== undefined && (
                        <div className="atlas-step" aria-label={`Step ${step} of ${totalSteps}`}>
                            <span className="atlas-step-rule" />
                            <span>{String(step).padStart(2, '0')}</span>
                            <span className="atlas-step-rule" />
                            <span>{String(totalSteps).padStart(2, '0')}</span>
                        </div>
                    )}
                </div>
                {onToggleLang && (
                    <button type="button" className="atlas-globe-btn" onClick={onToggleLang} aria-label="Toggle language">
                        <IconWorld size={16} stroke={1.4} />
                    </button>
                )}
            </header>

            <main className="atlas-frame-body">{children}</main>

            <footer className="atlas-frame-bottom">
                <span>{footerLeft}</span>
                <span>{footerRight}</span>
            </footer>
            <div className="atlas-corner atlas-corner--bl" aria-hidden="true" />
            <div className="atlas-corner atlas-corner--br" aria-hidden="true" />
        </div>
    );
}
