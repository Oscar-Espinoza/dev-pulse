# DevPulse Study Guide

A guide to reading, understanding, and eventually extending this codebase — written for
someone who is comfortable with programming but new to Python and FastAPI.

**This guide is about *why*, not *what*.** The code already tells you what it does. What
code can never tell you is which alternatives were considered and rejected, which lines
exist because something broke once, and which known problems were left in on purpose.
That's what's collected here.

## How to use it

Read in order. The chapters follow the order the project was actually built, so every
chapter only depends on ones before it — no forward references, no "we'll explain this
later."

Each chapter ends with a **🔍 Try it** section: a few concrete commands to run. Do them.
Reading about an event loop teaches you the words; watching a test fail because you broke
the event loop teaches you the thing.

Budget roughly 30–45 minutes per chapter if you do the exercises. There are 16, so this is
a week or two of evenings, not an afternoon.

## Setup (do this once)

```bash
cd /path/to/dev-pulse
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                    # should print "53 passed"
```

You need the virtual environment active in every terminal where you run `pytest`,
`uvicorn`, or `alembic`. If a command says "not found," that's almost always why.

You do **not** need Postgres or a network connection to run the tests — chapter 13 explains
how that's arranged and why it matters.

## Chapters

| # | Chapter | What you'll understand after it |
|---|---------|--------------------------------|
| [00](00-orientation.md) | Orientation | What the app does, how the pieces fit, why it all runs in one process |
| [01](01-stack.md) | The stack | Why each library was chosen, and what was rejected |
| [02](02-python-survival-kit.md) | Python survival kit | The ~8 Python ideas you need; where each first appears here |
| [03](03-config-and-logging.md) | Config & logging | Why limits are settings, not constants; how logs stay searchable |
| [04](04-data-model.md) | The data model | The three tables, and why an outage is a *row* rather than an error |
| [05](05-database-plumbing.md) | Database plumbing | Sessions per request, and five lines that exist purely because of hosting reality |
| [06](06-migrations.md) | Migrations | Why the schema is code, and why migrations reuse the app's own connection logic |
| [07](07-checker.md) | The checker | The core product decision: failures are data, not exceptions |
| [08](08-scheduler.md) | The scheduler | One job per monitor, and every guard that keeps the loop alive |
| [09](09-schemas-api-boundary.md) | Schemas: the boundary | Why the API's shape is deliberately not the database's shape |
| [10](10-auth-and-users.md) | Auth & users | API keys over JWT (twice), and how FastAPI dependency injection works |
| [11](11-monitors-crud.md) | Monitors CRUD | 404-not-403, quota rules, and **two real bugs** that shipped and got fixed |
| [12](12-lifespan-assembly.md) | Lifespan: assembly | Where everything gets wired together, and **a resource leak** that hid there |
| [13](13-testing-philosophy.md) | Testing philosophy | Why these tests are fast, hermetic, and written before the code |
| [14](14-case-study-stats.md) | Case study: the stats endpoint | One whole feature reverse-engineered, decisions first |
| [15](15-what-was-not-built.md) | What wasn't built | Known holes, why they're still there, and what to build next |

## Legend

Chapters use four kinds of callout:

> 🐍 **Python note:** A language feature explained the first time it appears. Never
> repeated — if you forget one, chapter 02 is the index.

> ⚙️ **FastAPI note:** A framework concept, same rule.

> 💥 **War story:** Something that actually broke, with the cause and the fix. There are
> five in this guide. They're the most valuable pages here.

> 🔑 **Decision:** A choice with a recorded rationale, including what was rejected.

## Where the "why" comes from

This project was built plan-first, and the plans survive in the repo. When this guide says
"the reason was X," you can usually check:

- `docs/superpowers/plans/2026-08-07-devpulse-monitoring-engine.md` — the original
  2,400-line build plan for tasks 1–8, with rationale attached to nearly every decision.
- `docs/superpowers/plans/2026-08-07-monitor-stats-endpoint.md` — the stats feature plan.
- `git log` — commit messages here carry real explanations, not just "fix bug."

Citations look like `app/models.py:28` (file and line) or `commit 7283069`. Everything is
checkable. If the code drifts from this guide, trust the code and fix the guide.
