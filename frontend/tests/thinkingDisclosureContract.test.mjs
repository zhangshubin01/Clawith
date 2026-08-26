import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);

test('analysis thinking items render the full reasoning text', () => {
  // Regression guard: thinking items used a 360-char preview plus a
  // collapsible "show more"; long reasoning stages (up to tens of KB per
  // model step) therefore always looked truncated in the analysis trace.
  // The full content must render inline, with no preview slicing.
  assert.doesNotMatch(agentDetail, /itemPreview/);
  assert.doesNotMatch(agentDetail, /item\.content\.slice\(0, 360\)/);
  assert.doesNotMatch(agentDetail, /item\.content\.length > 360/);
  assert.doesNotMatch(agentDetail, /agent\.chat\.showMore/);
  const thinkingRow = agentDetail.match(
    /if \(item\.type === 'thinking'\) \{[\s\S]*?\{item\.content\}/,
  );
  assert.ok(thinkingRow, 'thinking items must render {item.content}');
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
