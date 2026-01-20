#!/usr/bin/env python3
"""
Agency Recruiting Digest: Lever → Slack
Shows pipeline for candidates sourced from a specific agency (Perfectly).
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Pacific timezone (PST = UTC-8)
PST = timezone(timedelta(hours=-8))

# === CONFIG ===
LEVER_API_KEY = os.environ["LEVER_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL_AGENCY"]  # Separate webhook for agency channel

AGENCY_NAME = "Perfectly"  # Filter to this agency
ARCHIVED_LOOKBACK_DAYS = 14  # How many days back to show archived candidates

# Lever pipeline stages by ID
STAGE_NAMES = {
    "3b5d887e-0629-4ceb-973a-663952c97b21": "Hiring Manager Review",
    "94d7f5df-ec0f-4061-b54d-bea369ace17b": "Schedule Intro Call",
    "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2": "Introductory Call",
    "160000bb-2cba-40df-b9f0-f69c77cd6175": "Schedule Technical Interview",
    "fae3d918-0118-4f17-b206-f7f29dca3bec": "Technical Interview",
    "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3": "Schedule Technical Interview #2",
    "a57980a4-4fc4-4252-a0f2-e765e96cfee5": "Technical Interview (#2)",
    "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272": "Schedule Onsite",
    "cb7dd941-ed9f-4803-9ed5-158681732b65": "Onsite Interview",
    "359f9594-ada0-4ca2-bec2-8b3f7eb2106a": "Debrief",
    "d03862a2-e446-4ade-bee6-4b200cf9b399": "Reference Check",
    "offer": "Offer",
}

# Stage order for sorting
STAGE_ORDER = [
    "Hiring Manager Review",
    "Schedule Intro Call",
    "Introductory Call",
    "Schedule Technical Interview",
    "Technical Interview",
    "Schedule Technical Interview #2",
    "Technical Interview (#2)",
    "Schedule Onsite",
    "Onsite Interview",
    "Debrief",
    "Reference Check",
    "Offer",
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


def get_upcoming_interview(opportunity_id):
    """Get the next upcoming interview for a candidate."""
    try:
        result = lever_request(f"opportunities/{opportunity_id}/interviews")
        interviews = result.get("data", [])
        
        now = datetime.now(timezone.utc)
        upcoming = []
        
        for interview in interviews:
            # Get interview date
            date_ms = interview.get("date")
            if date_ms:
                # Lever returns UTC timestamps
                interview_date = datetime.fromtimestamp(date_ms / 1000, tz=timezone.utc)
                # Only include future interviews
                if interview_date > now:
                    upcoming.append({
                        "date": interview_date,
                        "subject": interview.get("subject", "Interview"),
                    })
        
        if upcoming:
            # Sort by date and return the soonest
            upcoming.sort(key=lambda x: x["date"])
            next_interview = upcoming[0]
            # Convert to PST and format: "Jan 20, 10:00 AM PST"
            pst_time = next_interview["date"].astimezone(PST)
            return pst_time.strftime("%b %d, %I:%M %p PST")
        
        return None
    except Exception as e:
        print(f"Error fetching interviews for {opportunity_id}: {e}")
        return None


def get_all_opportunities():
    """Fetch all active (non-archived) opportunities from Lever."""
    opportunities = []
    has_next = True
    offset = None
    
    while has_next:
        params = {
            "limit": 100,
            "archived": "false",
            "expand": "applications",
        }
        if offset:
            params["offset"] = offset
        
        result = lever_request("opportunities", params)
        opportunities.extend(result.get("data", []))
        
        has_next = result.get("hasNext", False)
        offset = result.get("next")
    
    return opportunities


def get_archived_opportunities(since_days):
    """Fetch recently archived opportunities from Lever."""
    opportunities = []
    has_next = True
    offset = None
    since_date = datetime.now() - timedelta(days=since_days)
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


def get_archive_reasons():
    """Fetch archive reasons from Lever and return as a map of ID -> text."""
    result = lever_request("archive_reasons")
    reasons = result.get("data", [])
    
    reasons_map = {}
    for reason in reasons:
        reasons_map[reason.get("id")] = reason.get("text", "Archived")
    
    return reasons_map


def is_from_agency(opportunity, agency_name):
    """Check if candidate was sourced from the specified agency."""
    sources = opportunity.get("sources", [])
    for source in sources:
        if isinstance(source, str) and agency_name.lower() in source.lower():
            return True
    return False


def get_stage_name(stage_id):
    """Convert stage ID to human-readable name."""
    return STAGE_NAMES.get(stage_id, "Unknown Stage")


def get_candidate_details(opportunity, postings_map):
    """Extract candidate name, role, location, stage, and interview info."""
    name = opportunity.get("name", "Unknown")
    opp_id = opportunity.get("id")
    stage_id = opportunity.get("stage", "")
    stage_name = get_stage_name(stage_id)
    
    # Get upcoming interview
    upcoming_interview = get_upcoming_interview(opp_id)
    
    # Get role and location from posting
    role = "Unknown Role"
    location = ""
    posting_id = None
    
    if opportunity.get("posting"):
        posting_id = opportunity.get("posting")
    
    if not posting_id:
        applications = opportunity.get("applications", [])
        if applications:
            first_app = applications[0]
            if isinstance(first_app, dict):
                posting_data = first_app.get("posting")
                if isinstance(posting_data, dict):
                    posting_id = posting_data.get("id")
                elif isinstance(posting_data, str):
                    posting_id = posting_data
    
    if posting_id and posting_id in postings_map:
        posting_info = postings_map[posting_id]
        role = posting_info.get("title", "Unknown Role")
        location = posting_info.get("location", "")
    
    return {
        "name": name,
        "role": role,
        "location": location,
        "stage": stage_name,
        "stage_id": stage_id,
        "interview": upcoming_interview,
    }


def get_archived_candidate_details(opportunity, postings_map, archive_reasons_map):
    """Extract archived candidate name, role, archive reason, and date."""
    name = opportunity.get("name", "Unknown")
    
    # Get role from posting
    role = "Unknown Role"
    posting_id = None
    
    if opportunity.get("posting"):
        posting_id = opportunity.get("posting")
    
    if not posting_id:
        applications = opportunity.get("applications", [])
        if applications:
            first_app = applications[0]
            if isinstance(first_app, dict):
                posting_data = first_app.get("posting")
                if isinstance(posting_data, dict):
                    posting_id = posting_data.get("id")
                elif isinstance(posting_data, str):
                    posting_id = posting_data
    
    if posting_id and posting_id in postings_map:
        posting_info = postings_map[posting_id]
        role = posting_info.get("title", "Unknown Role")
    
    # Get archive reason and date
    archived_info = opportunity.get("archived", {})
    reason_id = archived_info.get("reason", "")
    reason = archive_reasons_map.get(reason_id, "Archived")
    archived_at = archived_info.get("archivedAt")
    
    archived_date = ""
    if archived_at:
        date = datetime.fromtimestamp(archived_at / 1000)
        archived_date = date.strftime("%b %d")
    
    return {
        "name": name,
        "role": role,
        "reason": reason,
        "date": archived_date,
    }


def format_slack_message(data):
    """Format the digest as a Slack message with blocks."""
    today = datetime.now()
    date_str = today.strftime("%B %d, %Y")
    
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Perfectly Pipeline — {date_str}",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Active Candidates: {len(data['candidates'])}*"
            }
        },
        {
            "type": "divider"
        },
    ]
    
    if data["candidates"]:
        # Group candidates by stage
        by_stage = defaultdict(list)
        for c in data["candidates"]:
            by_stage[c["stage"]].append(c)
        
        # Sort stages by pipeline order
        for stage_name in STAGE_ORDER:
            if stage_name in by_stage:
                stage_text = f"*{stage_name}*\n"
                for c in by_stage[stage_name]:
                    interview = c.get("interview")
                    
                    if interview:
                        # Has upcoming interview scheduled
                        interview_text = f" — 📅 {interview}"
                    elif "Schedule" in c.get("stage", ""):
                        # In a scheduling stage, no interview yet
                        interview_text = " — ⏳ _Pending Schedule_"
                    else:
                        # Completed interview, awaiting review
                        interview_text = " — 🔍 _Awaiting Review_"
                    
                    stage_text += f"• {c['name']} — {c['role']}{interview_text}\n"
                
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": stage_text
                    }
                })
        
        # Handle any stages not in our order list
        for stage_name, candidates in by_stage.items():
            if stage_name not in STAGE_ORDER:
                stage_text = f"*{stage_name}*\n"
                for c in candidates:
                    interview = c.get("interview")
                    
                    if interview:
                        interview_text = f" — 📅 {interview}"
                    elif "Schedule" in c.get("stage", ""):
                        interview_text = " — ⏳ _Pending Schedule_"
                    else:
                        interview_text = " — 🔍 _Awaiting Review_"
                    
                    stage_text += f"• {c['name']} — {c['role']}{interview_text}\n"
                
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": stage_text
                    }
                })
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "_No active candidates from Perfectly at this time._"
            }
        })
    
    # Add archived candidates section
    if data.get("archived_candidates"):
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📁 Recently Archived (Last {ARCHIVED_LOOKBACK_DAYS} Days)*"
            }
        })
        
        archived_text = ""
        for c in data["archived_candidates"]:
            date_text = f" ({c['date']})" if c.get("date") else ""
            archived_text += f"• {c['name']} — {c['role']} — _{c['reason']}_{date_text}\n"
        
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": archived_text
            }
        })
    
    return {"blocks": blocks}


def post_to_slack(message):
    """Post formatted message to Slack webhook."""
    response = requests.post(SLACK_WEBHOOK_URL, json=message)
    response.raise_for_status()
    print("✅ Posted to Slack successfully")


def main():
    print("Fetching data from Lever...")
    
    opportunities = get_all_opportunities()
    print(f"Found {len(opportunities)} total active candidates")
    
    archived_opportunities = get_archived_opportunities(ARCHIVED_LOOKBACK_DAYS)
    print(f"Found {len(archived_opportunities)} archived candidates in last {ARCHIVED_LOOKBACK_DAYS} days")
    
    postings = get_open_postings()
    print(f"Found {len(postings)} open positions")
    
    archive_reasons = get_archive_reasons()
    print(f"Found {len(archive_reasons)} archive reasons")
    
    # Build postings map
    postings_map = {}
    for posting in postings:
        postings_map[posting.get("id")] = {
            "title": posting.get("text", "Unknown Role"),
            "location": posting.get("categories", {}).get("location", ""),
        }
    
    # Filter to agency candidates only (active)
    agency_candidates = []
    for opp in opportunities:
        if is_from_agency(opp, AGENCY_NAME):
            agency_candidates.append(get_candidate_details(opp, postings_map))
    
    print(f"Found {len(agency_candidates)} active candidates from {AGENCY_NAME}")
    
    # Filter to agency candidates only (archived)
    archived_agency_candidates = []
    for opp in archived_opportunities:
        if is_from_agency(opp, AGENCY_NAME):
            archived_agency_candidates.append(get_archived_candidate_details(opp, postings_map, archive_reasons))
    
    print(f"Found {len(archived_agency_candidates)} archived candidates from {AGENCY_NAME}")
    
    data = {
        "candidates": agency_candidates,
        "archived_candidates": archived_agency_candidates,
    }
    
    message = format_slack_message(data)
    post_to_slack(message)


if __name__ == "__main__":
    main()
