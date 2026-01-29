#!/usr/bin/env python3
"""
Monthly Recruiting Metrics: Lever → Slack
Tracks key recruiting metrics and posts a monthly report to Slack.
"""

import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# === CONFIG ===
LEVER_API_KEY = os.environ["LEVER_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL_METRICS"]

# How many days to look back for metrics (default: 30 days)
LOOKBACK_DAYS = 30

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
            "expand": "applications,stage,stageChanges",
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
            "expand": "applications,stageChanges",
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


def get_open_postings():
    """Fetch all published job postings."""
    result = lever_request("postings", {"state": "published", "limit": 100})
    return result.get("data", [])


def calculate_time_to_hire(archived_opportunities, archive_reasons):
    """
    Calculate average time to hire for candidates who were hired.
    Time is measured from when they first entered the interview pipeline
    (Hiring Manager Review stage) to when they were marked as hired.
    """
    hired_times = []
    hired_by_dept = defaultdict(list)
    
    # Get all tracked stage IDs (the interview pipeline)
    tracked_stage_ids = []
    for stage_list in STAGE_GROUPS.values():
        tracked_stage_ids.extend(stage_list)
    
    # Find archive reason IDs that indicate "hired"
    hired_reason_ids = []
    for reason_id, reason_text in archive_reasons.items():
        if "hired" in reason_text.lower():
            hired_reason_ids.append(reason_id)
    
    for opp in archived_opportunities:
        archived_info = opp.get("archived", {})
        reason_id = archived_info.get("reason", "")
        
        if reason_id in hired_reason_ids:
            archived_at = archived_info.get("archivedAt")
            
            # Find when they first entered the interview pipeline
            # Look for the earliest stage change into a tracked stage
            stage_changes = opp.get("stageChanges", [])
            pipeline_entry_time = None
            
            for change in stage_changes:
                to_stage = change.get("toStageId")
                updated_at = change.get("updatedAt")
                
                if to_stage in tracked_stage_ids and updated_at:
                    if pipeline_entry_time is None or updated_at < pipeline_entry_time:
                        pipeline_entry_time = updated_at
            
            if pipeline_entry_time and archived_at:
                days = (archived_at - pipeline_entry_time) / (1000 * 60 * 60 * 24)
                hired_times.append(days)
                
                # Get department from application
                dept = "Unknown"
                apps = opp.get("applications", [])
                if apps:
                    posting = apps[0].get("posting", {})
                    if isinstance(posting, dict):
                        dept = posting.get("categories", {}).get("department", "Unknown")
                
                hired_by_dept[dept].append(days)
    
    avg_overall = sum(hired_times) / len(hired_times) if hired_times else 0
    
    avg_by_dept = {}
    for dept, times in hired_by_dept.items():
        avg_by_dept[dept] = sum(times) / len(times) if times else 0
    
    return {
        "overall": round(avg_overall, 1),
        "by_department": {k: round(v, 1) for k, v in avg_by_dept.items()},
        "total_hires": len(hired_times),
    }


def calculate_stage_conversion_rates(all_opportunities):
    """Calculate conversion rates between stages."""
    # Count candidates who reached each stage group
    reached_stage = defaultdict(int)
    
    for opp in all_opportunities:
        stage_changes = opp.get("stageChanges", [])
        stages_visited = set()
        
        for change in stage_changes:
            stage_id = change.get("toStageId")
            if stage_id and stage_id in STAGE_ID_TO_GROUP:
                stages_visited.add(STAGE_ID_TO_GROUP[stage_id])
        
        # Also count current stage
        current_stage = opp.get("stage")
        if current_stage and current_stage in STAGE_ID_TO_GROUP:
            stages_visited.add(STAGE_ID_TO_GROUP[current_stage])
        
        for stage in stages_visited:
            reached_stage[stage] += 1
    
    # Calculate conversion rates
    conversions = {}
    for i in range(len(STAGE_ORDER) - 1):
        from_stage = STAGE_ORDER[i]
        to_stage = STAGE_ORDER[i + 1]
        
        from_count = reached_stage.get(from_stage, 0)
        to_count = reached_stage.get(to_stage, 0)
        
        if from_count > 0:
            rate = (to_count / from_count) * 100
        else:
            rate = 0
        
        conversions[f"{from_stage} → {to_stage}"] = {
            "from": from_count,
            "to": to_count,
            "rate": round(rate, 1),
        }
    
    return conversions


def calculate_time_in_stage(all_opportunities):
    """Calculate average time spent in each stage."""
    stage_times = defaultdict(list)
    
    for opp in all_opportunities:
        stage_changes = opp.get("stageChanges", [])
        
        # Sort by timestamp
        sorted_changes = sorted(stage_changes, key=lambda x: x.get("updatedAt", 0))
        
        for i, change in enumerate(sorted_changes):
            stage_id = change.get("toStageId")
            entered_at = change.get("updatedAt")
            
            if not stage_id or stage_id not in STAGE_ID_TO_GROUP:
                continue
            
            stage_group = STAGE_ID_TO_GROUP[stage_id]
            
            # Find when they left this stage
            left_at = None
            if i + 1 < len(sorted_changes):
                left_at = sorted_changes[i + 1].get("updatedAt")
            
            if entered_at and left_at:
                days = (left_at - entered_at) / (1000 * 60 * 60 * 24)
                stage_times[stage_group].append(days)
    
    avg_times = {}
    for stage, times in stage_times.items():
        if times:
            avg_times[stage] = round(sum(times) / len(times), 1)
    
    return avg_times


def calculate_offer_acceptance_rate(archived_opportunities, archive_reasons):
    """Calculate offer acceptance rate."""
    offers_extended = 0
    offers_accepted = 0
    
    # Find hired reason IDs
    hired_reason_ids = []
    for reason_id, reason_text in archive_reasons.items():
        if "hired" in reason_text.lower():
            hired_reason_ids.append(reason_id)
    
    for opp in archived_opportunities:
        # Check if they reached offer stage
        stage_changes = opp.get("stageChanges", [])
        reached_offer = False
        
        for change in stage_changes:
            stage_id = change.get("toStageId")
            if stage_id and STAGE_ID_TO_GROUP.get(stage_id) == "Offer":
                reached_offer = True
                break
        
        # Also check current stage
        current_stage = opp.get("stage")
        if current_stage and STAGE_ID_TO_GROUP.get(current_stage) == "Offer":
            reached_offer = True
        
        if reached_offer:
            offers_extended += 1
            
            archived_info = opp.get("archived", {})
            if archived_info.get("reason") in hired_reason_ids:
                offers_accepted += 1
    
    # Also count active candidates with offers
    # (they haven't accepted/declined yet, so don't count in rate)
    
    rate = (offers_accepted / offers_extended * 100) if offers_extended > 0 else 0
    
    return {
        "extended": offers_extended,
        "accepted": offers_accepted,
        "rate": round(rate, 1),
    }


def calculate_source_effectiveness(all_opportunities, archived_opportunities, archive_reasons):
    """Calculate conversion rates by source."""
    source_stats = defaultdict(lambda: {"total": 0, "hired": 0, "in_process": 0})
    
    # Find hired reason IDs
    hired_reason_ids = []
    for reason_id, reason_text in archive_reasons.items():
        if "hired" in reason_text.lower():
            hired_reason_ids.append(reason_id)
    
    # Count active candidates by source
    for opp in all_opportunities:
        sources = opp.get("sources", [])
        source = sources[0] if sources else "Unknown"
        if isinstance(source, str):
            source_stats[source]["total"] += 1
            source_stats[source]["in_process"] += 1
    
    # Count archived candidates by source
    for opp in archived_opportunities:
        sources = opp.get("sources", [])
        source = sources[0] if sources else "Unknown"
        if isinstance(source, str):
            source_stats[source]["total"] += 1
            
            archived_info = opp.get("archived", {})
            if archived_info.get("reason") in hired_reason_ids:
                source_stats[source]["hired"] += 1
    
    # Calculate conversion rates
    results = {}
    for source, stats in source_stats.items():
        if stats["total"] > 0:
            results[source] = {
                "total": stats["total"],
                "hired": stats["hired"],
                "in_process": stats["in_process"],
                "conversion": round((stats["hired"] / stats["total"]) * 100, 1) if stats["total"] > 0 else 0,
            }
    
    # Sort by total candidates
    sorted_results = dict(sorted(results.items(), key=lambda x: x[1]["total"], reverse=True))
    
    return sorted_results


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
    tth_text += f"Overall average: *{tth['overall']} days*\n"
    if tth["by_department"]:
        tth_text += "\nBy department:\n```\n"
        for dept, days in sorted(tth["by_department"].items()):
            tth_text += f"{dept:<25} {days:>5} days\n"
        tth_text += "```"
    
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": tth_text}})
    blocks.append({"type": "divider"})
    
    # 2. Stage Conversion Rates
    conv = metrics["stage_conversions"]
    conv_text = f"*🔄 Stage Conversion Rates*\n```\n"
    for stage_pair, data in conv.items():
        conv_text += f"{stage_pair:<35} {data['rate']:>5}%  ({data['to']}/{data['from']})\n"
    conv_text += "```"
    
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": conv_text}})
    blocks.append({"type": "divider"})
    
    # 3. Time in Stage
    tis = metrics["time_in_stage"]
    if tis:
        tis_text = f"*⏳ Average Time in Stage*\n```\n"
        for stage in STAGE_ORDER:
            if stage in tis:
                tis_text += f"{stage:<25} {tis[stage]:>5} days\n"
        tis_text += "```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": tis_text}})
        blocks.append({"type": "divider"})
    
    # 4. Offer Acceptance Rate
    oar = metrics["offer_acceptance"]
    oar_text = f"*🎯 Offer Acceptance Rate*\n"
    oar_text += f"Rate: *{oar['rate']}%* ({oar['accepted']}/{oar['extended']} offers accepted)"
    
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": oar_text}})
    blocks.append({"type": "divider"})
    
    # 5. Source Effectiveness (top 10)
    sources = metrics["source_effectiveness"]
    source_text = f"*📍 Source Effectiveness (Top 10)*\n```\n"
    source_text += f"{'Source':<25} {'Total':>6} {'Hired':>6} {'Conv%':>6}\n"
    source_text += "-" * 45 + "\n"
    
    for source, data in list(sources.items())[:10]:
        # Truncate long source names
        source_name = source[:24] if len(source) > 24 else source
        source_text += f"{source_name:<25} {data['total']:>6} {data['hired']:>6} {data['conversion']:>5}%\n"
    source_text += "```"
    
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": source_text}})
    
    return {"blocks": blocks}


def post_to_slack(message):
    """Post formatted message to Slack webhook."""
    response = requests.post(SLACK_WEBHOOK_URL, json=message)
    response.raise_for_status()
    print("✅ Posted to Slack successfully")


def main():
    print("Fetching data from Lever...")
    
    since_date = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    
    # Fetch data
    active_opportunities = get_all_opportunities(archived=False)
    print(f"Found {len(active_opportunities)} active candidates")
    
    archived_opportunities = get_archived_opportunities_since(since_date)
    print(f"Found {len(archived_opportunities)} archived candidates in last {LOOKBACK_DAYS} days")
    
    archive_reasons = get_archive_reasons()
    
    # Combine for some metrics
    all_opportunities = active_opportunities + archived_opportunities
    
    # Calculate metrics
    print("Calculating metrics...")
    
    time_to_hire = calculate_time_to_hire(archived_opportunities, archive_reasons)
    print(f"Time to hire: {time_to_hire['overall']} days ({time_to_hire['total_hires']} hires)")
    
    stage_conversions = calculate_stage_conversion_rates(all_opportunities)
    
    time_in_stage = calculate_time_in_stage(all_opportunities)
    
    offer_acceptance = calculate_offer_acceptance_rate(archived_opportunities, archive_reasons)
    print(f"Offer acceptance: {offer_acceptance['rate']}%")
    
    source_effectiveness = calculate_source_effectiveness(active_opportunities, archived_opportunities, archive_reasons)
    
    metrics = {
        "time_to_hire": time_to_hire,
        "stage_conversions": stage_conversions,
        "time_in_stage": time_in_stage,
        "offer_acceptance": offer_acceptance,
        "source_effectiveness": source_effectiveness,
        "total_hires": time_to_hire["total_hires"],
    }
    
    # Format and send
    message = format_slack_message(metrics)
    post_to_slack(message)


if __name__ == "__main__":
    main()
