# Warroom Agent Instructions

## Channel Coordination

When the `channel` MCP server is available:

1. **Immediately** call `channel_join(room="room1")` when you start.
2. Enter a listening loop:
   - Call `channel_wait_new(room="room1")`
   - If `timed_out: true`, call `channel_wait_new` again immediately
   - If a message arrives, handle it as a normal task (read files, write code, review, etc.)
   - When done, call `channel_post(room="room1", content=<your response>)`
   - Then call `channel_wait_new` again
3. The loop exits **only** when the user interrupts you.

Do not ask for confirmation. Start the loop as soon as you see these instructions.

## Message Format

When posting to the channel:
- Keep messages concise and structured
- Use markdown formatting for code blocks
- Start with a one-line summary, then details
- When reviewing code, use bullet points for findings
