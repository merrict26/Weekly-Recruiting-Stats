#!/usr/bin/env python3
"""
Weekly Recruiting Digest: Lever → Slack
Pulls pipeline stats from Lever API and posts a formatted digest to Slack.
"""

import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# === CONFIG ===
LEVER_API_KEY = os.environ["LEVER_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

# === DISPLAY CONFIG ===
# The public digest stays counts-only by default. Candidate names and LinkedIn
# links belong in the private #tmp-recruiting-<candidate> channels, not a
# company-wide post — most candidates haven't told their employer they're
# interviewing. Flip this to True only if you deliberately want names back.
SHOW_CANDIDATE_NAMES = False

# Pinned reference canvases in #recruiting (linked at the bottom of the digest).
HOW_HIRING_WORKS_URL = "https://zaimler.slack.com/docs/TSG5HLXK7/F0BKGTENSBD"
ROLE_LIBRARY_URL = "https://zaimler.slack.com/docs/TSG5HLXK7/F0BKP526WV8"

# Referral link (Lever).
REFERRAL_URL = "https://hire.lever.co/referrals/new"

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

# Onsite stage IDs (for "candidates in onsite stages" metric)
ONSITE_STAGE_IDS = [
    "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272",
    "cb7dd941-ed9f-4803-9ed5-158681732b65",
]

# Offer stage ID
OFFER_STAGE_ID = "offer"

# Final stages IDs (Debrief + Reference check)
FINAL_STAGE_IDS = [
    "359f9594-ada0-4ca2-bec2-8b3f7eb2106a",
    "d03862a2-e446-4ade-bee6-4b200cf9b399",
]

# Location abbreviations
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


def shorten_location(location):
    """Convert full location to short abbreviation."""
    if not location:
        return ""
    return LOCATION_SHORT.get(location, location)


def lever_request(endpoint, params=None):
    """Make authenticated request to Lever API."""
    url = f"https://api.lever.co/v1/{endpoint}"
    response = requests.get(
        url,
        auth=(LEVER_API_KEY, ""),
        params=params or {}
    )
    response.raise_for_status()
    return response.json()


def get_all_opportunities():
    """Fetch all active (non-archived) opportunities from Lever."""
    opportunities = []
    has_next = True
    offset = None

    while has_next:
        params = {
            "limit": 100,
            "archived": "false",  # Only get active candidates
            "expand": "applications",  # Include application data with posting info
        }
        if offset:
            params["offset"] = offset

        result = lever_request("opportunities", params)
        opportunities.extend(result.get("data", []))

        has_next = result.get("hasNext", False)
        offset = result.get("next")

    return opportunities


def get_open_postings():
    """Fetch all published (open) job postings from Lever."""
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
    """Extract current stage ID from opportunity."""
    stage = opportunity.get("stage")
    if stage:
        return stage
    return "Unknown"


def get_posting_title(opportunity):
    """Get the job posting title for an opportunity."""
    applications = opportunity.get("applications", [])
    if applications:
        posting = applications[0].get("posting")
        if posting:
            return posting.get("text", "Unknown Role")
    return "Unknown Role"


def count_by_stage(opportunities):
    """Count candidates in each stage."""
    counts = defaultdict(int)
    for opp in opportunities:
        stage = get_stage_id(opp)
        counts[stage] += 1
    return counts


def count_by_stage_group(stage_counts):
    """Aggregate stage counts into groups."""
    grouped = {}
    for group_name, stages in STAGE_GROUPS.items():
        grouped[group_name] = sum(stage_counts.get(s, 0) for s in stages)
    return grouped


def get_tracked_stage_ids():
    """Flat set of every stage ID that counts as 'in the active pipeline'."""
    ids = []
    for stage_list in STAGE_GROUPS.values():
        ids.extend(stage_list)
    return set(ids)


def get_candidates_added_since(opportunities, since_date):
    """Count candidates added since a given date (only in tracked stages)."""
    tracked_stage_ids = get_tracked_stage_ids()

    count = 0
    for opp in opportunities:
        # Only count if in a tracked stage
        stage = get_stage_id(opp)
        if stage not in tracked_stage_ids:
            continue

        created_at = opp.get("createdAt")
        if created_at:
            created = datetime.fromtimestamp(created_at / 1000)
            if created >= since_date:
                count += 1
    return count


def get_onsites_this_week(opportunities, since_date):
    """Count candidates currently in onsite stages."""
    count = 0
    for opp in opportunities:
        stage = get_stage_id(opp)
        if stage in ONSITE_STAGE_IDS:
            count += 1
    return count


def get_opp_posting_id(opp):
    """Best-effort extraction of the posting ID for an opportunity."""
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
    """Candidates per posting, counting ONLY those in an active pipeline stage.

    The old per-role number counted every non-archived opportunity (including
    hundreds of sourced leads/prospects never in the funnel), which is why it
    swung wildly week to week. Restricting to tracked pipeline stages makes it
    a stable, honest 'how many are actually interviewing for this role' count.
    """
    tracked = get_tracked_stage_ids()
    counts = defaultdict(int)
    for opp in opportunities:
        if get_stage_id(opp) in tracked:
            pid = get_opp_posting_id(opp)
            if pid:
                counts[pid] += 1
    return counts


def get_candidate_details(opportunity, postings_map):
    """Extract candidate name, role, location, and LinkedIn from an opportunity."""
    name = opportunity.get("name", "Unknown")

    # Get LinkedIn URL from links
    linkedin_url = None
    links = opportunity.get("links", [])
    for link in links:
        if isinstance(link, str) and "linkedin.com" in link:
            linkedin_url = link
            break

    # Get role and location from posting
    role = "Unknown Role"
    location = ""
    posting_id = get_opp_posting_id(opportunity)

    if posting_id and posting_id in postings_map:
        posting_info = postings_map[posting_id]
        role = posting_info.get("title", "Unknown Role")
        location = posting_info.get("location", "")

    return {
        "name": name,
        "role": role,
        "location": location,
        "linkedin": linkedin_url,
    }


def get_candidates_in_stages(opportunities, stage_ids, postings_map):
    """Get list of candidates currently in specified stages."""
    candidates = []
    for opp in opportunities:
        stage = get_stage_id(opp)
        if stage in stage_ids:
            candidates.append(get_candidate_details(opp, postings_map))
    return candidates


def build_tldr(data):
    """One-line human summary for the top of the digest."""
    parts = [f"{data['total_active']} in pipeline"]
    offers = data["by_group"].get("Offer", 0)
    if offers:
        parts.append(f"{offers} offer{'s' if offers != 1 else ''} out")
    if data["new_candidates"]:
        parts.append(f"{data['new_candidates']} new this week")
    return " · ".join(parts)


def render_candidate_lines(candidates):
    """Render a bullet list of candidates (only used when names are shown)."""
    text = ""
    for c in candidates:
        location = f" ({c['location']})" if c.get("location") else ""
        if c.get("linkedin"):
            text += f"• <{c['linkedin']}|{c['name']}> — {c['role']}{location}\n"
        else:
            text += f"• {c['name']} — {c['role']}{location}\n"
    return text


def format_slack_message(data):
    """Format the digest as a Slack message with blocks."""
    # Get Monday of the current week
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    week_of = monday.strftime("%B %d, %Y")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 Recruiting Digest — Week of {week_of}",
                "emoji": True
            }
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*TL;DR:* {build_tldr(data)}"}
            ]
        },
    ]

    # What changed this week
    changed_text = f"*🔄 What changed this week*\n• New candidates added: *{data['new_candidates']}*"
    offers = data["by_group"].get("Offer", 0)
    if offers:
        changed_text += f"\n• Offers out: *{offers}*"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": changed_text}
    })

    blocks.append({"type": "divider"})

    # Pipeline snapshot by stage group
    pipeline_text = "*📊 Pipeline snapshot*\n"
    for group_name in STAGE_GROUPS.keys():
        count = data["by_group"].get(group_name, 0)
        emoji = STAGE_EMOJI.get(group_name, "•")
        pipeline_text += f"{emoji} {group_name}: *{count}*\n"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": pipeline_text}
    })

    if offers > 0:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🎯 *{offers} offer(s) currently out!*"
            }
        })

    # Candidate name lists are OFF by default (privacy). Counts above already
    # convey movement; names live in the private per-candidate channels.
    if SHOW_CANDIDATE_NAMES:
        for label, key in [
            ("🏢 Candidates in Onsite", "onsite_candidates"),
            ("📋 Candidates in Final Stages", "final_candidates"),
            ("🎉 Candidates with Offers", "offer_candidates"),
        ]:
            if data.get(key):
                blocks.append({"type": "divider"})
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{label}*"}
                })
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": render_candidate_lines(data[key])}
                })

    blocks.append({"type": "divider"})

    # Open positions — counts-only, per-role ACTIVE-PIPELINE count
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*🧩 Open roles ({data['total_open_positions']})*  ·  _count = candidates in active pipeline_"
        }
    })

    if data["open_positions_grouped"]:
        for group_name, roles in data["open_positions_grouped"].items():
            # Sort roles by active-pipeline count (most to least)
            sorted_roles = sorted(
                roles,
                key=lambda r: data["pipeline_per_role"].get(r["id"], 0),
                reverse=True
            )

            group_text = f"*{group_name}*\n```\n"
            for role in sorted_roles:
                short_loc = shorten_location(role.get("location", ""))
                loc_str = f"({short_loc})" if short_loc else ""
                count = data["pipeline_per_role"].get(role["id"], 0)

                title_with_loc = f"{role['title']} {loc_str}".strip()
                count_str = str(count) if count > 0 else "–"
                group_text += f"{title_with_loc:<45} {count_str:>3}\n"

            group_text += "```"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": group_text}
            })

    blocks.append({"type": "divider"})

    # Pointers to the pinned reference canvases (priority, JDs, process, FAQ)
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"📌 <{HOW_HIRING_WORKS_URL}|How Hiring Works>"
                f"  ·  📚 <{ROLE_LIBRARY_URL}|Role Library> — priority, JDs & interview panels"
            )
        }
    })

    # Refer a candidate button
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "🎯 Refer a Candidate",
                    "emoji": True
                },
                "url": REFERRAL_URL,
                "style": "primary"
            }
        ]
    })

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"Total active candidates: {data['total_active']}  ·  Referral bonus: $10,000 / ₹5 lakh"
            }
        ]
    })

    return {"blocks": blocks}


def post_to_slack(message):
    """Post formatted message to Slack webhook."""
    response = requests.post(SLACK_WEBHOOK_URL, json=message)
    response.raise_for_status()
    print("✅ Posted to Slack successfully")


def main():
    print("Fetching data from Lever...")

    # Get all active opportunities
    opportunities = get_all_opportunities()
    print(f"Found {len(opportunities)} active candidates")

    # Get open postings
    postings = get_open_postings()
    print(f"Found {len(postings)} open positions")

    # Calculate metrics
    one_week_ago = datetime.now() - timedelta(days=7)

    stage_counts = count_by_stage(opportunities)
    grouped_counts = count_by_stage_group(stage_counts)
    new_candidates = get_candidates_added_since(opportunities, one_week_ago)
    onsites = get_onsites_this_week(opportunities, one_week_ago)

    # Build postings map for role lookup (includes title and location)
    postings_map = {}
    for posting in postings:
        postings_map[posting.get("id")] = {
            "title": posting.get("text", "Unknown Role"),
            "location": posting.get("categories", {}).get("location", ""),
        }

    # Detailed candidate lists (only rendered if SHOW_CANDIDATE_NAMES is True)
    onsite_candidates = get_candidates_in_stages(opportunities, ONSITE_STAGE_IDS, postings_map)
    final_candidates = get_candidates_in_stages(opportunities, FINAL_STAGE_IDS, postings_map)
    offer_candidates = get_candidates_in_stages(opportunities, [OFFER_STAGE_ID], postings_map)

    # Per-role counts, restricted to candidates actually in the active pipeline
    pipeline_per_role = count_pipeline_per_role(opportunities)

    # Format postings for display, grouped by department
    open_positions_by_dept = defaultdict(list)
    for posting in postings:
        location = posting.get("categories", {}).get("location", "")
        department = posting.get("categories", {}).get("department", "")
        team = posting.get("categories", {}).get("team", "")

        # Get the job posting URL
        posting_url = posting.get("hostedUrl") or posting.get("urls", {}).get("show", "")

        # Use team if available, otherwise department, otherwise "Other"
        group = team or department or "Other"

        open_positions_by_dept[group].append({
            "id": posting.get("id"),
            "title": posting.get("text", "Unknown Role"),
            "location": location,
            "url": posting_url,
        })

    # Sort positions within each group
    for group in open_positions_by_dept:
        open_positions_by_dept[group].sort(key=lambda x: x["title"])

    # Convert to regular dict and sort groups alphabetically
    open_positions_grouped = dict(sorted(open_positions_by_dept.items()))

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

    # Format and send
    message = format_slack_message(data)
    post_to_slack(message)


if __name__ == "__main__":
    main()
