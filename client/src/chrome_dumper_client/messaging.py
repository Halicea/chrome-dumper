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

import json
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import yaml

from .client import DumperClient
from .debug import _sse_events

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


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

    job = _job_meta(root, slug)
    job_fm = job.get("frontmatter") or {}
    name = str(fm.get("name") or "").strip()
    skills = fm.get("top_skills") or []
    if isinstance(skills, str):
        skills = [skills]
    highlight = ", ".join(str(s) for s in skills[:2]) if skills else str(fm.get("headline") or "").strip()
    tokens = {
        "first_name": name.split()[0] if name else "",
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


def insert_for_tab(d: DumperClient, root: Path, tab_id: Optional[int],
                   recruiter="", template=None, job=None) -> dict:
    """scan drawer → match local candidate → render → inject. Never sends."""
    scan = d._cmd({"type": "messaging_scan", **_tab(tab_id)})
    if not scan.get("composeFound"):
        return {"ok": False, "error": "no compose drawer open", "scan": scan}
    profile_url = scan.get("profile_url") or ""
    name = scan.get("name") or ""
    if not profile_url and not name:
        return {"ok": False, "error": "no candidate identity in drawer", "scan": scan}

    slug, cslug, fm = find_candidate(root, profile_url=profile_url, name=name, job=job)
    if not fm:
        return {"ok": False, "error": f"no local candidate matched (url={profile_url!r} name={name!r})"}

    draft = render_message(root, slug, fm, template_id=template, recruiter=recruiter)
    if not draft:
        return {"ok": False, "error": f"no message template for job {slug}"}

    inj = d._cmd({"type": "inject_value", "value": draft["body"], **_tab(tab_id)})
    cand = fm.get("name") or cslug
    ok = bool(inj.get("ok", True))
    message = f"draft for {cand} inserted — review & send" if ok else f"inject failed: {inj.get('error')}"
    try:
        d._cmd({"type": "messaging_set_status", "text": message, **_tab(tab_id)})
    except Exception:
        pass  # panel may not be mounted — non-fatal
    return {"ok": ok, "job": slug, "candidate": cand, "template_id": draft["template_id"], "message": message}


def _watch(d: DumperClient, root: Path, recruiter="") -> None:
    print(f"[messaging] watching {d.base_url}/events — open a drawer and click "
          f"'Insert draft' (Ctrl-C to stop)")
    try:
        for ev in _sse_events(d.base_url, None, d.session):
            if ev.get("type") != "plugin_event" or ev.get("plugin") != "messaging":
                continue
            if ev.get("action") != "insert_draft":
                continue
            tab_id = ev.get("tabId")
            try:
                res = insert_for_tab(d, root, tab_id, recruiter=recruiter)
                print(f"[messaging] {res.get('message') or res.get('error')}")
                if not res.get("ok"):
                    d._cmd({"type": "messaging_set_status", "text": res.get("error", "failed"), **_tab(tab_id)})
            except Exception as e:
                print(f"[messaging] error: {e}")
                try:
                    d._cmd({"type": "messaging_set_status", "text": f"error: {e}", **_tab(tab_id)})
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[messaging] stopped.")


# ---------- argparse wiring ----------

def register(sub) -> None:
    """Add the `messaging` subcommand to the existing argparse subparser group."""
    p = sub.add_parser("messaging", help="LinkedIn draft-assist: match local candidate, inject draft")
    p.add_argument("action", choices=["watch", "insert", "mount", "unmount"],
                   help="watch (daemon), insert (one-shot), mount/unmount the panel")
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
    if action == "watch":
        _watch(d, root, recruiter=args.recruiter)
        return True
    return True
