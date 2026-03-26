#!/usr/bin/env python3
"""
Monthly Recruiting Metrics: Lever → Slack
Tracks key recruiting metrics and posts a monthly report to Slack.

Note: This version does not use stageChanges API (not available in permissions).
Conversion rates are calculated from archived candidate dropoff data.
"""

import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# === CONFIG ===
LEVER_API_KEY = os.environ["LEVER_API_KEY"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL_METRICS", "")
SLACK_RESPONSE_URL = os.environ.get("SLACK_RESPONSE_URL", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

# How many days to look back for metrics
LOOKBACK_DAYS = 30
LOOKBACK_DAYS_LONG = 90

# Archive reason ID for "Hired" in Lever
HIRED_REASON_ID = "7fbd076c-7224-415a-bf96-ebd45b9a70dc"

# Source renaming
SOURCE_RENAME = {
    "Added manually": "Sourced by zaimler",
}

# Stage groups for conversion tracking
STAGE_ORDER = [
    "Hiring Manager Review",
    "Intro",
    "Technical",
    "Onsite",
    "Final Stages",
    "Offer",
]

STAGE_GROUPS = {
    "Hiring Manager Review": ["3b5d887e-0629-4ceb-973a-663952c97b21"],
    "Intro": [
        "94d7f5df-ec0f-4061-b54d-bea369ace17b",
        "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2",
    ],
    "Technical": [
        "160000bb-2cba-40df-b9f0-f69c77cd6175",
        "fae3d918-0118-4f17-b206-f7f29dca3bec",
        "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3",
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5",
    ],
    "Onsite": [
        "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272",
        "cb7dd941-ed9f-4803-9ed5-158681732b65",
    ],
    "Final Stages": [
        "359f9594-ada0-4ca2-bec2-8b3f7eb2106a",
        "d03862a2-e446-4ade-bee6-4b200cf9b399",
    ],
    "Offer": ["offer"],
}

# Flatten stage IDs to group names
STAGE_ID_TO_GROUP = {}
for group, ids in STAGE_GROUPS.items():
    for stage_id in ids:
        STAGE_ID_TO_GROUP[stage_id] = group

# All tracked stage IDs
ALL_TRACKED_STAGE_IDS = set()
for ids in STAGE_GROUPS.values():
    ALL_TRACKED_STAGE_IDS.update(ids)


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


def get_all_opportunities(archived=False):
    """Fetch all opportunities from Lever."""
    opportunities = []
    has_next = True
    offset = None

    while has_next:
        params = {
            "limit": 100,
            "expand": "applications",
        }
        if archived:
            params["archived"] = "true"
        else:
            params["archived"] = "false"

        if offset:
            params["offset"] = offset

        result = lever_request("opportunities", params)
        opportunities.extend(result.get("data", []))

        has_next = result.get("hasNext", False)
        offset = result.get("next")

    return opportunities


def get_archived_opportunities_since(since_date):
    """Fetch opportunities archived since a given date."""
    opportunities = []
    has_next = True
    offset = None
    since_timestamp = int(since_date.timestamp() * 1000)

    while has_next:
        params = {
            "limit": 100,
            "archived": "true",
            "expand": "applications",
            "archived_at_start": since_timestamp,
        }
        if offset:
            params["offset"] = offset

        result = lever_request("opportunities", params)
        opportunities.extend(result.get("data", []))

        has_next = result.get("hasNext", False)
        offset = result.get("next")

    return opportunities


def get_archive_reasons():
    """Fetch archive reasons from Lever."""
    result = lever_request("archive_reasons")
    reasons = result.get("data", [])

    reasons_map = {}
    for reason in reasons:
        reasons_map[reason.get("id")] = reason.get("text", "Unknown")

    return reasons_map


def get_all_postings():
    """Fetch all job postings (published and closed)."""
    postings = []
    has_next = True
    offset = None
    
    # Fetch published postings
    while has_next:
        params = {"state": "published", "limit": 100}
        if offset:
            params["offset"] = offset
        result = lever_request("postings", params)
        postings.extend(result.get("data", []))
        has_next = result.get("hasNext", False)
        offset = result.get("next")
    
    # Also fetch closed postings (for archived candidates)
    has_next = True
    offset = None
    while has_next:
        params = {"state": "closed", "limit": 100}
        if offset:
            params["offset"] = offset
        result = lever_request("postings", params)
        postings.extend(result.get("data", []))
        has_next = result.get("hasNext", False)
        offset = result.get("next")
    
    return postings


def get_stage_group(stage_id):
    """Get the stage group for a stage ID."""
    if stage_id == "offer":
        return "Offer"
    return STAGE_ID_TO_GROUP.get(stage_id)


def calculate_time_to_hire(archived_opportunities):
    """
    Calculate average time to hire for candidates who were hired.
    Uses createdAt → archivedAt since stageChanges isn't available.
    Note: This will be inflated compared to true pipeline entry time.
    """
    hired_times = []

    for opp in archived_opportunities:
        archived_info = opp.get("archived", {})
        reason_id = archived_info.get("reason", "")

        if reason_id == HIRED_REASON_ID:
            created_at = opp.get("createdAt")
            archived_at = archived_info.get("archivedAt")

            if created_at and archived_at:
                days = (archived_at - created_at) / (1000 * 60 * 60 * 24)
                hired_times.append(days)

    avg_overall = sum(hired_times) / len(hired_times) if hired_times else 0

    return {
        "overall": round(avg_overall, 1),
        "total_hires": len(hired_times),
    }


def calculate_stage_conversions(archived_opportunities, lookback_days):
    """
    Calculate conversion rates between stages using archived candidates.
    Works backwards from dropoff data to estimate conversion rates.
    """
    cutoff = datetime.now() - timedelta(days=lookback_days)
    cutoff_ms = cutoff.timestamp() * 1000

    # Filter to lookback period
    recent_archived = [
        opp for opp in archived_opportunities
        if opp.get("archived", {}).get("archivedAt", 0) >= cutoff_ms
    ]

    # Count dropoffs at each stage and hires
    dropped_at = defaultdict(int)
    hired_count = 0

    for opp in recent_archived:
        archived_info = opp.get("archived", {})
        reason_id = archived_info.get("reason", "")

        if reason_id == HIRED_REASON_ID:
            hired_count += 1
        else:
            # Dropped - count at their stage
            stage_id = opp.get("stage")
            stage_group = get_stage_group(stage_id)
            if stage_group:
                dropped_at[stage_group] += 1

    # Work backwards to calculate who entered each stage
    # entered[stage] = dropped_at[stage] + entered[next_stage]
    stage_transitions = [
        ("Intro", "Technical"),
        ("Technical", "Onsite"),
        ("Onsite", "Offer"),  # Combines Final Stages
        ("Offer", "Hired"),
    ]

    # Start from the end
    entered = {}
    entered["Hired"] = hired_count
    entered["Offer"] = dropped_at.get("Offer", 0) + dropped_at.get("Final Stages", 0) + hired_count
    entered["Onsite"] = dropped_at.get("Onsite", 0) + entered["Offer"]
    entered["Technical"] = dropped_at.get("Technical", 0) + entered["Onsite"]
    entered["Intro"] = dropped_at.get("Intro", 0) + entered["Technical"]

    # Calculate conversion rates
    conversions = {}
    for from_stage, to_stage in stage_transitions:
        from_count = entered.get(from_stage, 0)
        to_count = entered.get(to_stage, 0)

        if from_count > 0:
            rate = (to_count / from_count) * 100
            conversions[f"{from_stage} → {to_stage}"] = {
                "rate": round(rate, 0),
                "from": from_count,
                "to": to_count,
            }

    return conversions


def calculate_current_pipeline(active_opportunities):
    """Count active candidates in each tracked stage."""
    pipeline = defaultdict(int)

    for opp in active_opportunities:
        stage_id = opp.get("stage")
        stage_group = get_stage_group(stage_id)
        if stage_group:
            pipeline[stage_group] += 1

    return dict(pipeline)


def calculate_source_effectiveness(active_opportunities, archived_opportunities):
    """
    Calculate conversion rates by source.
    Only counts candidates who made it past HM Review.
    """
    source_stats = defaultdict(lambda: {"total": 0, "hired": 0, "onsite": 0})

    # Stages at Onsite or beyond
    onsite_and_beyond = {"Onsite", "Final Stages", "Offer"}

    # Process active candidates (past HM Review = in tracked pipeline)
    for opp in active_opportunities:
        stage_id = opp.get("stage")
        stage_group = get_stage_group(stage_id)

        # Only count if past HM Review
        if stage_group and stage_group != "Hiring Manager Review":
            sources = opp.get("sources", [])
            source = sources[0] if sources else "Unknown"
            if isinstance(source, str):
                source = SOURCE_RENAME.get(source, source)
                source_stats[source]["total"] += 1

                if stage_group in onsite_and_beyond:
                    source_stats[source]["onsite"] += 1

    # Process archived candidates
    for opp in archived_opportunities:
        stage_id = opp.get("stage")
        stage_group = get_stage_group(stage_id)

        # Only count if past HM Review
        if stage_group and stage_group != "Hiring Manager Review":
            sources = opp.get("sources", [])
            source = sources[0] if sources else "Unknown"
            if isinstance(source, str):
                source = SOURCE_RENAME.get(source, source)
                source_stats[source]["total"] += 1

                archived_info = opp.get("archived", {})
                if archived_info.get("reason") == HIRED_REASON_ID:
                    source_stats[source]["hired"] += 1

                if stage_group in onsite_and_beyond:
                    source_stats[source]["onsite"] += 1

    # Calculate conversion rates and sort
    results = []
    for source, stats in source_stats.items():
        if stats["total"] > 0:
            results.append({
                "source": source,
                "total": stats["total"],
                "hired": stats["hired"],
                "onsite": stats["onsite"],
                "conversion": round((stats["hired"] / stats["total"]) * 100, 1),
            })

    # Sort by conversion rate (descending)
    results.sort(key=lambda x: x["conversion"], reverse=True)

    return results


def calculate_offer_acceptance_by_role(archived_opportunities, postings_map):
    """
    Calculate offer acceptance/rejection by position.
    Tracks candidates who reached Offer stage or Final Stages.
    """
    # Stages that indicate an offer was extended
    offer_stages = {"Offer", "Final Stages"}
    
    role_stats = defaultdict(lambda: {"extended": 0, "accepted": 0, "rejected": 0, "candidates": []})
    
    for opp in archived_opportunities:
        stage_id = opp.get("stage")
        stage_group = get_stage_group(stage_id)
        
        # Only count candidates who reached offer/final stages
        if stage_group not in offer_stages:
            continue
        
        # Get role from posting
        role = "Unknown Role"
        apps = opp.get("applications", [])
        if apps:
            posting = apps[0].get("posting")
            if isinstance(posting, dict):
                role = posting.get("text", "Unknown Role")
            elif isinstance(posting, str):
                # It's a posting ID - look up in map
                role = postings_map.get(posting, posting[:20] + "...")
        
        candidate_name = opp.get("name", "Unknown")
        archived_info = opp.get("archived", {})
        reason_id = archived_info.get("reason", "")
        
        role_stats[role]["extended"] += 1
        
        if reason_id == HIRED_REASON_ID:
            role_stats[role]["accepted"] += 1
            role_stats[role]["candidates"].append({"name": candidate_name, "status": "✅"})
        else:
            role_stats[role]["rejected"] += 1
            role_stats[role]["candidates"].append({"name": candidate_name, "status": "❌"})
    
    # Calculate acceptance rates and format
    results = []
    for role, stats in role_stats.items():
        if stats["extended"] > 0:
            rate = (stats["accepted"] / stats["extended"]) * 100
            results.append({
                "role": role,
                "extended": stats["extended"],
                "accepted": stats["accepted"],
                "rejected": stats["rejected"],
                "rate": round(rate, 0),
                "candidates": stats["candidates"],
            })
    
    # Sort by number of offers extended (descending)
    results.sort(key=lambda x: x["extended"], reverse=True)
    
    return results


def format_slack_message(metrics):
    """Format metrics as a Slack message."""
    today = datetime.now()
    month_name = today.strftime("%B %Y")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Recruiting Metrics — {month_name}",
                "emoji": True
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Last {LOOKBACK_DAYS} days • {metrics['total_hires']} hires"
                }
            ]
        },
        {"type": "divider"},
    ]

    # 1. Time to Hire
    tth = metrics["time_to_hire"]
    tth_text = f"*⏱️ Time to Hire*\n"
    tth_text += f"Average: *{tth['overall']} days* (from application to offer accepted)\n"
    tth_text += f"_Based on {tth['total_hires']} hires_"

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": tth_text}})
    blocks.append({"type": "divider"})

    # 2. Current Pipeline
    pipeline = metrics["pipeline"]
    if pipeline:
        pipe_text = f"*📋 Current Pipeline*\n```\n"
        total = 0
        for stage in STAGE_ORDER:
            count = pipeline.get(stage, 0)
            total += count
            pipe_text += f"{stage:<25} {count:>4}\n"
        pipe_text += f"{'─' * 30}\n"
        pipe_text += f"{'Total':<25} {total:>4}\n"
        pipe_text += "```"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": pipe_text}})
        blocks.append({"type": "divider"})

    # 3. Stage Conversion Rates (30d vs 90d)
    conv_30 = metrics["stage_conversions_30d"]
    conv_90 = metrics["stage_conversions_90d"]

    conv_text = f"*🔄 Stage Conversion Rates*\n```\n"
    conv_text += f"{'Stage':<22} {'30d':>10} {'90d':>12}\n"
    conv_text += "─" * 46 + "\n"

    for stage_pair in conv_30.keys():
        data_30 = conv_30.get(stage_pair, {})
        data_90 = conv_90.get(stage_pair, {})

        rate_30 = f"{int(data_30.get('rate', 0))}%"
        detail_30 = f"({data_30.get('to', 0)}/{data_30.get('from', 0)})"

        rate_90 = f"{int(data_90.get('rate', 0))}%"
        detail_90 = f"({data_90.get('to', 0)}/{data_90.get('from', 0)})"

        conv_text += f"{stage_pair:<22} {rate_30:>4} {detail_30:<6} {rate_90:>4} {detail_90:<6}\n"

    conv_text += "```"

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": conv_text}})
    blocks.append({"type": "divider"})

    # 4. Offer Acceptance by Role
    offers = metrics.get("offer_acceptance_by_role", [])
    if offers:
        offer_text = f"*🎯 Offer Acceptance by Role* _(90 days)_\n```\n"
        offer_text += f"{'Role':<30} {'Rate':>5} {'✅':>3} {'Tot':>4}\n"
        offer_text += "─" * 45 + "\n"
        
        for o in offers:
            role_name = o["role"][:29] if len(o["role"]) > 29 else o["role"]
            rate = f"{int(o['rate'])}%"
            offer_text += f"{role_name:<30} {rate:>4} {o['accepted']:>3} {o['extended']:>4}\n"
        
        # Overall totals
        total_extended = sum(o["extended"] for o in offers)
        total_accepted = sum(o["accepted"] for o in offers)
        overall_rate = (total_accepted / total_extended * 100) if total_extended > 0 else 0
        
        offer_text += "─" * 45 + "\n"
        offer_text += f"{'Overall':<30} {int(overall_rate):>3}% {total_accepted:>3} {total_extended:>4}\n"
        offer_text += "```"
        
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": offer_text}})
        blocks.append({"type": "divider"})

    # 5. Source Effectiveness (top sources with hires or high volume)
    sources = metrics["source_effectiveness"]
    source_text = f"*📍 Source Effectiveness* _(past HM Review)_\n```\n"
    source_text += f"{'Source':<25} {'Conv':>5} {'Hire':>5} {'Ons':>4} {'Tot':>4}\n"
    source_text += "─" * 45 + "\n"

    # Show top 8 sources
    shown = 0
    for s in sources:
        if shown >= 8:
            break
        source_name = s["source"][:24] if len(s["source"]) > 24 else s["source"]
        source_text += f"{source_name:<25} {s['conversion']:>4}% {s['hired']:>5} {s['onsite']:>4} {s['total']:>4}\n"
        shown += 1

    source_text += "```"

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": source_text}})

    return {"blocks": blocks}


def post_to_slack(message):
    """Post formatted message to Slack."""

    if SLACK_RESPONSE_URL:
        # Slash command - use response_url
        message["response_type"] = "in_channel"
        response = requests.post(SLACK_RESPONSE_URL, json=message)
        response.raise_for_status()
        print("✅ Posted to Slack via response_url")

    elif SLACK_CHANNEL_ID and SLACK_BOT_TOKEN:
        # @mention - use Slack API
        payload = {
            "channel": SLACK_CHANNEL_ID,
            "blocks": message.get("blocks", []),
        }
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload
        )
        result = response.json()
        if result.get("ok"):
            print("✅ Posted to Slack via API")
        else:
            print(f"❌ Slack API error: {result.get('error')}")

    elif SLACK_WEBHOOK_URL:
        # Webhook
        response = requests.post(SLACK_WEBHOOK_URL, json=message)
        response.raise_for_status()
        print("✅ Posted to Slack via webhook")

    else:
        print("❌ No Slack destination configured")


def main():
    print("Fetching data from Lever...")

    since_date_30 = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    since_date_90 = datetime.now() - timedelta(days=LOOKBACK_DAYS_LONG)

    # Fetch active opportunities
    active_opportunities = get_all_opportunities(archived=False)
    print(f"Found {len(active_opportunities)} active candidates")

    # Fetch archived opportunities (90 days for comparison)
    archived_opportunities = get_archived_opportunities_since(since_date_90)
    print(f"Found {len(archived_opportunities)} archived candidates in last {LOOKBACK_DAYS_LONG} days")

    # Fetch all postings and build lookup map
    postings = get_all_postings()
    print(f"Found {len(postings)} postings")
    postings_map = {}
    for p in postings:
        postings_map[p.get("id")] = p.get("text", "Unknown Role")

    # Calculate metrics
    print("Calculating metrics...")

    time_to_hire = calculate_time_to_hire(archived_opportunities)
    print(f"Time to hire: {time_to_hire['overall']} days ({time_to_hire['total_hires']} hires)")

    pipeline = calculate_current_pipeline(active_opportunities)

    stage_conversions_30d = calculate_stage_conversions(archived_opportunities, LOOKBACK_DAYS)
    stage_conversions_90d = calculate_stage_conversions(archived_opportunities, LOOKBACK_DAYS_LONG)

    # For source effectiveness, use 30d archived
    archived_30d = [
        opp for opp in archived_opportunities
        if opp.get("archived", {}).get("archivedAt", 0) >= since_date_30.timestamp() * 1000
    ]
    source_effectiveness = calculate_source_effectiveness(active_opportunities, archived_30d)

    # Offer acceptance by role (90 days)
    offer_acceptance_by_role = calculate_offer_acceptance_by_role(archived_opportunities, postings_map)
    
    # Debug: Print offer details
    print("\n=== OFFER DETAILS (for verification) ===")
    for o in offer_acceptance_by_role:
        print(f"\n{o['role']}:")
        for c in o["candidates"]:
            print(f"  {c['status']} {c['name']}")
    print("=========================================\n")

    metrics = {
        "time_to_hire": time_to_hire,
        "pipeline": pipeline,
        "stage_conversions_30d": stage_conversions_30d,
        "stage_conversions_90d": stage_conversions_90d,
        "offer_acceptance_by_role": offer_acceptance_by_role,
        "source_effectiveness": source_effectiveness,
        "total_hires": time_to_hire["total_hires"],
    }

    # Format and send
    message = format_slack_message(metrics)
    post_to_slack(message)


if __name__ == "__main__":
    main()
