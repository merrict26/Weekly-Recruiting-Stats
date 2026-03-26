#!/usr/bin/env python3
"""
Full cache refresh for Recruiting Assistant
Fetches all Lever data and updates Cloudflare KV cache

Run via GitHub Actions (no subrequest limits)
"""

import os
import json
import requests
from collections import defaultdict
from datetime import datetime, timedelta

# Environment variables
LEVER_API_KEY = os.environ.get("LEVER_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")

HIRED_REASON_ID = "7fbd076c-7224-415a-bf96-ebd45b9a70dc"

STAGE_GROUPS = {
    "New Lead": ["lead-new"],
    "Candidate DB": ["af4f63d6-e22a-40e1-9ced-038196761712"],
    "Reached Out": ["lead-reached-out"],
    "Responded": ["lead-responded"],
    "New Applicant": ["applicant-new"],
    "Contacted": ["9a1eefa9-8bb0-4bf9-9bdb-19ef616845c7"],
    "Hiring Manager Review": ["3b5d887e-0629-4ceb-973a-663952c97b21"],
    "Intro": ["94d7f5df-ec0f-4061-b54d-bea369ace17b", "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2"],
    "Technical": [
        "160000bb-2cba-40df-b9f0-f69c77cd6175",
        "fae3d918-0118-4f17-b206-f7f29dca3bec",
        "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3",
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5",
    ],
    "Onsite": ["af0f3cb5-4bec-4fbe-8360-f30e9d0c7272", "cb7dd941-ed9f-4803-9ed5-158681732b65"],
    "Final Stages": ["359f9594-ada0-4ca2-bec2-8b3f7eb2106a", "d03862a2-e446-4ade-bee6-4b200cf9b399"],
    "Offer": ["offer"],
}

# Build reverse lookup
STAGE_ID_TO_GROUP = {}
for group, ids in STAGE_GROUPS.items():
    for stage_id in ids:
        STAGE_ID_TO_GROUP[stage_id] = group


def lever_request(endpoint, params=None):
    """Make authenticated request to Lever API."""
    url = f"https://api.lever.co/v1/{endpoint}"
    response = requests.get(url, auth=(LEVER_API_KEY, ""), params=params or {})
    response.raise_for_status()
    return response.json()


def fetch_interviews(opp_id):
    """Fetch interviews for an opportunity."""
    try:
        result = lever_request(f"opportunities/{opp_id}/interviews")
        return result.get("data", [])
    except Exception as e:
        print(f"  Warning: Could not fetch interviews for {opp_id}: {e}")
        return []


def fetch_all_opportunities(archived=False):
    """Fetch all opportunities with pagination."""
    opportunities = []
    has_next = True
    offset = None
    pages = 0

    while has_next:
        params = {"limit": 100, "expand": "applications"}
        if archived:
            params["archived"] = "true"
            # Fetch last 365 days for historical analysis
            one_year_ago = int((datetime.now() - timedelta(days=365)).timestamp() * 1000)
            params["archived_at_start"] = one_year_ago
        else:
            params["archived"] = "false"

        if offset:
            params["offset"] = offset

        result = lever_request("opportunities", params)
        opportunities.extend(result.get("data", []))
        has_next = result.get("hasNext", False)
        offset = result.get("next")
        pages += 1
        
        if pages % 10 == 0:
            print(f"  Fetched {len(opportunities)} {'archived' if archived else 'active'} candidates...")

    return opportunities


def fetch_all_postings():
    """Fetch all postings for role name lookup."""
    postings = []
    
    result = lever_request("postings", {"state": "published", "limit": 100})
    postings.extend(result.get("data", []))
    
    result = lever_request("postings", {"state": "closed", "limit": 100})
    postings.extend(result.get("data", []))
    
    return {p["id"]: p.get("text", "Unknown Role") for p in postings}


def get_stage_group(stage_id):
    return STAGE_ID_TO_GROUP.get(stage_id, "Unknown")


def get_role(opp, postings_map):
    apps = opp.get("applications", [])
    if apps and apps[0].get("posting"):
        posting = apps[0]["posting"]
        posting_id = posting.get("id") if isinstance(posting, dict) else posting
        return postings_map.get(posting_id, "Unknown Role")
    return "Unknown Role"


def build_data_summary(active, archived, postings_map):
    """Build the data summary for Claude."""
    
    # Pipeline counts
    pipeline = {stage: 0 for stage in STAGE_GROUPS.keys()}
    for opp in active:
        group = get_stage_group(opp.get("stage"))
        if group in pipeline:
            pipeline[group] += 1

    # Active by role (counts only)
    active_by_role = {}
    for opp in active:
        role = get_role(opp, postings_map)
        if role not in active_by_role:
            active_by_role[role] = {"total": 0, "byStage": {}}
        active_by_role[role]["total"] += 1
        stage = get_stage_group(opp.get("stage"))
        active_by_role[role]["byStage"][stage] = active_by_role[role]["byStage"].get(stage, 0) + 1

    # Active pipeline candidates (HM Review+) with specific status
    pipeline_stages = {"Hiring Manager Review", "Intro", "Technical", "Onsite", "Final Stages", "Offer"}
    active_pipeline_candidates = []
    now_ms = datetime.now().timestamp() * 1000
    
    # Map stage IDs to specific status
    stage_status_map = {
        "94d7f5df-ec0f-4061-b54d-bea369ace17b": ("Intro", "waiting"),  # Schedule Intro Call
        "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2": ("Intro", "scheduled"),  # Introductory Call
        "160000bb-2cba-40df-b9f0-f69c77cd6175": ("Technical", "waiting"),  # Schedule Technical
        "fae3d918-0118-4f17-b206-f7f29dca3bec": ("Technical", "scheduled"),  # Technical Interview
        "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3": ("Technical", "waiting"),  # Schedule Tech #2
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5": ("Technical", "scheduled"),  # Technical #2
        "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272": ("Onsite", "waiting"),  # Schedule Onsite
        "cb7dd941-ed9f-4803-9ed5-158681732b65": ("Onsite", "scheduled"),  # Onsite interview
    }
    
    # Scheduled stage IDs (need to fetch interview dates for these)
    scheduled_stage_ids = {
        "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2",  # Introductory Call
        "fae3d918-0118-4f17-b206-f7f29dca3bec",  # Technical Interview
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5",  # Technical #2
        "cb7dd941-ed9f-4803-9ed5-158681732b65",  # Onsite interview
    }
    
    print("📥 Fetching interview dates for scheduled candidates...")
    scheduled_count = 0
    
    for opp in active:
        stage_id = opp.get("stage")
        stage_group = get_stage_group(stage_id)
        if stage_group not in pipeline_stages:
            continue
        
        # Get specific status
        status_info = stage_status_map.get(stage_id)
        status = status_info[1] if status_info else "active"
        
        created_at = opp.get("createdAt")
        days_in_process = None
        if created_at:
            days_in_process = round((now_ms - created_at) / (1000 * 60 * 60 * 24))
        
        # Fetch interview date for scheduled candidates
        interview_date = None
        if stage_id in scheduled_stage_ids:
            opp_id = opp.get("id")
            if opp_id:
                interviews = fetch_interviews(opp_id)
                # Get the most recent/upcoming interview
                for interview in interviews:
                    interview_date_ms = interview.get("date")
                    if interview_date_ms:
                        interview_date = datetime.fromtimestamp(interview_date_ms / 1000).strftime("%Y-%m-%d")
                        break
                scheduled_count += 1
        
        active_pipeline_candidates.append({
            "name": opp.get("name", "Unknown"),
            "role": get_role(opp, postings_map),
            "stage": stage_group,
            "status": status,  # "waiting", "scheduled", or "active"
            "interview_date": interview_date,  # Actual interview date if scheduled
            "sources": (opp.get("sources") or [])[:1],
            "created_at": datetime.fromtimestamp(created_at / 1000).strftime("%Y-%m-%d") if created_at else None,
            "days_in_process": days_in_process,
        })
    
    print(f"✓ Fetched interview dates for {scheduled_count} candidates")

    # Offer details
    offer_stages = {"Offer", "Final Stages"}
    offer_details = []
    for opp in archived:
        group = get_stage_group(opp.get("stage"))
        if group not in offer_stages:
            continue
        archived_info = opp.get("archived") or {}
        archived_at = archived_info.get("archivedAt")
        created_at = opp.get("createdAt")
        
        # Calculate days in process
        days_in_process = None
        if archived_at and created_at:
            days_in_process = round((archived_at - created_at) / (1000 * 60 * 60 * 24))
        
        offer_details.append({
            "name": opp.get("name", "Unknown"),
            "role": get_role(opp, postings_map),
            "accepted": archived_info.get("reason") == HIRED_REASON_ID,
            "archived_at": datetime.fromtimestamp(archived_at / 1000).strftime("%Y-%m-%d") if archived_at else None,
            "created_at": datetime.fromtimestamp(created_at / 1000).strftime("%Y-%m-%d") if created_at else None,
            "days_in_process": days_in_process,
        })

    # Hires (with time to hire)
    hires = []
    for opp in archived:
        archived_info = opp.get("archived") or {}
        if archived_info.get("reason") != HIRED_REASON_ID:
            continue
        archived_at = archived_info.get("archivedAt")
        created_at = opp.get("createdAt")
        
        # Calculate days to hire
        days_to_hire = None
        if archived_at and created_at:
            days_to_hire = round((archived_at - created_at) / (1000 * 60 * 60 * 24))
        
        hires.append({
            "name": opp.get("name", "Unknown"),
            "role": get_role(opp, postings_map),
            "hired_date": datetime.fromtimestamp(archived_at / 1000).strftime("%Y-%m-%d") if archived_at else None,
            "created_date": datetime.fromtimestamp(created_at / 1000).strftime("%Y-%m-%d") if created_at else None,
            "days_to_hire": days_to_hire,
            "sources": (opp.get("sources") or [])[:1],
        })

    # Source stats
    source_stats = {}
    hm_review_stages = {"Intro", "Technical", "Onsite", "Final Stages", "Offer"}
    
    for opp in active + archived:
        sources = opp.get("sources") or []
        source = sources[0] if sources else "Unknown"
        if not isinstance(source, str):
            continue
        if source == "Added manually":
            source = "Sourced by zaimler"
        
        if source not in source_stats:
            source_stats[source] = {"total": 0, "hired": 0, "past_hm_review": 0}
        source_stats[source]["total"] += 1
        
        if get_stage_group(opp.get("stage")) in hm_review_stages:
            source_stats[source]["past_hm_review"] += 1
        
        archived_info = opp.get("archived") or {}
        if archived_info.get("reason") == HIRED_REASON_ID:
            source_stats[source]["hired"] += 1

    # Time to hire stats
    hire_times = [h["days_to_hire"] for h in hires if h.get("days_to_hire") is not None]
    time_to_hire_stats = {
        "avg_days": round(sum(hire_times) / len(hire_times)) if hire_times else None,
        "min_days": min(hire_times) if hire_times else None,
        "max_days": max(hire_times) if hire_times else None,
        "total_hires_measured": len(hire_times),
    }

    # Stage activity tracking (interviews in last N days)
    now_ms = datetime.now().timestamp() * 1000
    days_30_ms = 30 * 24 * 60 * 60 * 1000
    days_7_ms = 7 * 24 * 60 * 60 * 1000
    days_90_ms = 90 * 24 * 60 * 60 * 1000

    # To count COMPLETED interviews, we track:
    # 1. When candidates move to the NEXT stage (they passed)
    # 2. When candidates are archived FROM a stage (they completed but didn't pass)
    
    completed_markers = {
        # If someone moved TO these stages, it means they COMPLETED the previous stage
        "160000bb-2cba-40df-b9f0-f69c77cd6175": "Intro",  # Schedule Technical = completed Intro
        "fae3d918-0118-4f17-b206-f7f29dca3bec": "Intro",  # Technical Interview = completed Intro
        "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3": "Technical",  # Schedule Tech #2 = completed Technical #1
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5": "Technical",  # Technical #2 = completed Technical #1
        "af0f3cb5-4bec-4fbe-8360-f30e9d0c7272": "Technical",  # Schedule Onsite = completed Technical
        "cb7dd941-ed9f-4803-9ed5-158681732b65": "Technical",  # Onsite = completed Technical
        "359f9594-ada0-4ca2-bec2-8b3f7eb2106a": "Onsite",  # Debrief = completed Onsite
        "d03862a2-e446-4ade-bee6-4b200cf9b399": "Onsite",  # Ref Check = completed Onsite (or Debrief)
        "offer": "Final Stages",  # Offer = completed Final Stages
    }
    
    # Map stage IDs to interview type (for tracking archived candidates)
    stage_to_interview_type = {
        "fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2": "Intro",  # Introductory Call
        "fae3d918-0118-4f17-b206-f7f29dca3bec": "Technical",  # Technical Interview
        "a57980a4-4fc4-4252-a0f2-e765e96cfee5": "Technical",  # Technical #2
        "cb7dd941-ed9f-4803-9ed5-158681732b65": "Onsite",  # Onsite interview
        "359f9594-ada0-4ca2-bec2-8b3f7eb2106a": "Final Stages",  # Debrief
        "d03862a2-e446-4ade-bee6-4b200cf9b399": "Final Stages",  # Ref Check
    }
    
    # Stage IDs for SCHEDULED interviews (moved to actual interview stage = interview is scheduled)
    # "Schedule X" stages mean WAITING to schedule, not scheduled
    scheduled_stage_ids = {
        "Intro": ["fb6d2f07-aeab-4f1c-bf35-72f0cffa37f2"],  # Introductory Call = intro is scheduled/happening
        "Technical": [
            "fae3d918-0118-4f17-b206-f7f29dca3bec",  # Technical Interview = technical is scheduled
            "a57980a4-4fc4-4252-a0f2-e765e96cfee5",  # Technical Interview #2 = tech #2 is scheduled
        ],
        "Onsite": ["cb7dd941-ed9f-4803-9ed5-158681732b65"],  # Onsite interview = onsite is scheduled
    }
    
    # Stage IDs for WAITING TO SCHEDULE (in scheduling stage, not yet scheduled)
    waiting_to_schedule_ids = {
        "Intro": ["94d7f5df-ec0f-4061-b54d-bea369ace17b"],  # Schedule Intro Call
        "Technical": [
            "160000bb-2cba-40df-b9f0-f69c77cd6175",  # Schedule Technical Interview
            "7ce6a4ba-c34e-4582-be77-dac4b1cf2fe3",  # Schedule Technical Interview #2
        ],
        "Onsite": ["af0f3cb5-4bec-4fbe-8360-f30e9d0c7272"],  # Schedule Onsite
    }

    # Build reverse lookup for scheduled
    scheduled_stage_lookup = {}
    for stage_name, ids in scheduled_stage_ids.items():
        for stage_id in ids:
            scheduled_stage_lookup[stage_id] = stage_name
    
    # Build reverse lookup for waiting to schedule
    waiting_stage_lookup = {}
    for stage_name, ids in waiting_to_schedule_ids.items():
        for stage_id in ids:
            waiting_stage_lookup[stage_id] = stage_name

    # Count completed interviews by time period
    interviews_completed = {
        "last_7_days": {"Intro": 0, "Technical": 0, "Onsite": 0, "Final Stages": 0},
        "last_30_days": {"Intro": 0, "Technical": 0, "Onsite": 0, "Final Stages": 0},
        "last_90_days": {"Intro": 0, "Technical": 0, "Onsite": 0, "Final Stages": 0},
    }
    
    # Count scheduled interviews by time period (entered actual interview stage)
    interviews_scheduled = {
        "last_7_days": {"Intro": 0, "Technical": 0, "Onsite": 0},
        "last_30_days": {"Intro": 0, "Technical": 0, "Onsite": 0},
        "last_90_days": {"Intro": 0, "Technical": 0, "Onsite": 0},
    }

    # Recent completed interviews with candidate details
    recent_interviews = []
    
    # Recent scheduled interviews with candidate details
    recent_scheduled = []
    
    # Current candidates waiting to be scheduled (by checking current stage)
    waiting_to_schedule = {"Intro": [], "Technical": [], "Onsite": []}
    
    # Track which candidates we've already counted for each stage (to avoid double counting)
    counted_completions = set()

    for opp in active + archived:
        opp_id = opp.get("id", "")
        stage_changes = opp.get("stageChanges") or []
        
        for change in stage_changes:
            to_stage_id = change.get("toStageId")
            updated_at = change.get("updatedAt")
            
            if not to_stage_id or not updated_at:
                continue

            age_ms = now_ms - updated_at
            change_date = datetime.fromtimestamp(updated_at / 1000).strftime("%Y-%m-%d")
            
            # Check if this stage change indicates a COMPLETED interview (passed to next stage)
            completed_stage = completed_markers.get(to_stage_id)
            if completed_stage:
                completion_key = f"{opp_id}:{completed_stage}"
                if completion_key not in counted_completions:
                    counted_completions.add(completion_key)
                    if age_ms <= days_7_ms:
                        interviews_completed["last_7_days"][completed_stage] += 1
                    if age_ms <= days_30_ms:
                        interviews_completed["last_30_days"][completed_stage] += 1
                        recent_interviews.append({
                            "opp_id": opp_id,
                            "name": opp.get("name", "Unknown"),
                            "role": get_role(opp, postings_map),
                            "stage_completed": completed_stage,
                            "outcome": "passed",
                            "date": change_date,
                            "days_ago": round(age_ms / (24 * 60 * 60 * 1000)),
                        })
                    if age_ms <= days_90_ms:
                        interviews_completed["last_90_days"][completed_stage] += 1
            
            # Check if scheduled interview
            scheduled_stage = scheduled_stage_lookup.get(to_stage_id)
            if scheduled_stage:
                if age_ms <= days_7_ms:
                    interviews_scheduled["last_7_days"][scheduled_stage] += 1
                if age_ms <= days_30_ms:
                    interviews_scheduled["last_30_days"][scheduled_stage] += 1
                    recent_scheduled.append({
                        "name": opp.get("name", "Unknown"),
                        "role": get_role(opp, postings_map),
                        "stage": scheduled_stage,
                        "date": change_date,
                        "days_ago": round(age_ms / (24 * 60 * 60 * 1000)),
                    })
                if age_ms <= days_90_ms:
                    interviews_scheduled["last_90_days"][scheduled_stage] += 1
        
        # Check for archived candidates - they completed the interview but didn't pass
        archived_info = opp.get("archived") or {}
        if archived_info and archived_info.get("reason") != HIRED_REASON_ID:
            archived_at = archived_info.get("archivedAt")
            last_stage = opp.get("stage")
            
            if archived_at and last_stage:
                interview_type = stage_to_interview_type.get(last_stage)
                if interview_type:
                    completion_key = f"{opp_id}:{interview_type}"
                    if completion_key not in counted_completions:
                        counted_completions.add(completion_key)
                        age_ms = now_ms - archived_at
                        change_date = datetime.fromtimestamp(archived_at / 1000).strftime("%Y-%m-%d")
                        
                        if age_ms <= days_7_ms:
                            interviews_completed["last_7_days"][interview_type] += 1
                        if age_ms <= days_30_ms:
                            interviews_completed["last_30_days"][interview_type] += 1
                            recent_interviews.append({
                                "opp_id": opp_id,
                                "name": opp.get("name", "Unknown"),
                                "role": get_role(opp, postings_map),
                                "stage_completed": interview_type,
                                "outcome": "rejected",
                                "date": change_date,
                                "days_ago": round(age_ms / (24 * 60 * 60 * 1000)),
                            })
                        if age_ms <= days_90_ms:
                            interviews_completed["last_90_days"][interview_type] += 1

    # Sort recent interviews by date (most recent first)
    recent_interviews.sort(key=lambda x: x["date"], reverse=True)
    recent_scheduled.sort(key=lambda x: x["date"], reverse=True)
    
    # Populate waiting_to_schedule from active candidates in "Schedule X" stages
    for opp in active:
        current_stage = opp.get("stage")
        waiting_stage = waiting_stage_lookup.get(current_stage)
        if waiting_stage:
            # Get when they entered this stage
            stage_changes = opp.get("stageChanges") or []
            entered_date = None
            days_waiting = None
            for change in stage_changes:
                if change.get("toStageId") == current_stage:
                    updated_at = change.get("updatedAt")
                    if updated_at:
                        entered_date = datetime.fromtimestamp(updated_at / 1000).strftime("%Y-%m-%d")
                        days_waiting = round((now_ms - updated_at) / (24 * 60 * 60 * 1000))
            
            waiting_to_schedule[waiting_stage].append({
                "name": opp.get("name", "Unknown"),
                "role": get_role(opp, postings_map),
                "waiting_since": entered_date,
                "days_waiting": days_waiting,
            })
    
    # Sort waiting lists by days_waiting (longest first)
    for stage in waiting_to_schedule:
        waiting_to_schedule[stage].sort(key=lambda x: x.get("days_waiting") or 0, reverse=True)
    
    # Build scheduled_by_stage from active_pipeline_candidates (with real interview dates)
    scheduled_by_stage = {"Onsite": [], "Technical": [], "Intro": []}
    for cand in active_pipeline_candidates:
        if cand.get("status") == "scheduled" and cand.get("stage") in scheduled_by_stage:
            scheduled_by_stage[cand["stage"]].append({
                "name": cand["name"],
                "role": cand["role"],
                "interview_date": cand.get("interview_date"),  # Real interview date
            })
    
    # Sort by interview date (soonest first)
    for stage in scheduled_by_stage:
        scheduled_by_stage[stage].sort(key=lambda x: x.get("interview_date") or "9999-99-99")
    
    # Group recent_interviews by stage for easier display
    # First, fetch real interview dates for Onsite completions (most important)
    # Skip interns - they don't do onsites
    print("📥 Fetching interview dates for completed onsites...")
    onsite_completions = [
        item for item in recent_interviews 
        if item.get("stage_completed") == "Onsite" and "Intern" not in item.get("role", "")
    ]
    for item in onsite_completions[:20]:  # Limit to avoid too many API calls
        opp_id = item.get("opp_id")
        if opp_id:
            interviews = fetch_interviews(opp_id)
            # Find the most recent interview (likely the onsite)
            for interview in sorted(interviews, key=lambda x: x.get("date", 0), reverse=True):
                interview_date_ms = interview.get("date")
                if interview_date_ms:
                    item["interview_date"] = datetime.fromtimestamp(interview_date_ms / 1000).strftime("%Y-%m-%d")
                    break
    print(f"✓ Fetched interview dates for {len(onsite_completions)} completed onsites")
    
    # Also look at candidates CURRENTLY in end stages (Debrief, Ref Check, Offer)
    # who may have completed onsites recently but weren't captured by stage change tracking
    end_stage_ids = {
        "359f9594-ada0-4ca2-bec2-8b3f7eb2106a",  # Debrief
        "d03862a2-e446-4ade-bee6-4b200cf9b399",  # Reference check
        "offer",  # Offer
    }
    tracked_opp_ids = {item.get("opp_id") for item in onsite_completions}
    
    print("📥 Checking end-stage candidates for recent onsites...")
    end_stage_onsites = []
    for opp in active:
        stage_id = opp.get("stage")
        if stage_id not in end_stage_ids:
            continue
        
        # Skip interns - they don't do onsites
        role = get_role(opp, postings_map)
        if "Intern" in role:
            continue
            
        opp_id = opp.get("id")
        if opp_id in tracked_opp_ids:
            continue  # Already tracked
        
        # Find when they entered end stage (to find the right interview)
        stage_changes = opp.get("stageChanges") or []
        entered_end_stage_at = None
        for change in stage_changes:
            if change.get("toStageId") in end_stage_ids:
                entered_end_stage_at = change.get("updatedAt")
                break
        
        # Fetch interviews to find onsite date
        interviews = fetch_interviews(opp_id)
        
        # Find interview that happened BEFORE they entered end stage (that's the onsite)
        best_interview = None
        for interview in sorted(interviews, key=lambda x: x.get("date", 0), reverse=True):
            interview_date_ms = interview.get("date")
            if interview_date_ms:
                # If we know when they entered end stage, find interview before that
                if entered_end_stage_at and interview_date_ms < entered_end_stage_at:
                    best_interview = interview
                    break
                elif not entered_end_stage_at:
                    # Fallback: just use most recent
                    best_interview = interview
                    break
        
        if best_interview:
            interview_date_ms = best_interview.get("date")
            interview_date = datetime.fromtimestamp(interview_date_ms / 1000)
            days_ago = (datetime.now() - interview_date).days
            if days_ago <= 30:  # Only include recent onsites
                end_stage_onsites.append({
                    "name": opp.get("name", "Unknown"),
                    "role": role,
                    "stage_completed": "Onsite",
                    "outcome": "passed",
                    "date": interview_date.strftime("%Y-%m-%d"),
                    "days_ago": days_ago,
                })
    
    print(f"✓ Found {len(end_stage_onsites)} additional onsites from end-stage candidates")
    
    # Combine all onsite completions
    all_onsite_completions = onsite_completions + end_stage_onsites
    
    completed_by_stage = {"Onsite": [], "Technical": [], "Intro": [], "Final Stages": []}
    
    # Add onsites (from both sources), deduplicated by name
    seen_names = set()
    for item in all_onsite_completions:
        if item["name"] in seen_names:
            continue
        seen_names.add(item["name"])
        completed_by_stage["Onsite"].append({
            "name": item["name"],
            "role": item["role"],
            "stage_completed": "Onsite",
            "outcome": item["outcome"],
            "date": item.get("interview_date") or item["date"],
            "days_ago": item["days_ago"],
        })
    
    # Add other stages from recent_interviews
    for item in recent_interviews[:50]:
        stage = item.get("stage_completed")
        if stage in completed_by_stage and stage != "Onsite":
            completed_by_stage[stage].append({
                "name": item["name"],
                "role": item["role"],
                "stage_completed": stage,
                "outcome": item["outcome"],
                "date": item.get("interview_date") or item["date"],
                "days_ago": item["days_ago"],
            })
    
    # Sort completed by date (most recent first)
    for stage in completed_by_stage:
        completed_by_stage[stage].sort(key=lambda x: x.get("date") or "0000-00-00", reverse=True)

    # ============ FUNNEL CONVERSION STATS ============
    # Calculate true pass rates by tracking who made it to later stages
    print("📊 Calculating funnel conversion stats...")
    
    days_90_ms = 90 * 24 * 60 * 60 * 1000
    
    # Stage IDs for funnel tracking
    debrief_id = "359f9594-ada0-4ca2-bec2-8b3f7eb2106a"
    ref_check_id = "d03862a2-e446-4ade-bee6-4b200cf9b399"
    offer_id = "offer"
    
    # Onsite stage ID (the actual interview, not "Schedule Onsite")
    onsite_interview_id = "cb7dd941-ed9f-4803-9ed5-158681732b65"
    
    # All post-onsite stages (completing onsite = entering any of these)
    post_onsite_stages = {debrief_id, ref_check_id, offer_id}
    
    # Track candidates through the funnel (last 90 days)
    funnel_90d = {
        "did_intro": {},        # opp_id -> candidate info
        "passed_intro": set(),  # Made it to technical+
        "did_technical": {},    # opp_id -> candidate info
        "passed_technical": set(),  # Made it to onsite+
        "did_onsite": {},       # opp_id -> candidate info (had an onsite)
        "passed_onsite": set(), # Made it to offer
        "reached_offer": set(),
        "offers_extended": 0,
        "offers_accepted": 0,
        "offers_declined": 0,
    }
    
    for opp in active + archived:
        opp_id = opp.get("id", "")
        stage_changes = opp.get("stageChanges") or []
        current_stage = opp.get("stage")
        archived_info = opp.get("archived") or {}
        name = opp.get("name", "Unknown")
        role = get_role(opp, postings_map)
        
        # Check if any stage change happened in last 90 days
        recent_activity = False
        for change in stage_changes:
            updated_at = change.get("updatedAt")
            if updated_at and (now_ms - updated_at) <= days_90_ms:
                recent_activity = True
                break
        
        if not recent_activity:
            continue
        
        # Track what stages they went through
        did_intro = False
        did_technical = False
        did_onsite = False
        reached_debrief_or_later = False
        reached_offer = False
        
        for change in stage_changes:
            to_stage_id = change.get("toStageId")
            updated_at = change.get("updatedAt")
            
            if not to_stage_id or not updated_at:
                continue
            if (now_ms - updated_at) > days_90_ms:
                continue
            
            # Did intro = entered technical stage (Schedule Technical, Technical, Tech #2)
            tech_stage_ids = (
                waiting_to_schedule_ids.get("Technical", []) + 
                scheduled_stage_ids.get("Technical", [])
            )
            if to_stage_id in tech_stage_ids:
                did_intro = True
                funnel_90d["passed_intro"].add(opp_id)
            
            # Did technical = entered onsite stage (Schedule Onsite, Onsite)
            onsite_stage_ids = (
                waiting_to_schedule_ids.get("Onsite", []) + 
                scheduled_stage_ids.get("Onsite", [])
            )
            if to_stage_id in onsite_stage_ids:
                did_technical = True
                funnel_90d["passed_technical"].add(opp_id)
            
            # Did onsite = entered post-onsite stage (Debrief, Ref Check, Offer)
            if to_stage_id in post_onsite_stages:
                did_onsite = True
                reached_debrief_or_later = True
                funnel_90d["did_onsite"][opp_id] = {"name": name, "role": role}
            
            # Reached offer
            if to_stage_id == offer_id:
                reached_offer = True
                funnel_90d["reached_offer"].add(opp_id)
                funnel_90d["passed_onsite"].add(opp_id)
    
    # Track onsite failures: completed onsite (in Debrief/Ref Check) but got rejected
    onsite_failures = []
    for opp in archived:
        opp_id = opp.get("id", "")
        archived_info = opp.get("archived") or {}
        archived_at = archived_info.get("archivedAt")
        reason = archived_info.get("reason")
        last_stage = opp.get("stage")
        name = opp.get("name", "Unknown")
        role = get_role(opp, postings_map)
        
        if not archived_at or (now_ms - archived_at) > days_90_ms:
            continue
        
        # Rejected from Debrief or Ref Check = failed onsite
        if last_stage in [debrief_id, ref_check_id] and reason != HIRED_REASON_ID:
            archived_date = datetime.fromtimestamp(archived_at / 1000).strftime("%Y-%m-%d")
            onsite_failures.append({
                "name": name,
                "role": role,
                "stage": "Debrief" if last_stage == debrief_id else "References",
                "date": archived_date,
            })
            # Make sure they're counted in did_onsite
            funnel_90d["did_onsite"][opp_id] = {"name": name, "role": role}
        
        # Also check if rejected from Onsite stage itself (never made it to Debrief)
        if last_stage == onsite_interview_id and reason != HIRED_REASON_ID:
            archived_date = datetime.fromtimestamp(archived_at / 1000).strftime("%Y-%m-%d")
            onsite_failures.append({
                "name": name,
                "role": role,
                "stage": "Onsite",
                "date": archived_date,
            })
            funnel_90d["did_onsite"][opp_id] = {"name": name, "role": role}
    
    # Count offers extended, accepted, declined from archived candidates
    for opp in archived:
        opp_id = opp.get("id", "")
        archived_info = opp.get("archived") or {}
        archived_at = archived_info.get("archivedAt")
        reason = archived_info.get("reason")
        
        if not archived_at or (now_ms - archived_at) > days_90_ms:
            continue
        
        # Check if they were in offer stage
        stage_changes = opp.get("stageChanges") or []
        was_in_offer = False
        for change in stage_changes:
            if change.get("toStageId") == offer_id:
                was_in_offer = True
                break
        
        if was_in_offer:
            funnel_90d["offers_extended"] += 1
            if reason == HIRED_REASON_ID:
                funnel_90d["offers_accepted"] += 1
            else:
                funnel_90d["offers_declined"] += 1
    
    # Also count active candidates currently in offer stage
    for opp in active:
        if opp.get("stage") == offer_id:
            funnel_90d["offers_extended"] += 1
    
    # Calculate conversion rates
    total_did_onsite = len(funnel_90d["did_onsite"])
    total_passed_onsite = len(funnel_90d["passed_onsite"])
    
    funnel_stats = {
        "period": "last_90_days",
        "intro_to_technical": {
            "passed": len(funnel_90d["passed_intro"]),
            "total": interviews_completed["last_90_days"]["Intro"],
            "rate": round(len(funnel_90d["passed_intro"]) / max(interviews_completed["last_90_days"]["Intro"], 1) * 100, 1),
        },
        "technical_to_onsite": {
            "passed": len(funnel_90d["passed_technical"]),
            "total": interviews_completed["last_90_days"]["Technical"],
            "rate": round(len(funnel_90d["passed_technical"]) / max(interviews_completed["last_90_days"]["Technical"], 1) * 100, 1),
        },
        "onsite_to_offer": {
            "did_onsite": total_did_onsite,
            "passed": total_passed_onsite,
            "failed": total_did_onsite - total_passed_onsite,
            "rate": round(total_passed_onsite / max(total_did_onsite, 1) * 100, 1),
        },
        "onsite_failures": onsite_failures,  # List of who failed
        "offer_acceptance": {
            "extended": funnel_90d["offers_extended"],
            "accepted": funnel_90d["offers_accepted"],
            "declined": funnel_90d["offers_declined"],
            "pending": funnel_90d["offers_extended"] - funnel_90d["offers_accepted"] - funnel_90d["offers_declined"],
            "acceptance_rate": round(funnel_90d["offers_accepted"] / max(funnel_90d["offers_accepted"] + funnel_90d["offers_declined"], 1) * 100, 1),
        },
    }
    
    print(f"✓ Funnel: {funnel_stats['intro_to_technical']['rate']}% intro→tech, {funnel_stats['technical_to_onsite']['rate']}% tech→onsite, {funnel_stats['onsite_to_offer']['rate']}% onsite→offer")
    print(f"  Onsite failures: {len(onsite_failures)}")

    return {
        "pipeline": pipeline,
        "active_by_role": active_by_role,
        "active_pipeline_candidates": active_pipeline_candidates,
        "offer_details": offer_details,
        "hires": hires,
        "time_to_hire_stats": time_to_hire_stats,
        "interviews_completed": interviews_completed,
        "interviews_scheduled": interviews_scheduled,
        "completed_by_stage": completed_by_stage,
        "scheduled_by_stage": scheduled_by_stage,
        "waiting_to_schedule": waiting_to_schedule,
        "funnel_stats": funnel_stats,
        "source_stats": source_stats,
        "total_active": len(active),
        "total_archived_365d": len(archived),
        "archived_since": (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
        "cached_at": datetime.utcnow().isoformat() + "Z",
    }


def update_kv_cache(data):
    """Update Cloudflare KV cache."""
    # Check env vars
    if not CF_ACCOUNT_ID:
        raise ValueError("CF_ACCOUNT_ID is not set")
    if not CF_KV_NAMESPACE_ID:
        raise ValueError("CF_KV_NAMESPACE_ID is not set")
    if not CF_API_TOKEN:
        raise ValueError("CF_API_TOKEN is not set")
    
    # Debug: show first/last chars of IDs
    print(f"  Account ID length: {len(CF_ACCOUNT_ID)}, starts with: {CF_ACCOUNT_ID[:8]}...")
    print(f"  Namespace ID length: {len(CF_KV_NAMESPACE_ID)}, value: {CF_KV_NAMESPACE_ID}")
    
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/lever_data"
    print(f"  URL: {url[:60]}...")
    
    response = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "text/plain",
        },
        data=json.dumps(data),
    )
    
    if response.status_code == 200:
        print("✅ KV cache updated successfully")
    else:
        print(f"❌ KV update failed: {response.status_code} {response.text}")
        response.raise_for_status()


def main():
    print("🔄 Starting full cache refresh...")
    print()
    
    # Validate required env vars
    if not LEVER_API_KEY:
        raise ValueError("LEVER_API_KEY is not set")
    
    print("📥 Fetching active candidates...")
    active = fetch_all_opportunities(archived=False)
    print(f"✓ {len(active)} active candidates")
    print()
    
    print("📥 Fetching archived candidates (365 days)...")
    archived = fetch_all_opportunities(archived=True)
    print(f"✓ {len(archived)} archived candidates")
    print()
    
    print("📥 Fetching postings...")
    postings_map = fetch_all_postings()
    print(f"✓ {len(postings_map)} postings")
    print()
    
    print("🔧 Building data summary...")
    data = build_data_summary(active, archived, postings_map)
    print()
    
    # Summary
    print("📊 Summary:")
    print(f"  Pipeline candidates: {sum(data['pipeline'].get(s, 0) for s in ['Hiring Manager Review', 'Intro', 'Technical', 'Onsite', 'Final Stages', 'Offer'])}")
    print(f"  Offers made: {len(data['offer_details'])}")
    print(f"  Hires: {len(data['hires'])}")
    tth = data.get('time_to_hire_stats', {})
    if tth.get('avg_days'):
        print(f"  Avg time to hire: {tth['avg_days']} days (range: {tth['min_days']}-{tth['max_days']})")
    
    # Stage activity
    completed = data.get('interviews_completed', {}).get('last_30_days', {})
    scheduled = data.get('interviews_scheduled', {}).get('last_30_days', {})
    waiting = data.get('waiting_to_schedule', {})
    print(f"  Last 30 days completed: {completed.get('Intro', 0)} intros, {completed.get('Technical', 0)} technicals, {completed.get('Onsite', 0)} onsites")
    print(f"  Last 30 days scheduled: {scheduled.get('Intro', 0)} intros, {scheduled.get('Technical', 0)} technicals, {scheduled.get('Onsite', 0)} onsites")
    print(f"  Waiting to schedule: {len(waiting.get('Intro', []))} intros, {len(waiting.get('Technical', []))} technicals, {len(waiting.get('Onsite', []))} onsites")
    print()
    
    print("☁️ Updating Cloudflare KV cache...")
    update_kv_cache(data)
    print()
    
    print("✅ Done!")


if __name__ == "__main__":
    main()
