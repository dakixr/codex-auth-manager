# codex-auth-manager

Small `uv`-managed Codex CLI OAuth account manager.

Command: `cxauth`

## What it does

Codex stores its active OAuth session in:

```text
~/.codex/auth.json
```

This tool stores named copies in an isolated vault:

```text
~/.codex-auth/accounts/<name>/auth.json
```

It lets you login, store, inspect, export, and switch between multiple Codex OAuth sessions without manually copying `auth.json` files. Live quota checks refresh tokens safely and sync rotated auth files back into the saved account.

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
cxauth login dakixr
cxauth login work
cxauth login personal
```

If you explicitly want the localhost browser callback flow:

```bash
cxauth login dakixr --browser
```

Choose the best account and switch to it:

```bash
cxauth use-best
```

`use-best` is pace-aware. It compares current usage with how far through each quota window you are:

- usage above expected pace is shown as `in deficit`
- usage below expected pace is shown as `in reserve`
- accounts near weekly exhaustion are avoided
- 5-hour and weekly reset timing affect the score
- quota probes run in a small parallel pool, then the selected account is written once

Show accounts without touching live auth:

```bash
cxauth list
```

Check one account's live quota:

```bash
cxauth quota dakixr
```

Quota output includes usage bars, relative reset timing, and the same pace details used by `use-best`, including reserve/deficit and projected exhaustion.
When checking every account, live quota requests use the same parallel engine as `use-best`.

Check live quota for every saved account:

```bash
cxauth quota
```

Manually switch to a named account:

```bash
cxauth switch dakixr
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
cxauth quota [NAME]
cxauth use-best [NAME ...]
cxauth remove NAME
cxauth export [-o FILE]
```

## Export format

`cxauth export` emits a JSON object keyed by account name:

```json
{
  "dakixr": {
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
