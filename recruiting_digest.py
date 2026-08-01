#!/usr/bin/env python3
"""
Weekly Recruiting Digest: Lever → Slack
Pulls pipeline stats from Lever API and posts a formatted digest to Slack.

Scalable model — nothing role-specific is hardcoded:
  • Priority tier comes from a Lever TAG on each posting: "P0", "P1", or "P2".
    Untagged roles show up under "Unprioritized" so they get noticed.
  • Target-close dates are computed by calendar rule (see TARGET RULES below),
    so they roll forward on their own — no edits when a month/quarter turns.
  • A hard deadline can override the rule with a tag "due:YYYY-MM-DD".

Run modes:
  DRY_RUN=1             render PREVIEW.md / ROLES.md, post nothing anywhere
  DIGEST_MODE=preview   post to SLACK_PREVIEW_WEBHOOK_URL with a PREVIEW banner
  DIGEST_MODE=publish   post to SLACK_WEBHOOK_URL (the public channel)
Default is preview: publishing requires asking for it explicitly.
"""
import os
import json
import calendar
import requests
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict

# === CONFIG ===
LEVER_API_KEY = os.environ["LEVER_API_KEY"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_PREVIEW_WEBHOOK_URL = os.environ.get("SLACK_PREVIEW_WEBHOOK_URL", "")

# "publish" -> public channel. Anything else -> pvt-recruiting-preview.
# Defaults to preview so a missing or typo'd value can never publish.
DIGEST_MODE = os.environ.get("DIGEST_MODE", "preview")

# When DRY_RUN is set, render to PREVIEW.md / ROLES.md instead of posting to Slack.
DRY_RUN = bool(os.environ.get("DRY_RUN"))

# True only when this run came from a cron schedule, not a manual dispatch.
IS_SCHEDULED = os.environ.get("IS_SCHEDULED", "").lower() == "true"
# If someone published early via the button, the 4:30 cron would post a second
# copy. The scheduled publish checks run history and stands down if a manual
# publish succeeded within this many hours. Manual publishes are never blocked.
SKIP_IF_PUBLISHED_WITHIN_HOURS = 20

# Remembers which postings were published on the previous run, so a role that
# disappears and comes back is detected as a reopen with no human action. Lives
# in the repo and is committed by the workflow. Safe to delete: the next run
# rebuilds it from createdAt, losing only reopen history.
STATE_FILE = os.environ.get("ROLE_STATE_FILE", "role_state.json")

# Bump when the date rules change. Stamped into PREVIEW.md / ROLES.md and the
# run log so you can tell at a glance which version produced an output.
DIGEST_VERSION = "2026-08-01 · auto reopen/promotion tracking"

# === DISPLAY CONFIG ===
# Counts-only by default — candidate names/LinkedIn stay in the private
# #tmp-recruiting-<candidate> channels. Flip True only to list names.
SHOW_CANDIDATE_NAMES = False

HOW_HIRING_WORKS_URL = "https://zaimler.atlassian.net/wiki/spaces/ZCH1/pages/1063288834/How+We+Hire"
ROLE_LIBRARY_URL = "https://zaimler.atlassian.net/wiki/spaces/ZCH1/pages/1063354369/Open+Roles"
REFERRAL_URL = "https://hire.lever.co/referrals/new"
# Where the "Publish now" button in the preview points. Slack incoming webhooks
# are one-way, so a button can only link out — it cannot trigger the run itself.
GITHUB_ACTIONS_URL = ("https://github.com/merrict26/Weekly-Recruiting-Stats"
                      "/actions/workflows/weekly-digest.yml")

# === TARGET RULES ===
# Fiscal year starts in February (Feb→Feb), so fiscal quarters end
# Apr 30 / Jul 31 / Oct 31 / Jan 31.
FISCAL_START_MONTH = 2
# P0 = this many months from the date the SEARCH OPENED (posting createdAt),
# not from today — so the deadline is fixed and can actually go overdue.
# P1 = end of fiscal quarter (with runway).
P0_MONTHS = 1

# Roles that were already open before the open-date rule existed, and so would
# get a nonsense retroactive deadline. An entry here wins over every other rule,
# including a due: tag.
#
# Keys may be any of:
#   "<posting id>"                 — safest, survives renames. Get IDs from ROLES.md.
#   ("<title>", "<short loc>")     — disambiguates two postings sharing a title.
#   "<title>"                      — brittle: silently stops applying on rename,
#                                    and applies to EVERY posting with that title.
# Swap these for posting IDs after the first dry run.
# Delete an entry once the role closes; a key matching nothing warns each run.
P0_DATE_OVERRIDES = {
    # BI Integration Engineer (BLR) — search opened 2026-07-14
    "04ddccc0-34e1-4f30-9591-ff46dd428b89": date(2026, 8, 14),
    # Backend Engineer (BLR) — search opened 2025-11-04
    "31008cbb-9ba9-422b-89d9-5589a345f708": date(2026, 8, 24),
    # Cloud Infrastructure Engineer (San Mateo) — search opened 2025-02-14.
    # NOT the Bengaluru posting of the same title, which is P1.
    "0cac2143-7856-4913-9d6a-445066207c9c": date(2026, 8, 24),
}
# If the current quarter ends within this many days, P1 rolls to next quarter
# (so a freshly-prioritized role isn't handed an unrealistic deadline).
P1_RUNWAY_DAYS = 30

TIER_ORDER = ["P0", "P1", "P2"]
TIER_HEADER = {
    "P0": "*P0 — target close within ~1 month*",
    "P1": "*P1 — target close: end of quarter*",
    "P2": "*P2 — opportunistic (no target)*",
}

# Lever pipeline stages by ID (more reliable than names)
STAGE_GROUPS = {
    "Hiring Manager Review": ["3b5d887e-0629-4ceb-973a-663952c97b21"],
    "Intro": [
        "94d7f5df-ec0f-4061-b54d-bea369ace17b",  # Schedule Intro Call
        "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2",  # Introductory Call
    ],
    "Technical": [
        "160000bb-2cba-40df-b9f0-f69c77cd6175",  # Schedule Technical Interview
        "fae3d918-0118-4f17-b206-f7f29dca3bec",  # Technical Interview
        "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3",  # Schedule Technical Interview #2
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5",  # Technical Interview (#2)
    ],
    "Onsite": [
        "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272",  # Schedule Onsite
        "cb7dd941-ed9f-4803-9ed5-158681732b65",  # Onsite interview
    ],
    "Final Stages": [
        "359f9594-ada0-4ca2-bec2-8b3f7eb2106a",  # Debrief
        "d03862a2-e446-4ade-bee6-4b200cf9b399",  # Reference check
    ],
    "Offer": ["offer"],
}

ONSITE_STAGE_IDS = [
    "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272",
    "cb7dd941-ed9f-4803-9ed5-158681732b65",
]
OFFER_STAGE_ID = "offer"
FINAL_STAGE_IDS = [
    "359f9594-ada0-4ca2-bec2-8b3f7eb2106a",
    "d03862a2-e446-4ade-bee6-4b200cf9b399",
]

LOCATION_SHORT = {
    "San Mateo, CA": "SF",
    "San Francisco, CA": "SF",
    "New York, NY": "NYC",
    "Bengaluru": "BLR",
    "Bangalore": "BLR",
    "Remote": "Remote",
}

STAGE_EMOJI = {
    "Hiring Manager Review": "👀",
    "Intro": "📞",
    "Technical": "💻",
    "Onsite": "🏢",
    "Final Stages": "📋",
    "Offer": "🎉",
}


# ----------------------------------------------------------------------------
# Tag parsing + date rules
# ----------------------------------------------------------------------------
def parse_tier(tags):
    """Return 'P0'/'P1'/'P2' from a posting's Lever tags, or None if untagged."""
    for t in tags or []:
        u = str(t).strip().upper()
        if u in ("P0", "P1", "P2"):
            return u
    return None


def parse_opened(tags):
    """Return the date from an 'opened:YYYY-MM-DD' tag, or None.

    Lever's createdAt is when the posting was CREATED and does not reset when a
    role is closed and reopened. Add this tag on reopen to restart the P0 clock;
    it takes precedence over createdAt. Update it on every subsequent reopen.
    """
    for t in tags or []:
        s = str(t).strip().lower()
        if s.startswith("opened:"):
            try:
                return datetime.strptime(s[7:].strip(), "%Y-%m-%d").date()
            except ValueError:
                pass
    return None


def parse_due(tags):
    """Return a hard deadline date from a 'due:YYYY-MM-DD' tag, or None."""
    for t in tags or []:
        s = str(t).strip().lower()
        if s.startswith("due:"):
            try:
                return datetime.strptime(s[4:].strip(), "%Y-%m-%d").date()
            except ValueError:
                pass
    return None


def add_months(d, months):
    """Return d shifted forward by a whole number of months (clamped day)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def fiscal_quarter_end(d):
    """Last day of the fiscal quarter containing d (fiscal year starts Feb)."""
    m = d.month
    if 2 <= m <= 4:
        return date(d.year, 4, 30)
    if 5 <= m <= 7:
        return date(d.year, 7, 31)
    if 8 <= m <= 10:
        return date(d.year, 10, 31)
    # Nov, Dec -> next Jan 31; Jan -> this Jan 31
    return date(d.year + 1 if m in (11, 12) else d.year, 1, 31)


def next_fiscal_quarter_end(d):
    return fiscal_quarter_end(fiscal_quarter_end(d) + timedelta(days=1))


def opened_date(role):
    """Date the search opened.

    Precedence:
      1. an 'opened:YYYY-MM-DD' tag  — manual correction, always wins
      2. the tracked date in role_state.json — set automatically on reopen
      3. the posting's createdAt — first time we have ever seen this posting
    """
    tagged = parse_opened(role.get("tags"))
    if tagged:
        return tagged
    tracked = role.get("opened_tracked")
    if tracked:
        return tracked
    created = role.get("created")
    if not created:
        return None
    return date.fromtimestamp(created / 1000)


# ----------------------------------------------------------------------------
# Reopen tracking
# ----------------------------------------------------------------------------
def load_state():
    """Read role_state.json. Any problem returns empty state and rebuilds."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        if not isinstance(state.get("postings"), dict):
            raise ValueError("missing 'postings' object")
        return state
    except FileNotFoundError:
        print(f"ℹ {STATE_FILE} not found — seeding reopen tracking from createdAt.")
        return {"version": 1, "postings": {}}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"⚠ {STATE_FILE} unreadable ({e}) — rebuilding from createdAt. "
              f"Reopen history for existing roles is lost.")
        return {"version": 1, "postings": {}}


def update_state(state, postings, today):
    """Fold this run's published postings into state. Returns (state, events).

    `opened` is the P0 clock start, not merely the posting's birthday. It resets
    to today on either of two events:
      • reopen    — a posting recorded as published=False is published again
      • promotion — a posting recorded at P1/P2/untagged is now tagged P0
    A role that was already P0 and stayed P0 keeps its existing date. The first
    ever run seeds every posting from createdAt, so nothing is falsely flagged.

    events is {"reopened": [titles], "promoted": [titles]}.
    """
    known = state.setdefault("postings", {})
    live_ids = {p.get("id") for p in postings}
    events = {"reopened": [], "promoted": []}

    for p in postings:
        pid = p.get("id")
        if not pid:
            continue
        label = p.get("text") or pid
        tier = parse_tier(p.get("tags"))
        created = (date.fromtimestamp(p["createdAt"] / 1000)
                   if p.get("createdAt") else today)
        entry = known.get(pid)

        if entry is None:
            # Never seen before: genuinely new, or this is the first ever run.
            known[pid] = {"title": label, "opened": created.isoformat(),
                          "tier": tier, "reason": "created",
                          "published": True, "last_seen": today.isoformat()}
            continue

        reasons = []
        if not entry.get("published", True):
            reasons.append("reopened")
            events["reopened"].append(label)
        # "tier" may be absent on state written before promotions were tracked;
        # treat that as "unknown" and do not fire a promotion on the first pass.
        prior_tier = entry.get("tier", tier)
        if tier == "P0" and prior_tier != "P0":
            reasons.append("promoted")
            events["promoted"].append(label)

        if reasons:
            entry["opened"] = today.isoformat()
            entry["reason"] = "+".join(reasons)

        entry.update({"title": label, "tier": tier, "published": True,
                      "last_seen": today.isoformat()})

    for pid, entry in known.items():
        if pid not in live_ids:
            entry["published"] = False

    state["version"] = 2
    state["updated"] = today.isoformat()
    return state, events


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as e:
        print(f"⚠ Could not write {STATE_FILE} ({e}). Reopen tracking will "
              f"restart from createdAt next run.")


def tracked_open_date(state, posting_id):
    entry = state.get("postings", {}).get(posting_id) or {}
    raw = entry.get("opened")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def tracked_reason(state, posting_id):
    """Why the clock currently starts where it does: created/reopened/promoted."""
    return (state.get("postings", {}).get(posting_id) or {}).get("reason", "created")


def find_override(posting_id=None, title=None, location=""):
    """Pinned close date for a role. Most specific key wins.

    Order: posting ID > (title, short location) > bare title.
    """
    keys = [posting_id]
    if title:
        keys.append((title, shorten_location(location)))
        keys.append(title)
    for key in keys:
        if key and key in P0_DATE_OVERRIDES:
            return P0_DATE_OVERRIDES[key]
    return None


def compute_target(tier, due, today, opened=None, posting_id=None, title=None, location=""):
    """Target-close date for a role.

    Precedence: pinned override > due: tag > calendar rule by tier.
    P0 counts P0_MONTHS from `opened` (the day the search opened). If the
    posting has no createdAt, it falls back to `today` and the date rolls —
    same as the old behaviour, but that case is now logged by main().
    """
    pinned = find_override(posting_id, title, location)
    if pinned:
        return pinned
    if due:
        return due
    if tier == "P0":
        return add_months(opened or today, P0_MONTHS)
    if tier == "P1":
        qend = fiscal_quarter_end(today)
        if (qend - today).days < P1_RUNWAY_DAYS:
            return next_fiscal_quarter_end(today)
        return qend
    return None  # P2 or unprioritized


# ----------------------------------------------------------------------------
# Lever API
# ----------------------------------------------------------------------------
def shorten_location(location):
    if not location:
        return ""
    return LOCATION_SHORT.get(location, location)


def lever_request(endpoint, params=None):
    url = f"https://api.lever.co/v1/{endpoint}"
    response = requests.get(url, auth=(LEVER_API_KEY, ""), params=params or {})
    response.raise_for_status()
    return response.json()


def get_all_opportunities():
    opportunities = []
    has_next = True
    offset = None
    while has_next:
        params = {"limit": 100, "archived": "false", "expand": "applications"}
        if offset:
            params["offset"] = offset
        result = lever_request("opportunities", params)
        opportunities.extend(result.get("data", []))
        has_next = result.get("hasNext", False)
        offset = result.get("next")
    return opportunities


def get_open_postings():
    postings = []
    has_next = True
    offset = None
    while has_next:
        params = {"state": "published", "limit": 100}
        if offset:
            params["offset"] = offset
        result = lever_request("postings", params)
        postings.extend(result.get("data", []))
        has_next = result.get("hasNext", False)
        offset = result.get("next")
    return postings


def get_stage_id(opportunity):
    stage = opportunity.get("stage")
    return stage if stage else "Unknown"


def count_by_stage(opportunities):
    counts = defaultdict(int)
    for opp in opportunities:
        counts[get_stage_id(opp)] += 1
    return counts


def count_by_stage_group(stage_counts):
    grouped = {}
    for group_name, stages in STAGE_GROUPS.items():
        grouped[group_name] = sum(stage_counts.get(s, 0) for s in stages)
    return grouped


def get_tracked_stage_ids():
    ids = []
    for stage_list in STAGE_GROUPS.values():
        ids.extend(stage_list)
    return set(ids)


def get_candidates_added_since(opportunities, since_date):
    tracked = get_tracked_stage_ids()
    count = 0
    for opp in opportunities:
        if get_stage_id(opp) not in tracked:
            continue
        created_at = opp.get("createdAt")
        if created_at and datetime.fromtimestamp(created_at / 1000) >= since_date:
            count += 1
    return count


def get_onsites_this_week(opportunities, since_date):
    # NOTE: since_date is currently unused — this counts everyone sitting in an
    # onsite stage right now, not onsites scheduled this week. The result is not
    # rendered anywhere today. Left as-is deliberately; see the notes in chat.
    return sum(1 for opp in opportunities if get_stage_id(opp) in ONSITE_STAGE_IDS)


def get_opp_posting_id(opp):
    posting_id = opp.get("posting")
    if not posting_id:
        applications = opp.get("applications", [])
        if applications:
            first_app = applications[0]
            if isinstance(first_app, dict):
                posting_data = first_app.get("posting")
                if isinstance(posting_data, dict):
                    posting_id = posting_data.get("id")
                elif isinstance(posting_data, str):
                    posting_id = posting_data
                if not posting_id:
                    posting_id = first_app.get("postingId")
    return posting_id


def count_pipeline_per_role(opportunities):
    """Candidates per posting, ONLY those in a tracked pipeline stage (stable count)."""
    tracked = get_tracked_stage_ids()
    counts = defaultdict(int)
    for opp in opportunities:
        if get_stage_id(opp) in tracked:
            pid = get_opp_posting_id(opp)
            if pid:
                counts[pid] += 1
    return counts


def count_offers_per_role(opportunities):
    """Candidates sitting at the offer stage, per posting.

    A subset of count_pipeline_per_role — the offer stage is itself a tracked
    stage, so these candidates are counted in that role's pipeline number too.
    """
    counts = defaultdict(int)
    for opp in opportunities:
        if get_stage_id(opp) == OFFER_STAGE_ID:
            pid = get_opp_posting_id(opp)
            if pid:
                counts[pid] += 1
    return counts


def get_candidate_details(opportunity, postings_map):
    name = opportunity.get("name", "Unknown")
    linkedin_url = None
    for link in opportunity.get("links", []):
        if isinstance(link, str) and "linkedin.com" in link:
            linkedin_url = link
            break
    role, location = "Unknown Role", ""
    posting_id = get_opp_posting_id(opportunity)
    if posting_id and posting_id in postings_map:
        role = postings_map[posting_id].get("title", "Unknown Role")
        location = postings_map[posting_id].get("location", "")
    return {"name": name, "role": role, "location": location, "linkedin": linkedin_url}


def get_candidates_in_stages(opportunities, stage_ids, postings_map):
    return [get_candidate_details(o, postings_map)
            for o in opportunities if get_stage_id(o) in stage_ids]


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------
def build_tldr(data):
    parts = [f"{data['total_active']} in pipeline"]
    offers = data["by_group"].get("Offer", 0)
    if offers:
        parts.append(f"{offers} offer{'s' if offers != 1 else ''} out")
    if data["new_candidates"]:
        parts.append(f"{data['new_candidates']} new this week")
    return " · ".join(parts)


def render_candidate_lines(candidates):
    text = ""
    for c in candidates:
        location = f" ({c['location']})" if c.get("location") else ""
        if c.get("linkedin"):
            text += f"• <{c['linkedin']}|{c['name']}> — {c['role']}{location}\n"
        else:
            text += f"• {c['name']} — {c['role']}{location}\n"
    return text


def format_close(d, today):
    """'Aug 14' for this year, 'Sep 26, 2025' otherwise.

    Without the year a stale date reads as a future one — 'Sep 26' looked like
    next month when it was actually last year, and overdue.
    """
    return d.strftime("%b %d") if d.year == today.year else d.strftime("%b %d, %Y")


def render_bucket(header, recs, today, show_close):
    """recs: list of dicts {label, count, close}. Aligned monospace block."""
    recs.sort(key=lambda r: (r["close"] or date(9999, 1, 1), r["label"]))
    width = max((len(r["label"]) for r in recs), default=0)
    counts = [str(r["count"]) if r["count"] > 0 else "–" for r in recs]
    # Right-align on the widest count, or a two-digit pipeline shifts the row.
    cwidth = max((len(c) for c in counts), default=1)
    body = header + "\n```\n"
    for r, count_str in zip(recs, counts):
        line = f"{r['label']:<{width}}   {count_str:>{cwidth}}"
        if show_close and r["close"]:
            line += "   close " + format_close(r["close"], today)
            if r["close"] < today:
                line += "  ⚠ overdue"
        offers = r.get("offers", 0)
        if offers:
            line += f"   {offers} offer{'s' if offers != 1 else ''} out"
        body += line + "\n"
    body += "```"
    return body


def format_slack_message(data):
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    week_of = monday.strftime("%B %d, %Y")
    today_d = today.date()

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"📋 Recruiting Digest — Week of {week_of}", "emoji": True}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"*TL;DR:* {build_tldr(data)}"}]},
    ]

    changed_text = f"*🔄 What changed this week*\n• New candidates added: *{data['new_candidates']}*"
    offers = data["by_group"].get("Offer", 0)
    if offers:
        changed_text += f"\n• Offers out: *{offers}*"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": changed_text}})

    blocks.append({"type": "divider"})

    pipeline_text = "*📊 Pipeline snapshot*\n"
    for group_name in STAGE_GROUPS.keys():
        pipeline_text += f"{STAGE_EMOJI.get(group_name, '•')} {group_name}: *{data['by_group'].get(group_name, 0)}*\n"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": pipeline_text}})

    if offers > 0:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"🎯 *{offers} offer(s) currently out!*"}})

    if SHOW_CANDIDATE_NAMES:
        for label, key in [("🏢 Candidates in Onsite", "onsite_candidates"),
                           ("📋 Candidates in Final Stages", "final_candidates"),
                           ("🎉 Candidates with Offers", "offer_candidates")]:
            if data.get(key):
                blocks.append({"type": "divider"})
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{label}*"}})
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": render_candidate_lines(data[key])}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn",
                            "text": f"*🧩 Open roles ({data['total_open_positions']})*  ·  _count = in active pipeline (includes offers) · close = target fill date_"}})

    # Bucket by tier (from Lever tags); untagged -> Unprioritized.
    buckets = {t: [] for t in TIER_ORDER}
    other = []
    for group_name, roles in data["open_positions_grouped"].items():
        for role in roles:
            tier = role.get("tier")
            loc = shorten_location(role.get("location", ""))
            rec = {
                "label": f"{role['title']} ({loc})" if loc else role["title"],
                "count": data["pipeline_per_role"].get(role["id"], 0),
                "offers": data["offers_per_role"].get(role["id"], 0),
                "close": compute_target(tier, role.get("due"), today_d,
                                        opened=opened_date(role), posting_id=role.get("id"),
                                        title=role.get("title"), location=role.get("location", "")),
            }
            (buckets[tier] if tier in buckets else other).append(rec)

    for t in TIER_ORDER:
        if buckets[t]:
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn",
                                    "text": render_bucket(TIER_HEADER[t], buckets[t], today_d, show_close=(t != "P2"))}})

    if other:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": render_bucket("*Unprioritized — add a P0/P1/P2 tag in Lever*", other, today_d, show_close=False)}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn",
                            "text": (f"📌 <{HOW_HIRING_WORKS_URL}|How We Hire>"
                                     f"  ·  📚 <{ROLE_LIBRARY_URL}|Open Roles> — priority, JDs & interview guides")}})
    blocks.append({"type": "actions",
                   "elements": [{"type": "button",
                                 "text": {"type": "plain_text", "text": "🎯 Refer a Candidate", "emoji": True},
                                 "url": REFERRAL_URL, "style": "primary"}]})
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn",
                                 "text": f"Total active candidates: {data['total_active']}  ·  Referral bonus: $10,000 / ₹5 lakh"}]})

    return {"blocks": blocks}


def add_preview_banner(message):
    """Prepend a banner + publish button so a preview is never mistaken for the post."""
    banner = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (":eyes: *PREVIEW — not yet posted publicly.*\n"
                     "This posts to the team channel automatically at 4:30 PM PT. "
                     "Fix Lever tags now if anything below looks wrong, or publish "
                     "early with the button → *Run workflow* → mode `publish`."),
        },
    }
    publish = {
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "🚀 Publish now", "emoji": True},
            "url": GITHUB_ACTIONS_URL,
            "style": "primary",
        }],
    }
    return {**message, "blocks": [banner, publish, {"type": "divider"}, *message["blocks"]]}


def post_to_slack(message):
    if DIGEST_MODE == "publish":
        webhook, label = SLACK_WEBHOOK_URL, "public channel"
    else:
        # Fail loudly rather than quietly falling back to the public webhook.
        if not SLACK_PREVIEW_WEBHOOK_URL:
            raise SystemExit(
                "DIGEST_MODE=preview but SLACK_PREVIEW_WEBHOOK_URL is unset. "
                "Refusing to send a preview to the public webhook."
            )
        webhook, label = SLACK_PREVIEW_WEBHOOK_URL, "pvt-recruiting-preview"
        message = add_preview_banner(message)

    if not webhook:
        raise SystemExit(f"No webhook configured for DIGEST_MODE={DIGEST_MODE}")

    response = requests.post(webhook, json=message)
    response.raise_for_status()
    print(f"✅ Posted to Slack successfully ({label}, mode={DIGEST_MODE})")


def recent_manual_publish():
    """Timestamp of a recent successful manual publish, or None.

    Reads this workflow's own run history via the GitHub API. Needs
    `permissions: actions: read` and the run-name set in weekly-digest.yml.
    Any failure returns None — the digest posts rather than silently skipping.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    this_run = os.environ.get("GITHUB_RUN_ID")
    # "owner/repo/.github/workflows/weekly-digest.yml@refs/heads/main"
    wf_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    wf_file = wf_ref.split("@")[0].split("/")[-1] if wf_ref else ""
    if not (token and repo and wf_file):
        print("ℹ No GitHub API context; skipping the duplicate-publish check.")
        return None

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf_file}/runs"
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            params={"event": "workflow_dispatch", "status": "success", "per_page": 30},
            timeout=20,
        )
        r.raise_for_status()
        runs = r.json().get("workflow_runs", [])
    except Exception as e:                      # noqa: BLE001 — never block the post
        print(f"⚠ Duplicate-publish check failed ({e}); posting anyway.")
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=SKIP_IF_PUBLISHED_WITHIN_HOURS)
    for run in runs:
        if str(run.get("id")) == str(this_run):
            continue
        # run-name in the workflow embeds the mode, e.g. "Recruiting digest [publish]"
        if "[publish]" not in (run.get("display_title") or run.get("name") or ""):
            continue
        finished = run.get("updated_at")
        if not finished:
            continue
        when = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if when >= cutoff:
            return when
    return None


def write_preview(message):
    lines = []
    for b in message["blocks"]:
        t = b["type"]
        if t == "header":
            lines.append("## " + b["text"]["text"])
        elif t == "context":
            lines.append("_" + " ".join(e["text"] for e in b["elements"]) + "_")
        elif t == "section":
            lines.append(b["text"]["text"])
        elif t == "divider":
            lines.append("---")
        elif t == "actions":
            lines.append("**[ " + b["elements"][0]["text"]["text"] + " ]**")
    text = "\n\n".join(lines)
    with open("PREVIEW.md", "w") as f:
        f.write("<!-- Live Lever preview generated by DRY_RUN — NOT posted to Slack -->\n")
        f.write(f"<!-- digest version: {DIGEST_VERSION} -->\n\n")
        f.write(text + "\n")
    print(text)
    print("\n[DRY_RUN] wrote PREVIEW.md — nothing posted to Slack")


def write_roles(open_positions_grouped, today_d):
    def fmt(d):
        return d.strftime("%Y-%m-%d") if d else "—"

    lines = [
        "<!-- Live Lever postings from DRY_RUN — source of truth for the Role Library -->",
        f"<!-- digest version: {DIGEST_VERSION} -->",
        "",
        "| Role | Team | Location | Priority | Search started | Target close | Hosted URL | Posting ID |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for group, roles in open_positions_grouped.items():
        for r in roles:
            tier = r.get("tier") or "—"
            opened = opened_date(r)
            close = compute_target(r.get("tier"), r.get("due"), today_d,
                                   opened=opened, posting_id=r.get("id"),
                                   title=r.get("title"), location=r.get("location", ""))
            close_str = fmt(close)
            if close and close < today_d:
                close_str += " (overdue)"
            if find_override(r.get("id"), r.get("title"), r.get("location", "")):
                close_str += " [pinned]"
            started = opened.strftime("%Y-%m-%d") if opened else "(unknown)"
            created = (date.fromtimestamp(r["created"] / 1000) if r.get("created") else None)
            if opened and created and opened != created:
                if parse_opened(r.get("tags")):
                    started += " (tagged)"
                else:
                    started += f" ({r.get('opened_reason', 'reset')})"
            lines.append(
                f"| {r['title']} | {group} | {r.get('location','')} | {tier} | "
                f"{started} | {close_str} | {r.get('url','') or '(none)'} | {r['id']} |"
            )
    with open("ROLES.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("[DRY_RUN] wrote ROLES.md")


def main():
    print(f"recruiting_digest version: {DIGEST_VERSION}")
    print(f"Mode: {'DRY_RUN (posts nowhere)' if DRY_RUN else DIGEST_MODE}")

    # Stand down if someone already published early via the button.
    if not DRY_RUN and DIGEST_MODE == "publish" and IS_SCHEDULED:
        already = recent_manual_publish()
        if already:
            print(f"✋ Already published manually at {already:%Y-%m-%d %H:%M UTC} "
                  f"(within {SKIP_IF_PUBLISHED_WITHIN_HOURS}h). Skipping the "
                  f"scheduled post so the channel doesn't get it twice.")
            return

    print("Fetching data from Lever...")
    opportunities = get_all_opportunities()
    print(f"Found {len(opportunities)} active candidates")

    postings = get_open_postings()
    print(f"Found {len(postings)} open positions")

    one_week_ago = datetime.now() - timedelta(days=7)
    today_d = datetime.now().date()

    stage_counts = count_by_stage(opportunities)
    grouped_counts = count_by_stage_group(stage_counts)
    new_candidates = get_candidates_added_since(opportunities, one_week_ago)
    onsites = get_onsites_this_week(opportunities, one_week_ago)

    postings_map = {}
    for posting in postings:
        postings_map[posting.get("id")] = {
            "title": posting.get("text", "Unknown Role"),
            "location": posting.get("categories", {}).get("location", ""),
        }

    onsite_candidates = get_candidates_in_stages(opportunities, ONSITE_STAGE_IDS, postings_map)
    final_candidates = get_candidates_in_stages(opportunities, FINAL_STAGE_IDS, postings_map)
    offer_candidates = get_candidates_in_stages(opportunities, [OFFER_STAGE_ID], postings_map)
    pipeline_per_role = count_pipeline_per_role(opportunities)
    offers_per_role = count_offers_per_role(opportunities)

    # Detect reopens before building the role list, so dates reflect this run.
    state = load_state()
    state, events = update_state(state, postings, today_d)
    if events["reopened"]:
        print(f"🔁 Reopened since last run, clock restarted today: "
              f"{', '.join(sorted(events['reopened']))}")
    if events["promoted"]:
        print(f"⬆ Promoted to P0 since last run, clock restarted today: "
              f"{', '.join(sorted(events['promoted']))}")
    if DRY_RUN:
        print(f"[DRY_RUN] not writing {STATE_FILE}")
    else:
        save_state(state)

    open_positions_by_dept = defaultdict(list)
    for posting in postings:
        location = posting.get("categories", {}).get("location", "")
        department = posting.get("categories", {}).get("department", "")
        team = posting.get("categories", {}).get("team", "")
        posting_url = posting.get("hostedUrl") or posting.get("urls", {}).get("show", "")
        group = team or department or "Other"
        tags = posting.get("tags", []) or []
        open_positions_by_dept[group].append({
            "id": posting.get("id"),
            "title": posting.get("text", "Unknown Role"),
            "location": location,
            "url": posting_url,
            "created": posting.get("createdAt"),   # epoch ms — posting creation
            "opened_tracked": tracked_open_date(state, posting.get("id")),
            "opened_reason": tracked_reason(state, posting.get("id")),
            "tags": tags,
            "tier": parse_tier(tags),              # P0/P1/P2 from a Lever tag
            "due": parse_due(tags),                # optional hard deadline
        })

    for group in open_positions_by_dept:
        open_positions_by_dept[group].sort(key=lambda x: x["title"])
    open_positions_grouped = dict(sorted(open_positions_by_dept.items()))

    # Housekeeping warnings — surfaced in the run log, never in the Slack post.
    live_keys = set()
    for p in postings:
        loc = shorten_location(p.get("categories", {}).get("location", ""))
        live_keys.update({p.get("id"), p.get("text"), (p.get("text"), loc)})
    # key=str: the dict mixes plain strings and (title, loc) tuples, which are
    # not orderable against each other.
    for stale in sorted((k for k in P0_DATE_OVERRIDES if k not in live_keys), key=str):
        print(f"⚠ P0_DATE_OVERRIDES key {stale!r} matches no open posting — "
              f"role closed, or the title/location changed in Lever. Its pinned "
              f"date is being ignored. Fix or remove it in recruiting_digest.py.")
    for key in P0_DATE_OVERRIDES:
        if not isinstance(key, str):
            continue  # (title, loc) tuples are already unambiguous
        dupes = [p for p in postings if p.get("text") == key]
        if len(dupes) > 1:
            print(f"⚠ P0_DATE_OVERRIDES key '{key}' matches {len(dupes)} open "
                  f"postings; all of them get the pinned date. Use a posting ID "
                  f"or (title, location): {', '.join(p.get('id','?') for p in dupes)}")
    # A pin bypasses the tier rule entirely, so a pinned role that is no longer
    # P0 still gets its P0-style date. Flag it rather than silently honouring it.
    for roles in open_positions_grouped.values():
        for r in roles:
            if r["tier"] != "P0" and find_override(r.get("id"), r.get("title"), r.get("location", "")):
                print(f"⚠ '{r['title']}' is pinned in P0_DATE_OVERRIDES but is "
                      f"tagged {r['tier'] or 'untagged'} in Lever. The pinned date "
                      f"wins. Remove the pin or retag the role.")
    # A mistyped date tag parses to None and silently falls back to the default
    # rule, which looks like the tag being ignored. Say so out loud.
    for roles in open_positions_grouped.values():
        for r in roles:
            for tag in r.get("tags") or []:
                s = str(tag).strip().lower()
                for prefix, parser in (("opened:", parse_opened), ("due:", parse_due)):
                    if s.startswith(prefix) and parser([tag]) is None:
                        print(f"⚠ '{r['title']}' has tag '{tag}' but the date could "
                              f"not be read. Expected {prefix}YYYY-MM-DD. Ignoring it.")

    # A P0 whose clock has never been observed to reset, and whose target is
    # already past, is almost always a role promoted to P0 before tracking
    # existed — the date is its posting birthday, not its promotion date.
    for roles in open_positions_grouped.values():
        for r in roles:
            if r["tier"] != "P0":
                continue
            if find_override(r.get("id"), r.get("title"), r.get("location", "")):
                continue
            if parse_opened(r.get("tags")) or r.get("opened_reason") != "created":
                continue
            close = compute_target(r["tier"], r.get("due"), today_d,
                                   opened=opened_date(r), posting_id=r.get("id"),
                                   title=r.get("title"), location=r.get("location", ""))
            if close and close < today_d:
                print(f"⚠ '{r['title']}' is P0 with a target of {close}, already "
                      f"past. That date came from the posting's creation date "
                      f"({opened_date(r)}), so this role was likely promoted to P0 "
                      f"before tracking saw it. Add tag 'opened:YYYY-MM-DD' in "
                      f"Lever with the promotion date to restart the clock.")

    for roles in open_positions_grouped.values():
        for r in roles:
            if r["tier"] == "P0" and not r.get("created") and r["id"] not in P0_DATE_OVERRIDES:
                print(f"⚠ P0 role '{r['title']}' has no createdAt; its target "
                      f"date will roll forward each week instead of holding.")

    data = {
        "new_candidates": new_candidates,
        "onsites": onsites,
        "by_group": grouped_counts,
        "total_active": sum(grouped_counts.values()),
        "open_positions_grouped": open_positions_grouped,
        "total_open_positions": len(postings),
        "pipeline_per_role": pipeline_per_role,
        "offers_per_role": offers_per_role,
        "onsite_candidates": onsite_candidates,
        "final_candidates": final_candidates,
        "offer_candidates": offer_candidates,
    }

    message = format_slack_message(data)

    if DRY_RUN:
        write_preview(message)
        write_roles(open_positions_grouped, today_d)
    else:
        post_to_slack(message)


if __name__ == "__main__":
    main()
