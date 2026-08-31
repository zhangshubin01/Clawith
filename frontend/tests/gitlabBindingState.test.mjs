import assert from 'node:assert/strict';
import test from 'node:test';

import {
  POLL_INTERVAL_MS,
  POLL_MAX_TRIES,
  badgeFor,
  displayProjectPath,
  isInProgress,
  shouldKeepPolling,
  shouldPoll,
} from '../src/lib/gitlabBindingState.ts';

test('pending 与 initializing 同义为进行中（这是「保存无响应」事故的核心语义）', () => {
  assert.equal(isInProgress('pending'), true);
  assert.equal(isInProgress('initializing'), true);
  assert.equal(isInProgress('done'), false);
  assert.equal(isInProgress('failed'), false);
  assert.equal(isInProgress('unbound'), false);
  assert.equal(isInProgress(null), false);
  assert.equal(isInProgress(undefined), false);
});

test('轮询：进行中（含 pending）都轮询，且轮询读回 pending 必须继续', () => {
  assert.equal(shouldPoll('pending'), true);
  assert.equal(shouldPoll('initializing'), true);
  assert.equal(shouldPoll('done'), false);
  assert.equal(shouldPoll('failed'), false);

  // 轮询 tick 读回 pending：不能清定时器（历史 bug：pending → 停轮询 → UI 冻结）
  assert.equal(shouldKeepPolling('pending'), true);
  assert.equal(shouldKeepPolling('initializing'), true);
  assert.equal(shouldKeepPolling('done'), false);
  assert.equal(shouldKeepPolling('failed'), false);
  assert.equal(shouldKeepPolling(null), false);
});

test('徽标：进行中统一为「初始化中」，终态各自呈现，其余不显示', () => {
  assert.equal(badgeFor('pending'), 'initializing');
  assert.equal(badgeFor('initializing'), 'initializing');
  assert.equal(badgeFor('done'), 'done');
  assert.equal(badgeFor('failed'), 'failed');
  assert.equal(badgeFor('unbound'), null);
  assert.equal(badgeFor(undefined), null);
});

test('展示串：有显式实例时拼完整 URL，保证表单往返无损', () => {
  assert.equal(displayProjectPath('http://192.168.5.254', 'zhangshubin/mydome1'), 'http://192.168.5.254/zhangshubin/mydome1');
  assert.equal(displayProjectPath('http://192.168.5.254/', 'zhangshubin/mydome1'), 'http://192.168.5.254/zhangshubin/mydome1');
  assert.equal(displayProjectPath(null, 'zhangshubin/mydome1'), 'zhangshubin/mydome1');
  assert.equal(displayProjectPath(undefined, 'zhangshubin/mydome1'), 'zhangshubin/mydome1');
  assert.equal(displayProjectPath('http://h', ''), '');
});

test('轮询常量：3s 间隔 × 40 次 = 2 分钟观察窗（clone 自身有 600s 超时，不误标 failed）', () => {
  assert.equal(POLL_INTERVAL_MS, 3000);
  assert.equal(POLL_MAX_TRIES, 40);
});
