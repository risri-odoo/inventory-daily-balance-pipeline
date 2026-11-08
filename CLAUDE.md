# Working rules for Claude Code

## Prompt log (mandatory)

At the start of every turn, append the user's message verbatim, with an ISO timestamp, to prompt_history/claude_code_prompts.md. Do not edit earlier entries.

## How we work

- The full spec is the initial prompt in `prompt_history/00_initial_prompt.md`; the assignment is in `docs/assignment.md`.
- Work in phases. At the end of each phase: run the tests, explain what was built (5–10 plain-English bullets),
  list what the candidate should personally check, commit, push, then stop and wait for "continue".
- Keep the code simple enough for a junior engineer to explain line by line: small typed functions,
  docstrings, plain Python and readable SQL, no metaprogramming, no unnecessary abstractions.
- Pipeline code uses the Python standard library only (sqlite3 for storage). pytest is the only dependency.
- Use pathlib; everything must run on Windows and Linux.
- Conventional commit messages (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), one per meaningful step,
  keeping the Co-Authored-By trailer. Never rewrite history, force-push or squash.
- Run `git status` before every commit; never stage databases, `.venv`, `output/` or generated data.
- Push with `git push origin main` only after tests pass. If a push fails, stop and tell the candidate.
- Never fake evidence: every number in the docs must come from a command that was actually run.
- Never write claims about the candidate. In `AI_USAGE.md`, leave `TODO (candidate):` sections for them.
- No secrets, absolute local paths, usernames or emails in committed files, logs or evidence.
  `source_file` is stored relative to the input root.
- Ask before deviating from the spec. Record every assumption and deviation in
  `docs/assumptions_and_deviations.md`.
