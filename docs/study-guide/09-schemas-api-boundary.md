# 09 — Schemas: the API boundary

File: `app/schemas.py`. A short chapter about one decision with long consequences.

## Two models of the same thing, on purpose

Chapter 04 defined `Monitor` — the database table. This file defines `MonitorCreate`,
`MonitorUpdate`, `MonitorRead`, and `MonitorStats` — the API's shapes. They overlap heavily.
Writing the field list twice looks like a mistake.

> 🔑 **Decision: keep ORM models and API schemas as separate layers — explicitly rejecting
> SQLModel, which merges them.** Recorded as a decision made before any code was written.

The reason is that the two layers answer different questions and change for different
reasons. The table answers *"what do we store?"* The schema answers *"what may a client
send, and what will we show them?"* Whenever those answers differ — and they always
eventually differ — a merged model forces a compromise.

Look at the concrete divergences already present in this small app:

| | `Monitor` (table) | `MonitorCreate` (API in) | `MonitorRead` (API out) |
|---|---|---|---|
| `url` | `String(2048)` | `HttpUrl` — validated | `HttpUrl` |
| `interval_seconds` | plain integer | `ge=1, le=31_536_000` | same |
| `user_id` | a column | **absent** — taken from the API key | present |
| `id`, `created_at` | columns | **absent** — server-assigned | present |

`MonitorCreate` deliberately has no `user_id`. If it did, a client could create monitors
in someone else's account by supplying a different number. Ownership comes from the API key
(chapter 10), never from the request body. In a merged model, you'd need per-field
exclusion rules to express that; here it's simply a field that isn't on the class.

`UserRead` shows the other direction: it includes `api_key` (`app/schemas.py:90`), because
registration is the one moment the key is shown. A table-shaped response model would either
leak it on every user endpoint or need a special case.

The cost is real — add a column and you touch two files. That cost is the price of being
able to change your storage without changing your API contract, and vice versa.

## `HttpUrl` and the coercion it forces

`MonitorBase.url` is typed `HttpUrl` (`app/schemas.py:14`), so Pydantic rejects
`"not a url"` with a 422 before the route body runs. But `Monitor.url` is a plain string
column. So the route converts:

```python
monitor = Monitor(url=str(payload.url), ...)
```
`app/routers/monitors.py:89`

That `str(...)` is doing necessary work — and it's also the fuse for war story C2 in
chapter 11, where `str(None)` produced the literal text `"None"` and corrupted a row.
Note it now; the bug lands there.

## `MonitorUpdate`: why PATCH needs its own schema

```python
class MonitorUpdate(BaseModel):
    url: HttpUrl | None = None
    method: HttpMethod | None = None
    ...
```
`app/schemas.py:30-38`

Every field optional, defaulting to `None`. That's what makes PATCH *partial*: send only
`{"interval_seconds": 600}` and nothing else changes.

The route then does:

```python
updates = payload.model_dump(exclude_unset=True, exclude_none=True)
```
`app/routers/monitors.py:136`

> ⚙️ **FastAPI note:** `model_dump` turns the schema back into a dictionary.
> `exclude_unset=True` drops fields the client didn't mention at all. `exclude_none=True`
> drops fields explicitly sent as `null`. The difference between those two flags is
> precisely war story C2 — and for a long time only the first one was there.

## `from_attributes`: reading straight from ORM objects

```python
class MonitorRead(MonitorBase):
    model_config = ConfigDict(from_attributes=True)
```
`app/schemas.py:41-44`

By default Pydantic builds from dictionaries. `from_attributes=True` lets it build from any
object with matching attributes — an ORM instance, for example. That's why routes can
`return monitor` (a SQLAlchemy object) while declaring `response_model=MonitorRead`:
FastAPI reads the attributes off it and produces validated JSON.

Now notice which schema *doesn't* have it:

```python
class MonitorStats(BaseModel):
    monitor_id: int
    window_hours: int
    ...
```

> 🔑 **Decision: `MonitorStats` deliberately omits `from_attributes`.** The stats plan says
> why: *"this schema is constructed directly, never from an ORM object."* There's no
> `Stats` table — the values come from an aggregation query and are assembled by hand at
> `app/routers/monitors.py:250-261`. Adding `from_attributes` would imply a relationship to
> a database object that doesn't exist. Chapter 14 has the full story.

## Two-level validation: schema bounds vs. policy

`interval_seconds` is constrained in two different places:

```python
interval_seconds: int = Field(ge=1, le=31_536_000,
    description="Check period; the API enforces the configured minimum.")
```
`app/schemas.py:17-21`

```python
if interval_seconds < settings.min_interval_seconds:
    raise HTTPException(status_code=422, detail=...)
```
`app/routers/monitors.py:32-38`

> 🔑 **Decision: the schema enforces *absolute sanity*; the route enforces *policy*.**

The schema's job is "this could never be a valid interval under any configuration" — zero,
negative, or larger than a year. Those are facts about the type. The 60-second floor is a
*policy* that lives in settings (chapter 03) and can differ per environment, so it can't be
baked into a schema that's evaluated before settings are consulted. The `description`
tells API users where the real limit comes from, and it appears in the generated `/docs`.

Both bounds matter. The upper bound `le=31_536_000` was added after a bug; the lower policy
floor was there from the start. Chapter 11.

## `HttpMethod`: a type that documents itself

```python
HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
```
`app/schemas.py:8`

> 🐍 **Python note:** `Literal[...]` means "one of exactly these values." Not a string that
> happens to look like one — the value must be in the list.

Anything else gets a 422 naming the allowed options, and `/docs` renders it as a dropdown.
One line replaces a validator function, an error message, and a documentation entry.

### 🔍 Try it

1. **Watch validation reject bad input before any route code runs:**

   ```bash
   python -c "
   from app.schemas import MonitorCreate
   from pydantic import ValidationError
   for bad in [{'url':'not-a-url','interval_seconds':60},
               {'url':'https://x.com','interval_seconds':0},
               {'url':'https://x.com','interval_seconds':60,'method':'FETCH'}]:
       try:
           MonitorCreate(**bad)
       except ValidationError as e:
           print(bad, '->', e.errors()[0]['msg'])
   "
   ```

2. **Prove `user_id` can't be injected.** Try creating a monitor with `\"user_id\": 999` in
   the JSON body against a running app — the field is ignored, because it isn't on
   `MonitorCreate`. Then check `app/routers/monitors.py:88` to see where `user_id` actually
   comes from.

3. **Find the exact line that makes PATCH partial:** `app/routers/monitors.py:136`. Delete
   `exclude_none=True` from it, run `pytest tests/test_monitors_api.py -k explicit_null -v`,
   watch it fail, then `git checkout app/routers/monitors.py`. You've just reproduced war
   story C2 — chapter 11 tells you what it cost.

---

Previous: [08 — The scheduler](08-scheduler.md) · Next: [10 — Auth & users](10-auth-and-users.md)
