# Recruiting Digest: Lever → Slack

Automated weekly digest that pulls your recruiting pipeline from Lever and posts a summary to Slack.

## What it posts

Every Friday at 4pm PT, your team sees:

```
📊 Recruiting Digest — Week of January 17, 2026

This Week's Activity
• New candidates added: 12
• Candidates in onsite stages: 4

Pipeline Snapshot
🔝 Top of Funnel: 22
💻 Technical: 21
🏢 Onsite: 4
📋 Final Stages: 3
🎉 Offer: 1

🎯 1 offer(s) currently out!

Total active candidates: 48 across 10 open roles
```

---

## Setup (15 minutes)

### 1. Create a Slack Incoming Webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name it "Recruiting Digest" and select your workspace
4. Go to **Incoming Webhooks** → Toggle **On**
5. Click **Add New Webhook to Workspace**
6. Select your recruiting channel
7. Copy the webhook URL (starts with `https://hooks.slack.com/...`)

### 2. Get your Lever API Key

1. In Lever, go to **Settings** → **Integrations & API** → **API Credentials**
2. Click **Generate New Key**
3. Give it a name like "Recruiting Digest"
4. Copy the API key

### 3. Create the GitHub Repository

1. Create a new private repo on GitHub (e.g., `recruiting-digest`)
2. Upload these files:
   ```
   recruiting_digest.py
   .github/workflows/weekly-digest.yml
   ```

### 4. Add Secrets to GitHub

1. In your repo, go to **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add:
   - Name: `LEVER_API_KEY` — Value: your Lever API key
   - Name: `SLACK_WEBHOOK_URL` — Value: your Slack webhook URL

### 5. Test It

1. Go to **Actions** tab in your repo
2. Select **Weekly Recruiting Digest**
3. Click **Run workflow** → **Run workflow**
4. Check your Slack channel!

---

## Customization

### Change the schedule

Edit `.github/workflows/weekly-digest.yml`:

```yaml
schedule:
  # Current: Friday 4pm PT (Saturday 00:00 UTC)
  - cron: '0 0 * * 6'
  
  # Friday 9am PT would be:
  - cron: '0 17 * * 5'
```

Use [crontab.guru](https://crontab.guru/) to build your cron expression. Remember GitHub Actions uses UTC.

### Update your pipeline stages

If your Lever stages have different names, edit `recruiting_digest.py`:

```python
STAGE_GROUPS = {
    "Top of Funnel": ["Schedule Intro Call", "Introductory Call"],
    "Technical": ["Schedule Technical Interview", "Technical Interview", 
                  "Schedule Technical Interview #2", "Technical Interview (#2)"],
    "Onsite": ["Schedule Onsite", "Onsite Interview"],
    "Final Stages": ["Debrief", "Reference Check"],
    "Offer": ["Offer"],
}
```

### Add more metrics

The script is set up to be extended. Some ideas:
- Interview count (requires calendar integration or Lever webhook events)
- Time-in-stage averages
- Source breakdown (referral vs. sourced vs. inbound)

---

## Troubleshooting

**Workflow not running?**
- Check the Actions tab for errors
- Verify secrets are named exactly `LEVER_API_KEY` and `SLACK_WEBHOOK_URL`

**Wrong data showing?**
- Stage names must match exactly what's in Lever (case-sensitive)
- Run locally first to debug: `LEVER_API_KEY=xxx SLACK_WEBHOOK_URL=xxx python recruiting_digest.py`

**Want to run locally?**
```bash
export LEVER_API_KEY="your-key"
export SLACK_WEBHOOK_URL="your-webhook"
pip install requests
python recruiting_digest.py
```

---

## Questions?

This is a simple starting point. Feel free to extend it or ping me if you want to add features like:
- Per-role breakdowns
- Graphs/charts (via QuickChart API)
- Bi-weekly or daily digests
- Integration with Gem for sourcing metrics
