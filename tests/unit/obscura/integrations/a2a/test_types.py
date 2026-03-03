"""Tests for sdk.a2a.types — A2A protocol data model."""

from __future__ import annotations

import json

from obscura.integrations.a2a.types import (
    A2AMessage,
    A2AMethod,
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    AuthScheme,
    DataPart,
    FileContent,
    FilePart,
    InvalidTransitionError,
    PushNotificationConfig,
    SendMessageConfiguration,
    Task,
    TaskArtifactUpdateEvent,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    VersionNotSupportedError,
)


class TestTaskState:
    def test_all_states_exist(self) -> None:
        states = {s.value for s in TaskState}
        assert states == {
            "pending",
            "working",
            "input-required",
            "auth-required",
            "completed",
            "failed",
            "canceled",
            "rejected",
        }

    def test_terminal_states(self) -> None:
        assert TaskState.COMPLETED in TERMINAL_STATES
        assert TaskState.FAILED in TERMINAL_STATES
        assert TaskState.CANCELED in TERMINAL_STATES
        assert TaskState.REJECTED in TERMINAL_STATES
        assert TaskState.WORKING not in TERMINAL_STATES

    def test_valid_transitions_from_pending(self) -> None:
        allowed = VALID_TRANSITIONS[TaskState.PENDING]
        assert TaskState.WORKING in allowed
        assert TaskState.REJECTED in allowed
        assert TaskState.COMPLETED not in allowed

    def test_valid_transitions_from_working(self) -> None:
        allowed = VALID_TRANSITIONS[TaskState.WORKING]
        assert TaskState.INPUT_REQUIRED in allowed
        assert TaskState.COMPLETED in allowed
        assert TaskState.FAILED in allowed
        assert TaskState.CANCELED in allowed
        assert TaskState.PENDING not in allowed

    def test_terminal_states_have_no_transitions(self) -> None:
        for state in TERMINAL_STATES:
            assert len(VALID_TRANSITIONS[state]) == 0


class TestParts:
    def test_text_part_serialization(self) -> None:
        part = TextPart(text="hello world")
        d = part.model_dump()
        assert d["kind"] == "text"
        assert d["text"] == "hello world"

    def test_file_part_with_uri(self) -> None:
        part = FilePart(
            file=FileContent(
                name="report.pdf",
                mimeType="application/pdf",
                uri="https://example.com/report.pdf",
            )
        )
        d = part.model_dump()
        assert d["kind"] == "file"
        assert d["file"]["uri"] == "https://example.com/report.pdf"

    def test_file_part_with_bytes(self) -> None:
        part = FilePart(
            file=FileContent(name="img.png", mimeType="image/png", bytes="iVBORw0KGgo=")
        )
        d = part.model_dump()
        assert d["file"]["bytes"] == "iVBORw0KGgo="

    def test_data_part(self) -> None:
        part = DataPart(data={"key": "value", "count": 42})
        d = part.model_dump()
        assert d["kind"] == "data"
        assert d["data"]["count"] == 42

    def test_part_with_metadata(self) -> None:
        part = TextPart(text="test", metadata={"lang": "en"})
        d = part.model_dump()
        assert d["metadata"]["lang"] == "en"


class TestMessage:
    def test_user_message(self) -> None:
        msg = A2AMessage(
            role="user",
            messageId="msg-001",
            parts=[TextPart(text="Hello agent")],
        )
        assert msg.role == "user"
        assert msg.parts[0].text == "Hello agent"  # type: ignore[union-attr]
        assert msg.timestamp is not None

    def test_agent_message_with_context(self) -> None:
        msg = A2AMessage(
            role="agent",
            messageId="msg-002",
            parts=[TextPart(text="How can I help?")],
            contextId="ctx-abc",
            taskId="task-123",
        )
        assert msg.contextId == "ctx-abc"
        assert msg.taskId == "task-123"

    def test_message_serialization_roundtrip(self) -> None:
        msg = A2AMessage(
            role="user",
            messageId="msg-003",
            parts=[
                TextPart(text="Process this"),
                DataPart(data={"format": "csv"}),
            ],
        )
        json_str = msg.model_dump_json()
        restored = A2AMessage.model_validate_json(json_str)
        assert restored.messageId == "msg-003"
        assert len(restored.parts) == 2


class TestArtifact:
    def test_artifact_creation(self) -> None:
        artifact = Artifact(
            artifactId="art-001",
            name="report.txt",
            parts=[TextPart(text="Report content here")],
        )
        assert artifact.artifactId == "art-001"
        assert artifact.name == "report.txt"


class TestTask:
    def test_task_creation(self) -> None:
        task = Task(
            id="task-001",
            contextId="ctx-001",
            status=TaskStatus(state=TaskState.PENDING),
        )
        assert task.id == "task-001"
        assert task.status.state == TaskState.PENDING
        assert task.kind == "task"
        assert task.artifacts == []
        assert task.history == []

    def test_task_with_artifacts(self) -> None:
        task = Task(
            id="task-002",
            contextId="ctx-001",
            status=TaskStatus(state=TaskState.COMPLETED),
            artifacts=[Artifact(artifactId="art-1", parts=[TextPart(text="result")])],
        )
        assert len(task.artifacts) == 1

    def test_task_serialization_roundtrip(self) -> None:
        task = Task(
            id="task-003",
            contextId="ctx-002",
            status=TaskStatus(state=TaskState.WORKING),
            history=[
                A2AMessage(role="user", messageId="m1", parts=[TextPart(text="go")])
            ],
        )
        json_str = task.model_dump_json()
        restored = Task.model_validate_json(json_str)
        assert restored.id == "task-003"
        assert restored.status.state == TaskState.WORKING
        assert len(restored.history) == 1


class TestStreamEvents:
    def test_status_update_event(self) -> None:
        event = TaskStatusUpdateEvent(
            taskId="task-001",
            contextId="ctx-001",
            status=TaskStatus(state=TaskState.WORKING),
        )
        assert event.kind == "status-update"
        assert not event.final

    def test_final_status_event(self) -> None:
        event = TaskStatusUpdateEvent(
            taskId="task-001",
            contextId="ctx-001",
            status=TaskStatus(state=TaskState.COMPLETED),
            final=True,
        )
        assert event.final

    def test_artifact_update_event(self) -> None:
        event = TaskArtifactUpdateEvent(
            taskId="task-001",
            contextId="ctx-001",
            artifact=Artifact(artifactId="art-1", parts=[TextPart(text="chunk")]),
            append=True,
            lastChunk=False,
        )
        assert event.kind == "artifact-update"
        assert event.append


class TestAgentCard:
    def test_minimal_card(self) -> None:
        card = AgentCard(name="TestAgent", url="https://example.com/a2a")
        assert card.name == "TestAgent"
        assert card.version == "1.0"
        assert card.protocolVersion == "0.3"
        assert card.capabilities.streaming is True

    def test_full_card(self) -> None:
        card = AgentCard(
            name="Support Agent",
            description="Handles customer support tickets",
            url="https://support.example.com/a2a",
            skills=[
                AgentSkill(
                    id="triage", name="Ticket Triage", description="Classify tickets"
                ),
                AgentSkill(
                    id="resolve", name="Issue Resolution", tags=["billing", "tech"]
                ),
            ],
            capabilities=AgentCapabilities(streaming=True, pushNotifications=True),
            securitySchemes={
                "bearer": AuthScheme(type="http", scheme="bearer"),
            },
            security=[{"bearer": []}],
            provider={"name": "Obscura", "url": "https://obscura.dev"},
        )
        assert len(card.skills) == 2
        assert card.capabilities.pushNotifications

    def test_card_serialization(self) -> None:
        card = AgentCard(
            name="Agent",
            url="https://example.com",
            skills=[AgentSkill(id="s1", name="Skill 1")],
        )
        d = json.loads(card.model_dump_json())
        assert d["name"] == "Agent"
        assert d["skills"][0]["id"] == "s1"


class TestConfiguration:
    def test_send_message_config(self) -> None:
        config = SendMessageConfiguration(
            acceptedOutputModes=["text/plain", "application/json"],
            blocking=True,
            historyLength=10,
        )
        assert config.blocking
        assert config.historyLength == 10

    def test_push_notification_config(self) -> None:
        config = PushNotificationConfig(
            url="https://client.example.com/webhook",
            token="secret-token",
        )
        assert config.url == "https://client.example.com/webhook"


class TestA2AMethod:
    def test_all_methods(self) -> None:
        assert A2AMethod.MESSAGE_SEND.value == "message/send"
        assert A2AMethod.MESSAGE_STREAM.value == "message/stream"
        assert A2AMethod.TASKS_GET.value == "tasks/get"
        assert A2AMethod.TASKS_LIST.value == "tasks/list"
        assert A2AMethod.TASKS_CANCEL.value == "tasks/cancel"


class TestErrors:
    def test_task_not_found(self) -> None:
        err = TaskNotFoundError("task-999")
        assert err.code == -32001
        assert "task-999" in err.message

    def test_task_not_cancelable(self) -> None:
        err = TaskNotCancelableError("task-001", "completed")
        assert err.code == -32002
        assert "completed" in err.message

    def test_invalid_transition(self) -> None:
        err = InvalidTransitionError("task-001", "completed", "working")
        assert err.code == -32003

    def test_version_not_supported(self) -> None:
        err = VersionNotSupportedError("0.1")
        assert err.code == -32005
