# HEARTBEAT

When this file is read during a heartbeat, you are performing a **periodic awareness check**.

## Phase 1: Review Context & Discover Interest Points

1. **Read `memory/reflections.md`** — Recall your recent hypotheses, open questions, and ongoing threads of inquiry. Build on them, don't start from scratch.
2. Review your **recent conversations** and your **role/responsibilities**.
   Identify topics or questions that:
   - Are directly relevant to your role and current work
   - Were mentioned by users but not fully explored at the time
   - Represent emerging trends or changes in your professional domain
   - Could improve your ability to serve your users

If no genuine, informative topics emerge from recent context, **skip exploration** and go directly to Phase 3.
Do NOT search for generic or obvious topics just to fill time. Quality over quantity.

## Phase 2: Targeted Exploration (Conditional)

Only if you identified genuine interest points in Phase 1:

1. Use `web_search` to investigate (maximum 5 searches per heartbeat)
2. Keep searches **tightly scoped** to your role and recent work topics
3. `memory/curiosity_journal.md` is a **raw exploration log**, not durable memory — durable findings are finalized into reflections in Phase 3. For each discovery:
   - Record it using `write_file` to `memory/curiosity_journal.md`
   - Include the **source URL** and a brief note on **why it matters to your work**
   - Rate its relevance (high/medium/low) to your current responsibilities

Format for curiosity_journal.md entries:
```
### [Date] - [Topic]
- **Finding**: [What you learned]
- **Source**: [URL]
- **Relevance**: [high/medium/low] — [Why it matters to your work]
- **Follow-up**: [Optional: questions this raises for next time]
```

## Phase 3: Reflect & Wrap Up

1. **Update `memory/reflections.md`** if this cycle produced anything worth recording:
   - Move resolved open questions into Insights & Discoveries (with sources).
   - Record new hypotheses generated this cycle (even unverified ones).
   - Log verified findings with evidence and source URLs.
   If nothing new surfaced, skip this update and continue to step 2.

2. **Converge your exploration log — always, even when nothing new surfaced.**
   Before ending the heartbeat, refresh the "Next Cycle Seeds" section of `memory/reflections.md`:
   - Read the **Follow-up** entries and **Active Questions** in `memory/curiosity_journal.md`, preferring entries not yet marked `→promoted`.
   - Promote the ones genuinely worth pursuing (at most 3) into Next Cycle Seeds.
   - Mark each promoted journal entry with `→promoted YYYY-MM-DD` at the end of its line; do not delete journal entries.
   - If no journal entry is worth promoting, leave Next Cycle Seeds unchanged and say so in your summary.
   - Then list: what to explore next if time allows, which hypothesis is most worth testing next, and any user tasks that need proactive follow-up.

3. **Reply** with `HEARTBEAT_OK` if no exploration was warranted and nothing needed attention; otherwise briefly summarize what you explored and why.
