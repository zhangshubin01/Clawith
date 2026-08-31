/** GitLab 绑定 init 状态机 —— 纯函数，供组件消费、合同测试直接覆盖。
 *
 * 状态语义（后端 channel_configs.extra_config.init_status）：
 * - pending:       已保存、init 任务尚未开始（排队中）
 * - initializing:  init 任务进行中
 * - done:          仓库就绪
 * - failed:        初始化失败（含 token 解密失败/未配置 token 等配置错误）
 * - unbound:       已解绑
 *
 * pending 与 initializing 对用户同义为「进行中」：都显示徽标、都轮询；
 * 轮询读回 pending 必须继续轮询（不能清定时器），否则 UI 会冻结。
 */

export const POLL_INTERVAL_MS = 3000;
export const POLL_MAX_TRIES = 40;

export type InitStatus = string;

/** 进行中 = 用户应看到「初始化中…」并可轮询。 */
export function isInProgress(status: InitStatus | null | undefined): boolean {
    return status === 'pending' || status === 'initializing';
}

/** 是否应该启动/继续轮询。 */
export function shouldPoll(status: InitStatus | null | undefined): boolean {
    return isInProgress(status);
}

/** 轮询一次后是否继续：终态（done/failed/unbound）停，进行中继续（含 pending）。 */
export function shouldKeepPolling(status: InitStatus | null | undefined): boolean {
    return isInProgress(status);
}

/** 徽标类型：进行中统一显示「初始化中…」，done/failed 各一个，其余不显示。 */
export function badgeFor(status: InitStatus | null | undefined): 'initializing' | 'done' | 'failed' | null {
    if (isInProgress(status)) return 'initializing';
    if (status === 'done' || status === 'failed') return status;
    return null;
}

/** 表单展示串：有显式实例时用完整 URL，保证表单往返无损。 */
export function displayProjectPath(baseUrl: string | null | undefined, projectPath: string): string {
    if (baseUrl && projectPath) return `${baseUrl.replace(/\/+$/, '')}/${projectPath}`;
    return projectPath;
}
