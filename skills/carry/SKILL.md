---
name: carry
description: Move the user's Claude Code setup (settings, skills, agents, commands, plugins, MCP servers, project memory, optionally transcripts) to or from another device with claude-carry, through a private GitHub repo (clanker-setup by default). Use when the user asks to export, back up, push, pull, import, sync or move their Claude Code setup or config between machines, or runs /carry.
argument-hint: "[push|pull|export|import] [--repo NAME] [flags]"
allowed-tools: Bash(python3:*), Bash(py:*), Bash(git:*), AskUserQuestion
---

# claude-carry

The CLI is `carry.py` in this plugin's root, two folders above this skill's base directory: `<base dir>/../../carry.py`. Run it with `python3` (on Windows, `py`). It needs only the Python 3 standard library, plus `git`, and the GitHub CLI `gh` for `push` and `pull`.

Arguments the user gave: `$ARGUMENTS`

## Commands

| Command | What it does |
|---|---|
| `push` | Export this machine's setup, commit, and push it to the private GitHub repo. |
| `pull` | Get the bundle from the private GitHub repo (cloning it the first time), then import it. |
| `export [folder]` | Only write the bundle folder, no git. |
| `import [folder]` | Only import from a bundle folder, no git. |
| `repo-check [--repo NAME]` | Prints JSON: which repo push/pull would use, whether it exists and is private. Changes nothing. |

The bundle folder defaults to `~/clanker-setup`. The repo defaults to `clanker-setup` under the user's `gh` account. `--repo NAME` (or `OWNER/NAME`) picks another.

Flags: `--with-history` (push/export: also carry transcripts), `--keep-secrets` (push/export: leave secrets in the bundle), `--force` (push: replace a newer remote bundle, history kept), `--create-repo` (push: create the private repo without asking), `--dry-run`, `--no-plugins`, `--yes` (pull/import).

If no command was given, ask which one: moving *from* this machine is `push`; moving *to* it is `pull`.

## Push

1. Run `carry.py repo-check`, adding `--repo NAME` if the user named a repo.
2. If `gh` is `false`, tell the user to install the GitHub CLI (https://cli.github.com). If `ghSignedIn` is `false`, tell them to type `! gh auth login`. Stop there.
3. If `linked` is `true`, this machine already pushes to `repo`: run `carry.py push` and go to step 6. Don't ask about the repo again.
4. Otherwise, ask the user with the AskUserQuestion tool before anything is created. Don't skip this, even if the user said to push.
   - If `exists` is `false`, ask: **Should I make a repo called "<name>" for you?** Here `<name>` is the repo name from `repo`, `clanker-setup` unless the user named another. Header: `GitHub repo`. Options:
     - `Yes`: create it as a private repo and push.
     - `No`: don't create anything.
     - `Different name`: the user types the name they want.
   - If `exists` is `true` and `private` is `true`, ask: **Push your setup to your existing private repo "<name>"?** Use the same three options.
   - If `exists` is `true` and `private` is `false`, tell the user that repo is public and carry won't use it. Ask for a different name, or tell them how to make it private. Never push to it.
5. Act on the answer:
   - `Yes`: run `carry.py push --repo <name> --create-repo`. Leave out `--create-repo` if the repo already exists.
   - `Different name`: if the user hasn't given the name yet, ask for it. Then run `carry.py repo-check --repo <new>`. If it doesn't exist, run `carry.py push --repo <new> --create-repo`. If it exists and is private, run `carry.py push --repo <new>`.
   - `No`: stop, and say nothing was created or pushed.
6. If push says the remote already has a newer bundle or one from another machine, explain the choice (`pull` first to take that setup, or `push --force` to replace it, keeping history) and let the user pick.
7. If carry says it moved secrets into `secrets.json`, tell the user to copy that file into the other device's bundle folder (`~/clanker-setup/`) by hand: password manager, scp or USB. It never goes to GitHub. Don't print its contents.

## Pull

1. Run `carry.py repo-check`, adding `--repo NAME` if the user named one. Handle `gh` problems as in Push step 2.
2. If `linked` is `false` and `exists` is `false`, tell the user there's no bundle on GitHub yet: run push on the machine they're moving from. If the user didn't name a repo and this machine isn't linked, say which repo you're about to pull from (`repo`) before going on.
3. Always run `carry.py pull --dry-run` first. Show the user the list under "adds or changes things that run code on this machine", and ask whether to apply it. Only after a yes, run `carry.py pull --yes`. Never add `--yes` on your own.
4. Tell the user to restart Claude Code. Point them to `~/clanker-setup/SETUP-HANDOFF.md`, the checklist of everything in the setup, for anything that didn't come across.

Report carry's own notes (skipped files, followed symlinks, possible secrets left) to the user plainly.
