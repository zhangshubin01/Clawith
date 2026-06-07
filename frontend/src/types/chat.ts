/** Chat-specific types — shared across chat components and API layer */

export interface ChatMessage {
  id?: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  thinking?: string;
  timestamp?: string;
  fileName?: string;
  imageUrl?: string;
  sender_name?: string;
  toolName?: string;
  toolArgs?: string;
  toolStatus?: string;
  toolResult?: string;
  toolThinking?: string;
  _streaming?: boolean;
}

export interface PendingChatMessage {
  runtimeKey: string;
  contentForLLM: string;
  userMsg: string;
  fileName?: string;
  imageUrl?: string;
  modelId?: string;
}
