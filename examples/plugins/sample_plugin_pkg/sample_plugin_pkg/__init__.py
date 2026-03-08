def make_provider(*args, **kwargs):
    """Factory function returning a simple provider object.

    The real obscura framework will import this via the entry point
    'obscura.tool_provider' and call make_provider() to obtain the
    provider instance. For this example we return a simple callable
    or object with a 'provide' method.
    """

    class SampleProvider:
        def __init__(self):
            self.name = "sample_provider"

        def provide(self, data=None):
            """Example provide method that echoes input with a message."""
            return {"provider": self.name, "echo": data}

        def __repr__(self):
            return f"<SampleProvider name={self.name}>"

    return SampleProvider()
