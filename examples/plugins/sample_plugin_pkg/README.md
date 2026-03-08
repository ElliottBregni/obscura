# sample_plugin_pkg

This is a small example plugin package for the Obscura project.

It exposes an entry point group "obscura.tool_provider" with a single
entry "sample" that points to `sample_plugin_pkg:make_provider`.

Usage (install with pip):

    pip install -e .

Then the host application can discover the provider via pkg_resources or
importlib.metadata entry points and call `make_provider()` to get the
provider instance.
