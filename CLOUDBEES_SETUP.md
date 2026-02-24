# CloudBees Workflow Setup Guide

## Overview
This guide will help you set up the Jira Bug Aging Report in CloudBees CI with AWS Bedrock (Claude AI) using OIDC authentication.

## Prerequisites
1. CloudBees CI access
2. AWS account with Bedrock enabled
3. Jira API token
4. EngOps support for IAM role creation

---

## Step 1: Request IAM Role from EngOps

Contact EngOps team and reference ticket: **[OPS-20629](https://cloudbees.atlassian.net/browse/OPS-20629)**

**Request Details:**
```
Subject: OIDC IAM Role for Jira Bug Summarizer (Bedrock/Claude)

Repository: [YOUR_REPO_NAME]
Required Permissions:
- bedrock:InvokeModel
- bedrock:InvokeModelWithResponseStream (optional)

Requested Role Name: infra-claude-bedrock-ci
Region: us-east-1
Model: us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

EngOps will:
1. Create the IAM role: `arn:aws:iam::ACCOUNT_ID:role/infra-claude-bedrock-ci`
2. Set up OIDC trust relationship for your repository
3. Provide the actual ARN to use in the workflow

---

## Step 2: Update Workflow File

Once you receive the IAM role ARN from EngOps, update `.cloudbees/workflows/jira-bug-report.yaml`:

```yaml
- name: Configure AWS credentials via OIDC
  uses: cloudbees-io/configure-aws-credentials@v1
  with:
    role-to-assume: arn:aws:iam::123456789012:role/infra-claude-bedrock-ci  # Replace with actual ARN
    aws-region: us-east-1
```

---

## Step 3: Configure CloudBees Secrets

Add these secrets to your CloudBees repository:

### Via CloudBees UI:
1. Navigate to your repository settings
2. Go to **Secrets** section
3. Add the following secrets:

| Secret Name | Description | Example Value |
|------------|-------------|---------------|
| `JIRA_EMAIL` | Your Jira email address | `user@cloudbees.com` |
| `JIRA_API_TOKEN` | Jira API token | `ATATT3xFfG...` |

### How to generate Jira API Token:
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click **Create API token**
3. Give it a name: "CloudBees CI - Jira Bug Summarizer"
4. Copy the token and save it as `JIRA_API_TOKEN` secret

---

## Step 4: Test the Workflow

### Manual Test:
1. Go to CloudBees CI dashboard
2. Navigate to your workflow: **Jira Bug Aging Report**
3. Click **Run workflow** (workflow_dispatch trigger)
4. Monitor the execution logs

### Expected Output:
```
==================================================
Starting Jira Bug Summarizer
==================================================
Fetching bugs with JQL: labels = qa_automation AND type = Bug AND status != Done AND status != Rejected
Found Atlassian Team field: Team (ID: customfield_12000)
Found 5 bugs

Processing CBP-33505...
  - Found 4 comments
  - Generated summary
  - Bug data structured ✓

...

==================================================
All bug data saved to bug_report.json
==================================================

Formatting bug report table...
✅ Report saved to bug_report.txt

==================================================
Completed! Processed 5 bugs
==================================================
```

**Note:** If you see `"Atlassian Team field not found, will use components/labels"`, the team names will fall back to components or labels.

---

## Step 5: Verify Generated Reports

After successful execution:

1. **Download artifacts:**
   - Go to workflow run details
   - Download `jira-bug-reports-{commit-sha}`

2. **Check the reports:**
   ```bash
   # View JSON report
   cat bug_report.json | jq

   # View text report
   cat bug_report.txt
   ```

3. **Report Features:**
   - **Main Table:** Bug details with Teams column (e.g., "CBP Ninja", "CBP Core UI")
   - **Comment Summaries:** Separate table with AI-generated summaries for each ticket
   - **Sorting:** Bugs automatically sorted by priority (Highest to Lowest)
   - **Statistics:** Aging breakdown with color-coded indicators
   - **JSON Output:** Structured data including `team_name`, comments, and summaries

4. **Team Field:**
   The script automatically detects the Atlassian Team field (`customfield_12000`) and displays team names. If issues don't have teams assigned, it will show "N/A" or fall back to components/labels.

---

## Workflow Schedule

The workflow runs automatically:
- **Schedule:** Every Monday at 9:00 AM UTC
- **Manual:** Via workflow_dispatch
- **Auto:** On push to `main` branch (optional - can be removed)

To modify the schedule, edit the `cron` value in the workflow file:
```yaml
on:
  schedule:
    - cron: '0 9 * * 1'  # Minute Hour DayOfMonth Month DayOfWeek
```

**Examples:**
- Daily at 8 AM: `'0 8 * * *'`
- Every weekday at 9 AM: `'0 9 * * 1-5'`
- Twice a day: `'0 9,17 * * *'`

---

## Troubleshooting

### Error: "Unable to locate credentials"
**Solution:** IAM role ARN is incorrect or OIDC trust relationship not configured properly. Contact EngOps.

### Error: "AccessDeniedException: User is not authorized"
**Solution:** IAM role doesn't have `bedrock:InvokeModel` permission. Contact EngOps to add Bedrock permissions.

### Error: "JIRA_EMAIL or JIRA_API_TOKEN not set"
**Solution:** Add secrets to CloudBees repository settings.

### Error: "Model not found"
**Solution:** Verify the model ID is correct and available in `us-east-1` region:
```yaml
BEDROCK_MODEL_ID: us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

### Team field showing "N/A" instead of team names
**Possible causes:**
1. Issues don't have teams assigned in Jira
2. Atlassian Team field not detected (check logs for `"Found Atlassian Team field"`)
3. Your Jira instance uses a different team field structure

**Solution:**
- Verify issues have teams assigned in Jira UI (check the "Team" field)
- Look for the log message: `"Found Atlassian Team field: Team (ID: customfield_12000)"`
- If using a custom team field, contact support to modify the `get_team_field_id()` method

---

## Script Configuration

The Python script uses these environment variables (configured in the workflow):

| Variable | Source | Description |
|----------|--------|-------------|
| `JIRA_URL` | Workflow | Jira instance URL |
| `JIRA_EMAIL` | Secret | Jira user email |
| `JIRA_API_TOKEN` | Secret | Jira API token |
| `JIRA_JQL` | Workflow/Optional | JQL query to filter bugs (see below) |
| `AWS_REGION` | Workflow | AWS region (us-east-1) |
| `BEDROCK_MODEL_ID` | Workflow | Claude model ID |
| `AWS_PROFILE` | Auto (OIDC) | Handled by OIDC auth |

---

## Customizing the JQL Query

To modify which bugs are fetched, set the `JIRA_JQL` environment variable in the workflow file (`.cloudbees/workflows/jira-bug-report.yaml`):

```yaml
env:
  JIRA_URL: https://cloudbees.atlassian.net
  JIRA_JQL: "labels = qa_automation AND type = Bug AND status != Done AND status != Rejected"
  AWS_REGION: us-east-1
  BEDROCK_MODEL_ID: us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

**Example JQL queries:**

```yaml
# All open bugs in a project
JIRA_JQL: "project = MYPROJECT AND type = Bug AND status in (Open, 'In Progress')"

# High priority bugs
JIRA_JQL: "type = Bug AND priority in (Highest, High) AND status != Done"

# Bugs older than 30 days
JIRA_JQL: "type = Bug AND created <= -30d AND status != Done"

# Specific team's bugs
JIRA_JQL: "Team = 'CBP Ninja' AND type = Bug AND status != Done"
```

**Note:** If `JIRA_JQL` is not set, the script defaults to:
```
"labels = qa_automation AND type = Bug AND status != Done AND status != Rejected"
```

---

## Security Best Practices

1. ✅ **Never commit secrets** to the repository
2. ✅ **Use OIDC** instead of static AWS credentials
3. ✅ **Rotate Jira API tokens** regularly
4. ✅ **Limit IAM role permissions** to only what's needed (bedrock:InvokeModel)
5. ✅ **Review workflow logs** for sensitive data before sharing

---

## Support

- **AWS/OIDC Issues:** Contact EngOps, reference OPS-20629
- **Workflow Issues:** Check CloudBees documentation
- **Script Issues:** Review `jira_bug_summarizer.py` logs

---

## Report Features

### Text Report Structure
The generated `bug_report.txt` contains:

1. **Main Bug Table**
   - Ticket ID, Days Open (with emoji indicators), Last Update
   - Status, Priority, **Teams** (from Atlassian Team field), Assignee
   - Automatically sorted by priority (Highest to Lowest)

2. **Comment Summaries Table**
   - Separate table below the main table
   - AI-generated summaries for each ticket
   - Includes Root Cause, Solutions, Current Status, Action Items
   - Row separators for easy readability

3. **Links Section**
   - Clickable links to all tickets

4. **Statistics Section**
   - Total bugs count
   - Breakdown by age: Critical (90+ days), Aging (31-90), Active (8-30), Recent (0-7)

### JSON Report Structure
The `bug_report.json` includes:
```json
{
  "bug_key": "CBP-12345",
  "team": {
    "team_name": "CBP Ninja",      // From Atlassian Team field
    "components": [],
    "labels": ["qa_automation"]
  },
  "comments": {
    "count": 4,
    "summary": "AI-generated summary...",  // Full AI analysis
    "details": [...]                       // All comment details
  }
}
```

This structured data is ideal for:
- Slack integration (custom formatting)
- Data analysis and reporting
- Historical tracking
- Team metrics

---

## Next Steps

After successful setup:
1. Monitor the first few automated runs
2. Share the reports with your team (Teams column helps with team-specific filtering)
3. Consider adding notifications (email, Slack) for report delivery
4. Archive reports for historical analysis and team performance tracking
5. Use the JSON output for custom integrations or dashboards
