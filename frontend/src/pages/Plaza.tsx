/**
 * Team Experience Library (route kept as /plaza per PRD "保留外壳").
 *
 * Human-curated, AI-consumed private knowledge. Replaces the old social feed.
 * - 团队经验: published entries visible to me (P0-6 filtered server-side).
 * - 我的经验: entries I can manage (I distilled, or I created the source agent).
 * - 历史沉淀: legacy Plaza imports (drafts, hard-isolated) awaiting triage.
 * Draft review drawer sets the four parts + tags + visibility before publish.
 */
import React, { useMemo, useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { experienceApi, type ExperienceEntry, type ExperienceView } from '../services/api';

type Draft = Partial<ExperienceEntry>;

const SCOPE_LABELS: Record<string, string> = { company: '全公司', department: '本部门', user: '指定人' };

function freshness(entry: ExperienceEntry): { label: string; stale: boolean } {
    if (entry.status !== 'published') return { label: '', stale: false };
    if (!entry.last_reviewed_at) return { label: '未复核', stale: true };
    const age = Date.now() - new Date(entry.last_reviewed_at).getTime();
    const stale = age > 90 * 86400000;
    return { label: stale ? '复核超期' : '已复核', stale };
}

const badgeStyle = (bg: string, fg: string): React.CSSProperties => ({
    display: 'inline-block', padding: '1px 7px', borderRadius: 10, fontSize: 11,
    background: bg, color: fg, whiteSpace: 'nowrap',
});

function Badge({ children, tone = 'muted' }: { children: React.ReactNode; tone?: 'muted' | 'warn' | 'ok' | 'accent' }) {
    const tones: Record<string, [string, string]> = {
        muted: ['var(--bg-tertiary, #eee)', 'var(--text-secondary, #666)'],
        warn: ['rgba(220,120,20,.14)', '#c46a10'],
        ok: ['rgba(30,160,90,.14)', '#1a9a56'],
        accent: ['var(--accent-soft, rgba(80,110,220,.14))', 'var(--accent-text, #4a6bdb)'],
    };
    const [bg, fg] = tones[tone];
    return <span style={badgeStyle(bg, fg)}>{children}</span>;
}

export default function Plaza() {
    const { t } = useTranslation();
    const qc = useQueryClient();
    const [params, setParams] = useSearchParams();
    const [view, setView] = useState<ExperienceView>('team');
    const [tag, setTag] = useState<string | null>(null);
    const [openId, setOpenId] = useState<string | null>(null);
    const [editing, setEditing] = useState<Draft | null>(null);

    const { data: entries = [], isLoading } = useQuery({
        queryKey: ['experience', view],
        queryFn: () => experienceApi.list({ view }),
    });

    // Deep-link from the chat "沉淀为经验" action: /plaza?draft=<id> opens the review drawer.
    const draftParam = params.get('draft');
    useEffect(() => {
        if (!draftParam) return;
        experienceApi.get(draftParam).then(e => { setEditing(e); }).catch(() => {});
        params.delete('draft');
        setParams(params, { replace: true });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [draftParam]);

    const tags = useMemo(() => {
        const counts = new Map<string, number>();
        entries.forEach(e => (e.tags || []).forEach(tg => counts.set(tg, (counts.get(tg) || 0) + 1)));
        return [...counts.entries()].sort((a, b) => b[1] - a[1]); // P1-2: order by count
    }, [entries]);

    const shown = tag ? entries.filter(e => (e.tags || []).includes(tag)) : entries;

    const refresh = () => qc.invalidateQueries({ queryKey: ['experience'] });

    const navItem = (key: ExperienceView, label: string) => (
        <button
            key={key}
            onClick={() => { setView(key); setTag(null); setOpenId(null); }}
            style={{
                display: 'block', width: '100%', textAlign: 'left', padding: '8px 12px', marginBottom: 2,
                borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 14,
                background: view === key ? 'var(--accent-soft, rgba(80,110,220,.12))' : 'transparent',
                color: view === key ? 'var(--accent-text, #4a6bdb)' : 'var(--text-primary, #222)',
                fontWeight: view === key ? 600 : 400,
            }}
        >{label}</button>
    );

    return (
        <div style={{ display: 'flex', height: '100%', gap: 0 }}>
            {/* Left nav */}
            <aside style={{ width: 220, flexShrink: 0, borderRight: '1px solid var(--border, #eee)', padding: 16, overflowY: 'auto' }}>
                <h2 style={{ fontSize: 16, margin: '0 0 12px', color: 'var(--text-primary, #222)' }}>
                    {t('experience.title', '经验库')}
                </h2>
                {navItem('team', t('experience.nav.team', '团队经验'))}
                {navItem('mine', t('experience.nav.mine', '我的经验'))}
                {navItem('history', t('experience.nav.history', '历史沉淀（待整理）'))}

                {tags.length > 0 && (
                    <div style={{ marginTop: 18 }}>
                        <div style={{ fontSize: 12, color: 'var(--text-tertiary, #999)', margin: '0 0 6px', padding: '0 12px' }}>
                            {t('experience.tags', '标签')}
                        </div>
                        <button onClick={() => setTag(null)} style={tagBtnStyle(tag === null)}>
                            {t('experience.allTags', '全部')}
                        </button>
                        {tags.map(([tg, n]) => (
                            <button key={tg} onClick={() => setTag(tg)} style={tagBtnStyle(tag === tg)}>
                                #{tg} <span style={{ opacity: .5 }}>{n}</span>
                            </button>
                        ))}
                    </div>
                )}
            </aside>

            {/* Right content */}
            <main style={{ flex: 1, padding: 20, overflowY: 'auto' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
                    <div>
                        <h1 style={{ fontSize: 20, margin: 0, color: 'var(--text-primary, #222)' }}>
                            {view === 'team' ? t('experience.feedTitle', '公司最新经验')
                                : view === 'mine' ? t('experience.nav.mine', '我的经验')
                                    : t('experience.nav.history', '历史沉淀（待整理）')}
                        </h1>
                        <p style={{ fontSize: 13, color: 'var(--text-tertiary, #999)', margin: '4px 0 0' }}>
                            {view === 'history'
                                ? t('experience.historyHint', '旧 Plaza 迁入的草稿，编辑补齐四段后可转正，不进入 AI 检索。')
                                : t('experience.subtitle', '人工策展、AI 按需检索的团队私有经验。')}
                        </p>
                    </div>
                    <button onClick={() => setEditing({ visibility_scope: 'company', tags: [] })} style={primaryBtn}>
                        + {t('experience.new', '新建经验')}
                    </button>
                </div>

                {isLoading ? (
                    <div style={{ color: 'var(--text-tertiary, #999)' }}>{t('common.loading', '加载中...')}</div>
                ) : shown.length === 0 ? (
                    <div style={{ color: 'var(--text-tertiary, #999)', padding: '40px 0', textAlign: 'center' }}>
                        {t('experience.empty', '暂无经验。冷启动不预置内容，靠真实沉淀逐步积累。')}
                    </div>
                ) : (
                    <div style={{ display: 'grid', gap: 12 }}>
                        {shown.map(e => (
                            <EntryCard key={e.id} entry={e} onOpen={() => (e.status === 'published' ? setOpenId(e.id) : setEditing(e))} />
                        ))}
                    </div>
                )}
            </main>

            {openId && (
                <EntryDrawer entryId={openId} onClose={() => setOpenId(null)} onEdit={(e) => { setOpenId(null); setEditing(e); }} onChanged={refresh} />
            )}
            {editing && (
                <DraftEditor draft={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} />
            )}
        </div>
    );
}

function tagBtnStyle(active: boolean): React.CSSProperties {
    return {
        display: 'block', width: '100%', textAlign: 'left', padding: '5px 12px', marginBottom: 1,
        borderRadius: 6, border: 'none', cursor: 'pointer', fontSize: 13,
        background: active ? 'var(--accent-soft, rgba(80,110,220,.12))' : 'transparent',
        color: active ? 'var(--accent-text, #4a6bdb)' : 'var(--text-secondary, #666)',
    };
}

const primaryBtn: React.CSSProperties = {
    padding: '8px 14px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 14,
    background: 'var(--accent, #4a6bdb)', color: '#fff', fontWeight: 500, flexShrink: 0,
};
const secondaryBtn: React.CSSProperties = {
    padding: '7px 12px', borderRadius: 8, border: '1px solid var(--border, #ddd)', cursor: 'pointer',
    fontSize: 13, background: 'var(--bg-secondary, #fff)', color: 'var(--text-primary, #222)',
};

function EntryCard({ entry, onOpen }: { entry: ExperienceEntry; onOpen: () => void }) {
    const f = freshness(entry);
    return (
        <div
            onClick={onOpen}
            style={{
                border: '1px solid var(--border, #eee)', borderRadius: 12, padding: 16, cursor: 'pointer',
                background: 'var(--bg-secondary, #fff)', transition: 'border-color .15s',
            }}
        >
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
                <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-primary, #222)' }}>
                    {entry.title || '(未命名)'}
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {entry.status !== 'published' && <Badge tone="warn">{entry.status === 'retired' ? '已下架' : '草稿'}</Badge>}
                    <Badge tone="accent">{SCOPE_LABELS[entry.visibility_scope] || entry.visibility_scope}</Badge>
                    {f.label && <Badge tone={f.stale ? 'warn' : 'ok'}>{f.label}</Badge>}
                </div>
            </div>
            {entry.scenario && (
                <div style={{ fontSize: 13, color: 'var(--text-secondary, #666)', margin: '8px 0', lineHeight: 1.5,
                    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                    {entry.scenario}
                </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {(entry.tags || []).map(tg => (
                    <span key={tg} style={{ fontSize: 12, color: 'var(--text-tertiary, #999)' }}>#{tg}</span>
                ))}
            </div>
        </div>
    );
}

function Drawer({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
    return (
        <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.35)', zIndex: 1000, display: 'flex', justifyContent: 'flex-end' }}>
            <div onClick={e => e.stopPropagation()} style={{
                width: 'min(560px, 92vw)', height: '100%', background: 'var(--bg-primary, #fff)',
                boxShadow: '-4px 0 24px rgba(0,0,0,.12)', overflowY: 'auto', padding: 24,
            }}>{children}</div>
        </div>
    );
}

const FIELDS: { key: keyof ExperienceEntry; label: string; hint?: string }[] = [
    { key: 'scenario', label: '场景' },
    { key: 'problem', label: '遇到的问题' },
    { key: 'solution', label: '解决方式' },
    { key: 'applicability', label: '适用条件与失效信号', hint: '必填：此经验何时成立、出现什么信号说明已失效' },
];

function EntryDrawer({ entryId, onClose, onEdit, onChanged }: {
    entryId: string; onClose: () => void; onEdit: (e: ExperienceEntry) => void; onChanged: () => void;
}) {
    const { data: entry } = useQuery({ queryKey: ['experience-entry', entryId], queryFn: () => experienceApi.get(entryId) });
    const { data: refs } = useQuery({ queryKey: ['experience-refs', entryId], queryFn: () => experienceApi.references(entryId) });
    const retire = useMutation({ mutationFn: () => experienceApi.retire(entryId), onSuccess: () => { onChanged(); onClose(); } });
    const review = useMutation({ mutationFn: () => experienceApi.review(entryId), onSuccess: () => onChanged() });

    if (!entry) return <Drawer onClose={onClose}><div>加载中...</div></Drawer>;
    const f = freshness(entry);
    return (
        <Drawer onClose={onClose}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                <h2 style={{ margin: 0, fontSize: 18, color: 'var(--text-primary, #222)' }}>{entry.title || '(未命名)'}</h2>
                <button onClick={onClose} style={{ ...secondaryBtn, padding: '4px 10px' }}>✕</button>
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '10px 0 16px' }}>
                <Badge tone="accent">{SCOPE_LABELS[entry.visibility_scope]}</Badge>
                {f.label && <Badge tone={f.stale ? 'warn' : 'ok'}>{f.label}</Badge>}
                {(entry.tags || []).map(tg => <Badge key={tg}>#{tg}</Badge>)}
            </div>
            {FIELDS.map(fl => (
                <section key={fl.key} style={{ marginBottom: 14 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-secondary, #666)', marginBottom: 4 }}>{fl.label}</div>
                    <div style={{ fontSize: 14, color: 'var(--text-primary, #222)', whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                        {(entry[fl.key] as string) || '—'}
                    </div>
                </section>
            ))}
            {refs && (
                <div style={{ margin: '8px 0 16px', fontSize: 13, color: 'var(--text-tertiary, #999)' }}>
                    被 AI 阅读 {refs.read_count} 次 · <strong style={{ color: 'var(--text-secondary,#666)' }}>实际采纳（引用） {refs.cited_count} 次</strong>
                </div>
            )}
            <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                <button onClick={() => onEdit(entry)} style={secondaryBtn}>编辑</button>
                {entry.status === 'published' && (
                    <button onClick={() => review.mutate()} style={secondaryBtn} disabled={review.isPending}>标记已复核</button>
                )}
                <button onClick={() => retire.mutate()} style={{ ...secondaryBtn, color: '#c0392b' }} disabled={retire.isPending}>
                    下架
                </button>
            </div>
            <p style={{ fontSize: 12, color: 'var(--text-tertiary,#999)', marginTop: 10 }}>
                下架仅数字员工创立者可执行；无权限时操作会被拒绝。
            </p>
        </Drawer>
    );
}

function DraftEditor({ draft, onClose, onSaved }: { draft: Draft; onClose: () => void; onSaved: () => void }) {
    const [form, setForm] = useState<Draft>({
        title: '', scenario: '', problem: '', solution: '', applicability: '',
        tags: [], visibility_scope: 'company', visibility_scope_id: null, ...draft,
    });
    const [err, setErr] = useState('');
    const isNew = !draft.id;

    const save = useMutation({
        mutationFn: async () => {
            const payload: Draft = {
                title: form.title, scenario: form.scenario, problem: form.problem,
                solution: form.solution, applicability: form.applicability, tags: form.tags,
                visibility_scope: form.visibility_scope, visibility_scope_id: form.visibility_scope_id || null,
            };
            if (isNew) return experienceApi.create(payload);
            return experienceApi.update(draft.id!, payload);
        },
        onSuccess: onSaved,
        onError: (e: any) => setErr(String(e?.message || e)),
    });

    const publish = useMutation({
        mutationFn: async () => {
            const id = isNew ? (await save.mutateAsync()).id : draft.id!;
            if (!isNew) await experienceApi.update(id, form);
            return experienceApi.publish(id);
        },
        onSuccess: onSaved,
        onError: (e: any) => setErr(String(e?.message || e)),
    });

    const fourFilled = FIELDS.every(f => ((form[f.key] as string) || '').trim());
    const set = (k: keyof ExperienceEntry, v: any) => setForm(p => ({ ...p, [k]: v }));

    return (
        <Drawer onClose={onClose}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h2 style={{ margin: 0, fontSize: 18, color: 'var(--text-primary, #222)' }}>
                    {isNew ? '新建经验草稿' : '审核 / 编辑草稿'}
                </h2>
                <button onClick={onClose} style={{ ...secondaryBtn, padding: '4px 10px' }}>✕</button>
            </div>
            <p style={{ fontSize: 12, color: 'var(--text-tertiary,#999)', margin: '6px 0 16px' }}>
                四段齐全（尤其“适用条件与失效信号”）方可发布；发布前均可修改。
            </p>

            <label style={labelStyle}>标题</label>
            <input value={form.title || ''} onChange={e => set('title', e.target.value)} style={inputStyle} maxLength={200} />

            {FIELDS.map(f => (
                <div key={f.key}>
                    <label style={labelStyle}>{f.label}{f.hint && <span style={{ color: 'var(--text-tertiary,#999)', fontWeight: 400 }}> — {f.hint}</span>}</label>
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
                    <option value="department">指定部门</option>
                    <option value="user">指定用户</option>
                </select>
                {form.visibility_scope !== 'company' && (
                    <input placeholder={form.visibility_scope === 'department' ? '部门 ID' : '用户 ID'}
                        value={form.visibility_scope_id || ''} onChange={e => set('visibility_scope_id', e.target.value)}
                        style={{ ...inputStyle, flex: 1 }} />
                )}
            </div>
            {form.visibility_scope !== 'company' && (
                <p style={{ fontSize: 12, color: 'var(--text-tertiary,#999)' }}>
                    组织架构未同步时，部门/用户可见性发布后会自动降级为全公司。
                </p>
            )}

            {err && <div style={{ color: '#c0392b', fontSize: 13, margin: '10px 0' }}>{err}</div>}

            <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
                <button onClick={() => save.mutate()} style={secondaryBtn} disabled={save.isPending}>保存草稿</button>
                <button onClick={() => publish.mutate()} style={{ ...primaryBtn, opacity: fourFilled ? 1 : .5 }}
                    disabled={!fourFilled || publish.isPending} title={fourFilled ? '' : '四段缺一不可发布'}>
                    确认入库（发布）
                </button>
            </div>
        </Drawer>
    );
}

const labelStyle: React.CSSProperties = {
    display: 'block', fontSize: 13, fontWeight: 600, color: 'var(--text-secondary, #666)', margin: '12px 0 4px',
};
const inputStyle: React.CSSProperties = {
    width: '100%', padding: '8px 10px', borderRadius: 8, border: '1px solid var(--border, #ddd)',
    fontSize: 14, background: 'var(--bg-secondary, #fff)', color: 'var(--text-primary, #222)', boxSizing: 'border-box',
};
