# codex-auth-manager

Small `uv`-managed Codex CLI OAuth account manager.

Command: `cxauth`

## Why this exists

Codex stores its active OAuth session in:

```text
~/.codex/auth.json
```

This tool stores named copies in an isolated vault:

```text
~/.codex-auth/accounts/<name>/auth.json
```

Unlike the reference tool this was inspired by, live quota checks always run with `account/read refreshToken=true` and sync any rotated auth file back into the saved account. That avoids losing a newly rotated refresh token inside a temporary `CODEX_HOME`.

## Installation

Preferred install from GitHub:

```bash
uv tool install git+https://github.com/dakixr/codex-auth-manager.git
```

Upgrade later:

```bash
uv tool upgrade codex-auth-manager
```

Development install from a local clone:

```bash
uv tool install --editable .
```

Or run without installing:

```bash
uv run cxauth --help
```

## Main workflow

Login and save each account. This uses Codex device authorization by default and runs login in an isolated temporary `CODEX_HOME`, so logging in one account does not let Codex touch or refresh the currently active account first:

```bash
cxauth login dr7878
cxauth login apavel
cxauth login javitj
```

If you explicitly want the localhost browser callback flow:

```bash
cxauth login dr7878 --browser
```

Choose the account with the lowest current Codex quota usage and switch to it:

```bash
cxauth use-best
```

Show accounts without touching live auth:

```bash
cxauth list
```

Check one account's live quota:

```bash
cxauth quota dr7878
```

Manually switch to a named account:

```bash
cxauth switch dr7878
```

Export every saved auth JSON for another system. Without `-o`, this prints bearer credentials to stdout:

```bash
cxauth export -o codex-auths.json
cxauth export
```

## Commands

```bash
cxauth current
cxauth switch NAME
cxauth login NAME [--browser]
cxauth quota NAME
cxauth use-best [NAME ...]
cxauth remove NAME  # alias: rm
cxauth export [-o FILE]
```

## Export format

`cxauth export` emits a JSON object keyed by account name:

```json
{
  "dr7878": {
    "auth_mode": "chatgpt",
    "tokens": {
      "access_token": "...",
      "id_token": "...",
      "refresh_token": "..."
    }
  }
}
```

Treat exports as secrets. The `-o` path is written with `0600` permissions.

## Limits

If OpenAI revokes one account's OAuth grant when another account logs in with the same Codex OAuth client, no local file manager can prevent that. This tool can preserve token rotations that happen during its own refresh/quota operations, but it cannot override server-side OAuth revocation policy.
