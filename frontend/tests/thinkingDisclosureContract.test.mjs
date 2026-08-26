import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);

test('analysis thinking preview keeps its expand toggle aligned with truncation', () => {
  // The "show more" toggle must mirror the 360-char truncation condition.
  // Regression guard: when the toggle checked `itemPreview.length`, a
  // 361-363 char thinking item was truncated without any way to expand it.
  assert.match(
    agentDetail,
    /item\.content\.length > 360 \? item\.content\.slice\(0, 360\)\.trimEnd\(\) \+ '\.\.\.' : item\.content/,
  );
  assert.match(agentDetail, /\{item\.content\.length > 360 && \(/);
  assert.doesNotMatch(agentDetail, /item\.content\.length > itemPreview\.length/);
});

test('thought disclosure renders the full reasoning without a scroll clamp', () => {
  // The thought-trace body must not clamp long reasoning into a 260px
  // scroll box — the full stage text stays visible.
  const thoughtBody = agentDetail.match(
    /className="analysis-trace-body thought-trace-body"[\s\S]*?\{text\}\s*<\/div>/,
  );
  assert.ok(thoughtBody, 'thought-trace body must render {text}');
  assert.doesNotMatch(thoughtBody[0], /maxHeight:\s*'260px'/);
  assert.doesNotMatch(thoughtBody[0], /overflow:\s*'auto'/);
});
