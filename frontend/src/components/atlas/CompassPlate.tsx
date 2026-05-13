interface Props {
    size?: number;
    className?: string;
}

/** Tiny scattered "stars" inside the compass rings — fixed positions for stability */
const COMPASS_STARS: Array<[number, number, number, number]> = [
    // [x, y, r, opacity]
    [60, -110, 1.0, 0.55],
    [200, -50, 1.2, 0.7],
    [-110, 90, 0.9, 0.5],
    [-30, 145, 1.1, 0.6],
    [120, 175, 0.9, 0.55],
    [-180, -20, 0.8, 0.45],
];

export default function CompassPlate({ size = 480, className }: Props) {
    const cls = ['atlas-illustration', className].filter(Boolean).join(' ');
    // Concentric ring radii
    const rings = [240, 180, 120, 60];
    const rOuter = rings[0];
    const labelDist = 264;

    return (
        <svg
            className={cls}
            viewBox="0 0 600 600"
            width={size}
            height={size}
            fill="none"
            stroke="currentColor"
            strokeWidth="0.5"
            aria-hidden="true"
        >
            <g transform="translate(300 300)">
                {/* Concentric rings */}
                {rings.map((r, i) => (
                    <circle key={i} cx="0" cy="0" r={r} opacity={i === 0 ? 0.45 : 0.32} />
                ))}

                {/* Cardinal axes — short segments at the rim */}
                <line x1={-rOuter - 12} y1="0" x2={-rOuter + 6} y2="0" opacity="0.4" />
                <line x1={rOuter - 6} y1="0" x2={rOuter + 12} y2="0" opacity="0.4" />
                <line x1="0" y1={-rOuter - 12} x2="0" y2={-rOuter + 6} opacity="0.4" />
                <line x1="0" y1={rOuter - 6} x2="0" y2={rOuter + 12} opacity="0.4" />

                {/* Tick marks every 15° on the outer ring (skip cardinal positions) */}
                {Array.from({ length: 24 }, (_, i) => {
                    if (i % 6 === 0) return null; // 0,90,180,270 = cardinals
                    const a = (i * 15 * Math.PI) / 180;
                    const r1 = rOuter - 4;
                    const r2 = rOuter + 4;
                    return (
                        <line
                            key={i}
                            x1={r1 * Math.cos(a)}
                            y1={r1 * Math.sin(a)}
                            x2={r2 * Math.cos(a)}
                            y2={r2 * Math.sin(a)}
                            opacity="0.35"
                        />
                    );
                })}

                {/* Scattered stars */}
                {COMPASS_STARS.map(([x, y, r, o], i) => (
                    <circle key={i} cx={x} cy={y} r={r} fill="currentColor" stroke="none" opacity={o} />
                ))}

                {/* Center origin marker — open ring + tiny dot */}
                <circle cx="0" cy="0" r="6" />
                <circle cx="0" cy="0" r="1.4" fill="currentColor" stroke="none" />

                {/* Cardinal labels */}
                <g
                    fill="currentColor"
                    stroke="none"
                    fontFamily="IBM Plex Mono, ui-monospace, monospace"
                    fontSize="11"
                    opacity="0.6"
                    style={{ letterSpacing: '0.1em' }}
                >
                    <text x="0" y={-labelDist} textAnchor="middle" dominantBaseline="middle">N</text>
                    <text x={labelDist} y="0" dominantBaseline="middle">E</text>
                    <text x="0" y={labelDist} textAnchor="middle" dominantBaseline="middle">S</text>
                    <text x={-labelDist} y="0" textAnchor="end" dominantBaseline="middle">W</text>
                </g>

                {/* Origin coordinate */}
                <g
                    fill="currentColor"
                    stroke="none"
                    fontFamily="IBM Plex Mono, ui-monospace, monospace"
                    fontSize="9.5"
                    opacity="0.5"
                    style={{ letterSpacing: '0.06em' }}
                >
                    <text x="14" y="-2">00°00′00″</text>
                    <text x="14" y="11">ORIGIN</text>
                </g>
            </g>
        </svg>
    );
}
