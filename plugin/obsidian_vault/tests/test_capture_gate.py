from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load("cg_under_test", "capture_gate.py")

DAILY = [
    ("customer", "Example reconciliation: order 1001 $3,000.00 total / $2,000.00 paid / $1,000.00 remaining; order 1002 $500.00 deposit."),
    ("customer", "Sample wires: $1,000.00 week one, $2,000.00 week two, $3,000.00 week three. Sum $6,000.00."),
    ("concept", "Job 1003 add-on: four widgets $400.00; accessory kit $100.00; subtotal $500.00; shipping $50; handling $25."),
    ("workflow", "Price sync example: widget A 1000 -> 1100 cents ($10.00 -> $11.00); widget B $20.00 -> $21.00; paid weekly; widget C unchanged pending."),
    ("other", "Shipments this week: order 1004 Tue, order 1005 Wed, waiting on stock for order 1006 today."),
]
DURABLE = [
    ("workflow", "Routing rule: all sample questions must go to queue A, never queue B; every external write needs a confirm step."),
    ("concept", "Root cause: a read-only SQLite open fails when the WAL side files were removed on clean close; use a query_only fallback instead."),
    ("sop", "Recovery: if the sync job times out, restart the worker, force a poll, then run the health check. Never bypass the restart guard."),
    ("customer", "Example Customer Co. is a sample account; always confirm deposit receipt before posting. Prefers text over email."),
    ("vendor", "The sample catalog import fails when the header row is missing; the importer maps columns by name."),
    # System/process notes that mention dates or statuses but no money.
    ("sop", "Checklist drafts 1-5 written today: setup, review, packaging, shipping, handoff; deposit step pending."),
    ("workflow", "Ledger shadow design: order 1007 and order 1008 checked against the reference system; mismatches listed for review."),
]
UNSURE = [
    # Price maps stay in the owner review queue, never auto-durable.
    ("workflow", "Price map: widget A $10.00, widget B $20.00, widget C $30.00; rule: always sync after price changes."),
]


@pytest.mark.parametrize("cat,text", UNSURE)
def test_money_in_system_notes_still_goes_to_review(cat, text):
    assert gate.classify(text, cat).label != "durable"


@pytest.mark.parametrize("cat,text", DAILY)
def test_day_to_day_is_not_durable(cat, text):
    assert gate.classify(text, cat).label == "daily"


@pytest.mark.parametrize("cat,text", DURABLE)
def test_rules_and_standing_facts_are_durable(cat, text):
    assert gate.classify(text, cat).label == "durable"


def _provider(tmp_path, mode):
    mod = _load("ov_under_test_gate", "__init__.py")
    p = mod.ObsidianVaultMemoryProvider()
    p._config = {"capture_gate": mode}
    p._index_path = tmp_path / "state" / "index.sqlite3"
    p._profile = "default"
    called = []
    p._capture_bounded = lambda args: called.append(args) or {"status": "created"}
    return p, called


def test_shadow_logs_but_never_blocks(tmp_path):
    p, called = _provider(tmp_path, "shadow")
    out = json.loads(p.handle_tool_call("obsidian_vault_capture", {"category": "customer", "content": DAILY[0][1]}))
    assert out == {"status": "created"} and len(called) == 1
    log = (tmp_path / "state" / "capture_gate_log.jsonl").read_text().splitlines()
    rec = json.loads(log[-1])
    assert rec["label"] == "daily" and "content" not in rec  # shadow log stores no text
    assert not (tmp_path / "state" / "capture_gate_held.jsonl").exists()


def test_enforce_holds_day_to_day_and_keeps_text_locally(tmp_path):
    p, called = _provider(tmp_path, "enforce")
    out = json.loads(p.handle_tool_call("obsidian_vault_capture", {"category": "customer", "content": DAILY[1][1]}))
    assert out["status"] == "held_daily" and out["vault_write"] is False and called == []
    held = json.loads((tmp_path / "state" / "capture_gate_held.jsonl").read_text().splitlines()[-1])
    assert held["content"] == DAILY[1][1]


def test_enforce_passes_durable_and_force_vault(tmp_path):
    p, called = _provider(tmp_path, "enforce")
    json.loads(p.handle_tool_call("obsidian_vault_capture", {"category": "workflow", "content": DURABLE[0][1]}))
    json.loads(p.handle_tool_call("obsidian_vault_capture", {"category": "customer", "content": DAILY[0][1], "force_vault": True}))
    assert len(called) == 2


def test_off_mode_and_gate_errors_fail_open(tmp_path, monkeypatch):
    p, called = _provider(tmp_path, "off")
    p.handle_tool_call("obsidian_vault_capture", {"category": "customer", "content": DAILY[0][1]})
    p2, called2 = _provider(tmp_path, "enforce")
    monkeypatch.setattr(p2, "_capture_state_dir", lambda: (_ for _ in ()).throw(OSError("boom")))
    p2.handle_tool_call("obsidian_vault_capture", {"category": "customer", "content": DAILY[0][1]})
    assert len(called) == 1 and len(called2) == 1


def test_enforce_never_copies_secret_shaped_capture_into_held_ledger(tmp_path):
    p, called = _provider(tmp_path, "enforce")
    p.handle_tool_call("obsidian_vault_capture", {"category": "inbox", "content": "api_key = abcdef1234567890"})
    assert not (tmp_path / "state" / "capture_gate_held.jsonl").exists()
    assert len(called) == 1  # falls through to capture, which rejects secrets


def test_log_records_whether_capture_named_a_system(tmp_path):
    p, called = _provider(tmp_path, "shadow")
    p.handle_tool_call("obsidian_vault_capture", {"category": "workflow", "content": DURABLE[0][1], "system_id": "backup-runner"})
    p.handle_tool_call("obsidian_vault_capture", {"category": "workflow", "content": DURABLE[1][1]})
    recs = [json.loads(l) for l in (tmp_path / "state" / "capture_gate_log.jsonl").read_text().splitlines()]
    assert [r["system_id"] for r in recs[-2:]] == ["backup-runner", None] and len(called) == 2
