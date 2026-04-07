def test_import_cli_api():
    # Importing the public API should be cheap and should not raise
    import obscura.cli.api as api

    registry = api.get_commands_registry()
    assert isinstance(registry, dict)
