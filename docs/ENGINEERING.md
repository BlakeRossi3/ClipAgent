# Engineering rules: fast changes that don't collide

ClipAgent is changed every day by three authors: Blake, the build chat, and Cecil editing himself.
Most of the painful bugs so far came from the same few shapes, not from hard algorithms:

* two systems doing one job slightly differently (two retry engines; the deferred queue turning a memory
  pass into a full-power prompt run; fifteen modules each deciding "may I run now?" their own way);
* errors thrown away (`except Exception: pass`, a canned "something went wrong") so nobody could say
  why a task "failed after one second";
* state written in the wrong order or without a lock (a record written *after* the work that updates it
  had already finished; `kv_get` + `kv_set` of a list, losing one writer's entry);
* work marked `running` with nobody left to finish it (a restart), which then blocked everything else.

The rules below exist to stop exactly those. The ones marked **[guarded]** are checked by
`tests/test_guardrails.py` on every test run. Code that predates a rule is listed in
`tests/guardrails_baseline.json`. That list may only shrink: after fixing an entry, run
`.venv/bin/python tests/test_guardrails.py --update`.

## Before you add anything: find the owner

Each job has exactly one owner module. Extend the owner; never build a second one beside it.

| Job | Owner | Use |
|---|---|---|
| May background work start now? | `assistant/usage.py` | `usage.wait_reason()` ("" = go, else why) |
| Long work in the background (a worker session) | `assistant/tasks.py` | `tasks.start(...)` |
| Work for later / after the plan resets | `assistant/usage.py` (deferred queue) | `usage.defer(...)`, `schedule.schedule(...)` |
| Retrying anything that failed | `tasks.retry_due` / `usage.retry_later` | never a private retry loop |
| Showing work on the rail | `assistant/tasks.py` | `tasks.track / track_step / track_done` |
| Telling Blake something | `tools.send_to_owner` / `notify` | never Twilio directly |
| What is on his screen | `assistant/screen.py` | `screen.read()`, `screen.clear()` |
| Staff hand-offs | `assistant/staff.py` | Agent tool + `StaffTracker` |
| Shared text cleaning | `assistant/textutil.py` | `one_line`, `paragraphs` |
| SQL | `db.py` / `assistant/store.py` | add a typed helper with a test |

If you cannot find an owner, write the new module, then add a row here in the same change.

## The rules

1. **One gate. [guarded]** Code that decides whether to *start* background work asks
   `usage.wait_reason()`. Only the modules that *report* usage or compute retry *timing* may read
   `constrained()` / `backoff_until()` directly (the allowlist in the guard test).
2. **One runner. [guarded]** Background work goes through `tasks.start` (a worker) or the deferred queue,
   so it is visible on the rail, retried and counted against the parallel limits. No new
   `threading.Thread` outside the runner modules.
3. **Never swallow an error. [guarded]** A broad `except` must log the error (at least
   `log.debug("...: %r", exc)`), record it, re-raise, or use it. A failure that reaches Blake or Cecil
   carries its real cause (`runtime.error_text`, `last_error`), never "unknown" or a canned apology.
4. **Atomic state. [guarded]** Never `kv_get` then `kv_set` the same key. Use `db.kv_update(key, fn)` or
   `db.kv_append(key, item, keep=N)`. Merge JSON meta with `store.merge_deferred_meta` /
   `merge_backlog_meta`. Two processes write the same database: the web server and the tool process of
   every text or dashboard turn.
5. **Bookkeeping before work. [guarded]** Write the record that describes a run *before* you start the
   run. A run can finish before `tasks.start()` returns, and a later write overwrites its outcome.
6. **Deferral reruns the same code.** Waiting for usage must never change *how* the work is done.
   Prefer "keep your place and try on the next tick" (`wait_reason()` + leave the cursor). Only scheduler
   jobs in `usage.JOB_FUNCS` may be queued with `should_defer`, which refuses anything else.
7. **Nothing is stuck in `running`.** Whatever marks a row or record `running` must also say who owns it
   (a live thread, a task id). It needs a sweeper that hands orphans back for a retry, not a failure:
   `usage.sweep_orphans`, `tasks._requeue_orphans`, `store.close_stale_runs`.
8. **Parallel by default.** Limits are counts (`MAX_PARALLEL`), never "one at a time, and only when
   nothing else runs". Unrelated work must not wait for each other.
9. **Retries go back through their own path.** A deferred item reruns in the deferred queue and a text
   reruns as his request. It keeps its rail row and attempt count, and the rail counts down to the next
   try.
10. **Tests never touch the real world.** No network, no real repo
    (`selfedit.set_app_dir(None)` is banned in tests **[guarded]**), no real browser, and threads
    inline (`usage.set_spawner`). A test that starts a background thread waits for it before it ends.

## Changing quickly without collisions

* Small changes, one concern each, each with its test. Run the module's tests while you work and the
  full suite before you push. `tests/test_guardrails.py` is fast; run it first.
* Grep before you write: `grep -rn "def <likely name>" assistant/ *.py`. If something close exists,
  extend it.
* Removing is part of the change. Delete the old path when a new one replaces it (e.g.
  `schedule.sweep_stale` was removed when `usage.sweep_orphans` took over). Two live paths for one job
  is how this kind of bug starts.
* Document where the next person will look: the module docstring for *how*, CLAUDE.md for the
  contract, and this file for the rules.
* Cecil: your self-edits run the same suite, guardrails included. If a guard fails, fix the code;
  never widen an allowlist or grow the baseline to get green.
