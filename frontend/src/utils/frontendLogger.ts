/**
 * 前端日志工具 — 同时输出到浏览器控制台 + 批量上报到后端。
 *
 * 设计要点:
 * - 同步输出到 console (开发调试友好)
 * - 异步批量上报到 POST /api/log/frontend (5s 间隔 / 50条批量 / 页面卸载前强制 flush)
 * - 上报失败静默 — 不产生递归日志
 */
type LogLevel = 'info' | 'warn' | 'error'
type LogModule = 'WS' | 'Stream' | 'Render' | 'App' | 'Auth'

interface FrontendLogEntry {
  level: LogLevel
  module: LogModule
  message: string
  data?: Record<string, unknown>
  ts: number
}

const MAX_BATCH = 50
const FLUSH_INTERVAL_MS = 5000

class FrontendLogger {
  private buffer: FrontendLogEntry[] = []
  private flushTimer: ReturnType<typeof setInterval> | null = null

  constructor() {
    this.flushTimer = setInterval(() => this.flush(), FLUSH_INTERVAL_MS)
    window.addEventListener('beforeunload', () => this.flush())
  }

  log(level: LogLevel, module: LogModule, message: string, data?: Record<string, unknown>) {
    const entry: FrontendLogEntry = { level, module, message, data, ts: Date.now() }

    // 同步控制台输出
    const prefix = `[${module}]`
    if (level === 'error') console.error(prefix, message, data || '')
    else if (level === 'warn') console.warn(prefix, message, data || '')
    else console.info(prefix, message, data || '')

    // 加入批量缓冲区
    this.buffer.push(entry)
    if (this.buffer.length >= MAX_BATCH || level === 'error') {
      this.flush()
    }
  }

  private async flush() {
    if (this.buffer.length === 0) return
    const batch = this.buffer.splice(0)
    try {
      await fetch('/api/log/frontend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ logs: batch }),
        keepalive: true,
      })
    } catch {
      // 上报失败静默 — 不递归日志
    }
  }
}

export const frontendLogger = new FrontendLogger()
