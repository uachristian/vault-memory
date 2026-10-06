"""Durability gate for vault captures.

Classifies a capture as:
  durable  - rules, decisions, how systems work, standing facts -> vault
  daily    - point-in-time detail (totals, balances, quotes, today's status) -> local ledger only
  unsure   - mixed signals -> owner review queue (see docs/capture-policy.md)

Deterministic and offline (no model call) so it is fast, explainable and testable.
Modes (config `capture_gate`): off | shadow (classify + log, never block) | enforce.
The provider fails open: any gate error results in a normal capture.
"""
from __future__ import annotations

import re

MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d\d)?|\b\d[\d,]* cents\b|\b\d+\.\d\d (?:vs|->)|\b(?:rate|pay)\b[^.\n]{0,40}\b\d{2,3}\.\d\d\b", re.I)
# Transactional record numbers (orders, tickets, invoices, purchase orders).
RECORD = re.compile(r"\b(?:order|ticket|invoice|PO|WO|job)\s?#?\s?\d{3,6}\b", re.I)
POINT_IN_TIME = re.compile(
    r"\b(balance|payoff|wire[ds]?|deposit|down payment|estimate total|order total|quote[ds]?|"
    r"payable|owed|paid|unpaid|invoice[ds]?|settlement|reconciliation|proceeds|commission|"
    r"eta|arriv(?:ed|ing)|picked up|dropped off|waiting on|in progress|"
    r"today|tonight|this morning|tomorrow)\b", re.I)
DURABLE = re.compile(
    r"\b(must|never|always|do not|don't|should not|rule|policy|prefer[s]?|preference|standing|"
    r"pitfall|gotcha|lesson|root cause|because|so that|workflow|procedure|runbook|boundary|"
    r"architecture|design|contract|recovery|rollback|fixed|installed|deployed|released|"
    r"retired|replaced|correction|corrected|confirmed|decided|decision|approved|requirement|"
    r"how to|use .{1,40} instead|fails? (?:when|if|because))\b", re.I)
# Categories whose captures are about systems/processes: durable unless clearly point-in-time.
SYSTEM_CATS = {"concept", "workflow", "sop", "system", "integration", "project"}
# Categories where money/record detail usually means day-to-day business.
BUSINESS_CATS = {"customer", "vendor", "inbox", "other"}


class Verdict:
    __slots__ = ("label", "score", "reasons")

    def __init__(self, label: str, score: int, reasons: list[str] | None = None):
        self.label, self.score, self.reasons = label, score, list(reasons or [])

    def as_dict(self) -> dict:
        return {"label": self.label, "score": self.score, "reasons": self.reasons}


def classify(content: str, category: str = "", title: str = "") -> Verdict:
    text = f"{title}\n{content}"
    cat = (category or "").strip().lower()
    money = len(MONEY.findall(text))
    records = len(RECORD.findall(text))
    pit = len(POINT_IN_TIME.findall(text))
    dur = len(DURABLE.findall(text))
    reasons = [f"money={money}", f"record={records}", f"point_in_time={pit}", f"durable={dur}", f"category={cat or '-'}"]

    daily = 2 * money + records + pit
    durable = 2 * dur + (3 if cat in SYSTEM_CATS else 0)
    score = durable - daily

    # Money-heavy notes in business categories with little rule language are day-to-day.
    if cat in BUSINESS_CATS and money >= 2 and dur <= 1:
        return Verdict("daily", score, reasons + ["money-heavy business note"])
    if money >= 4 and dur <= 2:
        return Verdict("daily", score, reasons + ["figures dominate"])
    # Rates, balances and settlement figures are point-in-time even in workflow/concept
    # notes when figures outnumber rule language.
    if money >= 3 and pit >= 2 and money + pit > 2 * dur:
        return Verdict("daily", score, reasons + ["figures outweigh rules"])
    if score >= 2:
        return Verdict("durable", score, reasons)
    if score <= -3:
        return Verdict("daily", score, reasons)
    # System/process notes with at most one figure stay durable even when they mention
    # dates or statuses ("today", "invoice"): those are usually build/decision records.
    if cat in SYSTEM_CATS and money <= 1 and (dur >= 1 or records == 0):
        return Verdict("durable", score, reasons + ["system note, no money"])
    return Verdict("unsure", score, reasons)
