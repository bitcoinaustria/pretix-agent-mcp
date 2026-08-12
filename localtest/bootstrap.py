"""Bootstrap the throwaway pretix: an organizer, a restricted team, an API token.

Run inside the pretix container via `pretix shell -c`. This is the only step that cannot go
through pretix-agent-mcp — everything else in the test is done by the agent through MCP,
which is the point of the exercise.

pretix 2026.7 replaced the old `can_*` team booleans with granular `group:action`
permissions in a JSONField. The set below is least-privilege for the tool surface under
test, so a permission the server actually needs shows up as a 403 from pretix rather than
being masked by an all-permissions team.
"""

from pretix.base.models import Organizer, Team

# What the 64 tools need. Note what is absent: organizer.teams (an agent must never be able
# to widen its own permissions), payment settings, customers, gift cards, devices.
EVENT_PERMISSIONS = [
    "event.settings.general:write",
    "event.settings.tax:write",
    "event.subevents:write",
    "event.items:write",
    "event.orders:read",
    "event.orders:write",
    "event.orders:checkin",
    "event.vouchers:read",
    "event.vouchers:write",
    "event:cancel",
]
ORGANIZER_PERMISSIONS = ["organizer.events:create"]

organizer, created = Organizer.objects.get_or_create(slug="demo", defaults={"name": "Demo Organizer"})
print(f"organizer: {organizer.slug} ({'created' if created else 'existing'})")

Team.objects.filter(organizer=organizer, name="agent-mcp").delete()
team = Team.objects.create(
    organizer=organizer,
    name="agent-mcp",
    all_events=True,
    all_event_permissions=False,
    all_organizer_permissions=False,
    limit_event_permissions={p: True for p in EVENT_PERMISSIONS},
    limit_organizer_permissions={p: True for p in ORGANIZER_PERMISSIONS},
)
token = team.tokens.create(name="local-test")

print(f"team: {team.name} — {len(EVENT_PERMISSIONS)} event + {len(ORGANIZER_PERMISSIONS)} organizer perms")
print(f"can manage teams: {team.has_organizer_permission('organizer.teams:write')} (must be False)")

# Payment, once, at organizer level — the operator's job, and the shape a real deployment
# has. Every event inherits it, including the ones an agent creates later, which is what
# lets a paid event go live without anybody opening the web UI again. pretix refuses
# `live=true` while a product costs money and no provider is enabled, and it will not let
# an agent near these settings: `event.settings.payment:write` is not in the team above,
# and update_event_settings refuses money-routing keys regardless of permissions.
organizer.plugins = "pretix.plugins.banktransfer"  # hybrid-level plugin: organizer first
organizer.save(update_fields=["plugins"])
organizer.settings.payment_banktransfer_bank_details_type = "sepa"
organizer.settings.payment_banktransfer_bank_details_sepa_name = "Demo Org"
organizer.settings.payment_banktransfer_bank_details_sepa_iban = "AT611904300234573201"
organizer.settings.payment_banktransfer_bank_details_sepa_bic = "BKAUATWW"
organizer.settings.payment_banktransfer_bank_details_sepa_bank = "Demo Bank"
organizer.settings.payment_banktransfer_bank_details = "Demo Org / AT61 1904 3002 3457 3201"
organizer.settings.payment_banktransfer__enabled = True
print(f"payment: banktransfer enabled at organizer level ({organizer.plugins})")

print(f"PRETIX_TOKEN={token.token}")
