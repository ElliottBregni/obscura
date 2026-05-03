"""User-interaction tools (ask_user, user_ask, user_interact)."""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from obscura.agent.interaction import AttentionPriority
from obscura.core.tool_context import current_tool_context
from obscura.core.tools import tool
from obscura.notifications.native import NativeNotifier
from obscura.tools.system._policy import Policy


class UI:
    """User-interaction tool namespace."""

    # ------------------------------------------------------------------
    # Module-level callback state (now ClassVars).
    # ``ask_user_callback`` is set by the CLI layer.  When ``None`` the
    # tool falls back to returning an error asking the model to rephrase
    # as a text question.  ``ask_user_called`` flags whether ask_user
    # fired during a turn so the CLI can skip auto-detection.
    # ------------------------------------------------------------------

    ask_user_callback: ClassVar[Any] = None
    ask_user_called: ClassVar[bool] = False
    user_interact_callback: ClassVar[Any] = None

    # ------------------------------------------------------------------
    # Setters / accessors
    # ------------------------------------------------------------------

    @classmethod
    def set_ask_user_callback(cls, cb: Any) -> None:
        """Register the CLI callback for the ``ask_user`` tool."""
        cls.ask_user_callback = cb

    @classmethod
    def was_ask_user_called(cls) -> bool:
        """Return whether ``ask_user`` was invoked since the last reset."""
        return cls.ask_user_called

    @classmethod
    def reset_ask_user_called(cls) -> None:
        """Reset the per-turn ``ask_user`` flag."""
        cls.ask_user_called = False

    @classmethod
    def set_user_interact_callback(cls, cb: Any) -> None:
        """Register the CLI callback for the ``user_interact`` tool."""
        cls.user_interact_callback = cb

    @classmethod
    def resolve_user_interact_callback(cls) -> Any:
        """Return the active user_interact callback (ToolContext first, global fallback)."""
        ctx = current_tool_context()
        cb = ctx.user_interact_callback if ctx is not None else None
        return cb if cb is not None else cls.user_interact_callback

    # ------------------------------------------------------------------
    # ask_user — interactive choice/question tool
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "ask_user",
        "Present the user with a question and a set of choices, and return "
        "their selection.  Use this when you need the user to pick between "
        "options or confirm a decision before proceeding.",
        {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to present to the user.",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of choices the user can pick from. "
                    "If empty, a free-text input is shown instead.",
                },
                "allow_custom": {
                    "type": "boolean",
                    "description": "If true, the user can type a custom response "
                    "in addition to the listed choices. Defaults to false.",
                },
            },
            "required": ["question"],
        },
    )
    async def ask_user(
        question: str,
        choices: list[str] | None = None,
        allow_custom: bool = False,
    ) -> str:
        """Present choices to the user via the TUI widget and return the selection."""
        UI.ask_user_called = True

        ctx = current_tool_context()
        cb = ctx.ask_user_callback if ctx is not None else None
        if cb is None:
            cb = UI.ask_user_callback
        if cb is None:
            return Policy.json_error(
                "no_ui",
                detail="Interactive UI not available. "
                "Ask the user directly in your text response instead.",
            )

        try:
            result = await cb(
                question=question,
                choices=choices or [],
                allow_custom=allow_custom,
            )
            return json.dumps({"ok": True, "selected": result})
        except Exception as exc:
            return Policy.json_error("ask_user_failed", detail=str(exc))

    @staticmethod
    @tool(
        "user_ask",
        "Present the user with one or more structured questions.  Accepts the "
        "Claude Code AskUserQuestion format — an array of question objects each "
        "containing a question string, header, options with labels/descriptions, "
        "and a multiSelect flag.  Returns the user's answers.",
        {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "One or more questions to present to the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The full question text.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Short label displayed as a chip/tag.",
                            },
                            "options": {
                                "type": "array",
                                "description": "Available choices.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Display text for the option.",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of what this option means.",
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Allow multiple selections.",
                            },
                        },
                        "required": ["question"],
                    },
                },
                "question": {
                    "type": "string",
                    "description": "Simple question text (flat format, alternative to questions array).",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Flat list of choices (used with question param).",
                },
            },
        },
    )
    async def user_ask(
        questions: list[dict[str, Any]] | None = None,
        question: str | None = None,
        choices: list[str] | None = None,
    ) -> str:
        """Handle Claude Code AskUserQuestion format by flattening into ask_user calls.

        Accepts either the structured ``questions`` array (Claude Code style) or a
        flat ``question`` string + optional ``choices`` list (Copilot / simple style).
        """
        UI.ask_user_called = True

        ctx = current_tool_context()
        cb = ctx.ask_user_callback if ctx is not None else None
        if cb is None:
            cb = UI.ask_user_callback
        if cb is None:
            return Policy.json_error(
                "no_ui",
                detail="Interactive UI not available. "
                "Ask the user directly in your text response instead.",
            )

        # Flat question fallback (Copilot or simple invocation)
        if not questions and question:
            questions = [
                {
                    "question": question,
                    "options": [{"label": c, "description": ""} for c in (choices or [])],
                }
            ]

        if not questions:
            return Policy.json_error("invalid_args", detail="No questions provided.")

        answers: dict[str, str] = {}
        for q_obj in questions:
            q_text = q_obj.get("question", "")
            if not q_text:
                continue
            header = q_obj.get("header", "")
            options = q_obj.get("options", [])
            multi = q_obj.get("multiSelect", False)

            # Build choice labels from structured options
            choice_labels: list[str] = []
            for opt in options:
                label = opt.get("label", "")
                desc = opt.get("description", "")
                if label and desc:
                    choice_labels.append(f"{label} — {desc}")
                elif label:
                    choice_labels.append(label)

            prompt = f"[{header}] {q_text}" if header else q_text

            try:
                result = await cb(
                    question=prompt,
                    choices=choice_labels,
                    allow_custom=True,
                    multi_select=multi,
                )
                answers[q_text] = result
            except TypeError:
                # Callback doesn't support multi_select — fall back
                try:
                    result = await cb(
                        question=prompt,
                        choices=choice_labels,
                        allow_custom=True,
                    )
                    answers[q_text] = result
                except Exception as exc:
                    answers[q_text] = f"error: {exc}"
            except Exception as exc:
                answers[q_text] = f"error: {exc}"

        return json.dumps({"ok": True, "answers": answers})

    # ------------------------------------------------------------------
    # user_interact — unified permission / notification / question tool
    # ------------------------------------------------------------------

    @staticmethod
    async def handle_ui_permission(action: str, reason: str, risk: str) -> str:
        """Handle permission mode of user_interact."""
        cb = UI.resolve_user_interact_callback()
        if cb is None:
            return Policy.json_error(
                "no_ui",
                detail="Interactive UI not available. "
                "Ask the user directly in your text response instead.",
            )
        try:
            result = await cb(
                mode="permission",
                action=action,
                reason=reason,
                risk=risk,
            )
            approved = result.get("approved", False)
            return json.dumps(
                {
                    "ok": True,
                    "approved": approved,
                    "action": "approve" if approved else "deny",
                },
            )
        except Exception as exc:
            return Policy.json_error("permission_failed", detail=str(exc))

    @staticmethod
    async def handle_ui_notify(
        title: str,
        message: str,
        priority: str,
        channels: list[str] | None,
    ) -> str:
        """Handle notify mode of user_interact."""
        resolved_channels = channels or ["tui", "bell"]
        delivered: list[str] = []

        # TUI channel — uses callback if available
        cb = UI.resolve_user_interact_callback()
        if "tui" in resolved_channels and cb is not None:
            try:
                await cb(
                    mode="notify",
                    title=title,
                    message=message,
                    priority=priority,
                )
                delivered.append("tui")
            except Exception:
                pass

        # OS notification channel — use NativeNotifier
        if "os" in resolved_channels:
            try:
                from obscura.agent.interaction import AttentionPriority
                from obscura.notifications.native import NativeNotifier

                prio_map = {
                    "low": AttentionPriority.LOW,
                    "normal": AttentionPriority.NORMAL,
                    "high": AttentionPriority.HIGH,
                    "critical": AttentionPriority.CRITICAL,
                }
                notifier = NativeNotifier()
                await notifier.notify(
                    title,
                    message,
                    priority=prio_map.get(priority, AttentionPriority.NORMAL),
                    sound=False,  # sound handled separately via "sound" channel
                )
                delivered.append("os")
            except Exception:
                pass

        # Terminal bell
        if "bell" in resolved_channels:
            sys.stdout.write("\a")
            sys.stdout.flush()
            delivered.append("bell")

        # Sound (macOS only via NativeNotifier)
        if "sound" in resolved_channels:
            try:
                if sys.platform == "darwin":
                    import asyncio as _asyncio

                    proc = await _asyncio.create_subprocess_exec(
                        "afplay",
                        "/System/Library/Sounds/Glass.aiff",
                        stdout=_asyncio.subprocess.DEVNULL,
                        stderr=_asyncio.subprocess.DEVNULL,
                    )
                    await proc.communicate()
                    delivered.append("sound")
            except Exception:
                pass

        return json.dumps({"ok": True, "delivered": True, "channels": delivered})

    @staticmethod
    async def handle_ui_question(
        question: str,
        choices: list[str] | None,
        allow_custom: bool,
    ) -> str:
        """Handle question mode of user_interact."""
        cb = UI.resolve_user_interact_callback()
        if cb is None:
            return Policy.json_error(
                "no_ui",
                detail="Interactive UI not available. "
                "Ask the user directly in your text response instead.",
            )
        try:
            result = await cb(
                mode="question",
                question=question,
                choices=choices or [],
                allow_custom=allow_custom,
            )
            return json.dumps({"ok": True, "selected": result.get("selected", "")})
        except Exception as exc:
            return Policy.json_error("question_failed", detail=str(exc))

    @staticmethod
    async def handle_ui_multi_select(
        question: str,
        choices: list[str] | None,
    ) -> str:
        """Handle multi_select mode of user_interact."""
        cb = UI.resolve_user_interact_callback()
        if cb is None:
            return Policy.json_error(
                "no_ui",
                detail="Interactive UI not available.",
            )
        if not choices:
            return Policy.json_error("no_choices", detail="Multi-select requires choices.")
        try:
            result = await cb(
                mode="multi_select",
                question=question,
                choices=choices,
            )
            return json.dumps({"ok": True, "selected": result.get("selected", [])})
        except Exception as exc:
            return Policy.json_error("multi_select_failed", detail=str(exc))

    @staticmethod
    @tool(
        "user_interact",
        "Interact with the user. "
        "permission: action + reason + risk → approved true/false. "
        "notify: title + message + priority (no response). "
        "question: question + optional choices (free-text if no choices). "
        "multi_select: question + choices → list of selected items.",
        {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["permission", "notify", "question", "multi_select"],
                    "description": "Interaction mode.",
                },
                "action": {
                    "type": "string",
                    "description": "(permission) The action being requested.",
                },
                "reason": {
                    "type": "string",
                    "description": "(permission) Why this action is needed.",
                },
                "risk": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "(permission) Risk level affecting visual styling.",
                },
                "title": {
                    "type": "string",
                    "description": "(notify) Notification title.",
                },
                "message": {
                    "type": "string",
                    "description": "(notify/permission) Message body.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                    "description": "(notify) Priority level affecting delivery channels.",
                },
                "channels": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["tui", "os", "bell", "sound"]},
                    "description": "(notify) Delivery channels. Default: ['tui', 'bell'].",
                },
                "question": {
                    "type": "string",
                    "description": "(question) The question to present.",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "(question) Optional list of choices.",
                },
                "allow_custom": {
                    "type": "boolean",
                    "description": "(question) Allow free-text response alongside choices.",
                },
            },
            "required": ["mode"],
        },
    )
    async def user_interact(
        mode: str,
        # permission params
        action: str = "",
        reason: str = "",
        risk: str = "low",
        # notify params
        title: str = "",
        message: str = "",
        priority: str = "normal",
        channels: list[str] | None = None,
        # question params
        question: str = "",
        choices: list[str] | None = None,
        allow_custom: bool = False,
    ) -> str:
        """Unified user interaction tool with permission, notify, and question modes."""
        if mode == "permission":
            return await UI.handle_ui_permission(action, reason, risk)
        if mode == "notify":
            return await UI.handle_ui_notify(title, message, priority, channels)
        if mode == "question":
            return await UI.handle_ui_question(question, choices, allow_custom)
        if mode == "multi_select":
            return await UI.handle_ui_multi_select(question, choices)
        return Policy.json_error("invalid_mode", detail=f"Unknown mode: {mode}")
