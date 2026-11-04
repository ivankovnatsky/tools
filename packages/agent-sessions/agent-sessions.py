"""Archive agent transcripts and convert them to markdown for qmd.

Usage: agent-sessions <config.json>

    {"dir": "/Users/me/.local/share/agent-sessions",
     "sources": {"claude": "/Users/me/.claude/projects", "codex": "..."}}

Copies each source to <dir>/raw/<agent> (rsync without --delete, so
agent-side cleanup can't lose history), then writes <dir>/md for qmd.
Keeps only user and assistant text (no tool calls, tool output or
thinking), so qmd search results from past sessions don't feed back into
the index. Headless runs (claude -p, codex exec) are skipped.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_CHARS = 4000

NOISE_TAGS = re.compile(
    r"<(system-reminder|environment_context|user_instructions|INSTRUCTIONS)>.*?</\1>",
    re.S,
)
NOISE_PREFIXES = (
    "<task-notification>",
    "<command-",
    "<local-command-",
    "Caveat:",
    "# AGENTS.md instructions",
    "Another Claude session sent a message",
    "<bash-",
    "<user_action>",
    "[Request interrupted",
)


def read_jsonl(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                yield r


def texts(content, kinds=("text", "input_text", "output_text")):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c["text"]
            for c in content
            if isinstance(c, dict)
            and c.get("type") in kinds
            and isinstance(c.get("text"), str)
        )
    return ""


def clean(text):
    text = NOISE_TAGS.sub("", text).strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[truncated]"
    return text


def claude(path):
    meta = {"session": path.stem, "resume": f"claude --resume {path.stem}"}
    msgs = []
    for r in read_jsonl(path):
        t = r.get("type")
        if t == "ai-title":
            meta["title"] = r.get("aiTitle")
        if t not in ("user", "assistant"):
            continue
        if r.get("isSidechain") or r.get("isMeta") or r.get("isCompactSummary"):
            continue
        # sdk-cli is claude -p; sdk-ts (desktop app) is interactive.
        if r.get("entrypoint") == "sdk-cli":
            return None
        meta.setdefault("cwd", r.get("cwd"))
        meta.setdefault("started", r.get("timestamp"))
        msgs.append((t, texts((r.get("message") or {}).get("content"))))
    return meta, msgs


def codex(path):
    meta, msgs = {}, []
    for r in read_jsonl(path):
        p = r.get("payload") or {}
        if r.get("type") == "session_meta":
            if p.get("originator") == "codex_exec":
                return None
            meta.update(session=p.get("id"), cwd=p.get("cwd"))
            meta["started"] = p.get("timestamp")
            meta["resume"] = f"codex resume {p.get('id')}"
        elif p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            msgs.append((p["role"], texts(p.get("content"))))
    return meta, msgs


def pi(path):
    meta, msgs = {}, []
    for r in read_jsonl(path):
        if r.get("type") == "session":
            meta.update(session=r.get("id"), cwd=r.get("cwd"))
            meta["started"] = r.get("timestamp")
            meta["resume"] = f"pi --session {r.get('id')}"
        elif r.get("type") == "message":
            m = r.get("message") or {}
            if m.get("role") in ("user", "assistant"):
                msgs.append((m["role"], texts(m.get("content"))))
    return meta, msgs


def grok(path):
    meta, msgs = {}, []
    # summary.json holds cwd and title; without it (not written yet, or
    # copied mid-write) wait for the next run instead of guessing a path.
    try:
        s = json.loads(path.with_name("summary.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(s, dict) or s.get("session_kind") == "headless":
        return None
    info = s.get("info") or {}
    session = info.get("id") or path.parent.name
    meta.update(session=session, cwd=info.get("cwd"))
    meta["title"] = s.get("session_summary")
    meta["started"] = s.get("created_at")
    meta["resume"] = f"grok --resume {session}"
    roles = {"user_message_chunk": "user", "agent_message_chunk": "assistant"}
    prev = None
    for r in read_jsonl(path):
        u = (r.get("params") or {}).get("update") or {}
        role = roles.get(u.get("sessionUpdate"))
        text = texts([u.get("content")]) if role else ""
        # Chunks stream in pieces: join a run of one role's chunks, but
        # start a new message after a tool call or thought in between.
        if role and role == prev:
            msgs[-1] = (role, msgs[-1][1] + text)
        elif role:
            msgs.append((role, text))
        prev = role
    return meta, msgs


# agent: (transcript glob under raw/<agent>, parser, extra rsync args)
AGENTS = {
    "claude": ("*/*.jsonl", claude, []),
    "codex": ("**/*.jsonl", codex, []),
    "pi": ("*/*.jsonl", pi, []),
    # Grok keeps lots of other state per session; copy only what's parsed.
    "grok": (
        "*/*/updates.jsonl",
        grok,
        ["-m", "--include=*/", "--include=updates.jsonl"]
        + ["--include=summary.json", "--exclude=*"],
    ),
}


def render(agent, meta, msgs):
    front = {"agent": agent, **{k: v for k, v in meta.items() if v}}
    lines = ["---"]
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()]
    lines += ["---", ""]
    if meta.get("title"):
        lines += [f"# {meta['title']}", ""]
    for role, text in msgs:
        lines += [f"## {role}", "", text, ""]
    return "\n".join(lines)


def sync(sources, raw):
    for agent, path in sources.items():
        src = Path(path).expanduser()
        if agent not in AGENTS:
            print(f"agent-sessions: unknown agent {agent}", file=sys.stderr)
        elif src.is_dir():
            dst = raw / agent
            dst.mkdir(parents=True, exist_ok=True)
            rsync = ["rsync", "-a", *AGENTS[agent][2], f"{src}/", f"{dst}/"]
            # 24: files vanished mid-copy (live sessions); the rest copied.
            code = subprocess.run(rsync).returncode
            if code not in (0, 24):
                print(f"agent-sessions: rsync {agent} exit {code}", file=sys.stderr)


def convert(agent, src, out):
    pattern, parse, _ = AGENTS[agent]
    parsed = parse(src)
    if not parsed:
        return False
    meta, msgs = parsed
    msgs = [(role, t) for role, t in ((r, clean(t)) for r, t in msgs) if t]
    if not msgs or not meta.get("session"):
        return False
    project = project_dir(meta.get("cwd"))
    day = (meta.get("started") or "")[:10] or "undated"
    dst = out / agent / project / f"{day}-{meta['session']}.md"
    # md carries its sources' mtime, so any change, even one rsync
    # copied while this run was converting, rewrites it next run.
    mtime = max(f.stat().st_mtime for f in sources(agent, src))
    if dst.exists() and dst.stat().st_mtime == mtime:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(render(agent, meta, msgs), encoding="utf-8")
    os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dst)
    return True


def project_dir(cwd):
    # One flat segment per cwd, relative to home: unique, and never a dot
    # dir or build/dist/vendor, which qmd skips when indexing.
    if not cwd:
        return "unknown"
    home = Path.home()
    path = Path(cwd)
    rel = path.relative_to(home) if path.is_relative_to(home) else path
    name = "-".join(part.lstrip(".") for part in rel.parts if part != "/")
    return name or "home"


def sources(agent, src):
    if agent == "grok":
        return [src, src.with_name("summary.json")]
    return [src]


def main(config_path):
    config = json.loads(Path(config_path).read_text())
    base = Path(config["dir"]).expanduser()
    raw, out = base / "raw", base / "md"
    out.mkdir(parents=True, exist_ok=True)
    sync(config.get("sources", {}), raw)

    written = 0
    for agent, (pattern, _, _) in AGENTS.items():
        for src in sorted((raw / agent).glob(pattern)):
            try:
                written += convert(agent, src, out)
            except Exception as e:  # one bad transcript must not stop the rest
                print(f"agent-sessions: {src}: {e}", file=sys.stderr)
    print(f"agent-sessions: wrote {written} files")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: agent-sessions <config.json>")
    main(sys.argv[1])
