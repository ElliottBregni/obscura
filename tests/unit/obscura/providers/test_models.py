from obscura.providers.models import ToolCallDefinition


def test_tool_call_definition_adds_empty_properties_for_object_schema() -> None:
    tool = ToolCallDefinition(
        name="ping",
        description="Parameterless ping tool",
        parameters={"type": "object"},
    )

    result = tool.to_openai_function()

    assert result["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }
