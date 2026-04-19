# Agent Primary Session & Unread Test Cases

This document covers the first-phase behavior for:

- primary platform sessions (`is_primary`)
- proactive agent-to-user platform messaging
- agent list unread badges
- session list unread badges
- temporary user-created sessions
- migration and upgrade safety

Environment assumptions:

- backend branch: `release`
- tenant: any tenant with at least one web user and one agent
- current platform channel name remains `web`

## 1. Migration and Backfill

### TC-01: Existing platform sessions elect one primary session

Preconditions:

- One user already has multiple `web` chat sessions with the same agent.
- At least one session contains user-authored messages.

Steps:

1. Upgrade database with the new migration.
2. Inspect `chat_sessions` for that `agent_id + user_id + source_channel='web'`.

Expected:

- Exactly one row has `is_primary = true`.
- The elected row prefers a session that already contains user messages.
- Other sessions remain `is_primary = false`.

### TC-02: Existing sessions do not become unread after upgrade

Preconditions:

- Database contains old sessions created before this feature.

Steps:

1. Run migration.
2. Open agent list and session list as the session owner.

Expected:

- Existing sessions do not all suddenly show unread badges.
- `last_read_at_by_user` is backfilled so old messages are treated as already read.

## 2. Primary Session Routing

### TC-03: Agent proactive platform message reuses primary session

Preconditions:

- A user already has a primary `web` session with an agent.

Steps:

1. Trigger a proactive platform message from the agent to that user.
2. Inspect created `chat_messages`.
3. Refresh the agent detail chat session list.

Expected:

- No new `web` session is created.
- The new assistant message is appended to the primary session.
- The primary session appears in `My Sessions`.

### TC-04: Agent proactive platform message lazily creates primary session

Preconditions:

- A user has never talked to the agent on-platform.

Steps:

1. Trigger a proactive platform message from the agent to that user.
2. Inspect `chat_sessions`.

Expected:

- A new `web` session is created.
- The session has `is_primary = true`.
- The proactive message lands in that session.

### TC-05: User-created temporary session is not marked primary

Preconditions:

- User can access an agent chat page.

Steps:

1. Click "New Session".
2. Inspect the newly created session.

Expected:

- The new session has `is_primary = false`.
- It is treated as a temporary side-topic session.

## 3. WebSocket Session Resolution

### TC-06: Opening chat without session_id lands in primary session

Preconditions:

- User already has a primary platform session with an agent.

Steps:

1. Open the agent chat page without manually selecting a session.
2. Watch the websocket `connected` payload.

Expected:

- The returned `session_id` is the primary platform session.

### TC-07: Opening chat with explicit session_id still respects chosen temporary session

Preconditions:

- User has one primary session and one temporary session.

Steps:

1. Click the temporary session in the session list.
2. Confirm the frontend reconnects to that session.

Expected:

- Chat history loads from the chosen temporary session.
- The system does not forcibly switch back to the primary session.

## 4. Unread Badges

### TC-08: Proactive agent message creates unread badge in agent list

Preconditions:

- User is logged in and has access to an agent.
- No unread items currently exist for that agent.

Steps:

1. Agent sends a proactive platform message to the user.
2. Do not open the session yet.
3. Observe the left sidebar agent list.

Expected:

- The corresponding agent shows an unread badge.
- Badge count increases from zero.

### TC-09: Proactive agent message creates unread badge in session list

Preconditions:

- Same as TC-08.

Steps:

1. Navigate to the agent detail page.
2. Open `My Sessions`.

Expected:

- The target session shows an unread badge.
- Assistant-only sessions are visible even if the user has not replied yet.

### TC-10: Opening the session clears unread badges

Preconditions:

- An unread session exists for the current user.

Steps:

1. Click the unread session.
2. Wait for messages to load.
3. Observe session list and agent list.

Expected:

- The session unread badge becomes zero immediately.
- The agent sidebar unread badge also clears or decreases accordingly.
- Backend updates `last_read_at_by_user`.

### TC-11: Reading one session does not clear unread in another session

Preconditions:

- Two sessions under the same agent both contain unread assistant messages.

Steps:

1. Open only one of the sessions.
2. Re-check the list.

Expected:

- The opened session clears.
- The other session still shows unread.
- Agent-level unread count reflects the remaining unread messages.

## 5. My Sessions vs Other Users

### TC-12: Agent-initiated session with only assistant messages appears in My Sessions

Preconditions:

- Agent has proactively contacted the current user on-platform.
- The user has not replied yet.

Steps:

1. Open the agent detail page.
2. Inspect `My Sessions`.

Expected:

- The proactive session is listed in `My Sessions`.
- It does not disappear just because `user_msg_count == 0`.

### TC-13: Sessions belonging to other humans still stay under Other users

Preconditions:

- Current viewer is an admin who can inspect all sessions.

Steps:

1. Open `Other users`.
2. Compare with `My Sessions`.

Expected:

- Sessions owned by the current user stay in `My Sessions`.
- Sessions owned by different users stay in `Other users`.

## 6. Multi-Account Behavior

### TC-14: Channel sessions already attributed to current user remain separate but count as mine

Preconditions:

- Current user has a platform account and a linked channel identity.
- There is at least one channel session already tied to the same `user_id`.

Steps:

1. Open the agent detail page.
2. Inspect `My Sessions` and `Other users`.

Expected:

- The channel session is shown in `My Sessions`.
- It is not physically merged with the platform session.
- It does not appear in `Other users`.

## 7. Upgrade Safety

### TC-15: Unique primary constraint prevents duplicate primary sessions

Preconditions:

- Database upgraded with the new migration.

Steps:

1. Attempt to mark two `web` sessions for the same `agent_id + user_id` as `is_primary=true`.

Expected:

- Database rejects the second conflicting write.
- At most one primary platform session exists per `agent + user`.

### TC-16: Non-web sessions are unaffected by primary constraint

Preconditions:

- Same agent/user pair also has channel sessions (for example `feishu`).

Steps:

1. Inspect non-web sessions after migration.

Expected:

- Non-web sessions remain valid.
- The primary uniqueness rule only affects `source_channel='web' AND is_group=false`.

## 8. Manual Regression Checks

### TC-17: Existing features still work

Steps:

1. Create a new manual session.
2. Rename a session.
3. Delete a session.
4. Send a normal chat message through websocket.

Expected:

- Existing session management behavior still works.
- No regression in normal chat send/receive flow.

### TC-18: Trigger notifications still reach the correct live user

Steps:

1. Trigger an agent-side proactive message while the target user is online.
2. Confirm a websocket notification appears only for that user's live sessions.

Expected:

- Only the intended user's connections receive the proactive notification.
- Other users connected to the same agent do not receive it.
