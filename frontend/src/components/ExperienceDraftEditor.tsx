/**
 * Shared experience draft editor — the single human-gated review drawer.
 *
 * Used by both the library page (Plaza) and the in-chat experience_draft card.
 * The agent never writes to the library; a row is created only when the human
 * confirms here (save draft or publish).
 */
import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { experienceApi, orgApi, type ExperienceEntry } from '../services/api';

export type Draft = Partial<ExperienceEntry>;

// The library's fixed four-part schema (P0-3). applicability is mandatory to publish.
export const EXP_FIELDS: { key: keyof ExperienceEntry; label: string; hint?: string }[] = [
    { key: 'scenario', label: '场景' },
    { key: 'problem', label: '遇到的问题' },
    { key: 'solution', label: '解决方式' },
    { key: 'applicability', label: '适用条件与失效信号', hint: '必填：此经验何时成立、出现什么信号说明已失效' },
];

export const primaryBtn: React.CSSProperties = {
    padding: '8px 14px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 14,
    background: 'var(--accent-primary)', color: 'var(--text-inverse)', fontWeight: 500, flexShrink: 0,
};
export const secondaryBtn: React.CSSProperties = {
    padding: '7px 12px', borderRadius: 8, border: '1px solid var(--border-default)', cursor: 'pointer',
    fontSize: 13, background: 'var(--bg-card)', color: 'var(--text-primary)',
};
const labelStyle: React.CSSProperties = {
    display: 'block', fontSize: 13, fontWeight: 700, color: 'var(--text-primary)', margin: '12px 0 4px',
};
const inputStyle: React.CSSProperties = {
    width: '100%', padding: '9px 11px', borderRadius: 8, border: '1px solid var(--border-strong, var(--border-default))',
    fontSize: 14, background: 'var(--bg-primary)', color: 'var(--text-primary)', boxSizing: 'border-box',
};

// 2026年7月9日; empty string for null/invalid so callers can fall back.
const fmtDate = (s?: string | null): string => {
    if (!s) return '';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '';
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
};

export function Drawer({ header, children, footer, onClose, docked = false }: {
    header?: React.ReactNode; children: React.ReactNode; footer?: React.ReactNode;
    onClose: () => void; docked?: boolean;
}) {
    // Flex column: fixed header / scrollable body / pinned footer. Solid opaque surface.
    const panel = (
        <div onClick={e => e.stopPropagation()} style={{
            position: 'fixed', top: 0, right: 0, height: '100vh',
            width: docked ? 'min(460px, 46vw)' : 'min(560px, 92vw)',
            background: 'var(--bg-elevated)', borderLeft: '1px solid var(--border-strong, var(--border-default))',
            boxShadow: '-8px 0 32px rgba(0,0,0,.28)', zIndex: 1001, boxSizing: 'border-box',
            display: 'flex', flexDirection: 'column',
        }}>
            {header && (
                <div style={{ flex: 'none', padding: '20px 24px 12px', borderBottom: '1px solid var(--border-default)' }}>
                    {header}
                </div>
            )}
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', WebkitOverflowScrolling: 'touch', padding: 24 }}>
                {children}
            </div>
            {footer && (
                <div style={{ flex: 'none', padding: '14px 24px', borderTop: '1px solid var(--border-default)', background: 'var(--bg-elevated)' }}>
                    {footer}
                </div>
            )}
        </div>
    );
    // Docked: no backdrop — the rest of the page stays fully bright and interactive.
    const content = docked ? panel : (
        <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.35)', zIndex: 1000 }}>
            {panel}
        </div>
    );
    // Portal to <body> so the drawer escapes any opacity/transform ancestor in the chat DOM
    // (which would otherwise make it translucent and break position:fixed sizing).
    return createPortal(content, document.body);
}

export function DraftEditor({ draft, onClose, onSaved, onDeleted, docked, autoExtractFailed }: {
    draft: Draft; onClose: () => void; onSaved: () => void; onDeleted?: () => void; docked?: boolean;
    // Set when a chat distill produced none of the four parts — shows a manual-fill hint.
    autoExtractFailed?: boolean;
}) {
    const [form, setForm] = useState<Draft>({
        title: '', scenario: '', problem: '', solution: '', applicability: '',
        tags: [], visibility_scope: 'company', visibility_scope_id: null, ...draft,
    });
    const [err, setErr] = useState('');
    const isNew = !draft.id;

    // "指定部门" only means something once an org directory is synced (Feishu/DingTalk/
    // WeCom). Without it, publish silently degrades department → company, so hide the
    // option entirely rather than let the user pick something that won't stick.
    const { data: deptData } = useQuery({
        queryKey: ['org-departments'], queryFn: orgApi.departments, staleTime: 300000, retry: false,
    });
    const hasDepartments = (deptData?.items?.length ?? 0) > 0;

    const buildPayload = (): Draft => ({
        title: form.title, scenario: form.scenario, problem: form.problem,
        solution: form.solution, applicability: form.applicability, tags: form.tags,
        visibility_scope: form.visibility_scope, visibility_scope_id: form.visibility_scope_id || null,
        // Provenance (chat-sourced drafts): records the source agent + conversation.
        origin_agent_id: form.origin_agent_id, origin_session_id: form.origin_session_id,
    });

    const save = useMutation({
        mutationFn: async () => {
            const payload = buildPayload();
            if (isNew) return experienceApi.create(payload);
            return experienceApi.update(draft.id!, payload);
        },
        onSuccess: onSaved,
        onError: (e: any) => setErr(String(e?.message || e)),
    });

    const publish = useMutation({
        // Calls the API directly (not the `save` mutation) so onSaved fires once, not twice.
        mutationFn: async () => {
            const payload = buildPayload();
            const id = isNew ? (await experienceApi.create(payload)).id : draft.id!;
            if (!isNew) await experienceApi.update(id, payload);
            return experienceApi.publish(id);
        },
        onSuccess: onSaved,
        onError: (e: any) => setErr(String(e?.message || e)),
    });

    const del = useMutation({
        mutationFn: () => experienceApi.remove(draft.id!),
        onSuccess: () => onDeleted && onDeleted(),
        onError: (e: any) => setErr(String(e?.message || e)),
    });
    const handleDelete = () => {
        if (window.confirm('确定删除这条草稿？此操作不可撤销。')) del.mutate();
    };

    const fourFilled = EXP_FIELDS.every(f => ((form[f.key] as string) || '').trim());
    const set = (k: keyof ExperienceEntry, v: any) => setForm(p => ({ ...p, [k]: v }));

    const header = (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 style={{ margin: 0, fontSize: 18, color: 'var(--text-primary)' }}>
                {isNew ? '新建经验草稿' : '审核 / 编辑草稿'}
            </h2>
            <button onClick={onClose} style={{ ...secondaryBtn, padding: '4px 10px' }}>✕</button>
        </div>
    );

    const footer = (
        <>
            {err && <div style={{ color: 'var(--error)', fontSize: 13, margin: '0 0 10px' }}>{err}</div>}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <button onClick={() => save.mutate()} style={secondaryBtn} disabled={save.isPending}>保存草稿</button>
                <button onClick={() => publish.mutate()} style={{ ...primaryBtn, opacity: fourFilled ? 1 : .5 }}
                    disabled={!fourFilled || publish.isPending} title={fourFilled ? '' : '四段缺一不可发布'}>
                    确认入库（发布）
                </button>
                {!isNew && onDeleted && (
                    <button onClick={handleDelete} disabled={del.isPending}
                        style={{ ...secondaryBtn, color: 'var(--error)', borderColor: 'var(--error)', marginLeft: 'auto' }}>
                        删除
                    </button>
                )}
            </div>
        </>
    );

    return (
        <Drawer onClose={onClose} docked={docked} header={header} footer={footer}>
            <p style={{ fontSize: 12, color: 'var(--text-tertiary)', margin: '0 0 16px' }}>
                四段齐全（尤其“适用条件与失效信号”）方可发布；发布前均可修改。
            </p>

            {autoExtractFailed && (
                <div style={{
                    fontSize: 13, color: 'var(--error)', background: 'var(--error-subtle)',
                    border: '1px solid var(--error)', borderRadius: 8, padding: '8px 11px', margin: '0 0 16px',
                }}>
                    未能自动抽取，请手动填写。
                </div>
            )}

            {draft.created_at && (
                <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, color: 'var(--text-tertiary)', margin: '0 0 16px' }}>
                    <span>添加日期：{fmtDate(draft.created_at) || '—'}</span>
                    <span>修改日期：{fmtDate(draft.updated_at) || '—'}</span>
                    <span>复核日期：{fmtDate(draft.last_reviewed_at) || '未复核'}</span>
                </div>
            )}

            <label style={labelStyle}>标题</label>
            <input value={form.title || ''} onChange={e => set('title', e.target.value)} style={inputStyle} maxLength={200} />

            {EXP_FIELDS.map(f => (
                <div key={f.key}>
                    <label style={labelStyle}>{f.label}{f.hint && <span style={{ color: 'var(--text-tertiary)', fontWeight: 400 }}> — {f.hint}</span>}</label>
                    <textarea value={(form[f.key] as string) || ''} onChange={e => set(f.key, e.target.value)}
                        style={{ ...inputStyle, minHeight: 72, resize: 'vertical' }} />
                </div>
            ))}

            <label style={labelStyle}>标签（逗号分隔）</label>
            <input value={(form.tags || []).join(', ')} onChange={e => set('tags', e.target.value.split(',').map(s => s.trim()).filter(Boolean))} style={inputStyle} />

            <label style={labelStyle}>可见范围</label>
            <div style={{ display: 'flex', gap: 8 }}>
                <select value={form.visibility_scope} onChange={e => set('visibility_scope', e.target.value)} style={{ ...inputStyle, flex: '0 0 140px' }}>
                    <option value="company">全公司</option>
                    {/* Show 指定部门 only when an org is synced — or when editing an entry that is already department-scoped. */}
                    {(hasDepartments || form.visibility_scope === 'department') && <option value="department">指定部门</option>}
                    <option value="user">指定用户</option>
                </select>
                {form.visibility_scope !== 'company' && (
                    <input placeholder={form.visibility_scope === 'department' ? '部门 ID' : '用户 ID'}
                        value={form.visibility_scope_id || ''} onChange={e => set('visibility_scope_id', e.target.value)}
                        style={{ ...inputStyle, flex: 1 }} />
                )}
            </div>
            {!hasDepartments && (
                <p style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                    未检测到已同步的组织架构，「指定部门」暂不可用；如需按部门可见，请先在「企业设置 › 组织架构」同步（飞书 / 钉钉 / 企业微信）。
                </p>
            )}
            {form.visibility_scope !== 'company' && (
                <p style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                    需填写目标 ID；留空则发布后自动降级为全公司。
                </p>
            )}
        </Drawer>
    );
}
