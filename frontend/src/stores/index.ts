/** Global state management with Zustand */

import { create } from 'zustand';
import type { User, Agent } from '../types';

interface AuthStore {
    user: User | null;
    token: string | null;
    setAuth: (user: User, token: string) => void;
    setUser: (user: User) => void;
    logout: () => void;
    isAuthenticated: () => boolean;
}

export const useAuthStore = create<AuthStore>((set, get) => ({
    user: null,
    token: localStorage.getItem('token'),

    setAuth: (user, token) => {
        localStorage.setItem('token', token);
        set({ user, token });
    },

    setUser: (user) => {
        set({ user });
    },

    logout: () => {
        localStorage.removeItem('token');
        set({ user: null, token: null });
    },

    isAuthenticated: () => {
        const t = get().token;
        if (!t) return false;
        // 解码 JWT payload 检查过期时间，避免用已过期 token 发送请求
        try {
            const payload = JSON.parse(atob(t.split('.')[1]));
            const exp = payload.exp ? payload.exp * 1000 : 0;
            return exp > Date.now();
        } catch { return false; }
    },
}));

interface AppStore {
    sidebarCollapsed: boolean;
    toggleSidebar: () => void;
    selectedAgentId: string | null;
    setSelectedAgent: (id: string | null) => void;
}

export const useAppStore = create<AppStore>((set) => ({
    sidebarCollapsed: localStorage.getItem('sidebar_collapsed') === 'true',
    toggleSidebar: () => set((s) => {
        const newState = !s.sidebarCollapsed;
        localStorage.setItem('sidebar_collapsed', String(newState));
        return { sidebarCollapsed: newState };
    }),
    selectedAgentId: null,
    setSelectedAgent: (id) => set({ selectedAgentId: id }),
}));
