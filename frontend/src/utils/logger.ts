const _traceStorage = { current: '' };
export const setTraceId = (id: string) => { _traceStorage.current = id; };
export const getTraceId = () => _traceStorage.current || '-';

export const logger = {
  info: (msg: string, ...args: unknown[]) => console.log(`[${new Date().toISOString()}] [trace=${getTraceId()}] ${msg}`, ...args),
  warn: (msg: string, ...args: unknown[]) => console.warn(`[${new Date().toISOString()}] [trace=${getTraceId()}] ${msg}`, ...args),
  error: (msg: string, ...args: unknown[]) => console.error(`[${new Date().toISOString()}] [trace=${getTraceId()}] ${msg}`, ...args),
  debug: (msg: string, ...args: unknown[]) => console.debug(`[${new Date().toISOString()}] [trace=${getTraceId()}] ${msg}`, ...args),
};
