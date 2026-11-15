"""assistant.approvals: standing ("always yes") approvals - fingerprints, the never-standing list, YES ALWAYS by
SMS, the dashboard button, keypad/spoken "always", auto-approval of identical requests, revoke. Offline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import dashboard
import db
import notify
from assistant import approvals, store, toolkit_web, tools
from assistant.approvals import Fingerprint
from assistant_fixtures import assistant_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")
H = {"Authorization": "Bearer tok-approvals-1"}


@pytest.fixture
def demo(monkeypatch):
    """An approval handler 'demo' whose requests are fingerprinted by data['x']."""
    applied: list[int] = []

    def handler(decision, data):
        applied.append(decision.id)
        return True

    tools.register_approval_handler("demo", handler)
    monkeypatch.setitem(approvals.FINGERPRINTERS, "demo",
                        lambda data: Fingerprint("demo", approvals.norm(data.get("x")), f"demo {data.get('x')}"))
    return applied


def ask(x: str = "A", summary: str = "Run the demo") -> int:
    return notify.request_approval("other", summary, {"action": "assistant_action", "handler": "demo", "data": {"x": x}})


def note(summary: str, action_type: str = "", target: str = "") -> int:
    payload = {"action": "assistant_action", "handler": "note", "data": {}}
    if action_type:
        payload["action_type"] = action_type
    if target:
        payload["target"] = target
    return notify.request_approval("other", summary, payload)


def approval_rows(did: int) -> list:
    return [n for n in db.list_notifications(limit=200) if n.decision_id == did]


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_fingerprints_and_never_standing(demo):
    assert approvals.classify(db.get_decision(ask("Gaming ")))[0] == Fingerprint("demo", "gaming", "demo Gaming ")
    fp, why = approvals.classify(db.get_decision(note("Switch the PC to Gaming mode", "desktop_mode", "Gaming")))
    assert fp is not None and (fp.action_type, fp.target) == ("desktop_mode", "gaming") and why == ""
    refused = {
        "Spend $20 on promoted posts": ("ads", "campaign"),
        "Make the new Short public on YouTube": ("youtube", "short 12"),
        "Email John the invoice summary": ("email", "john"),
        "Delete the old downloads folder": ("cleanup", "downloads"),
        "Install the screen recorder": ("software", "obs"),
        "Widen the strategy niche": ("strategy", "niche"),
    }
    for summary, (at, tg) in refused.items():
        fp, why = approvals.classify(db.get_decision(note(summary, at, tg)))
        assert fp is None and "always ask" in why, summary
    # no fingerprint named -> cannot be standing
    assert approvals.classify(db.get_decision(note("Do the thing")))[0] is None
    # phase-1 strategy decisions and content-specific handlers never
    did = db.add_decision("budget_increase", "Raise the budget", {"action": "apply_strategy"})
    assert approvals.classify(db.get_decision(did))[0] is None
    did = db.add_decision("other", "Improve a skill", {"action": "assistant_action", "handler": "improvement",
                                                       "data": {"proposal_id": "p1"}})
    assert approvals.classify(db.get_decision(did))[0] is None


def test_skill_installs_never_become_standing(monkeypatch):
    from assistant import skill_vetting

    reps = {"t": SimpleNamespace(reputation=SimpleNamespace(trusted=True, repo="anthropics/skills")),
            "u": SimpleNamespace(reputation=SimpleNamespace(trusted=False, repo="someone/skills"))}
    monkeypatch.setattr(skill_vetting, "load_report", lambda vid: reps[vid])

    def dec(vid: str) -> db.Decision:
        return db.get_decision(db.add_decision("other", "Add skill", {"action": "assistant_action",
                                                                      "handler": "skill_install",
                                                                      "data": {"vet_id": vid}}))

    for vid in ("t", "u"):   # even a trusted source: every skill version is different code
        d = dec(vid)
        fp, why = approvals.classify(d)
        assert fp is None and "third-party code" in why
        res = approvals.approve_always(d.id, via="sms")
        assert res["rule"] is None and approvals.list_rules() == []


# ---------------------------------------------------------------------------
# YES ALWAYS -> later identical requests auto-approve
# ---------------------------------------------------------------------------

def test_yes_always_by_sms_then_identical_requests_auto_approve(demo, clock):
    did = ask("A")
    assert db.get_decision(did).status == "pending" and approval_rows(did)
    assert f"YES ALWAYS {did}" in notify._approval_sms_text(db.get_decision(did))
    assert notify.is_pipeline_command(f"yes always {did}")
    reply = notify.handle_sms_command(f"YES ALWAYS {did}")
    assert f"#{did} approved and applied" in reply and "From now on" in reply
    assert demo == [did]
    (rule,) = approvals.list_rules()
    assert (rule["action_type"], rule["target"], rule["created_via"], rule["decision_id"]) == ("demo", "a", "sms", did)

    did2 = ask("a ")                       # same normalised target
    d2 = db.get_decision(did2)
    assert d2.status == "approved" and d2.resolved_via == "standing"
    assert demo == [did, did2] and approval_rows(did2) == []          # nobody was phoned
    assert approvals.get_rule(rule["id"])["uses"] == 1
    ev = [e for e in db.list_events(module="approvals", limit=10) if e.message.startswith("Approved #")]
    assert ev and f"#{did2}" in ev[0].message
    assert any(m.kind == "standing_approval" for m in store.list_messages(10, channel="dashboard"))

    did3 = ask("B")                        # a different target still asks
    assert db.get_decision(did3).status == "pending" and approval_rows(did3)

    assert approvals.revoke_rule(rule["id"], via="test") is True
    assert approvals.revoke_rule(rule["id"], via="test") is False
    assert db.get_decision(ask("A")).status == "pending"


def test_always_after_plain_yes_and_never_kinds_keep_asking(demo):
    did = ask("C")
    reply = notify.handle_sms_command(f"YES {did}")
    assert reply.startswith(f"#{did} approved and applied.") and f"Reply ALWAYS {did}" in reply
    reply = notify.handle_sms_command(f"ALWAYS {did}")
    assert "From now on" in reply and demo == [did]            # not applied twice
    assert notify.handle_sms_command("ALWAYS").startswith("Please include the request number")

    spend = note("Spend $40 on a microphone", "purchase", "microphone")
    assert "YES ALWAYS" not in notify._approval_sms_text(db.get_decision(spend))
    reply = notify.handle_sms_command(f"YES ALWAYS {spend}")
    assert "keep asking" in reply and "spending" in reply
    assert db.get_decision(spend).status == "approved"
    assert len(approvals.list_rules()) == 1
    rej = ask("D")
    notify.handle_sms_command(f"NO {rej}")
    assert "nothing to allow" in notify.handle_sms_command(f"YES ALWAYS {rej}")


def test_request_approval_tool_reports_standing_approval(demo):
    first = note("Switch to Work mode", "desktop_mode", "Work")
    approvals.approve_always(first, via="dashboard")
    ctx = tools.ToolContext(channel="dashboard")
    allowed = frozenset({"request_approval", "approval_rules_list", "approval_rule_revoke"})
    res = tools.dispatch("request_approval", {"summary": "Switch to Work mode", "action_type": "desktop_mode",
                                              "target": " work "}, ctx, allowed=allowed)
    assert res.ok and '"status": "approved"' in res.content
    res = tools.dispatch("request_approval", {"summary": "Switch to Gaming mode", "action_type": "desktop_mode",
                                              "target": "Gaming"}, ctx, allowed=allowed)
    assert res.ok and '"status": "pending"' in res.content
    listed = tools.dispatch("approval_rules_list", {}, ctx, allowed=allowed)
    assert listed.ok and "desktop mode: work" in listed.content
    rid = approvals.list_rules()[0]["id"]
    assert tools.dispatch("approval_rule_revoke", {"rule_id": rid}, ctx, allowed=allowed).ok
    assert not tools.dispatch("approval_rule_revoke", {"rule_id": 999}, ctx, allowed=allowed).ok
    assert approvals.list_rules() == []


def test_spoken_and_keypad_always_on_the_approval_call(demo):
    assert notify._spoken_digit("Always, please") == "3"
    assert notify._spoken_digit("yes") == "1" and notify._spoken_digit("no thanks") == "2"
    assert notify._spoken_digit("hmm") == ""
    did = ask("E")
    assert "press 3" in notify._approval_prompt(db.get_decision(did))
    spend = note("Spend $5 on a font", "purchase", "font")
    assert "press 3" not in notify._approval_prompt(db.get_decision(spend))


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok-approvals-1")
    config.reset_settings()
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(toolkit_web.router)
    return TestClient(app, base_url="http://testserver")


def test_dashboard_always_button_and_approvals_page(client, demo):
    did = ask("F")
    spend = note("Spend $9 on stock music", "purchase", "music")
    page = client.get("/dashboard/decisions", headers=H).text
    assert f"/dashboard/decisions/{did}/always" in page and f"/dashboard/decisions/{spend}/always" not in page
    r = client.post(f"/dashboard/decisions/{did}/always", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "always" and r.json()["rule_id"]
    assert db.get_decision(did).status == "approved" and demo == [did]
    r = client.post(f"/dashboard/decisions/{spend}/always", headers=H)
    assert r.json()["status"] == "always_refused" and db.get_decision(spend).status == "approved"

    assert client.get("/dashboard/approvals").status_code == 401
    page = client.get("/dashboard/approvals", headers=H).text
    assert "demo F" in page and "Revoke" in page
    rid = approvals.list_rules()[0]["id"]
    r = client.post("/dashboard/approvals/revoke", data={"rule_id": str(rid)}, headers=H)
    assert r.status_code == 200 and f"Revoked rule #{rid}" in r.text
    assert approvals.list_rules() == []
