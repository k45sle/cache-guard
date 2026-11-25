# Evidence

What cache-guard v0.2 is built on. Measured 21 to 25 November 2026 with the
Antigravity CLI (`agy`) 1.5.17 on macOS. Numbers marked "logs" come from
`python3 tools/cache_audit.py` run on 25 November 2026 at 11:38; numbers marked "lab" come from
throwaway chats driven by `agy -p`.

## How a miss is measured

Every Antigravity chat keeps a log of its model calls at
`~/.gemini/antigravity*/brain/<chat>/.system_generated/logs/transcript_full.jsonl`.
Each `PLANNER_RESPONSE` line has the provider's own accounting:

- `input_tokens`: input billed at the full price
- `cache_read_tokens`: input served from the prompt cache

For two consecutive calls in one chat, everything the first call sent is a prefix
of the second, so

```text
reused = cur.cache_read / (prev.input + prev.cache_read)
lost   = max(0, prev.input + prev.cache_read - cur.cache_read)
```

A call is a miss when it reused less than half of a prefix of at least 4,096
tokens. A turn start is the first model call after a user message; Antigravity
rebuilds the prompt there. Chats started by the lab are left out of the log
numbers (`tools/lab-chats.txt`), because their waits and edits were staged.

## The logs

45 chats (8 lab chats left out), 879 model calls, 833 pairs of calls. They sent
115.4 million input tokens, 89.0% of them served from cache. 40 misses re-billed
7.7 million tokens, 6.7% of all input.

Misses by the first cause found:

| Cause | Misses | Re-billed |
| --- | --- | --- |
| Orchestrator hand-off, Planner to Executor | 11 | 2,683k |
| No visible cause | 11 | 2,032k |
| Orchestrator hand-back, Executor to Planner | 5 | 1,482k |
| A prompt-file edit by the same chat | 5 | 806k |
| Right after a v0.1 stop | 2 | 281k |
| Compaction | 1 | 186k |
| Only edits that never reach the prompt | 1 | 149k |
| Effort switch | 3 | 68k |
| Model switch | 1 | 11k |

21 November holds 86% of all input tokens (99 of 115 million) and every
Orchestrator chat.

## The Orchestrator

Antigravity's `Orchestrator` model plans with one model (the Planner) and hands
the plan to another (the Executor) through an `exit_plan_mode` tool call. In the
logs every plan ran on `gemini-pro-orchestrator-g1-pro` or
`gemini-pro-orchestrator-neon` and every execution on `gemini-flash-worker-agy`.

- All 12 `exit_plan_mode` calls in the logs were on 21 November. 11 changed the
  model and all 11 missed (mean reuse 1%). The 12th kept the model and reused 88%.
- At the next message the chat goes back to the Planner. 5 of those 7 hand-backs
  missed. The 2 that reused the cache (100% and 59%) came while the Planner's own
  cache was still warm.
- Chat d80761bd ran 6 plans in 53 minutes. A guard that stops these switches
  would have stopped it 10 times, and resending pays the same re-read.
- No Orchestrator call appears after 21 November.

agy 1.5.17's changelog: picking the same model and thinking effort for both
Planner and Executor "runs single-agent directly on that model", while different
models, or different efforts of one model, still run two-phase plan-and-execute.
v0.2 therefore lets the Orchestrator's own switches through and checks every
other model switch.

## Lab: what reaches the prompt

Chat 5a651b03 on Gemini 3.8 Flash (low effort), about 70k tokens. Each change was
made outside the chat between turns, then the chat got "Reply with one word: ok".
The v0.1 guard stopped the first send each time, so the resend is what was
measured.

| Test | Change | Next call reused |
| --- | --- | --- |
| E2 | Edit the workspace `AGENTS.md` | 0% (step 29) |
| E3 | Edit `.agents/hooks.json` | 97% (step 35) |
| E4 | Add `.agents/skills/demo/SKILL.md` | 0% (step 41) |
| E5 | Edit that skill's body only | 97% (step 47) |
| E6 | Switch effort low to medium | 0% (step 54) |
| E6c | Stay on medium | 97% (step 57) |
| E6d | Switch back to low, 28 s after the last low call | 98% (step 63) |

E7, chat f0966d6e (14k tokens): the chat edited a rule file with its own tool
calls in the middle of a turn. The rest of that turn kept reusing 86% (steps 10,
13 and 15, the same as before the edit). The next turn missed completely (step
18).

What follows from these runs:

- Rule text and the list of skills (with each skill's name and description) are
  part of the prompt. `hooks.json` and skill bodies are not.
- The effort level is part of the model id, and each model id has its own cache.
- Going back to a model you used moments ago reuses that model's cache.
- The prompt is rebuilt at a turn start, so a mid-turn change costs nothing until
  the next turn.

The logs agree. A turn-start call after this chat's own prompt edit missed 9 times
out of 13, and every skill edit that changed a name or description (6 of 6) was
followed by a miss.

## Lab and logs: how long an idle cache lasts

Each lab probe waits, sends "Reply with one word: ok", and sends it again at once
as a control. Every control reused 95% to 100%, so a miss after a wait came from
the wait and not from a lasting change.

| Model | Still cached | Gone | Source |
| --- | --- | --- | --- |
| Claude Sonnet 5.5 (low) | 4.1 min (85%) | 6.2 min (16%), 12.1 min (15%) | lab C2, C |
| Gemini 3.8 Flash (low) | 5.1 min (95%), 7.6 min (95%) | 10.1 min (0%), 16.2 min (0%), 32.1 min (0%) | lab G3, G2, G |
| Gemini 4 Neon (high) | 9.8 min (98%), 10.8 min (99%), 13.1 min (95%), 15.1 min (95%, 96%) | none clean (see below) | logs (chat 28509590), lab N1, N2 |
| gemini-pro-orchestrator-g1-pro | 3.5 min (100%, switch back), 5.1 min (87%) | 5.6 min, 5.9 min, 15.3 min, 20 min, 33 min (all 0%, switch back) | logs, chats d80761bd and a1ea899f |
| gemini-pro-orchestrator-neon | 12.4 min (100%, switch back) | none seen | logs, chat 82f227cc |

Points left out:

- Lab C at 6.7 min and lab G at 8.6 min: another chat edited `~/AGENTS.md` during
  both waits, so those misses have two possible causes.
- The only Neon miss after a long gap (16.6 min, chat 11f2c2b4 step 126) followed
  an edit to a prompt file in the same chat.

The Claude result matches Anthropic's documented 5-minute default cache lifetime,
which is a check on the lab method.

## Idle timings in v0.2

`DEFAULT_IDLE_TTL` in `hooks/guard.py`. The longest key found in the model id
wins and `default` covers the rest.

| Key | Seconds | Why |
| --- | --- | --- |
| `claude` | 300 | alive at 4.1 min, gone at 6.2 min, documented 5 min |
| `gemini` | 450 | Flash alive at 7.6 min, gone at 10.1 min |
| `gemini-pro` | 300 | orchestrator alive at 5.1 min, gone from 5.6 min |
| `gemini-4-neon` | 900 | alive at 15.1 min in two lab chats; no clean "gone" point |
| `orchestrator-neon` | 900 | warm after 12.4 min; assumed to follow Gemini 4 Neon |
| `gpt` | 300 | not measured |
| `default` | 300 | not measured |

Each timing sits at the longest wait the cache was seen to survive. A gap past it
may still find a warm cache, so an idle stop there can be needless. The values
can be changed in the config file.

## Lab: v0.2 end to end

On 25 November 2026 the v0.2 plugin ran from a lab workspace's
`.agents/plugins/cache-guard` under agy 1.5.17, driven by `agy -p`. `min_tokens`
was 5000 so that these 14k to 18k token chats count. A stop comes back as an
`ERROR` result with the guard's message, and every resend went through.

Chat e560c7e9 on Gemini 3.8 Flash:

| Step | Before the message | Guard | Next call reused |
| --- | --- | --- | --- |
| A1b | Nothing changed | Sent | 86% (step 5) |
| A2 | Effort switched from low to medium | Stopped | 0% (step 11) |
| A3 | Back to low, 11 s after its last call | Sent | 83% (step 14) |
| A4 | Workspace `AGENTS.md` edited outside the chat | Stopped | 0% (step 20) |
| A5 | `.agents/hooks.json` edited | Sent | 87% (step 23) |
| A6 | The chat edits `AGENTS.md` with `edit_file` | Asked first | 88% for the rest of the turn (step 32) |
| A6b | First turn after that approved edit | Sent | 0% (step 35) |
| A7 | 51 s idle, with the idle timing set to 40 s | Stopped | 88% (step 41) |

For A2 and A4 the next call is the resend, and its 0% makes both stops useful by
the backtest's measure. A6b is the cost the user accepts by approving the edit.
A7 tests only the mechanism: the cache was still warm at 51 s, which fits the
450 s default for Gemini.

Chat d36f448a on the Orchestrator (`gemini-pro-orchestrator-neon`) got two "plan
first, then create a file" tasks. Both times the Planner (`gemini-4-neon-low`)
called `exit_plan_mode` and the Executor wrote the file. The guard stopped
neither hand-off, nor the second task's return to the Planner. The first
hand-off reused 0%; the second hand-off and the return reused 87% and 88%,
because each role's cache was seconds old. Antigravity ran the Executor on
`gemini-4-hydrogen-high` although `--settings` named Gemini 3.8 Flash (Low). The
hook payloads carried the same model ids as the transcript.

## Backtest

Both guard versions replayed over the logs for chats of at least 60k tokens
(640 pairs of calls, 36 misses). A stop is useful when a miss
follows it. The v0.1 rules are loaded from git commit f2940cf.

| Rule | v0.2 stops | then a miss | v0.1 stops | then a miss |
| --- | --- | --- | --- | --- |
| Model switch | 0 | 0 | 19 | 16 |
| Ask before a prompt edit | 18 | 6 | | |
| Prompt changed outside the chat | 2 | 1 | | |
| Idle | 3 | 3 | 6 | 3 |
| v0.1 edit check | | | 133 | 17 |
| All rules | 22 | 9 | 154 | 33 |

v0.2 is ahead of 10 of the 36 misses (2.2 million tokens), v0.1 of 21 (5.2
million). The gap is the Orchestrator: 16 of the misses (4.2 million tokens) were
its own switches, which v0.1 stopped and v0.2 lets through. Leaving those out,
v0.2 is ahead of 6 of the other 20 misses (1.0 of 3.5 million tokens) and v0.1 of
5 (1.0 million), with 22 stops against 154.

18 of v0.1's 19 model-switch stops in these chats were Orchestrator switches. The
19th was a placeholder model name and the cache held. Every other real model or
effort switch in the logs happened in a chat under 60k tokens, and all 4 missed.
The model check in v0.2 rests on those and on the lab (E6 to E6d).

Why the edit check scores low:

- 6 of its 12 needless asks came on a chat's last turn, so no
  later turn existed to miss.
- The logs do not keep old file contents, so the backtest counts every whole-file
  write to a `SKILL.md` as a change to its name or description. The live guard
  compares the old and new text and lets through writes that keep them the same.

v0.1's shell check read `2>/dev/null` and `2>&1` as file writes. Of the 409 shell
commands in the logs that its pattern took for writes, 250 matched only on those
redirections.

## Things that did not hold up

- Messages that Antigravity adds to a chat (hook notices, task results) did not
  cause misses with any consistency: 1 in 14 in one count, 5 in 84 in another. No
  rule is based on them.
- One mid-turn edit to a skill's frontmatter missed straight away (chat 21d8bc08,
  step 110) instead of at the next turn. It is a single case. The guard asks
  before such edits anyway.

## Limits

- One person's logs, most of them from one day.
- One lab run per idle timing.
- The end-to-end lab ran each check once, in chats under 20k tokens.
- Antigravity can change how it builds prompts or caches in any release; re-run
  the lab after an update.
- 11 misses have no cause the logs can show.

## Reproduce

```bash
python3 tools/cache_audit.py                 # summary of every local transcript
python3 tools/cache_audit.py --list-misses   # each miss in chats of 60k tokens or more
python3 tools/cache_audit.py --json > audit.json
python3 tools/cache_audit.py --include-lab   # keep the lab chats
```
