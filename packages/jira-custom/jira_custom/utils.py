"""Utility functions for jira-custom."""

import re
import time
import click


class IssueKeyType(click.ParamType):
    """Click parameter type that accepts an issue key or a Jira browse URL."""

    name = "issue_key"

    _URL_RE = re.compile(r"(?:https?://)?[^/]+/browse/([A-Z][A-Z0-9]+-\d+)")

    def convert(self, value, param, ctx):
        if value is None:
            return value
        m = self._URL_RE.match(value)
        if m:
            return m.group(1)
        return value


ISSUE_KEY = IssueKeyType()

_JIRA_URL_RE = re.compile(r"(?:https?://)?[^/]+/browse/([A-Z][A-Z0-9]+-\d+)(?:\?(.+))?")


def parse_jira_url(value):
    """Parse a Jira URL or issue key, returning (issue_key, comment_id).

    Supports:
      PROJ-123                -> ("PROJ-123", None)
      https://.../browse/PROJ-123  -> ("PROJ-123", None)
      https://.../browse/PROJ-123?focusedCommentId=197541 -> ("PROJ-123", "197541")
    """
    from urllib.parse import parse_qs

    m = _JIRA_URL_RE.match(value)
    if m:
        issue_key = m.group(1)
        query_string = m.group(2)
        comment_id = None
        if query_string:
            params = parse_qs(query_string)
            if "focusedCommentId" in params:
                comment_id = params["focusedCommentId"][0]
        return issue_key, comment_id
    return value, None


def parse_labels(labels):
    """Parse label arguments, separating adds from removes"""
    if not labels:
        return None, None

    add = []
    remove = []

    for label in labels:
        if label.startswith("-"):
            remove.append(label[1:])
        else:
            add.append(label)

    return add or None, remove or None


def parse_fields(field_args):
    """Parse field arguments (KEY=VALUE format)"""
    if not field_args:
        return None

    fields = {}
    for f in field_args:
        if "=" not in f:
            raise click.ClickException(f"Invalid field format '{f}'. Use KEY=VALUE")
        key, value = f.split("=", 1)
        fields[key] = value
    return fields


def get_issue_type_id_for_project(jira, project_key, type_name):
    """Get issue type ID by name using createmeta endpoint"""
    url = f"{jira._options['server']}/rest/api/3/issue/createmeta/{project_key}/issuetypes"
    response = jira._session.get(url)

    if response.status_code != 200:
        raise click.ClickException(f"Failed to get issue types: {response.text}")

    data = response.json()
    for it in data.get("issueTypes", data.get("values", [])):
        if it.get("name", "").lower() == type_name.lower():
            return it.get("id")

    available = [
        it.get("name") for it in data.get("issueTypes", data.get("values", []))
    ]
    raise click.ClickException(
        f"Issue type '{type_name}' not found in project {project_key}. "
        f"Available: {', '.join(available)}"
    )


def move_issue_type(jira, issue_key, type_name):
    """Change issue type using Bulk Move API (async operation)"""
    issue = jira.issue(issue_key)
    project_key = issue.fields.project.key
    type_id = get_issue_type_id_for_project(jira, project_key, type_name)

    # Build composite mapping key: "PROJECT_KEY,ISSUE_TYPE_ID"
    mapping_key = f"{project_key},{type_id}"

    payload = {
        "sendBulkNotification": True,  # Must be True to avoid permission error
        "targetToSourcesMapping": {
            mapping_key: {
                "issueIdsOrKeys": [issue_key],
                "inferClassificationDefaults": True,
                "inferFieldDefaults": True,
                "inferStatusDefaults": True,
                "inferSubtaskTypeDefault": True,
            }
        },
    }

    # Submit bulk move request
    url = f"{jira._options['server']}/rest/api/3/bulk/issues/move"
    response = jira._session.post(url, json=payload)

    if response.status_code not in (200, 201, 202):
        error_msg = response.text
        try:
            error_data = response.json()
            if "errorMessages" in error_data:
                error_msg = "; ".join(error_data["errorMessages"])
            elif "errors" in error_data:
                error_msg = "; ".join(
                    f"{k}: {v}" for k, v in error_data["errors"].items()
                )
        except Exception:
            pass
        raise click.ClickException(f"Move failed ({response.status_code}): {error_msg}")

    result = response.json()
    task_id = result.get("taskId")

    if not task_id:
        raise click.ClickException(f"No taskId in response: {result}")

    # Poll for completion
    queue_url = f"{jira._options['server']}/rest/api/3/bulk/queue/{task_id}"
    timeout = 120
    start_time = time.time()

    while time.time() - start_time < timeout:
        status_response = jira._session.get(queue_url)
        if status_response.status_code != 200:
            raise click.ClickException(
                f"Failed to check task status: {status_response.text}"
            )

        status_data = status_response.json()
        status = status_data.get("status")

        if status == "COMPLETE":
            return status_data
        elif status == "FAILED":
            raise click.ClickException(f"Move failed: {status_data}")

        time.sleep(2)

    raise click.ClickException(f"Move timed out after {timeout}s (task: {task_id})")


def truncate_text(text, max_len):
    """Truncate text to max_len, adding ellipsis if needed"""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def resolve_account_id(jira, user):
    """Resolve "me", an email or a display name to a Jira Cloud accountId.

    Returns None for "x" (unassign). Raises if nothing or several users match.
    """
    if user.lower() == "x":
        return None
    if user.lower() == "me":
        return jira.myself()["accountId"]

    url = f"{jira._options['server']}/rest/api/3/user/search"
    response = jira._session.get(url, params={"query": user})
    if response.status_code != 200:
        raise click.ClickException(f"User search failed: {response.text}")

    users = [u for u in response.json() if u.get("active", True)]
    # The search matches prefixes; require an exact email or name match
    # so a partial query never picks the wrong person.
    exact = [
        u
        for u in users
        if user.lower()
        in (
            (u.get("emailAddress") or "").lower(),
            (u.get("displayName") or "").lower(),
        )
    ]
    if len(exact) == 1:
        return exact[0]["accountId"]
    candidates = exact or users
    if not candidates:
        raise click.ClickException(f"No Jira user matches '{user}'")
    names = ", ".join(
        f"{u.get('displayName')} <{u.get('emailAddress', '?')}>" for u in candidates
    )
    raise click.ClickException(
        f"'{user}' is not an exact email or name of one user; candidates: {names}"
    )


def verify_assignee(jira, issue_key, account_id):
    """Read the issue back and fail if its assignee isn't account_id."""
    assignee = jira.issue(issue_key, fields="assignee").fields.assignee
    actual = getattr(assignee, "accountId", None) if assignee else None
    if actual != account_id:
        name = assignee.displayName if assignee else "Unassigned"
        raise click.ClickException(
            f"Assignee of {issue_key} is {name}, not the requested user"
        )
