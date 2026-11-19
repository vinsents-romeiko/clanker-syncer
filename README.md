# clanker-syncer (claude-carry)

move your whole claude code setup from one device to another. skills, plugins, settings, memory, mcp servers, all of it. works on macos, linux and windows. only needs python 3, nothing else to install.

made this cuz i went thru like 16 other sync tools and every single one had some big problem (wiping stuff, leaking keys, no windows, not reinstalling plugins etc). so this one tries to not do any of that. if any problems arise, pls dm or pull it and make changes.

## install as a plugin

this repo is also a plugin marketplace. in claude code:

```
/plugin marketplace add vinsents-romeiko/clanker-syncer
/plugin install clanker-syncer@clanker-syncer
```

or from a shell:

```sh
claude plugin marketplace add vinsents-romeiko/clanker-syncer
claude plugin install clanker-syncer@clanker-syncer
```

then just tell claude "push my setup" or "pull my setup", or run `/clanker-syncer:carry push`.

first time you push, it asks "should i make a repo called clanker-setup for you?" and you pick yes, no, or type a different name. when pulling, it does a dry run first and asks you before it applies any hooks, mcp servers, plugins or scripts. so nothing sketchy runs without you saying ok

## or just use the script

clone the repo and run `carry.py` directly:

```sh
git clone https://github.com/vinsents-romeiko/clanker-syncer
cd clanker-syncer

python3 carry.py push    # on the machine youre moving FROM
python3 carry.py pull    # on the machine youre moving TO (install claude code and log in first)
```

on windows use `py carry.py ...` instead.

## how push / pull works

1. you need `git` and the github cli, logged in (`gh auth login`)
2. your setup goes into a **private** github repo called `clanker-setup` under your account, and a local folder `~/clanker-setup`
3. first `push` asks before making the repo. answer `y`, `n`, or type another name. in scripts pass `--create-repo` to skip the question
4. wanna use a different repo? `--repo NAME` or `--repo OWNER/NAME`, or set `CARRY_REPO`
5. it refuses to use a public repo. your setup isnt going public by accident

if git doesnt have a name or email set, the bundle commits use your github name and no-reply email, only inside the bundle folder.

handy stuff:

- `pull --dry-run` shows what would change, without changing anything
- `python3 carry.py repo-check` shows which repo itd use
- `push --with-history` also carries your chat transcripts, `history.jsonl` and plans, so `claude --resume` works on the other device

if another device pushed a newer setup since your last pull, `push` stops. run `pull` to take theirs, or `push --force` to replace it with this machines. git history stays, so nothing is actually lost

### api keys and tokens

these dont go to github. export pulls them out into `secrets.json` in the bundle folder, which is git-ignored. copy that one file to the new device yourself (password manager, scp, usb, whatever) into `~/clanker-setup/` before you pull.

dont wanna do that? set the `CARRY_SECRET_...` env vars that import lists, or fill them in by hand.

### no github

`python3 carry.py export <folder>` writes the bundle to a folder, `python3 carry.py import <folder>` reads it. move the folder however you want

## what moves

| copied | reinstalled | never carried |
|---|---|---|
| `settings.json`, `CLAUDE.md`, `keybindings.json`, `skills/`, `agents/`, `commands/`, `output-styles/`, plugin state (`plugins/data`, `plugins/store`, `plugin-directory-bindings.json`), plugin-owned folders (like `glowup/`), project memory | marketplaces and user-scope plugins, using `claude plugin marketplace add` / `claude plugin install` | `~/.claude.json` account data, login credentials, `settings.local.json`, caches, sessions, shell snapshots, backups, session `tool-results/`, files over 95 MB |

mcp servers come from `~/.claude.json`, both your user-level `mcpServers` and each projects `mcpServers`. they get merged into the same spots on the new device. project servers are skipped if that project folder doesnt exist there.

symlinked skills and folders get copied as real files, and export tells you each one it followed.

paths under your home folder are saved as `{{CARRY_HOME}}` and rewritten on import, so a different username or os still works. project folder names (like `-Users-you`) get rewritten too.

every export also writes a `SETUP-HANDOFF.md` into the bundle. its basically a checklist of your whole setup: every plugin with its install command and marketplace, every skill, agent, slash command and output style by name, hooks and status line commands, mcp servers, the names of the secrets you need (never the values), settings, project memory folders, and what got skipped. if something doesnt come across, you can redo it by hand from that list

## safety

- before import changes anything, it lists every hook, status line, mcp server, plugin and script thats new or different, and asks you. `--yes` skips that, like for scripts
- import backs up every file it overwrites, plus `~/.claude.json`, to `~/.claude/backups/carry-<timestamp>/`. keeps the last 5
- import never guesses with `~/.claude.json`. if it cant read it (claude code might be writing to it), it stops and changes nothing. if the file changes mid-import, it starts over from the new version
- secrets in `settings.json` `env`, and in mcp `env`, `headers`, `args` and urls, get swapped for `${CARRY_SECRET_...}` placeholders. real values go to `secrets.json`. transcripts (`--with-history`) get secret-looking stuff blanked instead. then export scans the bundle and warns you about anything left. `--keep-secrets` turns all this off (dont lol)
- export only writes into an empty folder or an existing bundle, so a typo in the path cant wipe your other files
- skills synced from your claude.ai account (`skills/synced`) are left out, they sync on their own anyway

## heads up

- plugins reinstall at the marketplaces **current** version, not the exact one you had. if you edited plugin code locally, thats gone, since plugins get reinstalled not copied
- status line and hook commands that use `sh` need git bash or wsl on windows. import warns you if that applies
- windows hasnt been tested on a real windows machine yet. if something breaks, open an issue pls

## license

MIT
