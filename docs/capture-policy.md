# Capture policy

The vault is long-term memory. It stays useful only if it holds facts that
remain true, and stays out of the way of systems that already record
day-to-day activity.

## 1. Durable facts only

Capture a fact when it will still matter in 30+ days:

- rules and policies ("never X", "always Y before Z");
- decisions, with the reason;
- corrections to something the vault already says;
- workflows, SOPs, runbooks, recovery steps;
- root causes, pitfalls, gotchas, integration quirks;
- standing vendor/customer preferences and relationship facts.

Do **not** capture:

- balances, totals, quotes, payments, rates that change;
- ETAs, "arrived", "waiting on", "today/tomorrow" status;
- single-session instructions ("make it shorter");
- personal style preferences of the user (built-in memory owns those);
- secrets of any kind (the tool rejects them).

The capture gate enforces this mechanically:

| Mode | Behavior |
|---|---|
| `off` | No classification |
| `shadow` (default) | Classify and log to `capture_gate_log.jsonl`; never block |
| `enforce` | `daily` → `capture_gate_held.jsonl` (text kept locally); `unsure` → same ledger for review; `durable` → vault |

`force_vault: true` overrides a hold, only for a genuine rule or decision the
classifier misjudged. Run `shadow` for a couple of weeks, review the log, then
switch to `enforce`.

## 2. Current-state hubs

Every project gets one living hub: `20-WIKI/projects/<project>-current-state.md`
(start from `_template-current-state.md`, tag `current-state`).

- It answers "what is true now": state, what exists, decisions, done, next.
- Rewrite it as reality changes; do not append a diary. Move superseded detail
  into dated notes and link them under *History*.
- Older notes it replaces get `status: superseded` and `superseded-by:` so
  search ranks them down.
- The hub is published, so agents propose updates via `_drafts/`; the owner
  (or a weekly roll-up) merges.

## 3. Done-state recall

The classic failure: an older, longer "remaining work" note outranks the short
note saying the work finished. Prevent it:

1. When work completes, capture the done-state the same turn — a dated line in
   the hub's *Done* section (via draft) or a new note linked to the hub.
2. Mark the stale plan `status: superseded` with `superseded-by:` the hub.
3. Ranking then favours the recent note and the current-state hub, and demotes
   the superseded plan.
4. When answering "is X done?", read the current-state hub first and verify
   live systems; document status is evidence, not proof.

## 4. Weekly roll-up

Once a week (manually or with a scheduled Hermes job):

1. Review `10-RAW/inbox/` and route or archive each item.
2. Review `_drafts/` proposals; merge accepted ones into their targets and set
   the draft `status: applied` with `applied-to:`.
3. Apply `wiring_queue.jsonl` requests (add the new note's link to its hub).
4. Review `capture_gate_held.jsonl` / `unsure` items; re-capture real rules with
   `force_vault: true`, drop the rest.
5. Refresh each active project's current-state hub; supersede stale notes.
6. Write a short summary to `30-OUTPUT/weekly-reviews/YYYY-Www.md`.
