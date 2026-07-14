/**
 * useDropZone — reusable drag-and-drop file upload hook.
 *
 * Uses a counter-based approach to handle nested elements correctly:
 * dragenter/dragleave fire on every child element, so a simple boolean
 * would flicker. The counter increments on dragenter and decrements on
 * dragleave; isDragging is true when counter > 0.
 */
import { useState, useRef, useCallback, type DragEvent } from 'react';

export interface UseDropZoneOptions {
    /** Callback when files are dropped. Receives the filtered file list. */
    onDrop: (files: File[]) => void;
    /** When true, the drop zone is inactive (no visual feedback, drops ignored). */
    disabled?: boolean;
    /**
     * Optional comma-separated list of accepted MIME types or extensions.
     * e.g. ".json" or "image/*,.pdf"
     * Files not matching are silently filtered out.
     */
    accept?: string;
}

export interface UseDropZoneReturn {
    /** True when a drag-with-files is hovering over the zone. */
    isDragging: boolean;
    /** Spread these onto the container element acting as the drop zone. */
    dropZoneProps: {
        onDragEnter: (e: DragEvent) => void;
        onDragOver: (e: DragEvent) => void;
        onDragLeave: (e: DragEvent) => void;
        onDrop: (e: DragEvent) => void;
    };
}

/** Check whether a drag event contains files (vs plain text / URLs). */
function hasFiles(e: DragEvent): boolean {
    if (e.dataTransfer?.types) {
        for (const t of Array.from(e.dataTransfer.types)) {
            if (t === 'Files') return true;
        }
    }
    return false;
}

/** Filter a FileList by an accept string (same format as <input accept>). */
function filterFiles(files: File[] | FileList, accept?: string): File[] {
    const list = Array.from(files);
    if (!accept) return list;

    const tokens = accept.split(',').map(t => t.trim().toLowerCase());

    return list.filter(file => {
        const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
        const mime = file.type.toLowerCase();

        return tokens.some(token => {
            if (token.startsWith('.')) return ext === token;
            if (token.endsWith('/*')) return mime.startsWith(token.slice(0, -1));
            return mime === token;
        });
    });
}

/** Recursively traverse a file-system entry (file or directory), returning
 *  all files with their directory path baked into File.name ("sub/f.txt"). */
async function traverseEntry(entry: FileSystemEntry, prefix: string = ""): Promise<File[]> {
    if (entry.isFile) {
        const file = await new Promise<File>((resolve, reject) => {
            (entry as FileSystemFileEntry).file(resolve, reject);
        });
        const pathFile = new File([file], prefix + file.name, { type: file.type });
        return [pathFile];
    }
    if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntry).createReader();
        // readEntries may return the listing in batches — loop until empty
        const allEntries: FileSystemEntry[] = [];
        let batch: FileSystemEntry[];
        do {
            batch = await new Promise<FileSystemEntry[]>((resolve) => {
                reader.readEntries(resolve);
            });
            allEntries.push(...batch);
        } while (batch.length > 0);
        const results: File[] = [];
        for (const child of allEntries) {
            const children = await traverseEntry(child, prefix + entry.name + "/");
            results.push(...children);
        }
        return results;
    }
    return [];
}

/** Extract all files from a DataTransfer, traversing directory entries
 *  when the browser supports it (webkitGetAsEntry). Falls back to the
 *  flat dataTransfer.files list in unsupported environments. */
async function getAllFilesFromDrop(dt: DataTransfer): Promise<File[]> {
    if (dt.items && dt.items.length > 0) {
        const entries: FileSystemEntry[] = [];
        for (let i = 0; i < dt.items.length; i++) {
            const entry = dt.items[i].webkitGetAsEntry?.();
            if (entry) entries.push(entry);
        }
        if (entries.length > 0) {
            const files: File[] = [];
            for (const entry of entries) {
                const subFiles = await traverseEntry(entry);
                files.push(...subFiles);
            }
            return files;
        }
    }
    // Fallback: flat file list (no directory traversal)
    return Array.from(dt.files || []);
}

export async function traverseEntryForTest(entry: FileSystemEntry, prefix?: string) { return traverseEntry(entry, prefix); }

export function useDropZone({ onDrop, disabled = false, accept }: UseDropZoneOptions): UseDropZoneReturn {
    const [isDragging, setIsDragging] = useState(false);
    const counterRef = useRef(0);

    const handleDragEnter = useCallback((e: DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        if (disabled || !hasFiles(e)) return;
        counterRef.current += 1;
        if (counterRef.current === 1) setIsDragging(true);
    }, [disabled]);

    const handleDragOver = useCallback((e: DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        if (!disabled && hasFiles(e)) {
            e.dataTransfer.dropEffect = 'copy';
        }
    }, [disabled]);

    const handleDragLeave = useCallback((e: DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        if (disabled) return;
        counterRef.current -= 1;
        if (counterRef.current <= 0) {
            counterRef.current = 0;
            setIsDragging(false);
        }
    }, [disabled]);

    const handleDrop = useCallback((e: DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        counterRef.current = 0;
        setIsDragging(false);
        if (disabled || !e.dataTransfer) return;

        getAllFilesFromDrop(e.dataTransfer).then(rawFiles => {
            if (rawFiles.length === 0) return;
            const filtered = filterFiles(rawFiles, accept);
            if (filtered.length > 0) {
                onDrop(filtered);
            }
        });
    }, [disabled, accept, onDrop]);

    return {
        isDragging,
        dropZoneProps: {
            onDragEnter: handleDragEnter,
            onDragOver: handleDragOver,
            onDragLeave: handleDragLeave,
            onDrop: handleDrop,
        },
    };
}
