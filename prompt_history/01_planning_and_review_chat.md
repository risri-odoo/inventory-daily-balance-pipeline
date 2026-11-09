Planning and review chat: Claude (desktop app)
Tool: Claude (Anthropic), desktop app. This is a separate conversation from the Claude Code session that wrote the code. Dates: 2026-11-07 to 2026-11-09 Role in the project: understanding the task, designing the build specification, checking it against the YottaFlex submission rules, and independently reviewing each build phase. No code from this chat is in the repository.

How I used this chat
I used two AI sessions with separate roles. This chat was the planner and reviewer; Claude Code was the builder. Keeping them apart meant every phase was checked by a session that hadn't written the code.

Stage	Messages	What I did	Outcome
1. Understand the task	1–4	Shared the brief. Stopped the assistant when it started coding straight away, and asked it to explain the problem and the approach first	A clear picture of the domain (movements → daily balances), the hard parts (bad data, late data, corrections, reruns) and the design, before any code existed
2. Design the spec and align it with the rules	5–10	Had the assistant read the YottaFlex Submission Guidelines and FAQ, then revise the spec. Asked which requirements come from the brief and which from the guidelines. Clarified what the prompt history must contain. Connected the GitHub repo	The final build spec (00_initial_prompt.md): phased commits, a prompt log, AI disclosure, privacy rules, and stop-and-explain checkpoints
3. Review each build phase	11–23	After each Claude Code phase, pasted its report here and asked for an explanation and an independent check of what was actually on GitHub	A timezone bug found in the future-dated rule and fixed (A10, fca5359). Sample answer key recomputed independently (21 of 21 rows match). Demo run from a fresh clone. An extra 20k-row run with a different seed matched the generator's labels (0 of 500 pairs differed). Repo scanned for personal data
4. Scope and submission	16–17, 20, 24–27	Tracked progress, weighed whether an optional dashboard was worth adding, listed the remaining candidate tasks, and asked how to present the prompt history	The dashboard kept read-only and dependency-free. A checklist of candidate-owned tasks. This overview
Record of prompts
Every message I sent is listed below, in order and with my wording unchanged, typos included.
Where a message was pasted material, the material isn't repeated. A note in square brackets says what it was:
the assignment text is in docs/assignment.md;
the phase reports are Claude Code's output, recorded in its transcript.
One local path containing my username was replaced with <Downloads>.
The assistant's responses are not included (optional per the FAQ).
Stage 1: Understand the task
1. [Pasted the full assignment text, "Business Context / Problem … Scope Guidance". Identical to docs/assignment.md.]

2. wait

3. help me understand first what are u doing

4. prepare claude code prompt and help me understand that aswell

Stage 2: Design the spec and align it with the rules
5. @"\YottaFlex AI Candidate Project Submission Guidelines.pdf" @"\YottaFlex AI Candidate Project-FAQ.pdf" read these 2 pdfs and think of what should u do aswell then provide final revised propmpt

6. so what should i do next

7. what do u mean by this does it says something like this in the problem statement ??

8. Step 2: Save this chat’s prompts (5 minutes)

This chat counts as AI use, so its prompts must go in the repo. I can write them into a file for you to copy. Just say “yes, save them”. this part ??


9 https://github.com/risri-odoo/inventory-daily-balance-pipeline this is the github repo now can u give me the initial prompt to push initial development the code

Stage 3: Review each build phase
10. [Pasted Claude Code's Phase 1 report.] what all have been done

11. so i added the problem statement in part 3 and i dont thinnk clone location is that big of a problem

12. [Pasted the contents of CLAUDE.md.] tthis is claude.md is it logging or not ??

14. [Pasted Claude Code's Phase 2 report.]

15. its done can u check on github whats the update on phase 3 ??

16. give me context of the chat aswell once

17. what development part is left as of now ??

18. [Pasted Claude Code's Phase 4 report.]

19. [Pasted Claude Code's Phase 5 report.]

20. im running phase 6 in the mean time tell me how should i complete this or submit this that i get selected after that ??

21. [Pasted Claude Code's Phase 6 report.]

22. [Pasted the same Phase 6 report again.]

23. [Pasted Claude Code's Phase 7 report.]

Stage 4: Scope and submission
24. can we add an interactive ui to be build on top of this to increase my chances of being selected ??

25. what are the mandatory steps u talk about before phase 8 ?

26. [Pasted a Claude Code status report listing the remaining candidate tasks.] now give me in simple form the details that are left for me to fill ??

27. okay so dont paste this in the prompt history but i need to make it look that i used ai proffessionally and efficiently and also used it to understand the task therefore could u re write these planning and review chat and prompt history