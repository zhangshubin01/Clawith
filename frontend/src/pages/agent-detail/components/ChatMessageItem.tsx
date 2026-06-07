import React from 'react';
import type { TFunction } from 'i18next';
import type { ChatMessage } from '../../../types/chat';
import MarkdownRenderer from '../../../components/MarkdownRenderer';
import { IconPaperclip } from '@tabler/icons-react';
import { copyToClipboard } from '../../../utils/clipboard';

// ─── CopyMessageButton ─────────────────────────────────────────────
function CopyMessageButton({ text }: { text: string }) {
    const [copied, setCopied] = React.useState(false);
    React.useEffect(() => {
        if (!copied) return;
        const t = setTimeout(() => setCopied(false), 1800);
        return () => clearTimeout(t);
    }, [copied]);
    return (
        <button
            className="chat-msg-copy-btn"
            onClick={() => { copyToClipboard(text).then(() => setCopied(true)); }}
            title={copied ? 'Copied' : 'Copy'}
        >
            {copied ? (
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                </svg>
            ) : (
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
            )}
        </button>
    );
}

// ─── ChatMessageItem Props ─────────────────────────────────────────
interface ChatMessageItemProps {
    msg: ChatMessage;
    i: number;
    isLeft: boolean;
    t: TFunction;
    senderLabel?: string;
    avatarText?: string;
    forceSenderLabel?: boolean;
    hideAvatar?: boolean;
}

// ─── ChatMessageItem ───────────────────────────────────────────────
export const ChatMessageItem = React.memo(({
    msg, i, isLeft, t, senderLabel, avatarText, forceSenderLabel = false, hideAvatar = false,
}: ChatMessageItemProps) => {
    const fe = msg.fileName?.split('.').pop()?.toLowerCase() ?? '';
    const isImage = msg.imageUrl && ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(fe);
    const resolvedSenderLabel = msg.sender_name || senderLabel;
    const resolvedAvatarText = avatarText || (resolvedSenderLabel ? resolvedSenderLabel[0] : (isLeft ? 'A' : 'U'));
    const showSenderLabel = !!resolvedSenderLabel && (forceSenderLabel || !!msg.sender_name);
    const isStreaming = msg._streaming ?? false;

    // Parse [image_data:data:image/...;base64,...] markers from user message content.
    const IMAGE_DATA_RE = /\[image_data:(data:image\/[^;]+;base64,[^\]]+)\]/g;
    const inlineImages: string[] = [];
    let displayContent = msg.content || '';
    if (displayContent.includes('[image_data:')) {
        displayContent = displayContent.replace(IMAGE_DATA_RE, (_: string, dataUrl: string) => {
            if (!msg.imageUrl) inlineImages.push(dataUrl);
            return '';
        }).trim();
    }

    const timestampHtml = msg.timestamp ? (() => {
        const d = new Date(msg.timestamp);
        const now = new Date();
        const diffMs = now.getTime() - d.getTime();
        const isToday = d.toDateString() === now.toDateString();
        let timeStr = '';
        if (isToday) timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        else if (diffMs < 7 * 86400000) timeStr = d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        else timeStr = d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        return (
            <div className="chat-msg-timestamp">
                {timeStr}
                {msg.content && <CopyMessageButton text={msg.content} />}
            </div>
        );
    })() : null;

    return (
        <div key={i} className={`chat-msg-row${isLeft ? '' : ' chat-msg-row--user'}`}>
            <div
                className={`chat-msg-avatar${isLeft ? '' : ' chat-msg-avatar--user'}`}
                style={hideAvatar ? { visibility: 'hidden' } : undefined}
            >
                {resolvedAvatarText}
            </div>
            <div className="chat-msg-col">
                <div className={isLeft ? '' : 'chat-msg-user-line'}>
                    <div className={`chat-msg-bubble${isLeft ? '' : ' chat-msg-bubble--user'}${isStreaming && !msg.content && !msg.thinking ? ' chat-msg-bubble--thinking' : ''}`}>
                        {showSenderLabel && <div className="chat-msg-sender">{resolvedSenderLabel}</div>}
                        {isImage ? (
                            <div className="chat-msg-image-wrap">
                                <img src={msg.imageUrl} alt={msg.fileName} className="chat-msg-image" loading="lazy" />
                            </div>
                        ) : (msg.fileName && (
                            <div className="chat-msg-file-chip" style={{ marginBottom: msg.content ? '4px' : '0' }}>
                                <IconPaperclip size={14} stroke={1.8} />
                                <span className="chat-msg-file-chip-text">{msg.fileName}</span>
                            </div>
                        ))}
                        {inlineImages.length > 0 && (
                            <div className="chat-msg-inline-images">
                                {inlineImages.map((url, idx) => (
                                    <img
                                        key={idx}
                                        src={url}
                                        alt="attached image"
                                        className="chat-msg-image"
                                        loading="lazy"
                                    />
                                ))}
                            </div>
                        )}
                        {msg.role === 'assistant' ? (
                            isStreaming && !msg.content && !msg.thinking ? (
                                <div className="thinking-indicator">
                                    <div className="thinking-dots"><span /><span /><span /></div>
                                    <span style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('agent.chat.thinking', { defaultValue: 'Thinking...' })}</span>
                                </div>
                            ) : <MarkdownRenderer content={displayContent} />
                        ) : <MarkdownRenderer content={displayContent} />}
                    </div>
                </div>
                {timestampHtml}
            </div>
        </div>
    );
});
