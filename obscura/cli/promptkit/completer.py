"""obscura.cli.promptkit.completer — slash/at/dollar tab completion.

Provides ``SlashCommandCompleter``, the prompt_toolkit ``Completer``
implementation that powers tab completion for:

  * ``/commands``    (top-level slash commands and their subcommands)
  * ``@commands``    (named at-commands inside an utterance, callable
                      anywhere in the line)
  * ``$skills``      (skill chains, callable anywhere in the line)

Consumers
---------
* ``obscura.cli.promptkit.session_factory.create_prompt_session`` —
  installs ``SlashCommandCompleter`` on the ``PromptSession``.
* ``obscura.cli.prompt`` (legacy back-compat shim).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from prompt_toolkit.completion import CompleteEvent, Completer, Completion

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from prompt_toolkit.document import Document


class SlashCommandCompleter(Completer):
    """Tab-complete /commands, @commands, and $skills (including chains)."""

    def __init__(
        self,
        completions: dict[str, list[str]],
        at_command_names: Callable[[], list[str]] | None = None,
        dollar_skill_names: Callable[[], list[str]] | None = None,
    ) -> None:
        self._completions = completions
        self._at_command_names = at_command_names
        self._dollar_skill_names = dollar_skill_names

    @override
    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor.lstrip()

        # /slash commands — only at the very start
        if text.startswith("/"):
            parts = text.split()
            if len(parts) <= 1:
                prefix = text[1:]
                for cmd in sorted(self._completions):
                    if cmd.startswith(prefix):
                        yield Completion(
                            "/" + cmd,
                            start_position=-len(text),
                            display="/" + cmd,
                        )
                return
            cmd = parts[0].lstrip("/")
            subs = self._completions.get(cmd, [])
            if not subs:
                return
            partial = parts[1] if len(parts) > 1 else ""
            for sub in sorted(subs):
                if sub.startswith(partial):
                    yield Completion(sub, start_position=-len(partial))
            return

        # $ and @ — complete the current (last) token in a chain
        # e.g. "$python @rev" -> complete "@review"
        # e.g. "$py" -> complete "$python"
        # e.g. "$python $se" -> complete "$security"
        word = document.get_word_before_cursor(WORD=True)
        if not word:
            return

        if word.startswith("$") and self._dollar_skill_names is not None:
            prefix = word[1:]
            for name in self._dollar_skill_names():
                if name.startswith(prefix):
                    yield Completion(
                        "$" + name,
                        start_position=-len(word),
                        display="$" + name,
                    )
            return

        if word.startswith("@") and self._at_command_names is not None:
            prefix = word[1:]
            for name in self._at_command_names():
                if name.startswith(prefix):
                    yield Completion(
                        "@" + name,
                        start_position=-len(word),
                        display="@" + name,
                    )
            return
