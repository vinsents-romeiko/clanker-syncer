#!/usr/bin/env python3
"""claude-carry: move your Claude Code setup (skills, plugins, settings, memory) between devices.

  carry.py push   [bundle-dir] [--with-history] [--keep-secrets] [--force] [--repo NAME] [--create-repo]
  carry.py pull   [bundle-dir] [--dry-run] [--no-plugins] [--yes] [--repo NAME]
  carry.py export [bundle-dir] [--with-history] [--keep-secrets]
  carry.py import [bundle-dir] [--dry-run] [--no-plugins] [--yes]
  carry.py repo-check [bundle-dir] [--repo NAME]     # which repo push/pull would use, as JSON

push/pull use a private GitHub repo (default: clanker-setup under your gh account),
created on the first push after you say yes. The bundle folder defaults to ~/clanker-setup.

Stdlib only; works on macOS, Linux and Windows.
"""
import argparse
import datetime as dt
import fnmatch
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_VERSION = 2
HOME_TOKEN = "{{CARRY_HOME}}"
ENC_HOME_TOKEN = "CARRY-ENCODED-HOME"
SECRETS_FILE = "secrets.json"  # real secret values; git-ignored, moved by hand

# Top-level entries in ~/.claude that are machine-local, caches, or handled specially.
EXCLUDE_TOP = {
    "backups", "cache", "file-history", "session-env", "sessions", "shell-snapshots",
    "statsig", "todos", "ide", "debug", "logs", "telemetry", "paste-cache", "downloads",
    ".last-cleanup", ".credentials.json", ".claude.json", "plugins", "settings.local.json",
    # history-only; added back with --with-history
    "history.jsonl", "projects", "plans",
}
HISTORY_TOP = {"history.jsonl", "projects", "plans"}
# Nested paths (posix, relative to ~/.claude) that are runtime state or account-synced.
EXCLUDE_NESTED = {"skills/synced", "glowup/instances", "glowup/status"}
# Dir names never carried under projects/: raw tool output, the likeliest place for leaked secrets.
EXCLUDE_PROJECT_DIRS = {"tool-results"}
# File names never carried anywhere: debug dumps (can contain the system prompt) and locks.
EXCLUDE_NAMES = ["*classifier-error*", "*.lock", ".DS_Store"]
# Plugin state worth carrying (the plugin code itself is reinstalled from marketplaces).
PLUGIN_STATE_DIRS = ["plugins/data", "plugins/store"]
PLUGIN_STATE_FILES = ["plugins/plugin-directory-bindings.json"]
# GitHub rejects files over 100 MB.
MAX_FILE_BYTES = 95 * 1024 * 1024
BACKUPS_KEPT = 5

TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".sh", ".ps1", ".py", ".js", ".mjs", ".ts",
                 ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ""}
SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".py", ".js", ".mjs",
                   ".cjs", ".ts", ".rb", ".pl"}
# Secret-looking values, whatever key they sit under.
SECRET_VALUE = (
    r"sk-ant-[A-Za-z0-9_-]{10,}|(?:sk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
    r"xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|npm_[A-Za-z0-9]{36}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
SECRET_VALUE_RE = re.compile(SECRET_VALUE)
SECRET_RE = re.compile(
    "(" + SECRET_VALUE + r"|[Bb]earer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"[a-z][a-z0-9+.-]*://[^\s:/@\"']+:[^\s@/\"'$]{6,}@|"
    r"\"[^\"]*(api[_-]?key|token|secret|password)[^\"]*\"\s*:\s*\"[^\"$]{8,}\")",
    re.IGNORECASE)
# Key names whose values are treated as secrets in settings env, MCP env/headers/args/urls.
SECRET_NAME_RE = re.compile(
    r"(api[_-]?key|token|secret|passw(or)?d|passwd|credential|auth(?!or(?!i))|bearer|cookie|"
    r"private[_-]?key|dsn|(^|[_-])pat$|(^|[_-])key$)", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\$\{(CARRY_SECRET_[A-Z0-9_]+)\}")


def claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def global_config_path() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() / ".claude.json" if env else Path.home() / ".claude.json"


def encode_project_path(p: str) -> str:
    """Claude names project dirs by replacing every non-alphanumeric char with '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def swap_prefix(name: str, old: str, new: str) -> str:
    """Replace `old` at the start of a project-dir name, only on a '-' boundary."""
    if name == old or name.startswith(old + "-"):
        return new + name[len(old):]
    return name


def home_variants() -> list:
    """Every spelling of the home dir that may appear in files, longest first."""
    h = str(Path.home())
    forms = {h, Path.home().as_posix(), json.dumps(h)[1:-1]}
    return sorted(forms, key=len, reverse=True)


def is_text(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        with open(path, "rb") as f:
            return b"\0" not in f.read(8192)
    except OSError:
        return False


def path_pattern(path: str, encoded: bool = False) -> "re.Pattern":
    """Match `path` only as a whole path, so /Users/al never eats /Users/alex."""
    if encoded:  # -Users-al inside -Users-al-proj is fine, -Users-alex is not
        return re.compile(r"(?<![A-Za-z0-9-])" + re.escape(path) + r"(?![A-Za-z0-9])")
    return re.compile(r"(?<![A-Za-z0-9_.~-])" + re.escape(path) + r"(?![A-Za-z0-9_.~-])")


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.carry-tmp")
    tmp.write_text(data, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def rewrite_file(path: Path, pairs: list) -> None:
    """Apply (pattern-or-string, replacement) pairs to a text file in place."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    new = text
    for old, repl in pairs:
        new = old.sub(lambda _m: repl, new) if isinstance(old, re.Pattern) else new.replace(old, repl)
    if new != text:
        atomic_write(path, new)


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def tokenize(obj, pairs):
    """Apply text rewrite pairs to every string (keys included) inside a JSON value."""
    if isinstance(obj, dict):
        return {tokenize(k, pairs): tokenize(v, pairs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [tokenize(v, pairs) for v in obj]
    if isinstance(obj, str):
        for old, repl in pairs:
            obj = old.sub(lambda _m: repl, obj) if isinstance(old, re.Pattern) else obj.replace(old, repl)
    return obj


# ---------------------------------------------------------------- secrets

class Redactor:
    """Swaps secret values for ${CARRY_SECRET_...} placeholders and remembers the originals."""

    def __init__(self, keep: bool):
        self.keep = keep
        self.secrets = {}

    def _named(self, label: str, value: str) -> str:
        """Stash a value only because of its key name: skip flags, paths and short words."""
        v = value.strip()
        if len(v) < 8 or v.lower() in ("true", "false") or v.startswith(("/", "~", HOME_TOKEN, "-")):
            return self.string(label, value)
        return self._stash(label, value)

    def _stash(self, label: str, value: str) -> str:
        if self.keep or not value or PLACEHOLDER_RE.fullmatch(value) or value.startswith("${"):
            return value
        for name, v in self.secrets.items():
            if v == value:
                return "${%s}" % name
        base = "CARRY_SECRET_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")
        name, n = base, 2
        while name in self.secrets:
            name, n = f"{base}_{n}", n + 1
        self.secrets[name] = value
        return "${%s}" % name

    def string(self, label: str, value: str) -> str:
        """Redact secret-shaped substrings, URL passwords and secret query params."""
        if self.keep or not isinstance(value, str):
            return value
        value = SECRET_VALUE_RE.sub(lambda m: self._stash(label, m.group(0)), value)
        value = re.sub(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{16,})",
                       lambda m: m.group(1) + self._stash(label, m.group(2)), value)
        value = re.sub(r"(://[^\s:/@]+:)([^\s@/]+)(@)",
                       lambda m: m.group(1) + self._stash(label + "_PASSWORD", m.group(2)) + m.group(3),
                       value)
        return re.sub(r"([?&])([^=&#\s]+)=([^&#\s]+)",
                      lambda m: (m.group(1) + m.group(2) + "=" + self._stash(label + "_" + m.group(2), m.group(3))
                                 if SECRET_NAME_RE.search(m.group(2)) else m.group(0)), value)

    def mapping(self, label: str, d: dict) -> dict:
        """Redact values of secret-named keys (env vars, headers); scan the rest."""
        out = {}
        for k, v in d.items():
            if isinstance(v, str) and SECRET_NAME_RE.search(k):
                out[k] = self._named(f"{label}_{k}", v)
            else:
                out[k] = self.string(f"{label}_{k}", v)
        return out

    def args(self, label: str, args: list) -> list:
        """Redact `--token X`, `--token=X` and secret-shaped values in command arguments."""
        out, flag = [], None
        for a in args:
            if not isinstance(a, str):
                out.append(a)
                continue
            m = re.match(r"^(--?[A-Za-z0-9_-]+)=(.+)$", a)
            if flag:
                out.append(self._named(f"{label}_{flag}", a))
                flag = None
            elif m and SECRET_NAME_RE.search(m.group(1).lstrip("-")):
                out.append(m.group(1) + "=" + self._named(f"{label}_{m.group(1).lstrip('-')}", m.group(2)))
            else:
                if a.startswith("-") and SECRET_NAME_RE.search(a.lstrip("-")):
                    flag = a.lstrip("-")
                out.append(self.string(label, a))
        return out

    def mcp_server(self, name: str, server: dict) -> dict:
        label = "MCP_" + name
        s = dict(server)
        if isinstance(s.get("env"), dict):
            s["env"] = self.mapping(label, s["env"])
        if isinstance(s.get("headers"), dict):
            s["headers"] = self.mapping(label, s["headers"])
        if isinstance(s.get("args"), list):
            s["args"] = self.args(label, s["args"])
        for key in ("url", "command"):
            if isinstance(s.get(key), str):
                s[key] = self.string(f"{label}_{key}", s[key])
        return s


def fill_placeholders(obj, secrets: dict):
    """Put real values back in place of ${CARRY_SECRET_...}; unknown ones are left as is."""
    if isinstance(obj, dict):
        return {k: fill_placeholders(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill_placeholders(v, secrets) for v in obj]
    if isinstance(obj, str):
        return PLACEHOLDER_RE.sub(lambda m: secrets.get(m.group(1), m.group(0)), obj)
    return obj


def find_placeholders(obj) -> set:
    return set(PLACEHOLDER_RE.findall(json.dumps(obj)))


# ---------------------------------------------------------------- export

def collect_files(src: Path, with_history: bool, notes: list):
    """Yield paths (relative to src) of every file to carry."""
    exclude_top = EXCLUDE_TOP - (HISTORY_TOP if with_history else set())
    seen = set()
    for entry in sorted(src.iterdir()):
        if entry.name in exclude_top:
            continue
        yield from _walk(src, entry, seen, notes)
    for rel in PLUGIN_STATE_DIRS + PLUGIN_STATE_FILES:
        if (src / rel).exists():
            yield from _walk(src, src / rel, seen, notes)
    if not with_history and (src / "projects").is_dir():
        # Memory always travels, even without full transcripts.
        for mem in sorted((src / "projects").glob("*/memory")):
            yield from _walk(src, mem, seen, notes)


def _walk(root: Path, entry: Path, seen: set, notes: list):
    rel = entry.relative_to(root).as_posix()
    if any(rel == n or rel.startswith(n + "/") for n in EXCLUDE_NESTED):
        return
    parts = rel.split("/")
    if parts[0] == "projects" and EXCLUDE_PROJECT_DIRS.intersection(parts):
        return
    if entry.is_symlink():
        if not entry.exists():
            notes.append(f"skipped broken symlink {rel}")
            return
        if entry.is_dir() and entry.resolve() in seen:
            notes.append(f"skipped symlink {rel}: points at a folder already carried")
            return
        notes.append(f"followed symlink {rel} -> {os.readlink(entry)} (copied as a real "
                     f"{'folder' if entry.is_dir() else 'file'})")
    if entry.is_file():
        if any(fnmatch.fnmatch(entry.name, g) for g in EXCLUDE_NAMES):
            return
        if entry.stat().st_size > MAX_FILE_BYTES:
            notes.append(f"skipped {rel}: {entry.stat().st_size // 2**20} MB is over GitHub's 100 MB limit")
            return
        yield entry.relative_to(root)
    elif entry.is_dir():
        real = entry.resolve()
        if real in seen:  # symlink loop, or the same folder linked twice
            return
        seen.add(real)
        for child in sorted(entry.iterdir()):
            yield from _walk(root, child, seen, notes)


def plugin_manifest(src: Path) -> dict:
    markets = load_json(src / "plugins" / "known_marketplaces.json", {}) or {}
    installed = (load_json(src / "plugins" / "installed_plugins.json", {}) or {}).get("plugins", {})
    plugins = []
    for pid, installs in installed.items():
        for inst in installs:
            if inst.get("scope", "user") == "user":
                plugins.append({"id": pid, "version": inst.get("version")})
                break
    return {
        "marketplaces": {name: m.get("source") for name, m in markets.items()},
        "plugins": plugins,
    }


def prepare_bundle(bundle: Path) -> None:
    """Empty the bundle dir but keep git metadata and user-added docs.

    Refuses to touch a folder that has other files and isn't already a bundle, so a
    mistyped path can't wipe someone's documents."""
    keep = {".git", ".gitignore", "README.md"}
    others = [e for e in bundle.iterdir() if e.name not in keep]
    if others and not (bundle / "carry.json").is_file():
        sys.exit(f"Refusing to export into {bundle}: it already has files and is not a carry "
                 "bundle (no carry.json). Pick an empty or new folder.")
    for entry in others:
        shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()


def ensure_gitignore(bundle: Path) -> None:
    gi = bundle / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [l for l in (".DS_Store", "Thumbs.db", SECRETS_FILE, "*.carry-tmp") if l not in lines]
    if missing:
        gi.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")


def cmd_export(args) -> None:
    src = claude_home()
    if not src.is_dir():
        sys.exit(f"No Claude config found at {src}")
    bundle = bundle_path(args)
    if bundle == src or src in bundle.parents or bundle in src.parents:
        sys.exit("Bundle dir must be outside your Claude config dir (and not contain it).")
    bundle.mkdir(parents=True, exist_ok=True)
    prepare_bundle(bundle)
    files_dir = bundle / "files"
    red = Redactor(keep=args.keep_secrets)
    notes = []

    home_enc = encode_project_path(str(Path.home()))
    pairs = ([(path_pattern(v), HOME_TOKEN) for v in home_variants()]
             + [(path_pattern(home_enc, encoded=True), ENC_HOME_TOKEN)])
    count = 0
    for rel in collect_files(src, args.with_history, notes):
        # Project dirs embed the encoded home path in their names.
        parts = [swap_prefix(p, home_enc, ENC_HOME_TOKEN) if i == 1 and rel.parts[0] == "projects" else p
                 for i, p in enumerate(rel.parts)]
        dest = files_dir.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest)
        if is_text(dest):
            rewrite_file(dest, pairs)
            if rel.parts[0] in HISTORY_TOP and not args.keep_secrets:
                # Transcripts can't hold placeholders usefully; just blank secret-shaped values.
                rewrite_file(dest, [(SECRET_VALUE_RE, "[REDACTED-BY-CARRY]")])
        count += 1

    settings_path = files_dir / "settings.json"
    settings = load_json(settings_path)
    if isinstance(settings, dict) and isinstance(settings.get("env"), dict):
        env = red.mapping("SETTINGS_ENV", settings["env"])
        if env != settings["env"]:
            settings["env"] = env
            write_json(settings_path, settings)

    write_json(bundle / "plugins.json", plugin_manifest(src))

    gcfg = load_json(global_config_path(), {}) or {}
    user_mcp = {n: red.mcp_server(n, s) for n, s in (gcfg.get("mcpServers") or {}).items()}
    project_mcp = {}
    for ppath, pcfg in (gcfg.get("projects") or {}).items():
        servers = (pcfg or {}).get("mcpServers") or {}
        if servers:
            slug = re.sub(r"[^A-Za-z0-9]+", "_", Path(ppath).name)
            project_mcp[ppath] = {n: red.mcp_server(f"{slug}_{n}", s) for n, s in servers.items()}
    if user_mcp or project_mcp:
        write_json(bundle / "mcp.json", tokenize({"user": user_mcp, "projects": project_mcp}, pairs))

    if red.secrets:
        write_json(bundle / SECRETS_FILE, red.secrets)
        try:
            os.chmod(bundle / SECRETS_FILE, 0o600)
        except OSError:
            pass

    meta = {
        "bundleVersion": BUNDLE_VERSION,
        "exportedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sourcePlatform": platform.system(),
        "sourceHost": platform.node(),
        "claudeVersion": claude_version(),
        "withHistory": args.with_history,
    }
    write_json(bundle / "carry.json", meta)
    write_handoff(src, bundle, meta, notes, sorted(red.secrets))
    ensure_gitignore(bundle)

    n_proj = sum(len(v) for v in project_mcp.values())
    print(f"Exported {count} files, {len(plugin_manifest(src)['plugins'])} plugins, "
          f"{len(user_mcp)} user + {n_proj} project MCP servers -> {bundle}\n"
          f"Wrote {HANDOFF_FILE}: a checklist of everything, in case something doesn't carry over.")
    for n in notes:
        print("  note: " + n)
    if red.secrets:
        print(f"\nMoved {len(red.secrets)} secret(s) out of the bundle into {bundle / SECRETS_FILE}.\n"
              "That file is git-ignored. Copy it to the other device's bundle folder by hand\n"
              "(password manager, scp, USB), or set the ${CARRY_SECRET_...} env vars there.")
    warn_secrets(bundle)


# ---------------------------------------------------------------- handoff note

HANDOFF_FILE = "SETUP-HANDOFF.md"


def _frontmatter(path: Path) -> dict:
    """The `key: value` lines of a Markdown file's leading --- block."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def _short(text: str, n: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _shown(text: str) -> str:
    """Bundle text for the note: home tokens as ~, table pipes escaped."""
    return str(text).replace(HOME_TOKEN, "~").replace(ENC_HOME_TOKEN, "~").replace("|", "\\|")


def _md_names(folder: Path, pattern: str) -> list:
    """(name, description) for skills (`*/SKILL.md`) or agents/commands/styles (`**/*.md`)."""
    out = []
    for f in sorted(folder.glob(pattern)) if folder.is_dir() else []:
        fm = _frontmatter(f)
        name = f.parent.name if f.name == "SKILL.md" else f.relative_to(folder).with_suffix("").as_posix()
        out.append((fm.get("name") or name, _short(fm.get("description", ""))))
    return out


def claude_version() -> str:
    exe = shutil.which("claude")
    if not exe:
        return ""
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        if res.returncode != 0 or not res.stdout.strip():
            return ""
        return res.stdout.strip().splitlines()[0].replace("(Claude Code)", "").strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def write_handoff(src: Path, bundle: Path, meta: dict, notes: list, secret_names: list) -> None:
    """A Markdown checklist of the whole setup, for redoing by hand whatever didn't carry over."""
    files_dir = bundle / "files"
    manifest = load_json(bundle / "plugins.json", {}) or {}
    settings = load_json(files_dir / "settings.json", {}) or {}
    mcp = load_json(bundle / "mcp.json", {}) or {}
    enabled = settings.get("enabledPlugins") or {}
    L = [f"# Claude Code setup handoff",
         "",
         f"Exported from **{meta['sourceHost']}** ({meta['sourcePlatform']}) on {meta['exportedAt']}"
         + (f", Claude Code {meta['claudeVersion']}" if meta.get("claudeVersion") else "") + ".",
         "",
         "`carry.py import` (or `pull`) does all of this for you. This note is the fallback: a list of "
         "everything that was set up on the old machine, so anything that didn't come across can be "
         "redone by hand. `~` means your home folder.",
         "",
         "## First, by hand",
         "",
         "- [ ] Install Claude Code and log in (login credentials are never carried).",
         f"- [ ] Copy `{SECRETS_FILE}` into the bundle folder before importing"
         + (f" ({len(secret_names)} secret(s), listed below)." if secret_names else " (none in this export)."),
         "- [ ] Run `carry.py import <bundle> --dry-run`, then `carry.py import <bundle>`.",
         "- [ ] Restart Claude Code, then work down the lists below and tick what's there.",
         ""]

    markets = manifest.get("marketplaces") or {}
    plugins = manifest.get("plugins") or []
    L += ["## Plugins", ""]
    if plugins:
        L += ["| Plugin | Version | Enabled | Install by hand |", "|---|---|---|---|"]
        for p in plugins:
            on = enabled.get(p["id"])
            L.append(f"| `{p['id']}` | {p.get('version') or '?'} | "
                     f"{'yes' if on is not False else 'no'} | `claude plugin install {p['id']}` |")
        L += ["", "Marketplaces the plugins come from:", ""]
        for name, source in markets.items():
            arg = marketplace_arg(source) or json.dumps(source)
            note = " (built in, nothing to add)" if name == "claude-plugins-official" else ""
            L.append(f"- [ ] `{name}`: `claude plugin marketplace add {_shown(arg)}`{note}")
        if any(p["id"].endswith("@anthropic-plugin-directory") for p in plugins):
            L.append("- `anthropic-plugin-directory` plugins come from the built-in plugin directory: "
                     "install them from `/plugin` if the command above fails.")
    else:
        L.append("None.")
    L.append("")

    sections = [("Skills", files_dir / "skills", "*/SKILL.md"),
                ("Agents", files_dir / "agents", "**/*.md"),
                ("Slash commands", files_dir / "commands", "**/*.md"),
                ("Output styles", files_dir / "output-styles", "**/*.md")]
    for title, folder, pattern in sections:
        items = _md_names(folder, pattern)
        if not items and title != "Skills":
            continue
        L += [f"## {title}", ""]
        rel = folder.relative_to(files_dir).as_posix()
        L += [f"- [ ] `{n}`" + (f": {_shown(d)}" if d else "") for n, d in items] or ["None carried."]
        if items:
            L.append(f"\nCarried as files under `~/.claude/{rel}/`.")
        if title == "Skills" and (src / "skills" / "synced").is_dir():
            synced = _md_names(src / "skills" / "synced", "*/*/SKILL.md")  # <account>/<skill>/SKILL.md
            if synced:
                L.append("\nThese come from your claude.ai account and sync on their own after login: "
                         + ", ".join(f"`{n}`" for n, _ in synced) + ".")
        L.append("")

    cmds = runnable_settings(settings)
    if cmds:
        L += ["## Hooks and status line", "",
              "Commands from `settings.json`. Make sure each one's script or tool exists on the new machine.", "",
              "| Where | Command |", "|---|---|"]
        L += [f"| {w} | `{_shown(c)}` |" for w, c in cmds]
        L.append("")

    user_mcp, project_mcp = mcp.get("user") or {}, mcp.get("projects") or {}
    if user_mcp or project_mcp:
        L += ["## MCP servers", "", "| Server | Scope | Runs |", "|---|---|---|"]

        def runs(s):
            return _shown(s.get("url") or " ".join([s.get("command", "")] + [str(a) for a in s.get("args", [])]))
        L += [f"| `{n}` | user | `{runs(s)}` |" for n, s in user_mcp.items()]
        for ppath, servers in project_mcp.items():
            L += [f"| `{n}` | project `{_shown(ppath)}` | `{runs(s)}` |" for n, s in servers.items()]
        L += ["", "To add one by hand: `claude mcp add` (see `claude mcp add --help`).", ""]

    if secret_names:
        L += ["## Secrets", "",
              f"Values are in `{SECRETS_FILE}` (not in git). Import fills them in from that file. "
              "MCP servers can also read them from environment variables of the same name.", ""]
        L += [f"- [ ] `{n}`" for n in secret_names]
        L.append("")

    keys = []
    if settings.get("model"):
        keys.append(f"- Model: `{settings['model']}`")
    if (settings.get("env") or {}):
        keys.append("- Environment variables: " + ", ".join(f"`{k}`" for k in settings["env"]))
    perms = settings.get("permissions") or {}
    for kind in ("allow", "ask", "deny"):
        if perms.get(kind):
            keys.append(f"- Permission rules ({kind}): {len(perms[kind])}")
    other = sorted(k for k in settings if k not in
                   {"model", "env", "permissions", "hooks", "statusLine", "enabledPlugins"})
    if other:
        keys.append("- Other settings: " + ", ".join(f"`{k}`" for k in other))
    if keys:
        L += ["## Settings", "", "From `settings.json` (carried as a file):", ""] + keys + [""]

    mems = []
    for d in sorted((files_dir / "projects").glob("*/memory")) if (files_dir / "projects").is_dir() else []:
        n = sum(1 for f in d.rglob("*.md"))
        mems.append(f"- `{_shown(d.parent.name)}`: {n} memory file(s)")
    if mems:
        L += ["## Project memory", "",
              "Memory folders are named after the project's path. They only load if the project sits at "
              "the same place under your home folder on the new machine.", ""] + mems + [""]

    others = sorted(p.name for p in files_dir.iterdir()
                    if p.is_dir() and p.name not in {"skills", "agents", "commands", "output-styles",
                                                     "projects", "plugins", "plans"}) if files_dir.is_dir() else []
    if others:
        L += ["## Other folders carried", "",
              "Copied into `~/.claude/` as they are (often plugin settings or your own scripts):", ""]
        L += [f"- `{n}/`" for n in others] + [""]

    L += ["## Not carried", "",
          "- Login credentials and `~/.claude.json` account data: log in again.",
          "- `settings.local.json`, caches, sessions, shell snapshots, backups.",
          "- Plugin code: plugins are reinstalled from their marketplaces at the current version, so "
          "local edits to a plugin's own files are lost.",
          "- Transcripts and prompt history" + (" (this export included them)." if meta.get("withHistory")
                                                 else ", unless exported with `--with-history`.")]
    for n in notes:
        for v in home_variants():
            n = n.replace(v, "~")
        L.append(f"- {_shown(n)}")
    L.append("")
    (bundle / HANDOFF_FILE).write_text("\n".join(L), encoding="utf-8")


def warn_secrets(bundle: Path) -> None:
    hits = []
    for path in bundle.rglob("*"):
        if (".git" in path.parts or path.name == SECRETS_FILE or not path.is_file()
                or not is_text(path)):
            continue
        try:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if SECRET_RE.search(line):
                    hits.append(f"  {path.relative_to(bundle)}:{n}")
        except (UnicodeDecodeError, OSError):
            pass
    if hits:
        print("\nWARNING: possible secrets left in the bundle. Keep the repo private, or remove them:")
        print("\n".join(hits[:20]) + ("\n  ..." if len(hits) > 20 else ""))


# ---------------------------------------------------------------- import

def find_claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        sys.exit("`claude` CLI not found on PATH. Install Claude Code first, then rerun.")
    return exe


def run(cmd: list, dry: bool) -> bool:
    print("  $ " + " ".join(cmd))
    if dry:
        return True
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        print("    ! " + (out.splitlines()[-1] if out else f"exit {res.returncode}"))
    return res.returncode == 0


def marketplace_arg(source: dict):
    kind = (source or {}).get("source")
    if kind == "github":
        return source.get("repo")
    if kind in ("git", "url"):
        return source.get("url")
    if kind in ("directory", "file"):
        return source.get("path")
    return None


def read_global_config(path: Path) -> tuple:
    """Return (text, parsed). Exits instead of guessing if the file is unreadable."""
    if not path.exists():
        return "", {}
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        sys.exit(f"{path} is not valid JSON right now (Claude Code may be writing it). "
                 "Close Claude Code and run import again. Nothing in it was changed.")
    if not isinstance(data, dict):
        sys.exit(f"{path} does not hold a JSON object; refusing to edit it.")
    return text, data


def update_global_config(path: Path, change, backup: Path) -> None:
    """Read-modify-write ~/.claude.json, retrying if Claude Code writes it in between."""
    for _ in range(5):
        text, data = read_global_config(path)
        change(data)
        new = json.dumps(data, indent=2) + "\n"
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            continue  # someone wrote it while we worked; start over from their version
        if text:
            backup.mkdir(parents=True, exist_ok=True)
            (backup / ".claude.json").write_text(text, encoding="utf-8")
        atomic_write(path, new)
        return
    sys.exit(f"{path} keeps changing (Claude Code is running?). Close it and run import again.")


def load_bundle_mcp(bundle: Path, pairs: list, secrets: dict) -> tuple:
    """Return (user servers, {project path: servers}) with tokens and secrets filled in."""
    raw = load_json(bundle / "mcp.json")
    if not raw:
        return {}, {}
    if "user" not in raw and "projects" not in raw:  # bundle version 1: plain server map
        raw = {"user": raw, "projects": {}}
    raw = fill_placeholders(tokenize(raw, pairs), secrets)
    return raw.get("user") or {}, raw.get("projects") or {}


def runnable_settings(settings: dict) -> list:
    """Commands that settings.json makes Claude Code run: status line and hooks."""
    out = []
    sl = (settings.get("statusLine") or {}).get("command")
    if sl:
        out.append(("status line", sl))
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups or []:
            for h in g.get("hooks", []):
                if h.get("command"):
                    out.append((f"hook {event}", h["command"]))
    if settings.get("apiKeyHelper"):
        out.append(("apiKeyHelper", settings["apiKeyHelper"]))
    return out


def review_changes(files_dir, dest_root, home_enc, pairs, manifest, plugins_on,
                   user_mcp, project_mcp, gcfg, shown) -> list:
    """List everything an import would let run on this machine that isn't here already."""
    items = []
    new_settings = load_json(files_dir / "settings.json", {}) or {}
    new_settings = tokenize(new_settings, pairs)
    old_cmds = set(runnable_settings(load_json(dest_root / "settings.json", {}) or {}))
    for what, cmd in runnable_settings(new_settings):
        if (what, cmd) not in old_cmds:
            items.append(f"{what}: {cmd}")
    old_user = gcfg.get("mcpServers") or {}
    shown_user, shown_projects = shown  # same servers with secrets left as placeholders
    for name, s in user_mcp.items():
        if old_user.get(name) != s:
            s = shown_user[name]
            items.append(f"MCP server {name}: {s.get('command') or s.get('url')} "
                         + " ".join(map(str, s.get("args", []))))
    for ppath, servers in project_mcp.items():
        if not Path(ppath).exists():
            continue  # import skips these
        old = ((gcfg.get("projects") or {}).get(ppath) or {}).get("mcpServers") or {}
        for name, s in servers.items():
            if old.get(name) != s:
                s = shown_projects[ppath][name]
                items.append(f"MCP server {name} (project {ppath}): {s.get('command') or s.get('url')}")
    if plugins_on:
        have = set(((load_json(dest_root / "plugins" / "installed_plugins.json", {}) or {})
                    .get("plugins") or {}).keys())
        for p in manifest.get("plugins", []):
            if p["id"] not in have:
                items.append(f"plugin install {p['id']}")
    for src in files_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(files_dir)
        is_script = src.suffix.lower() in SCRIPT_SUFFIXES or (os.name != "nt" and os.access(src, os.X_OK))
        if not is_script or rel.parts[0] == "projects":
            continue
        dest = dest_root.joinpath(*[p.replace(ENC_HOME_TOKEN, home_enc, 1) for p in rel.parts])
        try:
            data = src.read_bytes()
            if is_text(src):
                text = data.decode("utf-8", "replace")
                for tok, repl in pairs:
                    text = text.replace(tok, repl)
                data = text.encode("utf-8")
            same = dest.exists() and dest.read_bytes() == data
        except OSError:
            same = False
        if not same:
            items.append(f"script {rel.as_posix()}" + ("" if dest.exists() else " (new)"))
    return items


def confirm(items: list, meta: dict, args) -> None:
    if not items:
        return
    print(f"\nThis bundle (from {meta.get('sourceHost') or meta.get('sourcePlatform')}, "
          f"{meta.get('exportedAt')}) adds or changes things that run code on this machine:")
    for it in items[:40]:
        print("  - " + it)
    if len(items) > 40:
        print(f"  ... and {len(items) - 40} more")
    if args.dry_run or args.yes:
        return
    if not sys.stdin.isatty():
        sys.exit("Not applying without confirmation. Rerun with --yes if you trust this bundle.")
    if input("Apply them? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("Nothing changed.")


def install_file(src: Path, dest: Path, backup: Path, rel_parts: list, pairs: list, secrets: dict) -> bool:
    """Copy one bundle file into place. Returns True if an existing file was backed up."""
    backed = False
    if dest.exists():
        b = backup.joinpath(*rel_parts)
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, b)
        backed = True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.carry-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    if is_text(dest):
        rewrite_file(dest, pairs)
        if dest.suffix == ".json" and secrets:
            data = load_json(dest)
            if data is not None and find_placeholders(data):
                write_json(dest, fill_placeholders(data, secrets))
    if dest.suffix == ".sh" and os.name != "nt":
        dest.chmod(dest.stat().st_mode | 0o111)
    return backed


def prune_backups(dest_root: Path) -> None:
    olds = sorted(p for p in (dest_root / "backups").glob("carry-*") if p.is_dir())
    for p in olds[:-BACKUPS_KEPT]:
        shutil.rmtree(p, ignore_errors=True)


def cmd_import(args) -> None:
    bundle = bundle_path(args)
    meta = load_json(bundle / "carry.json")
    if not meta:
        sys.exit(f"{bundle} is not a carry bundle (missing carry.json).")
    if meta.get("bundleVersion", 0) > BUNDLE_VERSION:
        sys.exit("Bundle was made by a newer claude-carry; update this script.")
    dry = args.dry_run
    dest_root = claude_home()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = dest_root / "backups" / f"carry-{stamp}"
    new_home = Path.home().as_posix()
    home_enc = encode_project_path(str(Path.home()))
    pairs = [(HOME_TOKEN, new_home), (ENC_HOME_TOKEN, home_enc)]
    files_dir = bundle / "files"
    secrets = load_json(bundle / SECRETS_FILE, {}) or {}
    manifest = load_json(bundle / "plugins.json", {}) or {}
    plugins_on = not args.no_plugins and bool(manifest.get("plugins"))
    gpath = global_config_path()
    _, gcfg = read_global_config(gpath)
    user_mcp, project_mcp = load_bundle_mcp(bundle, pairs, secrets)

    # 0. Show what would start running here, and ask.
    shown = load_bundle_mcp(bundle, pairs, {})  # for display: placeholders, not secrets
    confirm(review_changes(files_dir, dest_root, home_enc, pairs, manifest, plugins_on,
                           user_mcp, project_mcp, gcfg, shown), meta, args)

    # 1. Plugins first, so copied plugin state lands on top of fresh installs.
    if plugins_on:
        claude = find_claude()
        bindings = files_dir / "plugins" / "plugin-directory-bindings.json"
        if bindings.exists() and not dry:  # plugin-directory installs resolve through this
            install_file(bindings, dest_root / "plugins" / bindings.name, backup,
                         ["plugins", bindings.name], pairs, secrets)
        print("Adding marketplaces:")
        for name, source in manifest.get("marketplaces", {}).items():
            if name == "claude-plugins-official":
                continue  # Claude Code adds this one itself
            arg = marketplace_arg(source)
            if not arg:
                print(f"  ? skip {name}: unsupported source {source}")
                continue
            if source.get("source") in ("directory", "file"):
                arg = arg.replace(HOME_TOKEN, new_home)
                if not Path(arg).exists():
                    print(f"  ? skip {name}: local path {arg} does not exist here")
                    continue
            run([claude, "plugin", "marketplace", "add", arg], dry)
        print("Installing plugins:")
        for p in manifest["plugins"]:
            run([claude, "plugin", "install", p["id"], "--scope", "user"], dry)

    # 2. Files.
    copied = backed_up = 0
    print(f"Copying files into {dest_root}" + (" (dry run)" if dry else ""))
    for src in sorted(p for p in files_dir.rglob("*") if p.is_file()):
        rel = src.relative_to(files_dir)
        parts = [p.replace(ENC_HOME_TOKEN, home_enc, 1) for p in rel.parts]
        dest = dest_root.joinpath(*parts)
        if dry:
            print(f"  {Path(*parts)}" + ("  (overwrites)" if dest.exists() else ""))
        else:
            backed_up += install_file(src, dest, backup, parts, pairs, secrets)
        copied += 1

    # 3. MCP servers, merged into the global config without touching account data.
    if user_mcp or project_mcp:
        skipped = [p for p in project_mcp if not Path(p).exists()]
        project_mcp = {p: s for p, s in project_mcp.items() if p not in skipped}

        def merge(cfg):
            cfg.setdefault("mcpServers", {}).update(user_mcp)
            for ppath, servers in project_mcp.items():
                cfg.setdefault("projects", {}).setdefault(ppath, {}).setdefault("mcpServers", {}).update(servers)

        print(f"MCP servers: {len(user_mcp)} user, "
              f"{sum(len(s) for s in project_mcp.values())} project")
        for p in skipped:
            print(f"  ? skip project MCP for {p}: folder does not exist here")
        if not dry:
            update_global_config(gpath, merge, backup)
        missing = find_placeholders([user_mcp, project_mcp])
        if missing:
            print(f"  ! {len(missing)} secret(s) not in {SECRETS_FILE}; set these env vars before "
                  "starting Claude Code: " + ", ".join(sorted(missing)))

    if (bundle / HANDOFF_FILE).exists():
        print(f"Checklist of everything in this setup: {bundle / HANDOFF_FILE}")
    print(f"\n{'Would copy' if dry else 'Copied'} {copied} files"
          + (f"; {backed_up} existing files backed up to {backup}" if backed_up else ""))
    if not dry:
        prune_backups(dest_root)
        check_portability(dest_root, meta)
        print("Restart Claude Code to pick everything up.")


def check_portability(root: Path, meta: dict) -> None:
    settings = load_json(root / "settings.json", {}) or {}
    cmds = [c for _, c in runnable_settings(settings)]
    if os.name == "nt" and any(c.strip().startswith(("sh ", "bash ")) for c in cmds if c):
        print("NOTE: settings.json runs shell scripts (status line / hooks). On Windows these "
              "need Git Bash or WSL on PATH.")
    if find_placeholders(settings):
        print(f"NOTE: settings.json still has ${{CARRY_SECRET_...}} placeholders. Copy {SECRETS_FILE} "
              "into the bundle folder and import again, or fill them in by hand.")
    if meta.get("sourcePlatform") != platform.system():
        print(f"NOTE: bundle came from {meta.get('sourcePlatform')}; check any hooks or "
              "commands that call platform-specific tools.")


# ---------------------------------------------------------------- git sync

DEFAULT_BUNDLE = "~/clanker-setup"
DEFAULT_REPO = "clanker-setup"  # private GitHub repo push and pull use; owner = the gh login


def bundle_path(args) -> Path:
    return Path(args.bundle or DEFAULT_BUNDLE).expanduser().resolve()


def git(bundle: Path, *a, check=True, capture=False):
    res = subprocess.run(["git", "-C", str(bundle), *a], text=True,
                         capture_output=capture)
    if check and res.returncode != 0:
        err = (res.stderr or "").strip() if capture else ""
        hint = ("\nIf this is a sign-in problem, run: gh auth setup-git"
                if re.search(r"auth|credential|denied|403|401", err, re.I) else "")
        sys.exit(f"git {' '.join(a)} failed" + (f": {err}" if err else ".") + hint)
    return res


def gh(*a, check=True) -> subprocess.CompletedProcess:
    exe = shutil.which("gh")
    if not exe:
        sys.exit("The GitHub CLI `gh` is needed to find or create your private bundle repo.\n"
                 "Install it (https://cli.github.com), run `gh auth login`, and try again.\n"
                 "Or set the repo up yourself: git init <bundle> && "
                 "git -C <bundle> remote add origin <your-private-repo-url>")
    res = subprocess.run([exe, *a], capture_output=True, text=True)
    if check and res.returncode != 0:
        err = (res.stderr or res.stdout).strip()
        if "auth login" in err or "not logged" in err.lower():
            sys.exit("gh is not signed in. Run `gh auth login`, then try again.")
        sys.exit(f"gh {' '.join(a)} failed: {err}")
    return res


def repo_full_name(name: str) -> str:
    if "/" in name:
        return name
    return gh("api", "user", "--jq", ".login").stdout.strip() + "/" + name


def repo_info(full: str):
    """The repo's {visibility, url, sshUrl}, or None if it doesn't exist."""
    res = gh("repo", "view", full, "--json", "visibility,url,sshUrl", check=False)
    if res.returncode != 0:
        if re.search(r"Could not resolve|not found", res.stderr, re.I):
            return None
        gh("repo", "view", full)  # rerun to exit with gh's own message
    return json.loads(res.stdout)


def require_private(full: str, info: dict) -> None:
    if (info.get("visibility") or "").upper() != "PRIVATE":
        sys.exit(f"github.com/{full} is {info.get('visibility', 'not private').lower()}. carry only uses "
                 "a PRIVATE repo, because the bundle holds your hooks, settings and memory.\n"
                 f"Make it private (gh repo edit {full} --visibility private "
                 "--accept-visibility-change-consequences) or pick another with --repo.")


def clone_url(info: dict) -> str:
    proto = gh("config", "get", "git_protocol", check=False).stdout.strip()
    return info["sshUrl"] if proto == "ssh" else info["url"] + ".git"


def github_name(url: str):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def check_origin_private(bundle: Path) -> None:
    """If origin is on GitHub and gh is around, make sure it is still private."""
    url = git(bundle, "config", "--get", "remote.origin.url", check=False, capture=True).stdout.strip()
    full = github_name(url)
    if full and shutil.which("gh"):
        info = repo_info(full)
        if info:
            require_private(full, info)


def choose_repo(full: str, create: bool) -> str:
    """The repo to create when `full` doesn't exist: ask on a terminal, else need --create-repo."""
    if create:
        return full
    if not sys.stdin.isatty():
        print(f"No GitHub repo {full} yet. Rerun with --create-repo to create it as a private repo, "
              "or pass --repo <name> to use another.", file=sys.stderr)
        sys.exit(3)
    while True:
        try:
            ans = input(f"Create a private GitHub repo called {full.split('/', 1)[1]}? "
                        "[y/N, or type a different name] ").strip()
        except EOFError:
            ans = ""
        if ans.lower() in ("y", "yes"):
            return full
        if ans.lower() in ("", "n", "no"):
            sys.exit("Nothing created or pushed.")
        if re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?", ans):
            return repo_full_name(ans)
        print("Repo names can use letters, digits, '.', '_' and '-'.")


def git_identity_source() -> bool:
    """True if commits can be signed: git already has an identity, or gh can lend one."""
    have = all(subprocess.run(["git", "config", k], capture_output=True, text=True).stdout.strip()
               for k in ("user.name", "user.email"))
    return have or (bool(shutil.which("gh")) and gh("auth", "status", check=False).returncode == 0)


def ensure_git_identity(bundle: Path) -> None:
    """Without a git identity, sign bundle commits as the GitHub account, in this repo only."""
    missing = [k for k in ("user.name", "user.email")
               if not git(bundle, "config", k, check=False, capture=True).stdout.strip()]
    if not missing:
        return
    user = json.loads(gh("api", "user").stdout)
    ident = {"user.name": user.get("name") or user["login"],
             "user.email": f"{user['id']}+{user['login']}@users.noreply.github.com"}
    for k in missing:
        git(bundle, "config", "--local", k, ident[k])
    print(f"git has no identity set, so bundle commits use your GitHub one: {ident['user.name']} "
          f"<{ident['user.email']}> (set in {bundle} only).")


def setup_push_repo(bundle: Path, repo: str, create: bool = False) -> bool:
    """Make the bundle a git repo whose origin is the private GitHub repo, creating it if needed.

    Returns True if this attached to a repo that already holds another machine's bundle."""
    if any(bundle.iterdir()) if bundle.exists() else False:
        if not (bundle / ".git").is_dir() and not (bundle / "carry.json").is_file():
            sys.exit(f"{bundle} already has files and is not a carry bundle. Pick another folder.")
    bundle.mkdir(parents=True, exist_ok=True)
    if not (bundle / ".git").is_dir():
        git(bundle, "init", "-q")
        git(bundle, "symbolic-ref", "HEAD", "refs/heads/main")
    if git(bundle, "remote", "get-url", "origin", check=False, capture=True).returncode == 0:
        return False
    full = repo_full_name(repo)
    info = repo_info(full)
    while info is None:
        chosen = choose_repo(full, create)
        if chosen != full:  # the person typed another name: use it if it exists, else create it
            full, info, create = chosen, repo_info(chosen), True
            continue
        print(f"Creating private GitHub repo {full} for your bundle...")
        gh("repo", "create", full, "--private",
           "--description", "Claude Code setup bundle (made by claude-carry)")
        info = repo_info(full) or sys.exit(f"Created {full} but can't see it yet; try again.")
        break
    require_private(full, info)
    git(bundle, "remote", "add", "origin", clone_url(info))
    git(bundle, "fetch", "-q", "origin", capture=True)
    if git(bundle, "rev-parse", "-q", "--verify", "origin/main", check=False, capture=True).returncode != 0:
        return False  # empty repo: the first push makes main
    if git(bundle, "rev-parse", "-q", "--verify", "HEAD", check=False, capture=True).returncode == 0:
        sys.exit(f"{bundle} has its own git history and {full} already has a bundle. "
                 "Use a new folder, or `pull` to take the remote one.")
    git(bundle, "reset", "-q", "origin/main")  # adopt the remote history, keep the folder's files
    git(bundle, "branch", "-q", "--set-upstream-to", "origin/main")
    return True


def cmd_push(args) -> None:
    bundle = bundle_path(args)
    if not (bundle / ".git").is_dir() and not git_identity_source():
        sys.exit("git has no user.name/user.email, and `gh` isn't signed in to borrow them from.\n"
                 "Run `gh auth login`, or: git config --global user.name \"...\" && "
                 "git config --global user.email \"...\"")
    attached = setup_push_repo(bundle, args.repo, args.create_repo)
    ensure_git_identity(bundle)
    check_origin_private(bundle)
    git(bundle, "fetch", "-q", capture=True)
    res = git(bundle, "rev-parse", "--abbrev-ref", "@{u}", check=False, capture=True)
    upstream = res.stdout.strip() if res.returncode == 0 else None
    if upstream:
        ahead = git(bundle, "rev-list", "--count", f"HEAD..{upstream}", capture=True).stdout.strip()
        if ahead != "0" or attached:
            last = git(bundle, "log", "-1", "--format=%s (%cr)", upstream, capture=True).stdout.strip()
            if not args.force:
                why = (f"The remote already has a bundle from another machine, latest: {last}."
                       if attached else f"The remote has {ahead} newer bundle commit(s), latest: {last}.")
                sys.exit(why + "\nRun `pull` first to take that setup, or `push --force` to replace it "
                         "with this machine's (history is kept).")
            print(f"Replacing newer remote bundle ({last}) with this machine's setup.")
    cmd_export(args)
    git(bundle, "add", "-A")
    if upstream and args.force:
        git(bundle, "reset", "-q", "--soft", upstream)  # commit on top, so no force-push needed
    stamps = ["carry.json", HANDOFF_FILE]  # rewritten with a new time on every export
    if git(bundle, "diff", "--cached", "--quiet", "--", ".", *[f":!{f}" for f in stamps],
           check=False).returncode == 0:
        tracked = [f for f in stamps
                   if git(bundle, "cat-file", "-e", f"HEAD:{f}", check=False, capture=True).returncode == 0]
        if tracked or git(bundle, "rev-parse", "-q", "--verify", "HEAD", check=False, capture=True).returncode == 0:
            if tracked:
                git(bundle, "reset", "-q", "HEAD", "--", *tracked)
                git(bundle, "checkout", "-q", "HEAD", "--", *tracked)
            print("No changes to push.")
            return
    git(bundle, "commit", "-q", "-m", f"carry: {platform.node()} {dt.date.today().isoformat()}")
    git(bundle, "push", "-q", "-u", "origin", "HEAD", capture=True)
    url = git(bundle, "config", "--get", "remote.origin.url", capture=True).stdout.strip()
    print(f"Pushed to {github_name(url) or url}.")


def cmd_repo_check(args) -> None:
    """Print, as JSON, which repo push/pull would use and whether it exists, without changing anything."""
    bundle = bundle_path(args)
    origin = ""
    if (bundle / ".git").is_dir():
        origin = git(bundle, "config", "--get", "remote.origin.url", check=False, capture=True).stdout.strip()
    out = {"bundle": str(bundle), "linked": bool(origin), "origin": origin,
           "defaultRepo": DEFAULT_REPO, "gh": bool(shutil.which("gh"))}
    name = github_name(origin) if origin else args.repo
    if out["gh"] and name:
        if gh("auth", "status", check=False).returncode != 0:
            out["ghSignedIn"] = False
        else:
            out["ghSignedIn"] = True
            full = repo_full_name(name)
            info = repo_info(full)
            out.update({"repo": full, "exists": info is not None,
                        "private": bool(info) and (info.get("visibility") or "").upper() == "PRIVATE"})
    print(json.dumps(out, indent=2))


def cmd_pull(args) -> None:
    bundle = bundle_path(args)
    if not (bundle / ".git").is_dir():
        if bundle.exists() and any(bundle.iterdir()):
            sys.exit(f"{bundle} already has files and is not a git repo. Pick another folder.")
        full = repo_full_name(args.repo)
        info = repo_info(full)
        if info is None:
            sys.exit(f"No bundle on GitHub yet ({full}). Run `push` on the machine you're moving from.")
        require_private(full, info)
        print(f"Cloning {full} into {bundle}...")
        bundle.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["git", "clone", "-q", clone_url(info), str(bundle)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            sys.exit("git clone failed: " + res.stderr.strip() + "\nIf this is a sign-in problem, run: gh auth setup-git")
        if not (bundle / "carry.json").is_file():
            sys.exit(f"{full} has no bundle in it yet. Run `push` on the machine you're moving from.")
    else:
        check_origin_private(bundle)
        res = git(bundle, "pull", "-q", "--ff-only", check=False, capture=True)
        if res.returncode != 0:
            sys.exit("git pull failed: " + (res.stderr.strip() or "unknown error") +
                     "\nIf this machine pushed its own bundle since, the histories differ: "
                     f"run `git -C {bundle} reset --hard @{{u}}` to take the remote one.")
    cmd_import(args)


def main() -> None:
    ap = argparse.ArgumentParser(prog="carry", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    bundle_help = f"bundle folder (default {DEFAULT_BUNDLE})"
    repo_help = (f"private GitHub repo to use, NAME or OWNER/NAME (default {DEFAULT_REPO} "
                 "under your gh account; created on first push)")
    for name, fn in (("export", cmd_export), ("push", cmd_push)):
        p = sub.add_parser(name)
        p.add_argument("bundle", nargs="?", help=bundle_help)
        p.add_argument("--with-history", action="store_true",
                       help="also carry transcripts, history.jsonl and plans")
        p.add_argument("--keep-secrets", action="store_true",
                       help=f"leave secrets in the bundle instead of moving them to {SECRETS_FILE}")
        if name == "push":
            p.add_argument("--force", action="store_true",
                           help="replace a newer bundle on the remote with this machine's")
            p.add_argument("--repo", default=os.environ.get("CARRY_REPO", DEFAULT_REPO), help=repo_help)
            p.add_argument("--create-repo", action="store_true",
                           help="create the private repo if it doesn't exist, without asking")
        p.set_defaults(fn=fn, force=False, create_repo=False)
    for name, fn in (("import", cmd_import), ("pull", cmd_pull)):
        p = sub.add_parser(name)
        p.add_argument("bundle", nargs="?", help=bundle_help)
        p.add_argument("--dry-run", action="store_true", help="show what would change")
        p.add_argument("--no-plugins", action="store_true", help="skip plugin reinstall")
        p.add_argument("--yes", "-y", action="store_true",
                       help="don't ask before applying hooks, scripts, MCP servers and plugins")
        if name == "pull":
            p.add_argument("--repo", default=os.environ.get("CARRY_REPO", DEFAULT_REPO), help=repo_help)
        p.set_defaults(fn=fn)
    p = sub.add_parser("repo-check", help="show which GitHub repo push/pull would use (JSON)")
    p.add_argument("bundle", nargs="?", help=bundle_help)
    p.add_argument("--repo", default=os.environ.get("CARRY_REPO", DEFAULT_REPO), help=repo_help)
    p.set_defaults(fn=cmd_repo_check)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
