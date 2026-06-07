import { useState, useRef, useCallback } from 'react';

interface PaginationParams {
    limit: number;
    before: string;
}

interface UseHistoryPaginationReturn<T> {
    items: T[];
    setItems: (items: T[] | ((prev: T[]) => T[])) => void;
    hasMore: boolean;
    setHasMore: (v: boolean) => void;
    loadingMore: boolean;
    loadMore: (fetchFn: (params: PaginationParams) => Promise<T[]>) => Promise<void>;
    reset: () => void;
    oldestTimestampRef: React.MutableRefObject<string | null>;
}

export function useHistoryPagination<T extends { timestamp?: string }>(
    pageSize: number,
): UseHistoryPaginationReturn<T> {
    const [items, setItems] = useState<T[]>([]);
    const [hasMore, setHasMore] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const oldestTimestampRef = useRef<string | null>(null);

    const loadMore = useCallback(async (fetchFn: (params: PaginationParams) => Promise<T[]>) => {
        const ts = oldestTimestampRef.current;
        if (loadingMore || !hasMore || !ts) return;

        setLoadingMore(true);
        try {
            const msgs = await fetchFn({ limit: pageSize, before: ts });
            if (msgs.length === 0) {
                setHasMore(false);
                return;
            }
            const oldestTs = msgs[0]?.timestamp ?? null;
            setItems(prev => [...msgs, ...prev]);
            if (oldestTs) oldestTimestampRef.current = oldestTs;
            setHasMore(msgs.length >= pageSize);
        } finally {
            setLoadingMore(false);
        }
    }, [loadingMore, hasMore, pageSize]);

    const reset = useCallback(() => {
        setItems([]);
        setHasMore(true);
        setLoadingMore(false);
        oldestTimestampRef.current = null;
    }, []);

    return {
        items,
        setItems,
        hasMore,
        setHasMore,
        loadingMore,
        loadMore,
        reset,
        oldestTimestampRef,
    };
}
