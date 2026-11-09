# Prompt history

All prompts sent to AI tools while building this project.

| File | Contents | How it was captured |
|------|----------|---------------------|
| `00_initial_prompt.md` | The first message sent to Claude Code (the full build spec), verbatim | Saved by Claude Code at the start of session 1, as the spec required |
| `claude_code_prompts.md` | Every later message to Claude Code, verbatim, with a UTC timestamp | Appended by Claude Code at the start of each turn (rule in `CLAUDE.md`); earlier entries are never edited |
| `01_planning_and_review_chat.md` | Every message I sent to a separate Claude chat used for planning and review, with my wording unchanged, plus an overview of how that chat was used | Collected by me from the conversation (no export function); pasted material is marked rather than repeated |
| `session_01_transcript.md` | Full transcript of the Claude Code build session | Exported with `/export` at the end of the session |

## Tools

| Tool | Used for | Prompts in this folder |
|------|----------|------------------------|
| Claude Code (VS Code extension, Claude Opus 5.5) | Building: all code, tests, data, docs and commits | `00_initial_prompt.md`, `claude_code_prompts.md`, `session_01_transcript.md` |
| Claude (desktop app chat, separate session) | Understanding the task, writing and revising the build spec against the YottaFlex guidelines, and reviewing each phase independently | `01_planning_and_review_chat.md` |

## Known limitations

- The timestamps in `claude_code_prompts.md` are taken when the assistant processed the message, so they can
  be a few seconds after the message was actually sent.
- The running log is written by the assistant itself. The `/export` transcript is the independent record.
- The planning chat has no export function, so its prompts were collected from the conversation afterwards.
  Pasted material (the assignment text, Claude Code's phase reports) is noted in brackets rather than repeated,
  and one local file path containing my username was replaced with `<Downloads>`. AI responses from that chat
  are not included (optional per the FAQ).