# Warroom Agent Instructions

## Channel Coordination

When the `channel` MCP server is available and the user asks you to join the channel (or says "listen", "join channel", "channel-listen", or similar):

1. Call `channel_join(room="room1")`.
2. Enter a listening loop:
   - Call `channel_wait_new(room="room1")`
   - If `timed_out: true`, call `channel_wait_new` again immediately
   - If `ok: false`, report the error once and stop the loop
   - If a message arrives, check if it is addressed to you:
     - Messages containing `@codex` or `@all`, or messages that clearly need your input: process them as a normal task (read files, write code, review, etc.), then call `channel_post(room="room1", content=<your response>)`
     - Messages addressed to another agent (e.g. `@claude`) that don't need your input: skip, call `channel_wait_new` again
   - After posting your response, call `channel_wait_new` again
3. The loop exits **only** when the user interrupts you.

## Message Format

When posting to the channel:
- Start with a one-line summary of what you did or concluded
- Use markdown code blocks (```) for any code
- Use bullet points (`-`) for review findings or multiple points
- Keep it concise - no filler phrases
