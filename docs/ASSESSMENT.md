# cache-guard: assessment

Written on 25 November 2026 for the owner of this repo. The numbers come from
`tools/cache_audit.py` run on the owner's own Antigravity logs and from live
tests. The detail behind every number is in [EVIDENCE.md](EVIDENCE.md).

## Short answer

The idea is sound and worth keeping. A long Antigravity chat is cheap only while
the provider's prompt cache holds, and in your logs lost caches re-billed
7.7 million tokens, about 7% of all input.

Version 0.1 was built from reasoning about caching, not from measuring it, and
several of its guesses were wrong. Replayed on your logs, it would have stopped
you 154 times, and 121 of those stops were followed by no cache loss at all. Its
tests checked that the code did what it was written to do, not that Antigravity
behaves that way.

Version 0.2 is rebuilt on what the logs and live tests show. On the same logs it
stops you 22 times, 13 of them needlessly. Leaving aside the Orchestrator's
switches (below), it catches 1.0 million of the 3.5 million re-billed tokens,
about what v0.1 caught with 154 stops.

The two largest losses are out of a hook's reach. The Orchestrator's own model
switches cost 4.2 million tokens in 16 misses: when its Planner and Executor are
different models, each plan goes to the Executor and the next message goes back
to the Planner, and both steps re-read the whole chat. And 11 misses with no
visible cause cost 2.0 million. All the Orchestrator losses were on 21 November.
Since then you have worked on single models, mostly Gemini 4 Neon (High), so they
have not come back. If you use the Orchestrator again, pick the same model and effort for
Planner and Executor: agy 1.5.17 then runs it as one agent.

## Thinking it through

These are the five steps you asked for.

### 1. Your question in my words

You built a plugin that warns before Antigravity loses its prompt cache. You want
an honest view of whether it is good and on track. Then you want it brought up to
the standard a specialist would hold it to: work out what that person would do
that you did not ask for, start from raw data instead of summaries, use their
methods, prove the result with something other than the two of us, and fix
whatever needs fixing.

### 2. Three readings, and what each one assumes

1. Polish what exists: review the code, add tests and docs. This assumes v0.1's
   rules match how Antigravity really caches.
2. Check the rules against reality and rebuild on the results. This assumes the
   logs record trustworthy token counts and that live tests can isolate one cause
   at a time.
3. Question the product: a setting, a habit or a report might save more than a
   guard. This assumes the goal is saving tokens, not owning this plugin.

I took the second reading because the evidence broke the first one's assumption
in several places (listed under "What changed"). The third reading turned out
partly right, so its answer is folded in: the largest loss comes from a setting,
which the README names, and the audit tool ships in the repo so you can judge the
guard on your own data.

### 3. The four questions

#### What would a top professional do that you did not ask for?

- Measure the problem before writing rules: how many tokens are lost, and to
  which cause.
- Treat the guard as a detector that can be wrong in both directions, and score
  it on past data: how many stops were needless and how much loss it would have
  caught. Keep that score in the repo so every later change is checked against it.
- Prove each cause with a controlled test: change one thing in a throwaway chat,
  send a message, read the token counts.
- Write down what is measured and what is assumed, with the Antigravity version,
  because caching behaviour can change in any release.
- Fix a cause where it lives. For the Orchestrator's hand-offs a setting beats a
  warning.

#### What is the rawest material?

- Antigravity's own record of every model call, in
  `~/.gemini/antigravity*/brain/<chat>/.system_generated/logs/transcript_full.jsonl`.
  Each call lists `input_tokens` (billed in full) and `cache_read_tokens` (served
  from cache), which are the provider's own billing numbers. v0.1 started from
  general beliefs about what breaks a cache.
- The hook payloads Antigravity really sends, recorded during the lab tests, in
  place of guessed field names.
- Live chats run through the `agy` command line, one change at a time.

#### Which methods and tools?

- A log-mining script, `tools/cache_audit.py`. It pairs each model call with the
  one before it, works out how much of the earlier context came back from cache,
  calls anything under half a miss, and tags a likely cause from what happened in
  between: a model or effort switch, an Orchestrator hand-off or hand-back, a
  prompt-file edit by this chat or another one, an idle gap, or a compaction.
- A backtest that replays both guard versions over the same logs and scores them.
- A lab of scripted `agy -p … --conversation …` runs with timed waits, edits and
  model switches, each followed by a resend as a control.
- In the guard itself: SHA-256 fingerprints of only the parts of each file that
  reach the prompt, Python's `shlex` to read what a shell command writes, and a
  dry run of each file edit to see whether it changes those parts.
- Python's `unittest` with real payloads (personal details removed), GitHub
  Actions on Linux and macOS with Python 3.9 and 3.13, `ruff` and `shellcheck`.

#### What, other than you or me, shows the result is right?

- The provider's token counts in your logs. The backtest scores the guard against
  the bill, not against our opinion.
- Antigravity itself: the installed plugin stopping a real chat in the lab and
  letting the resend through.
- GitHub's machines running the tests on two systems and two Python versions.
- Anthropic's documentation gives prompt caches a 5-minute default lifetime, and
  the lab measured the same for Claude. That match is a check on the lab method.
- Your future logs. Running the audit again in a few weeks shows whether misses
  went down, and that is the test that counts.

### 4. The strongest case against

A guard is the wrong tool. Of the 7.7 million lost tokens, 4.2 million came from
the Orchestrator's own switches, which only a setting prevents, and 2.0 million
had no visible cause, which no rule can stop. An idle stop arrives after the cache
is already gone, so it saves nothing unless you start a new chat. Every stop costs
you a resend or an approval, and even in v0.2, 13 of 22 stops are needless. People
switch off noisy guards, and a switched-off guard protects nothing. The evidence
is also thin: one person's logs, 86% of them from a single day (21 November), and
one lab run per idle timing.

This changes the answer in four ways:

1. Needless stops were v0.1's main fault, so v0.2 cuts them from 121 to 13
   while catching about as much of the loss a stop can prevent.
2. The guard lets the Orchestrator's own switches through. Stopping them saves
   nothing and would interrupt every plan; the README names the setting that
   ends them.
3. The audit tool is in the repo, so you can measure the guard on your own logs
   and switch it off if it does not pay for itself.
4. Each rule and timing is written down with its evidence and the Antigravity
   version, and the timings can be changed in the config file.

It does not kill the answer. Outside the Orchestrator, a model or effort switch
lost the cache all 4 times in your logs and in every lab test, and at that moment
switching back is free. An edit stop is right less often in the backtest
(6 of 18), partly because the logs lack old file contents, but it is the only
point where you can still choose to make the change in a new chat instead.

### 5. Final answer

Keep the guard, rebuilt on evidence, and judge it by the audit rather than by
trust. If you use the Orchestrator again, give Planner and Executor the same
model and effort. Re-run the audit in two to four weeks and switch the guard off
if its stops are mostly needless.

## What changed in v0.2

Each change rests on a measurement in [EVIDENCE.md](EVIDENCE.md).

- Edits to `hooks.json` or to a skill's body no longer stop you. Lab tests showed
  that neither reaches the prompt.
- The prompt is fingerprinted by content, so saving a file without changing what
  reaches the prompt does not count.
- Prompt changes are checked at the start of a turn, when Antigravity rebuilds the
  prompt. The lab showed a rule edited mid-turn costs nothing until the next turn.
- Switching back to a model used within its cache lifetime is not stopped,
  because its cache is still warm.
- The Orchestrator's own switches are not stopped either. All 11 hand-offs and 5
  of the 7 hand-backs in your logs missed, but a stop cannot prevent that. An
  earlier v0.2 draft stopped them and would have interrupted your busiest
  Orchestrator chat 10 times in under an hour.
- Idle timing depends on the model: 5 minutes for Claude, 7.5 for Gemini Flash
  and 15 for Gemini 4 Neon. v0.1 used 5 minutes for every model and also
  checked in the middle of turns.
- The shell check no longer reads `2>/dev/null` or `2>&1` as a file write (250
  of the 409 commands v0.1's pattern took for writes matched only on those), and
  a script that only mentions files in a plugin folder no longer counts as
  editing the prompt.
- Messages are in plain words and say what helps.
- New: `tools/cache_audit.py`, 111 tests, CI, this file and the evidence file.

## What is still open

- 11 misses (2.0 million tokens) have no cause the logs
  can show. They may be cache evictions on the provider's side, which the plugin
  cannot see.
- Gemini 4 Neon has only a lower bound: its cache was still there after 15
  minutes in the lab (96% reused), and no wait has yet found where it ends.
- Each idle timing comes from one lab run. Run the probes again after an
  Antigravity update.
- The backtest undercounts the edit check. Old file contents are not in the logs,
  so there every whole-file skill write counts as a prompt change, while the live
  guard compares contents and skips writes that keep the skill's name and
  description.
- Run `python3 tools/cache_audit.py` again in two to four weeks.

## Your prompt, rewritten

```text
cache-guard (github.com/k45sle/cache-guard) is an Antigravity plugin that pauses
a long chat before something makes it lose its prompt cache. Judge it by
measurement, then fix it. You have full authority to change or replace anything.

1. Measure the loss. From every local Antigravity transcript
   (~/.gemini/antigravity*/brain/*/.system_generated/logs/transcript_full.jsonl),
   pair each model call with the one before it and work out how much of the
   earlier context came back from cache (cache_read_tokens) and how much was
   billed again (input_tokens). Count every miss (under half reused, on a prefix
   of 4k tokens or more), tag its likely cause (model or effort switch,
   Orchestrator hand-off, prompt-file edit by this chat or another, idle gap,
   compaction, unexplained) and total the re-billed tokens per cause.
2. Prove the causes. In throwaway chats driven by the agy CLI, change one thing
   at a time (rule edit, new skill, skill body edit, hooks.json edit, effort
   switch, switch back to a recent model, rule edit mid-turn, idle gaps of
   several lengths for each model) and read the next call's cache share, with a
   resend as a control. Record the agy version.
3. Score the guard. Replay the current rules and any redesign over the same
   logs: how many stops, how many with no miss after, and what share of the
   re-billed tokens each would have caught. Keep this backtest in the repo.
4. Rebuild only on what steps 1-3 show, and have every rule cite its evidence.
   Fewer correct stops beat many.
5. Check the result outside the AI: the provider's token counts, the installed
   plugin in a live agy chat, CI on Linux and macOS with the oldest and newest
   supported Python, and lint. Say what is measured, what is assumed and what
   result would change the conclusion.
6. Say which losses a hook cannot prevent and what would (a setting, a habit),
   and how I can re-run the audit in a few weeks to see whether misses went down.

Before answering, think it through. Restate the task in your own words. Give
three readings of it and the assumption each rests on, without favouring your
own. Answer four questions: what a top professional would do that I did not ask
for, what raw material they would start from, which exact methods and tools they
would use, and what outside the AI would show the result is right. Make the
strongest real case against your answer, and change the answer if it holds. The
final answer must agree with all of this.

Report in plain words: what changed, the before-and-after numbers, and what is
still unproven.
```
