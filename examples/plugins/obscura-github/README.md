# obscura-github

Reference plugin for the Obscura plugin platform demonstrating the full contract:

- **2 capabilities**: `repo.read` (default grant), `pr.comment` (requires approval)
- **4 tools**: `github_search_repo`, `github_get_file`, `github_list_branches`, `github_comment_pr`
- **1 workflow**: `review_pull_request`
- **1 instruction overlay**: GitHub code review best practices
- **Policy hints**: `pr.comment` recommends approval, `repo.read` recommends allow
- **Healthcheck**: checks `gh` binary availability

## Install

```bash
pip install -e examples/plugins/obscura-github
# or
obscura plugin install ./examples/plugins/obscura-github
```

## Configuration

Set `GITHUB_TOKEN` environment variable with a GitHub personal access token.

## Usage

Once installed, the plugin's tools become available to agents with the
appropriate capability grants:

```
/capability grant repo.read --agent my-agent
/capability grant pr.comment --agent my-agent
```
