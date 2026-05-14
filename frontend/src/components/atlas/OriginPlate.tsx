interface Props {
    size?: number;
    topLabel?: string;
    alphaDelta?: string;
    originLabel?: string;
    className?: string;
}

/** Tiny scattered stars across the chart — fixed positions for stability */
const STARS: Array<[number, number, number, number]> = [
    // [x, y, r, opacity]
    [-170, -110, 0.9, 0.55],
    [80, -160, 0.8, 0.5],
    [180, -50, 1.0, 0.6],
    [220, 130, 0.9, 0.5],
    [-200, 80, 0.8, 0.45],
    [-40, 200, 0.9, 0.55],
    [80, -230, 0.7, 0.4],
    [-130, 180, 0.9, 0.5],
    [240, -110, 0.8, 0.45],
    [40, 60, 0.6, 0.35],
    [-90, -180, 0.7, 0.4],
    [120, 200, 0.8, 0.45],
    [180, 250, 0.7, 0.4],
    [-220, -50, 0.8, 0.45],
    [-50, 90, 0.6, 0.3],
];

/**
 * The Login screen's "uncharted territory" plate — same chart chrome
 * as <UniverseMap /> (outer ring + tick marks + center ⊕ + top label)
 * but no orbits or roster, since at Login the universe is still empty.
 */
export default function OriginPlate({
    size = 620,
    topLabel = 'YOUR UNIVERSE',
    alphaDelta = 'α 00ʰ00ᵐ · δ +00°00′',
    originLabel = 'ORIGIN',
    className,
}: Props) {
    const cls = ['atlas-illustration', className].filter(Boolean).join(' ');
    const cx = 320;
    const cy = 340;
    // Outer perimeter is rendered as a "double-line track": two faint thin
    // rings very close together (rOuterOut + rOuterIn), with the inner ring
    // as a single prominent line at rInner.
    const rOuterOut = 268;
    const rOuterIn = 260;
    const rInner = 110;
    const padX = 60;
    const vbX = -padX;
    const vbW = 640 + padX * 2;
    const vbH = 700;

    return (
        <svg
            className={cls}
            viewBox={`${vbX} 0 ${vbW} ${vbH}`}
            width={size}
            height={(size * vbH) / vbW}
            fill="none"
            stroke="currentColor"
            strokeWidth="0.5"
            aria-hidden="true"
        >
            {/* Top label */}
            <text
                x={cx}
                y="22"
                textAnchor="middle"
                fill="currentColor"
                stroke="none"
                fontFamily="IBM Plex Mono, ui-monospace, monospace"
                fontSize="11"
                opacity="0.55"
                style={{ letterSpacing: '0.18em' }}
            >
                {topLabel}
            </text>

            <g transform={`translate(${cx} ${cy})`}>
                {/* Outer perimeter — two very faint thin concentric rings,
                    "double-line" / railway-track look */}
                <circle cx="0" cy="0" r={rOuterOut} opacity="0.2" />
                <circle cx="0" cy="0" r={rOuterIn} opacity="0.2" />

                {/* Inner ring — single prominent line */}
                <circle cx="0" cy="0" r={rInner} opacity="0.95" strokeWidth="0.7" />

                {/* Cross-hair axes spanning the entire chart */}
                <line x1={-rOuterOut} y1="0" x2={rOuterOut} y2="0" opacity="0.1" />
                <line x1="0" y1={-rOuterOut} x2="0" y2={rOuterOut} opacity="0.1" />

                {/* Sparse, short tick marks between the outer pair — every 6°,
                    with subtle longer majors at every 30°. Kept quite faint. */}
                {Array.from({ length: 60 }, (_, i) => {
                    const deg = i * 6;
                    const a = (deg * Math.PI) / 180;
                    const isMajor = deg % 30 === 0;
                    const r1 = rOuterIn;
                    const r2 = rOuterOut + (isMajor ? 3 : 0);
                    return (
                        <line
                            key={i}
                            x1={r1 * Math.cos(a)}
                            y1={r1 * Math.sin(a)}
                            x2={r2 * Math.cos(a)}
                            y2={r2 * Math.sin(a)}
                            opacity={isMajor ? 0.32 : 0.2}
                        />
                    );
                })}

                {/* Scattered stars */}
                {STARS.map(([x, y, r, o], i) => (
                    <circle key={i} cx={x} cy={y} r={r} fill="currentColor" stroke="none" opacity={o} />
                ))}

                {/* Center origin marker — open ring + dot */}
                <circle cx="0" cy="0" r="7" />
                <circle cx="0" cy="0" r="1.6" fill="currentColor" stroke="none" />
                {/* Crosshair extensions through the center marker */}
                <line x1="-13" y1="0" x2="-9" y2="0" opacity="0.45" />
                <line x1="9" y1="0" x2="13" y2="0" opacity="0.45" />
                <line x1="0" y1="-13" x2="0" y2="-9" opacity="0.45" />
                <line x1="0" y1="9" x2="0" y2="13" opacity="0.45" />

                {/* ORIGIN + coordinate labels next to the center */}
                <g
                    fill="currentColor"
                    stroke="none"
                    fontFamily="IBM Plex Mono, ui-monospace, monospace"
                    fontSize="9.5"
                    opacity="0.65"
                    style={{ letterSpacing: '0.1em' }}
                >
                    <text x="16" y="-2">{originLabel}</text>
                    <text x="16" y="11">{alphaDelta}</text>
                </g>
            </g>
        </svg>
    );
}
