"""文件系统发现会话(读 ~/.claude*/sessions、transcript、~/.codex/sessions)。"""
import json
import os
import re


def pick_latest_transcript(files, started_at_ms, claimed_sids):
    """选 mtime>=startedAt(含 5s 宽限)且 sid 未占用的最新 jsonl。"""
    grace = started_at_ms - 5000
    cands = [f for f in files if f["mtime_ms"] >= grace and f["sid"] not in claimed_sids]
    cands.sort(key=lambda f: f["mtime_ms"], reverse=True)
    return cands[0] if cands else None


def list_transcripts(proj_dir):
    try:
        names = [n for n in os.listdir(proj_dir) if n.endswith(".jsonl")]
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(proj_dir, n)
        try:
            out.append({"sid": n[:-6], "path": p, "mtime_ms": os.stat(p).st_mtime * 1000})
        except OSError:
            continue
    return out


def encode_cwd(cwd):
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)

def find_project_dir(claude, session_id):
    """在 <claude>/projects/* 下找含 <session_id>.jsonl 的目录;找不到返回 None。"""
    if not session_id:
        return None
    root = os.path.join(claude, "projects")
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for n in names:
        d = os.path.join(root, n)
        if os.path.isfile(os.path.join(d, session_id + ".jsonl")):
            return d
    return None


def count_subagents(subagents_dir):
    try:
        return sum(1 for n in os.listdir(subagents_dir) if n.endswith(".meta.json"))
    except OSError:
        return 0


def discover_claude_sessions(home_dirs, aliver_pids):
    out, claimed = [], set()
    for claude in home_dirs:
        sess_dir = os.path.join(claude, "sessions")
        try:
            files = [n for n in os.listdir(sess_dir) if n.endswith(".json")]
        except OSError:
            continue
        for fname in files:
            try:
                with open(os.path.join(sess_dir, fname), encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            pid = meta.get("pid")
            if not pid or pid not in aliver_pids:
                continue
            proj_dir = os.path.join(claude, "projects", encode_cwd(meta.get("cwd") or ""))
            transcripts = list_transcripts(proj_dir)
            if not transcripts:
                # 超长 cwd 的目录名会被 Claude Code 截断并加 hash 后缀(Windows 深路径常见),
                # encode_cwd 对不上 —— 按 sessionId 在所有 projects/* 里找。
                proj_dir = find_project_dir(claude, meta.get("sessionId")) or proj_dir
                transcripts = list_transcripts(proj_dir)
            direct = next((t for t in transcripts if t["sid"] == meta.get("sessionId")), None)
            chosen = direct or pick_latest_transcript(transcripts, meta.get("startedAt") or 0, claimed)
            if not chosen:
                continue
            claimed.add(chosen["sid"])
            out.append({
                "config_root": claude, "pid": pid, "session_id": chosen["sid"],
                "cwd": meta.get("cwd") or "", "started_at": meta.get("startedAt") or 0,
                "transcript_path": chosen["path"],
                "subagents_dir": os.path.join(proj_dir, chosen["sid"], "subagents"),
            })
    return out


def default_claude_home_dirs():
    home = os.path.expanduser("~")
    dirs = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs.append(env)
    try:
        for n in os.listdir(home):
            if n.startswith(".claude"):
                p = os.path.join(home, n)
                try:
                    if os.path.isdir(os.path.join(p, "sessions")) and p not in dirs:
                        dirs.append(p)
                except OSError:
                    pass
    except OSError:
        pass
    if not dirs:
        dirs.append(os.path.join(home, ".claude"))
    return dirs


def list_codex_rollouts(codex_root, limit=40):
    root = os.path.join(codex_root, "sessions")
    out = []

    def walk(d, depth=0):
        if depth > 5:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    walk(e.path, depth + 1)
                elif re.match(r"^rollout-.*\.jsonl(\.zst)?$", e.name):   # 新版 Codex 会把 rollout 压成 .zst
                    out.append({"path": e.path, "mtime_ms": e.stat().st_mtime * 1000})
            except OSError:
                continue

    walk(root, 0)
    out.sort(key=lambda x: x["mtime_ms"], reverse=True)
    return out[:limit]


_rollout_cache = {}   # path -> (mtime_ms, size, text):rollout 未变就不重读/重解压


def read_rollout_text(path):
    """读 rollout 文本;.zst 用 zstandard 解压(可选依赖,缺失则返回 "")。
    正在写入的 .zst 末尾可能是半个帧:保留已解出的完整部分。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (st.st_mtime * 1000, st.st_size)
    hit = _rollout_cache.get(path)
    if hit and hit[:2] == key:
        return hit[2]
    try:
        if path.endswith(".zst"):
            try:
                import zstandard
            except ImportError:
                return ""
            chunks = []
            with open(path, "rb") as fh:
                reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
                try:
                    while True:
                        b = reader.read(1 << 20)
                        if not b:
                            break
                        chunks.append(b)
                except zstandard.ZstdError:
                    pass
            text = b"".join(chunks).decode("utf-8", "ignore")
        else:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
    except OSError:
        return ""
    if len(_rollout_cache) > 64:
        _rollout_cache.clear()
    _rollout_cache[path] = (*key, text)
    return text
