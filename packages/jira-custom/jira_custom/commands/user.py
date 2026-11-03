"""User commands for jira-custom."""

import click

from ..client import get_jira_client


def show_me_fn():
    """Show current user info"""
    jira = get_jira_client()
    user = jira.myself()  # a dict, not a resource object

    click.echo(f"Name:     {user.get('displayName')}")
    click.echo(f"Email:    {user.get('emailAddress')}")
    click.echo(f"Account:  {user.get('accountId')}")
    click.echo(f"Active:   {user.get('active')}")
    click.echo(f"Timezone: {user.get('timeZone', 'N/A')}")


def show_serverinfo_fn():
    """Show Jira server information"""
    jira = get_jira_client()
    info = jira.server_info()

    click.echo(f"Server:      {info.get('baseUrl', 'N/A')}")
    click.echo(f"Version:     {info.get('version', 'N/A')}")
    click.echo(f"Build:       {info.get('buildNumber', 'N/A')}")
    click.echo(f"Deployment:  {info.get('deploymentType', 'N/A')}")
    click.echo(f"Server Time: {info.get('serverTime', 'N/A')}")


@click.command("me")
def me_cmd():
    """Show current user"""
    show_me_fn()


@click.command("serverinfo")
def serverinfo_cmd():
    """Show server info"""
    show_serverinfo_fn()
