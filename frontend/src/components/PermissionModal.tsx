import { useEffect, useRef } from 'react';

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

function DiffView({ oldContent, newContent }: { oldContent: string; newContent: string }) {
    const oldLines = oldContent.split('\n');
    const newLines = newContent.split('\n');

    const rows: { type: 'add' | 'remove'; line: string }[] = [];
    oldLines.forEach(line => rows.push({ type: 'remove', line }));
    newLines.forEach(line => rows.push({ type: 'add', line }));

    return (
        <div style={{
            fontFamily: 'monospace', fontSize: '12px', overflowY: 'auto',
            maxHeight: '50vh', border: '1px solid var(--border-subtle)',
            borderRadius: '6px', background: 'var(--bg-secondary)',
        }}>
            {rows.map((row, i) => (
                <div key={i} style={{
                    background: row.type === 'add' ? 'rgba(40,167,69,0.15)' : 'rgba(220,53,69,0.15)',
                    padding: '1px 8px',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    color: row.type === 'add' ? '#3fb950' : '#f85149',
                }}>
                    {row.type === 'add' ? '+' : '-'} {row.line}
                </div>
            ))}
        </div>
    );
}

export default function PermissionModal({ permission, onResult }: PermissionModalProps) {
    const confirmBtnRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        if (permission) setTimeout(() => confirmBtnRef.current?.focus(), 100);
    }, [permission]);

    if (!permission) return null;

    const isWriteFile = permission.toolName === 'ide_write_file';
    const title = isWriteFile
        ? `写入文件：${permission.filePath ?? ''}`
        : `执行操作：${permission.toolName}`;

    return (
        <div style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', zIndex: 10000,
        }}>
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
                    <button className="btn btn-secondary" onClick={() => onResult(false)}>拒绝</button>
                    <button ref={confirmBtnRef} className="btn btn-primary" onClick={() => onResult(true)}>
                        同意写入
                    </button>
                </div>
            </div>
        </div>
    );
}
