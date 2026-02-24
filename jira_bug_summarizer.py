"""
Jira Bug Comment Summarizer
Fetches bugs from Jira, summarizes comments using AI, and posts to Slack
"""

import os
import sys
from typing import List, Dict
from jira import JIRA
import boto3
import requests
from dotenv import load_dotenv
import json
from datetime import datetime, timezone

# Load environment variables from .env file
load_dotenv()


class JiraBugSummarizer:
    def __init__(self):
        # Load credentials from environment variables
        self.jira_url = os.getenv("JIRA_URL", "https://cloudbees.atlassian.net")
        self.jira_email = os.getenv("JIRA_EMAIL")
        self.jira_token = os.getenv("JIRA_API_TOKEN")
        self.aws_profile = os.getenv("AWS_PROFILE", "default")
        self.aws_region = os.getenv("AWS_REGION", "us-east-1")
        self.bedrock_model_id = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")
        self.slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        self.jql_query = os.getenv("JIRA_JQL", "labels = qa_automation AND type = Bug AND status != Done AND status != Rejected")
        self.team_field_id = None  # Cache for team field ID

        # Validate required credentials
        self._validate_credentials()

        # Initialize clients
        self.jira = JIRA(
            server=self.jira_url,
            basic_auth=(self.jira_email, self.jira_token),
            options={'rest_api_version': '3'}
        )

        # Initialize AWS session
        # In CI with OIDC, credentials come from environment variables (no profile needed)
        # In local development, use profile if AWS_PROFILE is set and no OIDC credentials exist
        if self.aws_profile and self.aws_profile != "default" and not os.getenv("AWS_SESSION_TOKEN"):
            # Use profile for local development
            session = boto3.Session(profile_name=self.aws_profile, region_name=self.aws_region)
        else:
            # Use default credentials chain (OIDC env vars in CI, or default profile locally)
            session = boto3.Session(region_name=self.aws_region)
        self.bedrock_client = session.client('bedrock-runtime')
    
    def _validate_credentials(self):
        """Validate that all required credentials are present"""
        required = {
            "JIRA_EMAIL": self.jira_email,
            "JIRA_API_TOKEN": self.jira_token,
            # "SLACK_WEBHOOK_URL": self.slack_webhook_url  # Commented out - not using Slack
        }

        missing = [key for key, value in required.items() if not value]
        if missing:
            print(f"Error: Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)
    
    def get_team_field_id(self) -> str:
        """Find the custom field ID for Team in Jira"""
        try:
            # Get all fields from Jira using REST API
            all_fields = self.jira.fields()

            # Look for the Atlassian Team field (customfield_12000)
            # Schema type: "team", Custom: "com.atlassian.jira.plugin.system.customfieldtypes:atlassian-team"
            for field in all_fields:
                schema = field.get('schema', {})
                schema_type = schema.get('type', '')
                custom_type = schema.get('custom', '')

                # Check for Atlassian Team field
                if (schema_type == 'team' and
                    'atlassian-team' in custom_type):
                    field_id = field.get('id')
                    print(f"Found Atlassian Team field: {field['name']} (ID: {field_id})")
                    return field_id

            print("Atlassian Team field not found, will use components/labels")
            return None
        except Exception as e:
            print(f"Error getting team field ID: {e}")
            return None

    def fetch_bugs(self, jql: str, max_results: int = 20) -> List:
        """Fetch bugs from Jira using JQL query"""
        print(f"Fetching bugs with JQL: {jql}")
        try:
            # Get team field ID (only once)
            if self.team_field_id is None:
                self.team_field_id = self.get_team_field_id()

            # Build fields list
            fields = "key,summary,status,priority,assignee,reporter,comment,created,updated,components,labels,customfield_10020"
            if self.team_field_id:
                fields += f",{self.team_field_id}"

            issues = self.jira.search_issues(
                jql_str=jql,
                maxResults=max_results,
                fields=fields
            )
            print(f"Found {len(issues)} bugs")
            return issues
        except Exception as e:
            print(f"Error fetching bugs: {e}")
            return []
    
    def get_bug_comments(self, issue) -> List[Dict]:
        """Extract comments from a Jira issue"""
        comments = []
        for comment in issue.fields.comment.comments:
            # Extract comment body text from raw data - Jira API v3 uses ADF (Atlassian Document Format)
            body_text = ""
            if hasattr(comment, 'raw') and 'body' in comment.raw:
                body = comment.raw['body']
                if isinstance(body, str):
                    # Plain text format
                    body_text = body
                elif isinstance(body, dict):
                    # ADF (Atlassian Document Format) - extract text from content
                    body_text = self._extract_text_from_adf(body)

            comments.append({
                "author": comment.author.displayName if hasattr(comment.author, 'displayName') else str(comment.author),
                "created": comment.created,
                "body": body_text
            })
        return comments

    def _extract_text_from_adf(self, adf_body: dict) -> str:
        """Extract plain text from Atlassian Document Format"""
        text_parts = []

        def extract_content(node):
            if isinstance(node, dict):
                if node.get('type') == 'text':
                    text_parts.append(node.get('text', ''))
                elif 'content' in node:
                    for child in node['content']:
                        extract_content(child)
            elif isinstance(node, list):
                for item in node:
                    extract_content(item)

        extract_content(adf_body)
        return ' '.join(text_parts)

    def calculate_bug_aging(self, created_date: str) -> int:
        """Calculate the number of days since the bug was created"""
        try:
            # Parse the Jira date format (ISO 8601)
            created = datetime.fromisoformat(created_date.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            age_delta = now - created
            return age_delta.days
        except Exception as e:
            print(f"Error calculating bug aging: {e}")
            return 0

    def get_team_from_issue(self, issue) -> str:
        """Extract Team field value from Jira issue (Atlassian Team field)"""
        if not self.team_field_id:
            return None

        try:
            # Get the team field value using the field ID
            # Replace hyphen with underscore for attribute access
            field_attr = self.team_field_id.replace('-', '_')
            team_value = getattr(issue.fields, field_attr, None)

            if not team_value:
                return None

            # Atlassian Team field returns a PropertyHolder object with:
            # - name: Team name (e.g., "CBP Ninja")
            # - title: Team title (usually same as name)
            # - id: Team ID
            # - avatarUrl, isVisible, isVerified, isShared
            if hasattr(team_value, 'name') and team_value.name:
                return team_value.name

            # Fallback: try title
            if hasattr(team_value, 'title') and team_value.title:
                return team_value.title

            # If neither works, try string conversion
            team_str = str(team_value)
            if team_str and not team_str.startswith('<'):
                return team_str

            return None

        except Exception as e:
            print(f"Error extracting team from issue {issue.key}: {e}")
            return None

    def structure_bug_data(self, issue, comments: List[Dict], summary: str) -> Dict:
        """Structure bug data into JSON format"""
        # Calculate bug aging
        created_date = issue.fields.created if hasattr(issue.fields, 'created') else None
        bug_age_days = self.calculate_bug_aging(created_date) if created_date else 0

        # Get team field value
        team_name = self.get_team_from_issue(issue)

        # Get team/components
        components = []
        if hasattr(issue.fields, 'components') and issue.fields.components:
            components = [comp.name for comp in issue.fields.components]

        # Get labels (can also represent teams)
        labels = []
        if hasattr(issue.fields, 'labels') and issue.fields.labels:
            labels = issue.fields.labels

        # Team information (prioritize team field, then components and labels)
        team_info = {
            "team_name": team_name,
            "components": components,
            "labels": labels
        }

        # Get reporter
        reporter = "Unknown"
        reporter_email = None
        if hasattr(issue.fields, 'reporter') and issue.fields.reporter:
            reporter = issue.fields.reporter.displayName if hasattr(issue.fields.reporter, 'displayName') else str(issue.fields.reporter)
            reporter_email = issue.fields.reporter.emailAddress if hasattr(issue.fields.reporter, 'emailAddress') else None

        # Get assignee
        assignee = "Unassigned"
        assignee_email = None
        if hasattr(issue.fields, 'assignee') and issue.fields.assignee:
            assignee = issue.fields.assignee.displayName if hasattr(issue.fields.assignee, 'displayName') else str(issue.fields.assignee)
            assignee_email = issue.fields.assignee.emailAddress if hasattr(issue.fields.assignee, 'emailAddress') else None

        # Get last updated
        last_updated = issue.fields.updated if hasattr(issue.fields, 'updated') else None

        # Structure the data
        structured_data = {
            "bug_key": issue.key,
            "summary": issue.fields.summary,
            "status": str(issue.fields.status) if issue.fields.status else "Unknown",
            "priority": str(issue.fields.priority) if issue.fields.priority else "Unknown",
            "bug_url": f"{self.jira_url}/browse/{issue.key}",
            "aging": {
                "created_date": created_date,
                "days_open": bug_age_days
            },
            "last_updated": last_updated,
            "team": team_info,
            "reporter": {
                "name": reporter,
                "email": reporter_email
            },
            "assignee": {
                "name": assignee,
                "email": assignee_email
            },
            "comments": {
                "count": len(comments),
                "summary": summary,
                "details": comments
            }
        }

        return structured_data
    
    def summarize_comments(self, bug_key: str, comments: List[Dict]) -> str:
        """Use AWS Bedrock to summarize bug comments"""
        if not comments:
            return "No comments available."

        # Prepare comments text
        comments_text = "\n\n".join([
            f"**{c['author']}** ({c['created']}):\n{c['body']}"
            for c in comments
        ])

        prompt = f"""You are analyzing Jira bug comments. Summarize the following comments for bug {bug_key}.

Focus on:
- Root cause identified
- Solutions attempted
- Current status/blockers
- Action items

Comments:
{comments_text}

Provide a concise summary in 3-5 bullet points."""

        try:
            # Prepare request for AWS Bedrock
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 500,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }

            response = self.bedrock_client.invoke_model(
                modelId=self.bedrock_model_id,
                body=json.dumps(request_body)
            )

            response_body = json.loads(response['body'].read())
            return response_body['content'][0]['text']
        except Exception as e:
            print(f"Error summarizing comments for {bug_key}: {e}")
            return "Failed to generate summary."
    
    def sort_bugs_by_priority(self, all_bug_data: List[Dict]) -> List[Dict]:
        """Sort bugs by priority (Highest to Lowest)"""
        priority_order = {
            "Highest": 1,
            "High": 2,
            "Medium": 3,
            "Low": 4,
            "Lowest": 5,
            "Unknown": 6
        }

        return sorted(all_bug_data, key=lambda bug: priority_order.get(bug['priority'], 6))

    def format_team_info(self, team_data: Dict) -> str:
        """Format team information for display"""
        # Priority 1: Use team_name field if available (starts with CBP)
        if team_data.get('team_name'):
            team_str = team_data['team_name']
            if len(team_str) > 18:
                team_str = team_str[:15] + "..."
            return team_str

        # Priority 2: Fall back to components and labels
        team_parts = []

        # Add components if available
        if team_data.get('components'):
            team_parts.extend(team_data['components'])

        # Add labels if available (limit to 2 for space)
        if team_data.get('labels'):
            labels = team_data['labels'][:2]  # Take first 2 labels
            team_parts.extend(labels)

        if not team_parts:
            return "N/A"

        # Join and truncate if too long
        team_str = ", ".join(team_parts)
        if len(team_str) > 18:
            team_str = team_str[:15] + "..."

        return team_str

    def format_slack_table(self, all_bug_data: List[Dict]) -> str:
        """Format all bug data into a rows and columns table"""
        if not all_bug_data:
            return "No bugs found."

        # Sort bugs by priority
        all_bug_data = self.sort_bugs_by_priority(all_bug_data)

        # Header
        message = """*🐛 JIRA BUG SUMMARY REPORT*
_Report generated on {}_

""".format(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        # Table header (added Teams column)
        table_header = """```
┌────┬─────────────┬───────────┬─────────────┬──────────┬──────────┬────────────────────┬────────────────┐
│ #  │ Ticket ID   │ Days Open │ Last Update │ Status   │ Priority │ Teams              │ Assignee       │
├────┼─────────────┼───────────┼─────────────┼──────────┼──────────┼────────────────────┼────────────────┤"""

        message += table_header + "\n"

        # Create table rows
        for idx, bug in enumerate(all_bug_data, 1):
            # Format last updated date
            last_updated_str = "Unknown   "
            if bug["last_updated"]:
                try:
                    last_updated = datetime.fromisoformat(bug["last_updated"].replace('Z', '+00:00'))
                    last_updated_str = last_updated.strftime("%Y-%m-%d")
                except:
                    last_updated_str = bug["last_updated"][:10] if bug["last_updated"] else "Unknown   "

            # Determine aging indicator
            days_open = bug['aging']['days_open']
            if days_open <= 7:
                age_emoji = "🟢"
            elif days_open <= 30:
                age_emoji = "🟡"
            elif days_open <= 90:
                age_emoji = "🟠"
            else:
                age_emoji = "🔴"

            # Truncate and pad fields for alignment
            ticket_id = bug['bug_key'][:11].ljust(11)
            days_str = f"{age_emoji} {str(days_open).rjust(3)}"[:9].ljust(9)
            status = bug['status'][:8].ljust(8)
            priority = bug['priority'][:8].ljust(8)
            teams = self.format_team_info(bug['team'])[:18].ljust(18)
            assignee = bug['assignee']['name'][:14].ljust(14)

            # Create row (added Teams column)
            row = f"│ {str(idx).rjust(2)} │ {ticket_id} │ {days_str} │ {last_updated_str} │ {status} │ {priority} │ {teams} │ {assignee} │\n"
            message += row

        # Table footer (added Teams column)
        table_footer = """└────┴─────────────┴───────────┴─────────────┴──────────┴──────────┴────────────────────┴────────────────┘
```
"""
        message += table_footer

        # Add comment summary table
        message += "\n*💬 COMMENT SUMMARIES*\n"
        message += "```\n"
        message += "┌────┬─────────────┬────────────────────────────────────────────────────────────────────────────────────────────────────────┐\n"
        message += "│ #  │ Ticket ID   │ AI-Generated Comment Summary                                                                       │\n"
        message += "├────┼─────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────┤\n"

        for idx, bug in enumerate(all_bug_data, 1):
            ticket_id = bug['bug_key'][:11].ljust(11)
            # Use full AI-generated summary
            summary_text = bug['comments']['summary']

            # Word wrap the summary to fit in table width (100 chars per line)
            max_width = 100

            # Split summary into lines for word wrapping
            words = summary_text.split()
            lines = []
            current_line = ""

            for word in words:
                if len(current_line) + len(word) + 1 <= max_width:
                    current_line += (word + " ")
                else:
                    lines.append(current_line.strip().ljust(max_width))
                    current_line = word + " "

            if current_line:
                lines.append(current_line.strip().ljust(max_width))

            # If no lines (empty summary), add placeholder
            if not lines:
                lines = ["No summary available".ljust(max_width)]

            # Print first line with ticket info
            message += f"│ {str(idx).rjust(2)} │ {ticket_id} │ {lines[0]} │\n"

            # Print remaining lines
            for line in lines[1:]:
                message += f"│    │             │ {line} │\n"

            # Add row separator (except for last row)
            if idx < len(all_bug_data):
                message += "├────┼─────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────┤\n"

        message += "└────┴─────────────┴────────────────────────────────────────────────────────────────────────────────────────────────────┘\n"
        message += "```\n"

        # Add hyperlinks section
        message += "\n*🔗 Links to Tickets:*\n"
        for idx, bug in enumerate(all_bug_data, 1):
            message += f"  {idx}. <{bug['bug_url']}|{bug['bug_key']}> - {bug['summary'][:60]}{'...' if len(bug['summary']) > 60 else ''}\n"

        # Footer with stats
        total_bugs = len(all_bug_data)
        critical_age = sum(1 for b in all_bug_data if b['aging']['days_open'] > 90)
        aging = sum(1 for b in all_bug_data if 30 < b['aging']['days_open'] <= 90)
        active = sum(1 for b in all_bug_data if 7 < b['aging']['days_open'] <= 30)
        recent = sum(1 for b in all_bug_data if b['aging']['days_open'] <= 7)

        footer = f"""
*📊 STATISTICS*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • Total Bugs: *{total_bugs}*
  • 🔴 Critical Age (90+ days): *{critical_age}*
  • 🟠 Aging (31-90 days): *{aging}*
  • 🟡 Active (8-30 days): *{active}*
  • 🟢 Recent (0-7 days): *{recent}*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        message += footer

        return message.strip()
    
    # def post_to_slack(self, message: str):
    #     """Post message to Slack webhook"""
    #     try:
    #         payload = {"message": message}
    #         response = requests.post(
    #             self.slack_webhook_url,
    #             json=payload,
    #             headers={'Content-Type': 'application/json'},
    #             timeout=10
    #         )
    #         response.raise_for_status()
    #         print(f"Message posted to Slack: Status {response.status_code}")
    #         return response
    #     except requests.exceptions.RequestException as e:
    #         print(f"Error posting to Slack: {e}")
    #         if hasattr(e, 'response') and e.response is not None:
    #             print(f"Response status: {e.response.status_code}")
    #             print(f"Response body: {e.response.text}")
    #         return None
    
    def run(self):
        """Main execution flow"""
        print("=" * 50)
        print("Starting Jira Bug Summarizer")
        print("=" * 50)

        # Fetch bugs using JQL from environment variable
        bugs = self.fetch_bugs(self.jql_query)

        if not bugs:
            print("No bugs found. Exiting.")
            return

        # Store all structured bug data
        all_bug_data = []

        # Process each bug
        for issue in bugs:
            print(f"\nProcessing {issue.key}...")

            # Get comments
            comments = self.get_bug_comments(issue)
            print(f"  - Found {len(comments)} comments")

            # Summarize comments
            summary = self.summarize_comments(issue.key, comments)
            print(f"  - Generated summary")

            # Structure bug data as JSON
            bug_data = self.structure_bug_data(issue, comments, summary)
            all_bug_data.append(bug_data)

            print(f"  - Bug data structured ✓")

        # Save all bug data to a JSON file
        output_file = "bug_report.json"
        with open(output_file, 'w') as f:
            json.dump(all_bug_data, f, indent=2, default=str)
        print(f"\n\n{'='*50}")
        print(f"All bug data saved to {output_file}")
        print("="*50)

        # Format bug report table
        print("\nFormatting bug report table...")
        slack_table_message = self.format_slack_table(all_bug_data)

        # Save formatted report to text file
        report_file = "bug_report.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(slack_table_message)
        print(f"✅ Report saved to {report_file}")

        # # Commented out - Slack communication disabled
        # print("\nSending report to Slack...")
        # response = self.post_to_slack(slack_table_message)
        #
        # if response and response.status_code == 200:
        #     print("✅ Report successfully posted to Slack!")
        # else:
        #     print("❌ Failed to post report to Slack")

        print("\n" + "=" * 50)
        print(f"Completed! Processed {len(bugs)} bugs")
        print("=" * 50)


if __name__ == "__main__":
    summarizer = JiraBugSummarizer()
    summarizer.run()
