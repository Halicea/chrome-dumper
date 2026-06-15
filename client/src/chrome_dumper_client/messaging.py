"""messaging plugin — client half.

Matches the open LinkedIn Recruiter InMail drawer's candidate against the local
`sourcing/` repo and injects a personalized outreach draft into the compose
field. All data work happens here, on the client, reading the repo directly —
there is no extra API server. The extension half provides only browser
primitives (`messaging_scan`, `inject_value`, `messaging_set_status`); the
bridge is a pure relay. Nothing here ever sends a message.

Subcommands (mirrors the debug.py register/dispatch pattern):
  messaging watch     stay running; react to panel 'Insert draft' clicks
  messaging insert    one-shot: scan the active tab, match, inject now
  messaging mount     show the in-drawer panel
  messaging unmount   hide the panel

Config: point `--root` (or $SOURCING_ROOT) at the sourcing repo. {{recruiter_name}}
comes from `--recruiter` or $RECRUITER_NAME.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional, Tuple

import yaml

from .client import DumperClient
from .debug import _sse_events

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_HIRE_ID = re.compile(r"/talent/hire/(\d+)")  # LinkedIn project id in the page URL


# ---------- local sourcing repo access ----------

def _root(args) -> Path:
    root = getattr(args, "root", None) or os.environ.get("SOURCING_ROOT")
    if not root:
        raise RuntimeError("set $SOURCING_ROOT (or pass --root) to the sourcing repo path")
    p = Path(root).expanduser()
    if not (p / "sourcing").is_dir():
        raise RuntimeError(f"{p} has no sourcing/ directory — is this the sourcing repo?")
    return p


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _profile_key(url: str) -> str:
    """Last path segment of a LinkedIn URL, lowercased — a stable match key."""
    if not url:
        return ""
    u = str(url).strip().lower().split("?")[0].split("#")[0].rstrip("/")
    return u.split("/")[-1] if u else ""


def _iter_candidates(root: Path, job: Optional[str]):
    sdir = root / "sourcing"
    slugs = [job] if job else [d.name for d in sorted(sdir.iterdir()) if d.is_dir()]
    for slug in slugs:
        jdir = sdir / slug
        if not jdir.is_dir():
            continue
        for f in sorted(jdir.glob("*.md")):
            fm = _parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            if fm:
                yield slug, f.stem, fm


def find_candidate(root: Path, profile_url="", name="", job=None) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """Match priority: profile-id substring (Recruiter or public URL) > exact name."""
    key = _profile_key(profile_url)
    name_l = (name or "").strip().lower()
    name_fallback = None
    for slug, cslug, fm in _iter_candidates(root, job):
        pu = str(fm.get("profile_url") or "").lower()
        ppu = str(fm.get("public_profile_url") or "").lower()
        if key and (key in pu or key in ppu):
            return slug, cslug, fm
        if name_l and not name_fallback and str(fm.get("name") or "").strip().lower() == name_l:
            name_fallback = (slug, cslug, fm)
    return name_fallback if name_fallback else (None, None, None)


def _job_meta(root: Path, slug: str) -> dict:
    jd = root / "jobs" / f"{slug}.md"
    if not jd.exists():
        return {}
    text = jd.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return {"title": m.group(1).strip() if m else slug, "frontmatter": _parse_frontmatter(text)}


def list_jobs(root: Path) -> list:
    """Every job (sourcing project dir), with a human title for the picker."""
    sdir = root / "sourcing"
    out = []
    for d in sorted(sdir.iterdir()):
        if d.is_dir():
            out.append({"slug": d.name, "title": _job_meta(root, d.name).get("title") or d.name})
    return out


def _project_id_from_url(url: str):
    m = _HIRE_ID.search(url or "")
    return m.group(1) if m else None


def resolve_job(root: Path, url: str = "", selected: str = "") -> "Optional[str]":
    """Determine the job for a tab: explicit per-tab selection wins, else the
    LinkedIn project id in the URL matched against each project.json."""
    if selected and (root / "sourcing" / selected).is_dir():
        return selected
    pid = _project_id_from_url(url)
    if pid:
        for pj in sorted((root / "sourcing").glob("*/project.json")):
            try:
                if str(json.loads(pj.read_text()).get("project_id")) == pid:
                    return pj.parent.name
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _load_message_items(root: Path, slug: str) -> Tuple[list, Optional[str]]:
    path = root / "sourcing" / slug / "messages.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    items = [i for i in (data.get("items") or []) if isinstance(i, dict) and i.get("id")]
    if not items:
        tpl = root / "skills" / "create-project" / "templates" / "messages.json"
        if tpl.exists():
            try:
                items = [i for i in (json.loads(tpl.read_text()).get("items") or []) if isinstance(i, dict) and i.get("id")]
            except json.JSONDecodeError:
                items = []
    return items, data.get("default_id")


def render_message(root: Path, slug: str, fm: dict, template_id=None, recruiter="") -> Optional[dict]:
    """Render a messages template into a draft. Unknown tokens are left intact.

    {{company}} resolves from the JD's `company` frontmatter only — never the
    candidate's employer — and is blank when the JD omits it.
    """
    items, default_id = _load_message_items(root, slug)
    if not items:
        return None
    tpl = None
    if template_id:
        tpl = next((i for i in items if i["id"] == template_id), None)
    if not tpl:
        tpl = next((i for i in items if i["id"] == default_id), None) or items[0]

    fm = fm or {}  # bulk/generic render → no candidate, neutral greeting
    job = _job_meta(root, slug)
    job_fm = job.get("frontmatter") or {}
    name = str(fm.get("name") or "").strip()
    skills = fm.get("top_skills") or []
    if isinstance(skills, str):
        skills = [skills]
    highlight = ", ".join(str(s) for s in skills[:2]) if skills else str(fm.get("headline") or "").strip()
    tokens = {
        "first_name": name.split()[0] if name else "there",
        "name": name,
        "role": job.get("title") or "",
        "company": str(job_fm.get("company") or ""),
        "highlight": highlight,
        "recruiter_name": recruiter or "",
    }

    def repl(m):
        return tokens.get(m.group(1), m.group(0))

    return {
        "template_id": tpl["id"],
        "subject": _TOKEN.sub(repl, tpl.get("subject") or ""),
        "body": _TOKEN.sub(repl, tpl.get("body") or ""),
        "tokens": tokens,
    }


# ---------- orchestration over the extension primitives ----------

def _tab(tab_id: Optional[int]) -> dict:
    return {"tabId": tab_id} if tab_id is not None else {}


def _status(d: DumperClient, tab_id, text) -> None:
    try:
        d._cmd({"type": "messaging_set_status", "text": text, **_tab(tab_id)})
    except Exception:
        pass  # panel may not be mounted — non-fatal


def _populate_jobs(d: DumperClient, root: Path, tab_id, selected) -> None:
    try:
        d._cmd({"type": "messaging_set_jobs", "jobs": list_jobs(root),
                "selected": selected or "", **_tab(tab_id)})
    except Exception:
        pass


def _inject(d: DumperClient, tab_id, body) -> dict:
    return d._cmd({"type": "inject_value", "value": body, **_tab(tab_id)})


def insert_for_tab(d: DumperClient, root: Path, tab_id: Optional[int],
                   recruiter="", template=None, job=None) -> dict:
    """Insert a draft. Behaviour depends on the page:

    • Inbox conversation (/inbox/) → per-candidate personalized draft.
    • Project pipeline / bulk page → job-level generic draft. The job comes from
      the per-tab selection, else the URL's project id. If neither resolves, the
      panel's job picker is populated and we ask the user to choose.
    Never sends.
    """
    scan = d._cmd({"type": "messaging_scan", **_tab(tab_id)})
    if not scan.get("composeFound"):
        return {"ok": False, "error": "no compose drawer open", "scan": scan}
    url = scan.get("url") or ""
    selected = scan.get("selectedJob") or job
    profile_url = scan.get("profile_url") or ""
    is_inbox = "/inbox/" in url
    resolved = resolve_job(root, url, selected or "")

    # Bulk / project pipeline → job-level generic template.
    if resolved and not is_inbox:
        draft = render_message(root, resolved, None, template_id=template, recruiter=recruiter)
        if not draft:
            return {"ok": False, "error": f"no message template for job {resolved}"}
        ok = bool(_inject(d, tab_id, draft["body"]).get("ok", True))
        msg = f"{resolved}: bulk draft inserted — review & send" if ok else "inject failed"
        _status(d, tab_id, msg)
        return {"ok": ok, "job": resolved, "mode": "bulk", "template_id": draft["template_id"], "message": msg}

    # Per-candidate (inbox, or single conversation).
    slug, cslug, fm = find_candidate(root, profile_url=profile_url,
                                     name=scan.get("name", ""), job=selected or None)
    if fm:
        draft = render_message(root, slug, fm, template_id=template, recruiter=recruiter)
        ok = bool(_inject(d, tab_id, draft["body"]).get("ok", True))
        cand = fm.get("name") or cslug
        msg = f"draft for {cand} inserted — review & send" if ok else "inject failed"
        _status(d, tab_id, msg)
        return {"ok": ok, "job": slug, "candidate": cand, "mode": "personal", "message": msg}

    # No candidate match. Fall back to a job-level draft if a job is selected,
    # otherwise prompt for one via the picker.
    if resolved:
        draft = render_message(root, resolved, None, template_id=template, recruiter=recruiter)
        if draft:
            ok = bool(_inject(d, tab_id, draft["body"]).get("ok", True))
            msg = f"{resolved}: bulk draft inserted — review & send" if ok else "inject failed"
            _status(d, tab_id, msg)
            return {"ok": ok, "job": resolved, "mode": "bulk", "message": msg}
    _populate_jobs(d, root, tab_id, None)
    _status(d, tab_id, "pick a job ▾ then Insert draft")
    return {"ok": False, "needs_job": True, "error": "no job/candidate resolved — pick a job"}


# ---------- conversation capture (per-candidate message log) ----------

def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", " ", (s or "").lower())).strip()


def _msg_id(sender: str, time_text: str, text: str) -> str:
    raw = f"{_norm_name(sender)}|{time_text}|{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _parse_ts(time_text: str):
    t = re.sub(r"\b(Accepted|Declined|Sent|Edited|Read)\b", "", time_text or "").strip()
    for fmt in ("%B %d, %Y at %I:%M %p", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(t, fmt).isoformat()
        except ValueError:
            continue
    return None


def _resolve_candidate(root: Path, cap: dict):
    """Map a scraped conversation to a local candidate: profile URL first, then
    any participant name. Returns (slug, cslug, fm) or (None, None, None)."""
    s, c, f = find_candidate(root, profile_url=cap.get("profile_url", ""))
    if f:
        return s, c, f
    for name in cap.get("participants", []):
        s, c, f = find_candidate(root, name=name)
        if f:
            return s, c, f
    return None, None, None


def capture_thread(d: DumperClient, root: Path, tab_id) -> dict:
    """Scrape the visible conversation, map it to a local candidate, and append
    new (deduped) messages to sourcing/<job>/<slug>_messages.jsonl. Never sends."""
    cap = d._cmd({"type": "messaging_scrape_thread", **_tab(tab_id)})
    msgs = cap.get("messages") or []
    if not msgs:
        return {"ok": False, "error": "no messages visible"}

    slug, cslug, fm = _resolve_candidate(root, cap)
    if not fm:
        return {"ok": False, "error": "no local candidate matched for this conversation"}

    cand_first = (_norm_name(fm.get("name", "")).split(" ") or [""])[0]
    path = root / "sourcing" / slug / f"{cslug}_messages.jsonl"
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line).get("id"))
            except json.JSONDecodeError:
                pass

    captured_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    new = []
    for m in msgs:
        sender, time_text, text = m.get("sender", ""), m.get("time", ""), m.get("text", "")
        atts = [a for a in (m.get("attachments") or []) if a.get("urn") or a.get("filename")]
        if not text and not atts:
            continue
        key = text if not atts else f"{text} [att:{','.join(a.get('urn', '') for a in atts)}]"
        mid = _msg_id(sender, time_text, key)
        if mid in seen:
            continue
        seen.add(mid)
        in_bound = bool(cand_first) and (_norm_name(sender).split(" ")[:1] == [cand_first])
        rec = {
            "id": mid, "ts": _parse_ts(time_text), "time_text": time_text,
            "direction": "in" if in_bound else "out", "sender": sender,
            "text": text, "captured_at": captured_at,
        }
        if atts:
            rec["attachments"] = atts
        new.append(rec)

    if new:
        with path.open("a", encoding="utf-8") as fh:
            for rec in new:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    cand = fm.get("name") or cslug
    return {
        "ok": True, "candidate": cand, "job": slug, "new": len(new),
        "file": str(path.relative_to(root)),
        "message": (f"{len(new)} new message(s) saved for {cand}" if new
                    else f"no new messages for {cand} (all {len(seen)} already saved)"),
    }


_CV_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain", "rtf": "application/rtf",
}


def _patch_cv_frontmatter(md_path: Path, meta: dict) -> None:
    """Set/replace the candidate .md's `cv:` block, preserving the rest of the
    frontmatter and body (matches the web app's cv shape)."""
    if not md_path.exists():
        return
    text = md_path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        return
    block = re.sub(r"(?m)^cv:\n(?: {2,}.*\n?)*", "", m.group(1)).rstrip("\n")
    cv_yaml = "cv:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in meta.items())
    md_path.write_text(f"---\n{block}\n{cv_yaml}---\n{text[m.end():]}", encoding="utf-8")


def fetch_resume(d: DumperClient, root: Path, tab_id) -> dict:
    """Download the candidate-sent (inbound) resume attachment and store it as the
    candidate's CV: sourcing/<job>/cv/<slug>.<ext> + cv: frontmatter. Only files
    the candidate sent are considered — never the recruiter's outbound files."""
    cap = d._cmd({"type": "messaging_scrape_thread", **_tab(tab_id)})
    slug, cslug, fm = _resolve_candidate(root, cap)
    if not fm:
        return {"ok": False, "error": "no local candidate matched"}
    cand_first = (_norm_name(fm.get("name", "")).split(" ") or [""])[0]
    inbound = []
    for m in cap.get("messages") or []:
        if cand_first and _norm_name(m.get("sender", "")).split(" ")[:1] == [cand_first]:
            inbound += [a for a in (m.get("attachments") or []) if a.get("urn")]
    if not inbound:
        return {"ok": False, "error": "no candidate-sent attachments found"}
    att = next((a for a in inbound if re.search(r"resume|cv|\.pdf|\.docx?", a.get("filename", ""), re.I)),
               inbound[-1])

    dl = d._cmd({"type": "messaging_download_attachment", "urn": att["urn"], **_tab(tab_id)}, timeout=30)
    if not dl.get("ok"):
        return {"ok": False, "error": f"download failed: {dl.get('error')}"}
    src = Path(dl["path"])
    if not src.exists():
        return {"ok": False, "error": f"downloaded file not found at {src}"}

    filename = att.get("filename") or src.name
    ext = (Path(filename).suffix or src.suffix).lstrip(".").lower()
    cv_dir = root / "sourcing" / slug / "cv"
    cv_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{cslug}.{ext}"
    dest = cv_dir / stored
    shutil.move(str(src), str(dest))
    meta = {
        "filename": filename, "stored": stored, "size": dest.stat().st_size,
        "type": _CV_MIME.get(ext, dl.get("mime") or "application/octet-stream"),
        "uploaded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _patch_cv_frontmatter(root / "sourcing" / slug / f"{cslug}.md", meta)
    cand = fm.get("name") or cslug
    return {"ok": True, "candidate": cand, "file": str(dest.relative_to(root)),
            "message": f"resume saved for {cand}: {stored}"}


def _on_panel_ready(d: DumperClient, root: Path, tab_id) -> None:
    """Panel just mounted (or asked for jobs): fill the picker, preselect the
    URL/stored job, and report what we resolved."""
    scan = d._cmd({"type": "messaging_scan", **_tab(tab_id)})
    url = scan.get("url") or ""
    selected = scan.get("selectedJob")
    resolved = resolve_job(root, url, selected or "")
    _populate_jobs(d, root, tab_id, resolved)
    if "/inbox/" in url:
        # Opening a conversation → auto-capture the visible thread.
        res = capture_thread(d, root, tab_id)
        _status(d, tab_id, res.get("message") if res.get("ok") else "inbox — per-candidate draft")
        if res.get("ok"):
            print(f"[messaging] auto-capture: {res['message']}")
        return
    if resolved and not selected:
        _status(d, tab_id, f"job: {resolved} (from URL)")
    elif resolved:
        _status(d, tab_id, f"job: {resolved}")
    else:
        _status(d, tab_id, "pick a job ▾")


def _watch(d: DumperClient, root: Path, recruiter="") -> None:
    print(f"[messaging] watching {d.base_url}/events — open a drawer and click "
          f"'Insert draft' (Ctrl-C to stop)")
    try:
        for ev in _sse_events(d.base_url, None, d.session):
            if ev.get("type") != "plugin_event" or ev.get("plugin") != "messaging":
                continue
            action = ev.get("action")
            tab_id = ev.get("tabId")
            payload = ev.get("payload") or {}
            try:
                if action == "insert_draft":
                    res = insert_for_tab(d, root, tab_id, recruiter=recruiter)
                    print(f"[messaging] {res.get('message') or res.get('error')}")
                elif action == "capture":
                    res = capture_thread(d, root, tab_id)
                    _status(d, tab_id, res.get("message") or res.get("error"))
                    print(f"[messaging] capture: {res.get('message') or res.get('error')}")
                elif action == "resume":
                    res = fetch_resume(d, root, tab_id)
                    _status(d, tab_id, res.get("message") or res.get("error"))
                    print(f"[messaging] resume: {res.get('message') or res.get('error')}")
                elif action == "tick":
                    # 3s autosave heartbeat — capture only when on a conversation.
                    if "/inbox/" in (payload.get("url") or ""):
                        res = capture_thread(d, root, tab_id)
                        if res.get("ok") and res.get("new"):
                            _status(d, tab_id, res["message"])
                            print(f"[messaging] autosave: {res['message']}")
                elif action == "navigated":
                    if "/inbox/" in (payload.get("url") or ""):
                        res = capture_thread(d, root, tab_id)
                        if res.get("ok"):
                            _status(d, tab_id, res["message"])
                            print(f"[messaging] auto-capture: {res['message']}")
                elif action in ("panel_ready", "list_jobs"):
                    _on_panel_ready(d, root, tab_id)
                elif action == "select_job":
                    job = payload.get("value") or ""
                    d._cmd({"type": "messaging_set_tab_job", "job": job, **_tab(tab_id)})
                    _status(d, tab_id, f"job set: {job}" if job else "job cleared")
                    print(f"[messaging] tab {tab_id} job → {job or '(cleared)'}")
            except Exception as e:
                print(f"[messaging] error on {action}: {e}")
                _status(d, tab_id, f"error: {e}")
    except KeyboardInterrupt:
        print("\n[messaging] stopped.")


# ---------- argparse wiring ----------

def register(sub) -> None:
    """Add the `messaging` subcommand to the existing argparse subparser group."""
    p = sub.add_parser("messaging", help="LinkedIn draft-assist: match local candidate, inject draft")
    p.add_argument("action", choices=["watch", "insert", "capture", "resume", "mount", "unmount"],
                   help="watch (daemon), insert/capture/resume (one-shot), mount/unmount the panel")
    p.add_argument("--root", help="path to the sourcing repo (default: $SOURCING_ROOT)")
    p.add_argument("--recruiter", default=os.environ.get("RECRUITER_NAME", ""),
                   help="value for {{recruiter_name}} (default: $RECRUITER_NAME)")
    p.add_argument("--template", help="message template id (default: the job's default)")
    p.add_argument("--job", help="restrict candidate matching to this job slug")
    p.add_argument("--tab", type=int, help="target tab id (default: active tab)")


def dispatch(args, d: DumperClient) -> bool:
    """Handle `messaging …`. Returns True if it consumed the command."""
    if getattr(args, "cmd", None) != "messaging":
        return False
    action = args.action
    if action == "mount":
        print(json.dumps(d._cmd({"type": "messaging_mount", **_tab(args.tab)}), indent=2))
        return True
    if action == "unmount":
        print(json.dumps(d._cmd({"type": "messaging_unmount", **_tab(args.tab)}), indent=2))
        return True
    root = _root(args)
    if action == "insert":
        res = insert_for_tab(d, root, args.tab, recruiter=args.recruiter,
                             template=args.template, job=args.job)
        print(json.dumps(res, indent=2))
        return True
    if action == "capture":
        print(json.dumps(capture_thread(d, root, args.tab), indent=2))
        return True
    if action == "resume":
        print(json.dumps(fetch_resume(d, root, args.tab), indent=2))
        return True
    if action == "watch":
        _watch(d, root, recruiter=args.recruiter)
        return True
    return True
