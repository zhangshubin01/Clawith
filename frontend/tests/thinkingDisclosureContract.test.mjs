import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const agentDetail = readFileSync(
  new URL('../src/pages/agent-detail/AgentDetailPage.tsx', import.meta.url),
  'utf8',
);

test('analysis thinking items render a 360-char preview with a collapsible show-more', () => {
  // Regression guard: long reasoning stages render as a 360-char preview
  // plus a collapsible "show more" so the analysis trace stays compact,
  // and the full content is always reachable through the details block.
  assert.match(agentDetail, /itemPreview/);
  assert.match(agentDetail, /item\.content\.slice\(0, 360\)/);
  assert.match(agentDetail, /item\.content\.length > 360/);
  assert.match(agentDetail, /agent\.chat\.showMore/);
  const thinkingRow = agentDetail.match(
    /if \(item\.type === 'thinking'\) \{[\s\S]*?\{item\.content\}/,
  );
  assert.ok(thinkingRow, 'thinking items must render the full {item.content} inside the details block');
});

test('thought disclosure clamps long reasoning into a 260px scroll box', () => {
  // Long thought-trace bodies scroll within a 260px clamp instead of
  // stretching the chat column.
  const thoughtBody = agentDetail.match(
    /className="analysis-trace-body thought-trace-body"[\s\S]*?\{text\}\s*<\/div>/,
  );
  assert.ok(thoughtBody, 'thought-trace body must render {text}');
  assert.match(thoughtBody[0], /maxHeight:\s*'260px'/);
  assert.match(thoughtBody[0], /overflow:\s*'auto'/);
});
