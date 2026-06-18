# SkillShare CLI

Command-line client for the [SkillShare](https://skillshare.think41.com) registry — search the marketplace, pull skills/MCPs/notes, manage your org's resources, and contribute local artifacts back to the platform.

## Install

```bash
pip install skillshare-cli
```

Or download a standalone binary from the [releases page](https://github.com/Aniket-think41/skillshare-cli/releases):

```bash
# Linux / macOS / Windows
curl -L https://github.com/Aniket-think41/skillshare-cli/releases/latest/download/skillshare-linux-amd64 -o skillshare
chmod +x skillshare
sudo mv skillshare /usr/local/bin/skillshare
```

## Quick start

```bash
# Log in via the browser (device-authorization flow)
skillshare login

# Who am I?
skillshare whoami

# Search the public marketplace
skillshare search "code review" --type SKILL
skillshare search postgres --type MCP

# List my orgs
skillshare orgs

# Browse org resources
skillshare list --org think41

# Get full resource content
skillshare get pr-reviewer

# Pull a resource to disk (SKILL.md / mcp-config.json / note.md + attachments)
skillshare pull pr-reviewer -o ./skills

# Create a note
echo "# My architecture decisions" | skillshare add note --org think41 --title "ADR-001" --tags "architecture"

# See what's new
skillshare inbox
skillshare watch

# Scan local machine for unshared skills/MCPs/notes
skillshare scan

# Push a local artifact to your org (secrets redacted, preview first)
skillshare push --org think41
```

## Auth

`skillshare login` runs the OAuth device-authorization flow: you open a URL in your browser, sign in (or create an account), and approve the CLI. Credentials are stored in `~/.config/skillshare/credentials.json` (chmod 600) and auto-refresh.

For CI/automation, set environment variables:

```bash
export SKILLSHARE_API_URL=https://skillshare-backend-1081098542602.us-central1.run.app
export SKILLSHARE_TOKEN=skst_...   # Personal Access Token from the dashboard
```

## Commands

| Command | Description |
|---------|-------------|
| `login` | Sign in via browser (device flow) |
| `logout` | Revoke stored token |
| `whoami` | Show authenticated user |
| `orgs` | List my organizations |
| `tokens` | List personal access tokens |
| `search` | Search the public marketplace |
| `get` | Show a resource's full content |
| `pull` | Save a resource locally |
| `list` | List org/pod resources |
| `add note` | Create a NOTE from a file or stdin |
| `inbox` | Notifications for your scopes |
| `watch` | Desktop notifications for new items |
| `scan` | Find local artifacts not on the platform |
| `push` | Push local artifacts to the platform |
| `github` | Import resources from a public GitHub repo |
| `install` | Install a resource into local tools |
| `star` / `pin` | Endorse or curate resources |
| `avatar` | Set your profile photo |
| `org-logo` | Set an org's logo (admin) |
| `publish` | Publish a resource to the marketplace |
| `import` | Copy a public resource into your org |
| `follow` | Follow publishers/orgs for notifications |
| `status` | One-line status for AI tool status bars |
| `panel` | Open the clickable install panel |
| `feedback` | Send product feedback |
| `setup-statusline` | Show install/push counts in Claude Code's status bar |

## Configuration

All configuration lives in `~/.config/skillshare/`. The CLI respects these environment variables:

- `SKILLSHARE_API_URL` — backend base URL (default: `https://skillshare-backend-1081098542602.us-central1.run.app`)
- `SKILLSHARE_TOKEN` — bearer token (PAT or access token; overrides stored credentials)
- `SKILLSHARE_CONFIG_DIR` — config directory (default `~/.config/skillshare`)

## License

MIT
