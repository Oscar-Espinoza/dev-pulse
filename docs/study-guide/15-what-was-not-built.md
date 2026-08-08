# 15 — What wasn't built, and why

The last chapter is about absences. A codebase's known holes — the ones someone found,
thought about, and deliberately left — say more about its engineering than its features do.

## <a id="war-story-c5"></a>💥 War story C5: the SSRF that was documented, not fixed

**Commit `46c6a53`.**

**The finding.** A code review of the finished app identified a genuine security problem.
DevPulse probes any URL a user gives it, with any headers they supply, and returns the
result — status code, timing, error message — through `GET /monitors/{id}/results`.

That is a **Server-Side Request Forgery** primitive. A user can point a monitor at
`http://localhost:6379`, or `http://169.254.169.254/latest/meta-data/` (the cloud metadata
endpoint that hands out credentials on AWS), or any address inside the private network the
app runs in. DevPulse makes the request *from inside that network* and reports back what
happened. The review's phrasing: *"effectively a probe oracle into whatever network the app
runs on."*

The response codes and timings alone are enough to map internal services, since "connection
refused instantly" and "connected, wrong status" are distinguishable outcomes.

**The decision: document it, don't fix it.** The commit records the reasoning: *"the
original plan never specified target-URL restrictions — implementing an allowlist/blocklist
now would be scope creep beyond what was planned and reviewed."*

It's worth taking that seriously rather than reading it as an excuse. A correct SSRF defense
is not a quick patch. You must resolve the hostname and check the resulting IP against
private ranges — for IPv4 *and* IPv6, including the mapped forms. You must handle redirects
(chapter 07 turned `follow_redirects=True` on, so a public URL can redirect to
`127.0.0.1`). You must handle DNS rebinding, where the name resolves publicly during your
check and privately during the actual request. Each of those has a well-known bypass if you
get it wrong, and a half-implemented defense is worse than a documented gap because it
*looks* safe.

Against that: this is a portfolio project, the exposure is understood, and it's now written
in the README where a reader — or an employer — will see it.

> **The lesson: "we know about this and here's why it's still here" is a legitimate,
> professional engineering position.** What's not legitimate is silence. An undocumented
> hole is indistinguishable from an unnoticed one, and the next reader can't tell whether
> you thought about it. Writing it down converts a liability into evidence of judgment.

If you ever productionize this, SSRF protection is item one, and it belongs in
`app/services/checker.py` before the request goes out.

## The quota that doesn't quite work

The README's other admitted limitation:

> *"per-user quotas aren't enforced across accounts since registration has no verification
> step"*

Chapter 03's `MAX_ACTIVE_MONITORS_PER_USER = 20` caps monitors per *account*. But
`POST /users` needs only an email address, and never checks that it exists. So the actual
cap on how much traffic one person can generate is 20 × (however many times they care to
call `POST /users`). The recorded assessment: *"Acceptable for a portfolio project;
production use would need registration gating (email verification, admin approval, or
similar) to make the quota meaningful."*

> **The lesson: a limit is only as strong as the identity it's attached to.** Per-user
> quotas assume users are expensive to create. When accounts are free and unverified,
> per-account limits are advisory — they stop accidents, not adversaries. The same logic
> applies to rate limits keyed on API keys, IP addresses, or anything else cheap to acquire.

## One instance, and a sleeping free tier

From chapter 00: the scheduler runs inside the web process. The consequences, recorded in
the deployment notes:

**Run exactly one instance.** Two instances both schedule every active monitor, so every
site gets checked twice as often — silently doubling your outbound traffic and your data.
Nothing in the code prevents this; it's an operational constraint you have to know.

**Free tiers sleep.** A platform that suspends idle services also suspends your scheduler.
Checks stop until something wakes the app. An uptime monitor with downtime is a good joke
and a real limitation.

Fixing either means the architecture change chapter 00 flagged: move the scheduler out of
the web process, with a shared job store or a leader election so exactly one runs. That is
a different project, and knowing it's a different project is the point of having written
the constraint down.

## JWT, dropped twice

Chapter 10 covered it: planned at the start, rejected; proposed again with a full
implementation plan, rejected again with one sentence. It's here in the "not built" list
because deleting planned work belongs in the same category as the items above — a decision
with a reason, worth recording.

## The index that should exist

Chapter 14's query filters on `(monitor_id, checked_at)` together. Chapter 04 gave those
columns *two separate* single-column indexes. A composite index on the pair would serve this
query better — a `hours=8760` request against a 60-second monitor scans on the order of
500,000 rows today.

The final review flagged it and it was deliberately not done, for a reason that's about
process rather than performance: the stats work had an explicit constraint of **no schema
changes**. Adding a migration to a branch scoped as "add one endpoint" widens what the
reviewer has to reason about and couples a schema change to a feature that doesn't require
it. The endpoint is correct without it; the index is an optimization with its own migration
and its own review.

> **The lesson: keeping a change small is itself a decision worth defending.** "While I'm in
> here" is how a reviewable diff becomes an unreviewable one.

It would also speed up chapter 11's `list_results`, which filters and sorts on the same
pair — so it's a genuinely good first contribution.

## What to build next

Roughly in order of value:

1. **The composite index.** `(monitor_id, checked_at)` on `check_results`. Small, benefits
   two endpoints, and walks you through the full migration loop from chapter 06.
2. **A results-retention policy.** Nothing ever deletes check results. One monitor is half
   a million rows a year. A scheduled job that prunes rows older than N days — or rolls them
   into hourly summaries — is the difference between this running for a year and running for
   a week.
3. **SSRF protection**, per war story C5.
4. **Key rotation.** `POST /users/me/rotate-key`. Currently a leaked key can only be dealt
   with by abandoning the account.
5. **Notifications.** The product currently records outages and tells nobody. A webhook
   fired on the transition from success to failure is the natural next feature — and note
   that chapter 14's incident logic already knows how to detect that transition.

## Three exercises to prove you've got it

**1. Add a field, end to end.** Give `Monitor` a `name: str`. You'll touch: `app/models.py`
(column), a new Alembic migration (chapter 06 — generate it against real Postgres),
`app/schemas.py` (all three monitor schemas — chapter 09 explains why they're separate),
and a test. The exercise teaches you which layers are genuinely independent.

**2. Trace a single check end to end.** Start at `MonitorScheduler.start()`
(`app/services/scheduler.py:46`) and follow the path to a row in `check_results`, naming
every guard you pass: the poison-row `try/except`, `_run_check`'s blanket handler, the
deleted-monitor no-op, `perform_check`'s four outcomes. Then ask, at each one: what fails
if I remove this? You'll have re-derived chapters 07, 08 and 11 from the code.

**3. Add the "time to first failure" metric** to `GET /monitors/{id}/stats` — the timestamp
of the earliest failure in the window. Constraint: it must stay **one SQL statement**, and
it must work on both Postgres and SQLite. (Hint: `func.min` with a `case`, exactly like the
average at `app/routers/monitors.py:229`.) Write the test first.

## The meta-lesson

You now have a codebase where you can answer *why* for essentially every decision, because
the decisions were written down as they were made — in plans, in commit messages, in
docstrings that state reasons rather than restating code, and in test names that describe
the bug they prevent.

That habit is the transferable skill in this repository. Code tells the next person what it
does. Only you can tell them what you considered and rejected, what you knew was wrong and
shipped anyway, and why the weird line is weird. Six months from now, the next person is
you.

### 🔍 Try it

1. **Read the limitations commit in full:**

   ```bash
   git show 46c6a53
   ```

   Then `git show 46d046b`, where that text was later trimmed for the README. Compare — the
   longer analysis lives on in history, which is a reasonable place for it.

2. **Confirm the SSRF for yourself, safely.** Point a monitor at `http://127.0.0.1:1` on a
   local run and read the recorded `error_message`. That message is the information leak: it
   distinguishes "nothing listening" from other failure modes.

3. **Do exercise 1.** It's the fastest way to find out which chapters you actually absorbed.

---

Previous: [14 — Case study: the stats endpoint](14-case-study-stats.md) · Back to
[the index](README.md)
