"""Engineering automation skill for Obscura.

Integrates Jira and GitHub to automate daily engineering workflows.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from obscura.skills.base import (
    Skill,
    SkillCapability,
    CapabilityType,
    CapabilityParameter,
    CapabilityReturn,
)

logger = logging.getLogger(__name__)


class EngineeringAutomationSkill(Skill):
    """Skill for automating engineering workflows.

    Provides capabilities to:
    - Fetch Jira tickets
    - Review GitHub PRs
    - Generate standup summaries
    - Track sprint progress
    """

    name = "engineering-automation"
    description = "Automate engineering workflows - Jira, GitHub PRs, standups"
    version = "1.0.0"

    # Load config from .obscura/automation/
    config_path = Path.home() / ".obscura" / "automation" / "config.env"

    def __init__(self) -> None:
        super().__init__()
        self._load_config()
        self._automation_module = None

    def _load_config(self) -> None:
        """Load configuration from env file."""
        if self.config_path.exists():
            with open(self.config_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        os.environ.setdefault(key, value)

    def _get_automation(self):
        """Lazy load automation module."""
        if self._automation_module is None:
            # Import from .obscura/automation/
            import sys

            automation_path = Path.home() / ".obscura" / "automation"
            if str(automation_path) not in sys.path:
                sys.path.insert(0, str(automation_path))

            from engineering_workflow import EngineeringAutomation

            self._automation_module = EngineeringAutomation()

        return self._automation_module

    @property
    def capabilities(self) -> list[SkillCapability]:
        """Define skill capabilities."""
        return [
            SkillCapability(
                name="daily_digest",
                description="Generate daily engineering digest with Jira tickets and PRs",
                parameters=[],
                returns=CapabilityReturn(
                    type="object",
                    description="Daily digest with tickets, PRs, and standup summary",
                ),
                capability_type=CapabilityType.QUERY,
            ),
            SkillCapability(
                name="my_tickets",
                description="Get assigned Jira tickets",
                parameters=[
                    CapabilityParameter(
                        name="status_filter",
                        type="string",
                        description="Filter by status (e.g., 'In Progress', 'To Do')",
                        required=False,
                        default="",
                    ),
                ],
                returns=CapabilityReturn(
                    type="array",
                    description="List of assigned Jira tickets",
                ),
                capability_type=CapabilityType.QUERY,
            ),
            SkillCapability(
                name="review_prs",
                description="Get PRs awaiting review",
                parameters=[],
                returns=CapabilityReturn(
                    type="array",
                    description="List of PRs to review",
                ),
                capability_type=CapabilityType.QUERY,
            ),
            SkillCapability(
                name="review_pr",
                description="Review a specific PR by number",
                parameters=[
                    CapabilityParameter(
                        name="pr_number",
                        type="number",
                        description="PR number to review",
                        required=True,
                    ),
                ],
                returns=CapabilityReturn(
                    type="object",
                    description="PR review analysis",
                ),
                capability_type=CapabilityType.QUERY,
            ),
            SkillCapability(
                name="standup_summary",
                description="Generate standup update text",
                parameters=[],
                returns=CapabilityReturn(
                    type="string",
                    description="Formatted standup summary",
                ),
                capability_type=CapabilityType.QUERY,
            ),
        ]

    async def execute(self, capability: str, params: dict[str, Any]) -> Any:
        """Execute a capability."""
        auto = self._get_automation()

        try:
            if capability == "daily_digest":
                digest = await auto.generate_daily_digest()
                return {
                    "date": digest.date,
                    "jira_tickets": digest.jira_tickets,
                    "prs_to_review": digest.prs_to_review,
                    "my_prs": digest.my_prs,
                    "standup_summary": digest.standup_summary,
                }

            elif capability == "my_tickets":
                if auto.jira:
                    tickets = await auto.jira.get_assigned_tickets()
                    status_filter = params.get("status_filter", "")
                    if status_filter:
                        tickets = [t for t in tickets if t["status"] == status_filter]
                    return tickets
                return {"error": "Jira not configured"}

            elif capability == "review_prs":
                if auto.github:
                    review_prs, _ = await auto.github.get_prs_to_review()
                    return review_prs
                return {"error": "GitHub not configured"}

            elif capability == "review_pr":
                pr_number = params.get("pr_number")
                if not pr_number:
                    return {"error": "pr_number required"}
                return await auto.review_pr(pr_number)

            elif capability == "standup_summary":
                digest = await auto.generate_daily_digest()
                return digest.standup_summary

            else:
                return {"error": f"Unknown capability: {capability}"}

        except Exception as e:
            logger.error(f"Error executing {capability}: {e}")
            return {"error": str(e)}

    async def health_check(self) -> dict[str, Any]:
        """Check skill health."""
        auto = self._get_automation()

        return {
            "status": "healthy",
            "jira_configured": auto.jira_config.is_configured() if auto.jira else False,
            "github_configured": auto.github_config.is_configured()
            if auto.github
            else False,
        }

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._automation_module:
            await self._automation_module.close()
