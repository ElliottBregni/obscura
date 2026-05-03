"""Git operations exposed as a unified-dispatch tool."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from obscura.core.tools import tool
from obscura.tools.system._policy import Policy
import logging

logger = logging.getLogger(__name__)


class Git:
    """Git-operations tool namespace."""

    @staticmethod
    async def git_subprocess(
        args: list[str],
        cwd: str = "",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Run a git command and return parsed result."""
        git_cmd = shutil.which("git")
        if git_cmd is None:
            return {"ok": False, "error": "git_not_found"}
        work_dir = cwd or str(Path.cwd())
        proc = await asyncio.create_subprocess_exec(
            git_cmd,
            *args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            logger.debug("suppressed exception in git_subprocess", exc_info=True)
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "timeout", "git_args": args, "cwd": work_dir}
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        # Git writes many confirmations (commit, push, branch) to stderr.
        # Merge into stdout so the LLM backend always sees the output.
        combined = out or err if proc.returncode == 0 else out
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "git_command": f"git {' '.join(args)}",
            "cwd": work_dir,
            "stdout": combined,
            "stderr": err,
        }

    @staticmethod
    @tool(
        "git",
        "Unified git operations: status, diff, log, commit, branch, push, tag.",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "diff",
                        "log",
                        "commit",
                        "branch",
                        "push",
                        "tag",
                    ],
                    "description": "Git operation to perform.",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (commit) or tag annotation (tag create).",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage (commit). Use ['.'] for all changes.",
                },
                "ref": {
                    "type": "string",
                    "description": "Ref/branch/tag name. Used by diff, log, branch, tag.",
                },
                "remote": {
                    "type": "string",
                    "description": "Remote name for push (default 'origin').",
                },
                "sub_action": {
                    "type": "string",
                    "description": "Sub-operation: branch='list|create|switch', tag='list|create|delete'.",
                },
                "path": {
                    "type": "string",
                    "description": "File/dir path filter (diff, log).",
                },
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes (diff --cached).",
                },
                "stat_only": {
                    "type": "boolean",
                    "description": "Show diffstat only (diff --stat).",
                },
                "short": {
                    "type": "boolean",
                    "description": "Short format (status, default true).",
                },
                "max_count": {
                    "type": "integer",
                    "description": "Number of commits (log, default 10).",
                },
                "oneline": {
                    "type": "boolean",
                    "description": "One-line format (log, default true).",
                },
                "author": {
                    "type": "string",
                    "description": "Filter by author (log).",
                },
                "since": {
                    "type": "string",
                    "description": "Show commits after date (log, e.g. '2024-01-01').",
                },
                "set_upstream": {
                    "type": "boolean",
                    "description": "Set upstream tracking (push -u).",
                },
                "push_tags": {
                    "type": "boolean",
                    "description": "Push all tags (push --tags).",
                },
                "cwd": {"type": "string"},
            },
            "required": ["action"],
        },
    )
    async def git(  # noqa: C901 — unified dispatch, complexity is expected
        action: str,
        message: str = "",
        files: list[str] | None = None,
        ref: str = "",
        remote: str = "origin",
        sub_action: str = "list",
        path: str = "",
        staged: bool = False,
        stat_only: bool = False,
        short: bool = True,
        max_count: int = 10,
        oneline: bool = True,
        author: str = "",
        since: str = "",
        set_upstream: bool = False,
        push_tags: bool = False,
        cwd: str = "",
    ) -> str:
        # -- status --
        if action == "status":
            args = ["status"]
            if short:
                args.append("--short")
            args.append("--branch")
            return json.dumps(await Git.git_subprocess(args, cwd=cwd))

        # -- diff --
        if action == "diff":
            args = ["diff"]
            if staged:
                args.append("--cached")
            if stat_only:
                args.append("--stat")
            if ref:
                args.append(ref)
            if path:
                args.extend(["--", path])
            result = await Git.git_subprocess(args, cwd=cwd)
            if result.get("ok") and len(result.get("stdout", "")) > 100_000:
                result["stdout"] = result["stdout"][:100_000] + "\n... (truncated)"
                result["truncated"] = True
            return json.dumps(result)

        # -- log --
        if action == "log":
            try:
                max_count = int(max_count)
            except (TypeError, ValueError):
                logger.debug("suppressed exception in git", exc_info=True)
                max_count = 10
            count = max(1, min(max_count, 100))
            args = ["log", f"-{count}"]
            if oneline:
                args.append("--oneline")
            else:
                args.extend(["--format=%H %an %ae %ai%n%s%n%b---"])
            if author:
                args.append(f"--author={author}")
            if since:
                args.append(f"--since={since}")
            if ref:
                args.append(ref)
            if path:
                args.extend(["--", path])
            return json.dumps(await Git.git_subprocess(args, cwd=cwd))

        # -- commit --
        if action == "commit":
            if not message.strip():
                return Policy.json_error("empty_commit_message")
            stage_files = files or ["."]
            add_result = await Git.git_subprocess(["add", *stage_files], cwd=cwd)
            if not add_result.get("ok"):
                return json.dumps(add_result)
            return json.dumps(
                await Git.git_subprocess(["commit", "-m", message], cwd=cwd),
            )

        # -- branch --
        if action == "branch":
            if sub_action == "list":
                return json.dumps(
                    await Git.git_subprocess(["branch", "-a", "--no-color"], cwd=cwd),
                )
            if sub_action == "create":
                if not ref.strip():
                    return Policy.json_error("branch_name_required")
                return json.dumps(
                    await Git.git_subprocess(["checkout", "-b", ref], cwd=cwd),
                )
            if sub_action == "switch":
                if not ref.strip():
                    return Policy.json_error("branch_name_required")
                return json.dumps(
                    await Git.git_subprocess(["checkout", ref], cwd=cwd),
                )
            return Policy.json_error(
                "invalid_sub_action",
                sub_action=sub_action,
                valid=["list", "create", "switch"],
            )

        # -- push --
        if action == "push":
            args = ["push"]
            if set_upstream:
                args.append("-u")
            args.append(remote)
            if ref:
                args.append(ref)
            if push_tags:
                args.append("--tags")
            return json.dumps(
                await Git.git_subprocess(args, cwd=cwd, timeout=60.0),
            )

        # -- tag --
        if action == "tag":
            if sub_action == "list":
                return json.dumps(
                    await Git.git_subprocess(
                        ["tag", "-l", "--sort=-creatordate"],
                        cwd=cwd,
                    ),
                )
            if sub_action == "create":
                if not ref.strip():
                    return Policy.json_error("tag_name_required")
                args = ["tag"]
                if message:
                    args.extend(["-a", ref, "-m", message])
                else:
                    args.append(ref)
                return json.dumps(await Git.git_subprocess(args, cwd=cwd))
            if sub_action == "delete":
                if not ref.strip():
                    return Policy.json_error("tag_name_required")
                return json.dumps(
                    await Git.git_subprocess(["tag", "-d", ref], cwd=cwd),
                )
            return Policy.json_error(
                "invalid_sub_action",
                sub_action=sub_action,
                valid=["list", "create", "delete"],
            )

        return Policy.json_error(
            "invalid_action",
            action=action,
            valid=["status", "diff", "log", "commit", "branch", "push", "tag"],
        )
