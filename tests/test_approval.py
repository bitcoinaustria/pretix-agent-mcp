"""The approval ceremony: propose → CLI approve → execute, and the live-event guard.

The load-bearing property is that a high-risk call mutates *nothing* until a human
approved it out of band. Every test here asserts that pretix saw no write.
"""

from __future__ import annotations

import time

import pytest

from pretix_agent_mcp.cli import main
from pretix_agent_mcp.pending import ApprovalError
from pretix_agent_mcp.registry import REGISTRY, execute_approved, run_tool

DRAFT = {"slug": "conf27", "name": {"en": "Conf 27"}, "live": False, "testmode": True}
LIVE = {"slug": "conf27", "name": {"en": "Conf 27"}, "live": True, "testmode": False}


async def call(app, name, **kwargs):
    return await run_tool(app, REGISTRY[name], kwargs)


async def test_high_risk_tool_proposes_instead_of_mutating(app, api):
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "live": True})

    result = await call(app, "publish_event", event="conf27")

    assert result["status"] == "awaiting_approval"
    assert result["pending_action_id"]
    assert "LIVE" in result["preview"]
    assert api.sent("PATCH", "events/conf27") == [], "nothing may be written before approval"


async def test_execute_before_approval_is_refused(app, api):
    api.route("GET", "events/conf27", DRAFT)
    proposal = await call(app, "publish_event", event="conf27")
    with pytest.raises(ApprovalError, match="awaiting approval"):
        await call(app, "execute_pending_action", pending_action_id=proposal["pending_action_id"])
    assert api.sent("PATCH", "events/conf27") == []


async def test_full_ceremony(app, api, monkeypatch):
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "live": True})
    proposal = await call(app, "publish_event", event="conf27")
    action_id = proposal["pending_action_id"]

    # A human approves on the server. The CLI is the whole approval surface.
    monkeypatch.setattr("pretix_agent_mcp.cli.load", lambda config_file=None: app.cfg)
    monkeypatch.setattr("pretix_agent_mcp.cli.build_app", lambda cfg: app)
    assert main(["approve", action_id]) == 0

    result = await call(app, "execute_pending_action", pending_action_id=action_id)

    assert result["published"]["slug"] == "conf27"
    assert api.sent("PATCH", "events/conf27") == [{"live": True}]
    assert app.pending.get(action_id).state == "executed"


async def test_an_action_runs_at_most_once(app, api, monkeypatch):
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "live": True})
    action_id = (await call(app, "publish_event", event="conf27"))["pending_action_id"]
    app.pending.decide(action_id, "approved")
    await execute_approved(app, action_id)
    with pytest.raises(ApprovalError):
        await execute_approved(app, action_id)
    assert len(api.sent("PATCH", "events/conf27")) == 1


async def test_rejected_action_never_executes(app, api):
    api.route("GET", "events/conf27", DRAFT)
    action_id = (await call(app, "publish_event", event="conf27"))["pending_action_id"]
    app.pending.decide(action_id, "rejected")
    with pytest.raises(ApprovalError, match="rejected"):
        await execute_approved(app, action_id)
    assert api.sent("PATCH", "events/conf27") == []


async def test_expired_action_is_refused(make_app, api):
    app = make_app(APPROVAL_TTL_SECONDS="0")
    api.route("GET", "events/conf27", DRAFT)
    action_id = (await call(app, "publish_event", event="conf27"))["pending_action_id"]
    time.sleep(0.01)
    with pytest.raises(ApprovalError, match="expired"):
        app.pending.decide(action_id, "approved")
    assert app.pending.get(action_id).state == "expired"


async def test_the_agent_cannot_approve_anything():
    """There is no tool that writes approval state — by construction, not by convention."""
    approving = [name for name, spec in REGISTRY.items() if "approve" in name and name != "execute_pending_action"]
    assert approving == []


async def test_live_event_guard_escalates_an_ordinary_write(app, api):
    api.route("GET", "events/conf27", LIVE)
    api.route("PATCH", "events/conf27", LIVE)

    result = await call(app, "update_event", event="conf27", presale_end="2027-06-01T00:00:00+02:00")

    assert result["status"] == "awaiting_approval"
    assert api.sent("PATCH", "events/conf27") == []


async def test_the_same_write_executes_directly_on_a_draft(app, api):
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "location": "Wien"})

    result = await call(app, "update_event", event="conf27", location="Wien")

    assert result["changed"] == ["location"]
    assert api.sent("PATCH", "events/conf27") == [{"location": "Wien"}]


async def test_test_mode_event_is_not_treated_as_live(app, api):
    api.route("GET", "events/conf27", {**LIVE, "testmode": True})
    api.route("PATCH", "events/conf27", LIVE)
    result = await call(app, "update_event", event="conf27", location="Wien")
    assert "awaiting_approval" not in repr(result)


async def test_operator_can_reclassify_a_high_risk_tool(make_app, api):
    app = make_app(MCP_AUTO_APPROVE="publish_event")
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "live": True})
    result = await call(app, "publish_event", event="conf27")
    assert result["published"]["slug"] == "conf27"
    assert api.sent("PATCH", "events/conf27") == [{"live": True}]


async def test_lifecycle_is_audited(app, api, monkeypatch):
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "live": True})
    action_id = (await call(app, "publish_event", event="conf27"))["pending_action_id"]
    monkeypatch.setattr("pretix_agent_mcp.cli.load", lambda config_file=None: app.cfg)
    monkeypatch.setattr("pretix_agent_mcp.cli.build_app", lambda cfg: app)
    main(["approve", action_id])
    await call(app, "execute_pending_action", pending_action_id=action_id)

    log = app.cfg.audit_log.read_text()
    assert '"proposed"' in log and '"approved"' in log and '"executed"' in log
    assert action_id in log
    assert app.cfg.pretix_api_token not in log
    assert app.cfg.mcp_bearer_token not in log


async def test_read_calls_are_not_audited(app, api):
    api.route("GET", "events/conf27", DRAFT)
    await call(app, "get_event", event="conf27")
    assert not app.cfg.audit_log.exists() or app.cfg.audit_log.read_text() == ""
