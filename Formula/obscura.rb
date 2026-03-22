class Obscura < Formula
  include Language::Python::Virtualenv

  desc "Multi-agent AI runtime with tool execution and memory"
  homepage "https://github.com/ElliottBregni/obscura"
  url "https://github.com/ElliottBregni/obscura/archive/refs/tags/v0.2.0.tar.gz"
  sha256 "62cb07e8dd3647691cf49ea0e6cac6b4e9e5cbdd8da58f5467d1085c19e6cfbe"
  version "0.2.0"
  license "MIT"

  depends_on "python@3.13"
  depends_on "uv" => :build

  def install
    # Create a virtualenv and install into it using uv
    venv = virtualenv_create(libexec, "python3.13")

    # Install the package and all core dependencies
    system "uv", "pip", "install", ".",
           "--python", libexec/"bin/python",
           "--no-cache-dir"

    # Symlink the CLI entry points into the Homebrew bin
    bin.install_symlink libexec/"bin/obscura"
    bin.install_symlink libexec/"bin/obscura-mcp"

    # Create default data directories
    (var/"obscura").mkpath
    (etc/"obscura").mkpath
  end

  def post_install
    # Ensure ~/.obscura/ structure exists for the installing user
    obscura_home = Pathname.new(Dir.home) / ".obscura"
    %w[output memory vector_memory plugins specs state mcp hooks].each do |subdir|
      (obscura_home / subdir).mkpath
    end
  end

  def caveats
    <<~EOS
      Obscura data is stored in ~/.obscura/

      Quick start:
        obscura                          # interactive REPL
        obscura -b claude "hello"        # one-shot with Claude
        obscura -b copilot "explain"     # one-shot with Copilot

      MCP server:
        obscura-mcp                      # stdio transport (default)
        obscura-mcp --transport sse      # SSE on port 8080

      Configuration:
        Workspace specs:  ~/.obscura/specs/
        Plugins:          ~/.obscura/plugins/
        MCP servers:      ~/.obscura/mcp/
    EOS
  end

  test do
    assert_match "obscura", shell_output("#{bin}/obscura --help")
  end
end
