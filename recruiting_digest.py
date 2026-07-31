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
import calendar
import requests
from datetime import datetime, timedelta, date
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

# === DISPLAY CONFIG ===
# Counts-only by default — candidate names/LinkedIn stay in the private
# #tmp-recruiting-<candidate> channels. Flip True only to list names.
SHOW_CANDIDATE_NAMES = False

HOW_HIRING_WORKS_URL = "https://zaimler.atlassian.net/wiki/spaces/ZCH1/pages/1063288834/How+We+Hire"
ROLE_LIBRARY_URL = "https://zaimler.atlassian.net/wiki/spaces/ZCH1/pages/1063354369/Open+Roles"
REFERRAL_URL = "https://hire.lever.co/referrals/new"

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
# Keys may be either a Lever posting ID or an exact posting title. Posting IDs
# are safer — a title key silently stops applying if anyone renames the role in
# Lever. Swap these for the IDs in the last column of ROLES.md when convenient.
# Delete an entry once the role closes; a key matching nothing warns each run.
P0_DATE_OVERRIDES = {
    "BI Integration Engineer": date(2026, 8, 14),
    "Backend Engineer": date(2026, 8, 24),
    "Cloud Infrastructure Engineer": date(2026, 8, 24),
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
    """Date the search opened, from the Lever posting's createdAt (epoch ms)."""
    created = role.get("created")
    if not created:
        return None
    return date.fromtimestamp(created / 1000)


def find_override(posting_id=None, title=None):
    """Pinned close date for a role, by posting ID or exact title. None if unpinned."""
    for key in (posting_id, title):
        if key and key in P0_DATE_OVERRIDES:
            return P0_DATE_OVERRIDES[key]
    return None


def compute_target(tier, due, today, opened=None, posting_id=None, title=None):
    """Target-close date for a role.

    Precedence: pinned override > due: tag > calendar rule by tier.
    P0 counts P0_MONTHS from `opened` (the day the search opened). If the
    posting has no createdAt, it falls back to `today` and the date rolls —
    same as the old behaviour, but that case is now logged by main().
    """
    pinned = find_override(posting_id, title)
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


def render_bucket(header, recs, today, show_close):
    """recs: list of dicts {label, count, close}. Aligned monospace block."""
    recs.sort(key=lambda r: (r["close"] or date(9999, 1, 1), r["label"]))
    width = max((len(r["label"]) for r in recs), default=0)
    body = header + "\n```\n"
    for r in recs:
        count_str = str(r["count"]) if r["count"] > 0 else "–"
        line = f"{r['label']:<{width}}   {count_str:>1}"
        if show_close and r["close"]:
            line += "   close " + r["close"].strftime("%b %d")
            if r["close"] < today:
                line += "  ⚠ overdue"
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
                            "text": f"*🧩 Open roles ({data['total_open_positions']})*  ·  _count = in active pipeline · close = target fill date_"}})

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
                "close": compute_target(tier, role.get("due"), today_d,
                                        opened=opened_date(role),
                                        posting_id=role.get("id"), title=role.get("title")),
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
    """Prepend a banner so a preview is never mistaken for the real post."""
    banner = {
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (":eyes: *PREVIEW — not yet posted publicly.* This goes to "
                     "the team channel at 4:30 PM PT. Fix Lever tags now if "
                     "anything below looks wrong."),
        }],
    }
    return {**message, "blocks": [banner, {"type": "divider"}, *message["blocks"]]}


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
        f.write("<!-- Live Lever preview generated by DRY_RUN — NOT posted to Slack -->\n\n")
        f.write(text + "\n")
    print(text)
    print("\n[DRY_RUN] wrote PREVIEW.md — nothing posted to Slack")


def write_roles(open_positions_grouped, today_d):
    def fmt(d):
        return d.strftime("%Y-%m-%d") if d else "—"

    lines = [
        "<!-- Live Lever postings from DRY_RUN — source of truth for the Role Library -->",
        "",
        "| Role | Team | Location | Priority | Search started | Target close | Hosted URL | Posting ID |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for group, roles in open_positions_grouped.items():
        for r in roles:
            tier = r.get("tier") or "—"
            opened = opened_date(r)
            close = compute_target(r.get("tier"), r.get("due"), today_d,
                                   opened=opened, posting_id=r.get("id"), title=r.get("title"))
            close_str = fmt(close)
            if close and close < today_d:
                close_str += " (overdue)"
            if find_override(r.get("id"), r.get("title")):
                close_str += " [pinned]"
            started = opened.strftime("%Y-%m-%d") if opened else "(unknown)"
            lines.append(
                f"| {r['title']} | {group} | {r.get('location','')} | {tier} | "
                f"{started} | {close_str} | {r.get('url','') or '(none)'} | {r['id']} |"
            )
    with open("ROLES.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("[DRY_RUN] wrote ROLES.md")


def main():
    print(f"Mode: {'DRY_RUN (posts nowhere)' if DRY_RUN else DIGEST_MODE}")
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
            "created": posting.get("createdAt"),   # epoch ms — when the search opened
            "tags": tags,
            "tier": parse_tier(tags),              # P0/P1/P2 from a Lever tag
            "due": parse_due(tags),                # optional hard deadline
        })

    for group in open_positions_by_dept:
        open_positions_by_dept[group].sort(key=lambda x: x["title"])
    open_positions_grouped = dict(sorted(open_positions_by_dept.items()))

    # Housekeeping warnings — surfaced in the run log, never in the Slack post.
    live_keys = {p.get("id") for p in postings} | {p.get("text") for p in postings}
    for stale in sorted(set(P0_DATE_OVERRIDES) - live_keys):
        print(f"⚠ P0_DATE_OVERRIDES key '{stale}' matches no open posting — "
              f"role closed, or the title was changed in Lever. Its pinned date "
              f"is being ignored. Fix or remove it in recruiting_digest.py.")
    for key in P0_DATE_OVERRIDES:
        dupes = [p for p in postings if p.get("text") == key]
        if len(dupes) > 1:
            print(f"⚠ P0_DATE_OVERRIDES key '{key}' matches {len(dupes)} open "
                  f"postings; all of them get the pinned date. Use posting IDs "
                  f"instead: {', '.join(p.get('id','?') for p in dupes)}")
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
