interface Props {
    size?: number;
    /** Rotation of the whole orbital system in degrees */
    rotation?: number;
    className?: string;
}

/** Pre-placed dots on the four orbits (using local pre-rotation coords) */
const DOTS: Array<{ orbit: number; theta: number }> = [
    { orbit: 3, theta: 165 }, // upper-left, on the outermost ring
    { orbit: 3, theta: 22 },  // upper-right, on the outermost ring
    { orbit: 2, theta: 155 }, // mid, left
    { orbit: 2, theta: 22 },  // mid, right
];

export default function CosmographyPlate({ size = 560, rotation = -18, className }: Props) {
    const cls = ['atlas-illustration', className].filter(Boolean).join(' ');
    // Four concentric, co-rotating ellipses (rx,ry)
    const orbits: Array<[number, number]> = [
        [80, 46],
        [160, 92],
        [240, 138],
        [320, 184],
    ];

    return (
        <svg
            className={cls}
            viewBox="0 0 800 800"
            width={size}
            height={size}
            fill="none"
            stroke="currentColor"
            strokeWidth="0.7"
            aria-hidden="true"
        >
            <g transform={`translate(400 400) rotate(${rotation})`}>
                {/* Concentric tilted orbits */}
                {orbits.map(([rx, ry], i) => (
                    <ellipse key={i} cx="0" cy="0" rx={rx} ry={ry} opacity={0.7 - i * 0.06} />
                ))}

                {/* Tick marks on outermost orbit every ~20° (alternating short/long pattern) */}
                {Array.from({ length: 18 }, (_, i) => {
                    const a = (i * 20 * Math.PI) / 180;
                    const [rx, ry] = orbits[orbits.length - 1];
                    const px = rx * Math.cos(a);
                    const py = ry * Math.sin(a);
                    const len = Math.sqrt(px * px + py * py);
                    const ux = px / len;
                    const uy = py / len;
                    // Hide some ticks to give the rim a sketched feel
                    if (i % 3 === 2) return null;
                    const tickLen = i % 6 === 0 ? 14 : 8;
                    return (
                        <line
                            key={i}
                            x1={px}
                            y1={py}
                            x2={px + ux * tickLen}
                            y2={py + uy * tickLen}
                            opacity="0.55"
                        />
                    );
                })}

                {/* Dots scattered on the orbits */}
                {DOTS.map((d, i) => {
                    const [rx, ry] = orbits[d.orbit];
                    const a = (d.theta * Math.PI) / 180;
                    return (
                        <circle
                            key={i}
                            cx={rx * Math.cos(a)}
                            cy={ry * Math.sin(a)}
                            r="4"
                            fill="currentColor"
                            stroke="none"
                        />
                    );
                })}

                {/* Center "⊕" marker — open circle with cross-hair extending slightly outside */}
                <circle cx="0" cy="0" r="14" />
                <line x1="-22" y1="0" x2="22" y2="0" />
                <line x1="0" y1="-22" x2="0" y2="22" />
            </g>
        </svg>
    );
}
