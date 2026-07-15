import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconRobot } from '@tabler/icons-react';
import { groupApi } from '../../services/groupApi';
import FileBrowser, { type FileBrowserApi } from '../../components/FileBrowser';
import type { GroupMember } from '../../types/group';

/**
 * Group memory is per (agent, group): each agent keeps its own memory.md for this group, loaded
 * only when that agent is mentioned here. Humans may read, edit and delete any of them.
 *
 * The backend stores a single fixed file (agents/{id}/memory/memory.md), so the FileBrowser here
 * lists exactly that one entry — this gives the same work-list view used for a private agent's
 * memory (Mind tab), rather than a bare textarea.
 */
export default function GroupMemoryTab({
    groupId,
    members,
}: {
    groupId: string;
    members: GroupMember[];
}) {
    const { t } = useTranslation();
    const agents = members.filter((member) => member.participant_type === 'agent');
    const [agentRefId, setAgentRefId] = useState<string | undefined>(agents[0]?.participant_ref_id);

    useEffect(() => {
        if (!agentRefId && agents.length > 0) setAgentRefId(agents[0].participant_ref_id);
    }, [agents, agentRefId]);

    const api = useMemo<FileBrowserApi>(() => ({
        // Always surface the memory.md card, even before it exists, so a human can open it and write
        // the agent's first memory; the backend returns exists:false with empty content until then.
        list: async () => [{ name: 'memory.md', path: 'memory.md', is_dir: false }],
        read: async () => {
            const file = await groupApi.agentMemory(groupId, agentRefId!);
            return { content: file.content };
        },
        write: async (_path: string, content: string) => {
            // FileBrowser hands us no version token, so read the current one first: writing with it
            // makes the backend reject a save that would clobber someone else's concurrent edit.
            let expected: string | null = null;
            try {
                const current = await groupApi.agentMemory(groupId, agentRefId!);
                expected = current.exists ? current.version_token : null;
            } catch {
                expected = null;
            }
            return groupApi.saveAgentMemory(groupId, agentRefId!, content, expected);
        },
        delete: () => groupApi.deleteAgentMemory(groupId, agentRefId!),
    }), [groupId, agentRefId]);

    if (agents.length === 0) {
        return (
            <div className="group-empty-hint">
                {t('groups.noAgentsForMemory', '群里还没有智能体。邀请一个之后，它在这个群的记忆会显示在这里。')}
            </div>
        );
    }

    return (
        <>
            <div className="group-memory-agents">
                {agents.map((agent) => (
                    <button
                        key={agent.participant_id}
                        type="button"
                        className={`group-memory-agent ${agent.participant_ref_id === agentRefId ? 'active' : ''}`}
                        onClick={() => setAgentRefId(agent.participant_ref_id)}
                    >
                        <IconRobot size={13} stroke={1.6} />
                        {agent.display_name}
                    </button>
                ))}
            </div>

            <div className="group-panel-note">
                {t('groups.memoryNote', '这是该智能体在本群的长期记忆，只在它于本群被 @ 时加载。')}
            </div>

            {agentRefId && (
                <FileBrowser
                    // Remount per agent so the viewer/edit state never leaks across the selector.
                    key={agentRefId}
                    api={api}
                    features={{ upload: false, newFile: false, newFolder: false, edit: true, delete: true }}
                />
            )}
        </>
    );
}
