# Prompt history

All prompts sent to AI tools while building this project.

| File | Contents | How it was captured |
|------|----------|---------------------|
| `00_initial_prompt.md` | The first message sent to Claude Code (the full build spec), verbatim | Saved by Claude Code at the start of session 1, as the spec required |
| `claude_code_prompts.md` | Every later message to Claude Code, verbatim, with a UTC timestamp | Appended by Claude Code at the start of each turn (rule in `CLAUDE.md`); earlier entries are never edited |

## Tools

| Tool | Prompts in this folder |
|------|------------------------|
| Claude Code (VS Code extension, Claude Opus 5.5) | `00_initial_prompt.md`, `claude_code_prompts.md` |

## Known limitations

- The timestamps in `claude_code_prompts.md` are taken when the assistant processed the message, so they can
  be a few seconds after the message was actually sent.
- The running log is written by the assistant itself. The `/export` transcripts (added by the candidate) are
  the independent record.

> **TODO (candidate):**
> - add the prompts from the planning chat used before Claude Code (and name that tool in the table above);
> - add the `/export` transcript of each Claude Code session (e.g. `session_01_transcript.md`);
> - note any gap, for example a tool that cannot export its history.
