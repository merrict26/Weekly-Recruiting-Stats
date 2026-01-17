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

# Lever pipeline stages (update if yours differ)
STAGE_GROUPS = {
    "Top of Funnel": ["Schedule Intro Call", "Introductory Call"],
    "Technical": ["Schedule Technical Interview", "Technical Interview", 
                  "Schedule Technical Interview #2", "Technical Interview (#2)"],
    "Onsite": ["Schedule Onsite", "Onsite Interview"],
    "Final Stages": ["Debrief", "Reference Check"],
    "Offer": ["Offer"],
}

# All stages flattened (for lookups)
ALL_STAGES = [stage for stages in STAGE_GROUPS.values() for stage in stages]


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
    """Fetch all active opportunities (candidates) from Lever."""
    opportunities = []
    has_next = True
    offset = None
    
    while has_next:
        params = {"limit": 100}
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


def get_stage_name(opportunity):
    """Extract current stage name from opportunity."""
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
        stage = get_stage_name(opp)
        counts[stage] += 1
    return counts


def count_by_stage_group(stage_counts):
    """Aggregate stage counts into groups."""
    grouped = {}
    for group_name, stages in STAGE_GROUPS.items():
        grouped[group_name] = sum(stage_counts.get(s, 0) for s in stages)
    return grouped


def get_candidates_added_since(opportunities, since_date):
    """Count candidates added since a given date."""
    count = 0
    for opp in opportunities:
        created_at = opp.get("createdAt")
        if created_at:
            created = datetime.fromtimestamp(created_at / 1000)
            if created >= since_date:
                count += 1
    return count


def get_interviews_this_week(since_date):
    """
    Fetch interview count from Lever.
    Note: This uses the interviews endpoint which requires iterating through opportunities.
    For simplicity, we'll estimate based on stage movements.
    """
    # Lever's API doesn't have a simple "interviews completed" endpoint
    # You could integrate with your calendar or use stage change timestamps
    # For now, return None and we'll skip this metric or add it manually
    return None


def get_onsites_this_week(opportunities, since_date):
    """Count candidates currently in onsite stages."""
    onsite_stages = STAGE_GROUPS.get("Onsite", [])
    count = 0
    for opp in opportunities:
        stage = get_stage_name(opp)
        if stage in onsite_stages:
            count += 1
    return count


def format_slack_message(data):
    """Format the digest as a Slack message with blocks."""
    week_of = datetime.now().strftime("%B %d, %Y")
    
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Recruiting Digest — Week of {week_of}",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*This Week's Activity*\n• New candidates added: *{data['new_candidates']}*\n• Candidates in onsite stages: *{data['onsites']}*"
            }
        },
        {
            "type": "divider"
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Pipeline Snapshot*"
            }
        },
    ]
    
    # Pipeline by stage group
    pipeline_text = ""
    for group_name in STAGE_GROUPS.keys():
        count = data["by_group"].get(group_name, 0)
        emoji = {
            "Top of Funnel": "🔝",
            "Technical": "💻",
            "Onsite": "🏢",
            "Final Stages": "📋",
            "Offer": "🎉"
        }.get(group_name, "•")
        pipeline_text += f"{emoji} {group_name}: *{count}*\n"
    
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn", 
            "text": pipeline_text
        }
    })
    
    # Offers out (highlight if any)
    if data["by_group"].get("Offer", 0) > 0:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🎯 *{data['by_group']['Offer']} offer(s) currently out!*"
            }
        })
    
    blocks.append({
        "type": "divider"
    })
    
    # Open positions section
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Open Positions ({len(data['open_positions'])})*"
        }
    })
    
    # List open roles (with candidate counts if available)
    if data["open_positions"]:
        roles_text = ""
        for role in data["open_positions"]:
            candidate_count = data["candidates_per_role"].get(role["id"], 0)
            if candidate_count > 0:
                roles_text += f"• {role['title']} — _{candidate_count} candidates_\n"
            else:
                roles_text += f"• {role['title']}\n"
        
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": roles_text
            }
        })
    
    blocks.append({
        "type": "divider"
    })
    
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"Total active candidates: {data['total_active']}"
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
    
    # Count candidates per role (by posting ID)
    # Note: Each opportunity may have multiple applications to different postings
    candidates_per_role = defaultdict(int)
    for opp in opportunities:
        # Try to get posting from different possible locations in the API response
        posting_id = None
        
        # Check if there's a direct posting field (some Lever API versions)
        if opp.get("posting"):
            posting_id = opp.get("posting")
        
        # Check applications - could be list of IDs or list of objects
        applications = opp.get("applications", [])
        if applications:
            first_app = applications[0]
            if isinstance(first_app, dict):
                posting_id = first_app.get("posting") or first_app.get("postingId")
            # If it's a string, it's an application ID, not a posting ID
        
        if posting_id:
            candidates_per_role[posting_id] += 1
    
    # Format postings for display
    open_positions = []
    for posting in postings:
        open_positions.append({
            "id": posting.get("id"),
            "title": posting.get("text", "Unknown Role"),
        })
    
    # Sort alphabetically by title
    open_positions.sort(key=lambda x: x["title"])
    
    data = {
        "new_candidates": new_candidates,
        "onsites": onsites,
        "by_group": grouped_counts,
        "total_active": len(opportunities),
        "open_positions": open_positions,
        "candidates_per_role": candidates_per_role,
    }
    
    print(f"New candidates this week: {new_candidates}")
    print(f"Pipeline: {grouped_counts}")
    
    # Format and send
    message = format_slack_message(data)
    post_to_slack(message)


if __name__ == "__main__":
    main()
