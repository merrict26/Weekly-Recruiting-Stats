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
    count = 0
    for opp in opportunities:
        stage = get_stage_id(opp)
        if stage in ONSITE_STAGE_IDS:
            count += 1
    return count


def get_candidate_details(opportunity, postings_map):
    """Extract candidate name, role, and LinkedIn from an opportunity."""
    name = opportunity.get("name", "Unknown")
    
    # Get LinkedIn URL from links
    linkedin_url = None
    links = opportunity.get("links", [])
    for link in links:
        if isinstance(link, str) and "linkedin.com" in link:
            linkedin_url = link
            break
    
    # Get role from posting - try multiple possible locations
    role = "Unknown Role"
    posting_id = None
    
    # Try direct posting field
    if opportunity.get("posting"):
        posting_id = opportunity.get("posting")
    
    # Try applications array (expanded format)
    if not posting_id:
        applications = opportunity.get("applications", [])
        if applications:
            first_app = applications[0]
            if isinstance(first_app, dict):
                # Could be nested posting object or just posting ID
                posting_data = first_app.get("posting")
                if isinstance(posting_data, dict):
                    posting_id = posting_data.get("id")
                elif isinstance(posting_data, str):
                    posting_id = posting_data
                # Also try postingId field
                if not posting_id:
                    posting_id = first_app.get("postingId")
            elif isinstance(first_app, str):
                # Application is just an ID, not expanded
                pass
    
    if posting_id and posting_id in postings_map:
        role = postings_map[posting_id]
    
    return {
        "name": name,
        "role": role,
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
            "Hiring Manager Review": "👀",
            "Intro": "📞",
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
    
    # Show candidates in Onsite stages
    if data.get("onsite_candidates"):
        blocks.append({
            "type": "divider"
        })
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*🏢 Candidates in Onsite*"
            }
        })
        onsite_text = ""
        for c in data["onsite_candidates"]:
            if c["linkedin"]:
                onsite_text += f"• <{c['linkedin']}|{c['name']}> — {c['role']}\n"
            else:
                onsite_text += f"• {c['name']} — {c['role']}\n"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": onsite_text
            }
        })
    
    # Show candidates in Final Stages (Debrief + Reference check)
    if data.get("final_candidates"):
        blocks.append({
            "type": "divider"
        })
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*📋 Candidates in Final Stages*"
            }
        })
        final_text = ""
        for c in data["final_candidates"]:
            if c["linkedin"]:
                final_text += f"• <{c['linkedin']}|{c['name']}> — {c['role']}\n"
            else:
                final_text += f"• {c['name']} — {c['role']}\n"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": final_text
            }
        })
    
    # Show candidates with Offers
    if data.get("offer_candidates"):
        blocks.append({
            "type": "divider"
        })
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*🎉 Candidates with Offers*"
            }
        })
        offer_text = ""
        for c in data["offer_candidates"]:
            if c["linkedin"]:
                offer_text += f"• <{c['linkedin']}|{c['name']}> — {c['role']}\n"
            else:
                offer_text += f"• {c['name']} — {c['role']}\n"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": offer_text
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
            "text": f"*Open Positions ({data['total_open_positions']})*"
        }
    })
    
    # List open roles grouped by department/team
    if data["open_positions_grouped"]:
        for group_name, roles in data["open_positions_grouped"].items():
            group_text = f"*{group_name}*\n"
            for role in roles:
                location = f" ({role['location']})" if role.get("location") else ""
                candidate_count = data["candidates_per_role"].get(role["id"], 0)
                if candidate_count > 0:
                    group_text += f"    • {role['title']}{location} — _{candidate_count} candidates_\n"
                else:
                    group_text += f"    • {role['title']}{location}\n"
            
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": group_text
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
    
    # Debug: print raw stage counts
    print(f"Raw stage counts: {dict(stage_counts)}")
    print(f"Grouped counts: {grouped_counts}")
    
    # Build postings map for role lookup
    postings_map = {}
    for posting in postings:
        postings_map[posting.get("id")] = posting.get("text", "Unknown Role")
    
    # Debug: show categories to verify grouping
    if postings:
        print(f"DEBUG - Sample posting categories: {postings[0].get('categories')}")
    
    # Get detailed candidate info for onsite, final stages, and offer
    onsite_candidates = get_candidates_in_stages(opportunities, ONSITE_STAGE_IDS, postings_map)
    final_candidates = get_candidates_in_stages(opportunities, FINAL_STAGE_IDS, postings_map)
    offer_candidates = get_candidates_in_stages(opportunities, [OFFER_STAGE_ID], postings_map)
    
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
    
    # Format postings for display, grouped by department
    open_positions_by_dept = defaultdict(list)
    for posting in postings:
        location = posting.get("categories", {}).get("location", "")
        department = posting.get("categories", {}).get("department", "")
        team = posting.get("categories", {}).get("team", "")
        
        # Use team if available, otherwise department, otherwise "Other"
        group = team or department or "Other"
        
        open_positions_by_dept[group].append({
            "id": posting.get("id"),
            "title": posting.get("text", "Unknown Role"),
            "location": location,
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
        "total_active": sum(grouped_counts.values()),  # Only count candidates in tracked stages
        "open_positions_grouped": open_positions_grouped,
        "total_open_positions": len(postings),
        "candidates_per_role": candidates_per_role,
        "onsite_candidates": onsite_candidates,
        "final_candidates": final_candidates,
        "offer_candidates": offer_candidates,
    }
    
    print(f"New candidates this week: {new_candidates}")
    print(f"Pipeline: {grouped_counts}")
    
    # Format and send
    message = format_slack_message(data)
    post_to_slack(message)


if __name__ == "__main__":
    main()
