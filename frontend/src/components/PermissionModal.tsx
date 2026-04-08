import { useEffect, useRef, useState } from 'react';

export interface PendingPermission {
    permissionId: string;
    wsKey: string;          // 用于找回对应 WebSocket
    toolName: string;
    filePath?: string;
    oldContent?: string;
    newContent?: string;
    argsSummary?: string;
}

interface PermissionModalProps {
    permission: PendingPermission | null;
    onResult: (granted: boolean) => void;
}

function computeDiff(oldLines: string[], newLines: string[]): { type: 'add' | 'remove' | 'same'; line: string }[] {
    // Myers diff via LCS table
    const m = oldLines.length;
    const n = newLines.length;
    // dp[i][j] = LCS length of oldLines[0..i) and newLines[0..j)
    const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            dp[i][j] = oldLines[i - 1] === newLines[j - 1]
                ? dp[i - 1][j - 1] + 1
                : Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
    }
    // Backtrack
    const rows: { type: 'add' | 'remove' | 'same'; line: string }[] = [];
    let i = m, j = n;
    while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
            rows.push({ type: 'same', line: oldLines[i - 1] });
            i--; j--;
        } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
            rows.push({ type: 'add', line: newLines[j - 1] });
            j--;
        } else {
            rows.push({ type: 'remove', line: oldLines[i - 1] });
            i--;
        }
    }
    rows.reverse();
    return rows;
}

const CONTEXT_LINES = 3; // 变更行前后各显示 3 行上下文

function DiffView({ oldContent, newContent }: { oldContent: string; newContent: string }) {
    const oldLines = oldContent ? oldContent.split('\n') : [];
    const newLines = newContent ? newContent.split('\n') : [];

    if (oldLines.length === 0 && newLines.length === 0) {
        return <div style={{ padding: '8px', color: 'var(--text-secondary)', fontSize: '12px' }}>（无内容）</div>;
    }

    const allRows = computeDiff(oldLines, newLines);
    const hasChanges = allRows.some(r => r.type !== 'same');

    if (!hasChanges) {
        return <div style={{ padding: '8px', color: 'var(--text-secondary)', fontSize: '12px' }}>（内容无变化）</div>;
    }

    // 只显示有改动的行及其上下文，折叠中间的 same 块
    const visible: (typeof allRows[0] & { lineNum?: number })[] = [];
    const changeIndices = new Set(allRows.map((r, i) => r.type !== 'same' ? i : -1).filter(i => i >= 0));
    let lastIncluded = -1;

    allRows.forEach((row, i) => {
        const nearChange = [...changeIndices].some(ci => Math.abs(ci - i) <= CONTEXT_LINES);
        if (nearChange) {
            if (lastIncluded >= 0 && i > lastIncluded + 1) {
                visible.push({ type: 'same', line: `@@ ... ${i - lastIncluded - 1} lines omitted ...` });
            }
            visible.push(row);
            lastIncluded = i;
        }
    });

    return (
        <div style={{
            fontFamily: 'monospace', fontSize: '12px', overflowY: 'auto',
            maxHeight: '50vh', border: '1px solid var(--border-subtle)',
            borderRadius: '6px', background: 'var(--bg-secondary)',
        }}>
            {visible.map((row, i) => (
                <div key={i} style={{
                    background: row.type === 'add' ? 'rgba(40,167,69,0.15)' : row.type === 'remove' ? 'rgba(220,53,69,0.15)' : 'transparent',
                    padding: '1px 8px',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    color: row.type === 'add' ? '#3fb950' : row.type === 'remove' ? '#f85149' : 'var(--text-secondary)',
                    fontStyle: row.type === 'same' && row.line.startsWith('@@') ? 'italic' : 'normal',
                }}>
                    {row.type === 'add' ? '+ ' : row.type === 'remove' ? '- ' : '  '}{row.line}
                </div>
            ))}
        </div>
    );
}

export default function PermissionModal({ permission, onResult }: PermissionModalProps) {
    const confirmBtnRef = useRef<HTMLButtonElement>(null);

    const [expired, setExpired] = useState(false);

    useEffect(() => {
        if (!permission) { setExpired(false); return; }
        setExpired(false);
        const timer = setTimeout(() => setExpired(true), 110_000);
        return () => clearTimeout(timer);
    }, [permission?.permissionId]);

    useEffect(() => {
        if (permission) setTimeout(() => confirmBtnRef.current?.focus(), 100);
    }, [permission]);

    if (!permission) return null;

    const isWriteFile = permission.toolName === 'ide_write_file';
    const title = isWriteFile
        ? `写入文件：${permission.filePath ?? ''}`
        : `执行操作：${permission.toolName}`;

    return (
        <div
            style={{
                position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
                background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center',
                justifyContent: 'center', zIndex: 10000,
            }}
            onClick={(e) => { if (e.target === e.currentTarget) onResult(false); }}
        >
            <div style={{
                background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px',
                width: isWriteFile ? '80vw' : '420px', maxWidth: '95vw',
                maxHeight: '90vh', display: 'flex', flexDirection: 'column',
                border: '1px solid var(--border-subtle)',
                boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
            }}>
                <h4 style={{ marginBottom: '12px', fontSize: '15px', flexShrink: 0 }}>{title}</h4>

                <div style={{ flex: 1, overflow: 'hidden', marginBottom: '16px' }}>
                    {isWriteFile ? (
                        <DiffView
                            oldContent={permission.oldContent ?? ''}
                            newContent={permission.newContent ?? ''}
                        />
                    ) : (
                        <pre style={{
                            fontSize: '12px', background: 'var(--bg-secondary)',
                            borderRadius: '6px', padding: '10px', overflowX: 'auto',
                            color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                        }}>
                            {permission.argsSummary ?? ''}
                        </pre>
                    )}
                </div>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', flexShrink: 0 }}>
                    {expired && (
                        <span style={{ fontSize: '12px', color: '#f85149', alignSelf: 'center', marginRight: '8px' }}>
                            请求已超时，操作已被自动拒绝
                        </span>
                    )}
                    <button className="btn btn-secondary" onClick={() => onResult(false)}>拒绝</button>
                    <button
                        ref={confirmBtnRef}
                        className="btn btn-primary"
                        onClick={() => onResult(true)}
                        disabled={expired}
                        style={expired ? { opacity: 0.5, cursor: 'not-allowed' } : undefined}
                    >
                        {isWriteFile ? '同意写入' : '同意执行'}
                    </button>
                </div>
            </div>
        </div>
    );
}
