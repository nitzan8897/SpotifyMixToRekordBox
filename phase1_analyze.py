#!/usr/bin/env python3
"""Phase 1 - Analyze a discovery capture.

Searches everything phase1_discover.py recorded (response bodies, WebSocket
frames, DOM snapshots, the web player's JS bundles) for transition-related
data and writes:

  report.md    human-readable findings, safe to share (secrets redacted)
  report.json  the same, machine-readable

Usage:
  python phase1_analyze.py                 # newest run in output/discovery
  python phase1_analyze.py output/discovery/20260926-101500
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from src.config import ConfigError, load_config
from src.logs import setup_logging
from src.scan import (
                      Hit, compile_keywords, generalize_path, get_at, match_text, parent_path,
                      printable_strings, redact, scan_json, tokenize, walk,
)

log = logging.getLogger("src.analyze")

# Tokens that make a hit clearly about mixing rather than e.g. "volume" or "low".
STRONG_TOKENS = {"transition", "transitions", "crossfade", "crossfades", "mix", "mixed", "mixes",
                 "automix", "blend", "blends", "segue", "segues", "fade", "fades", "fader"}

# Transition parameters we need for phase 2, with key-token heuristics.
PARAMETERS = [
    ("from/to track identity", r"^(uri|id|gid|track|item|from|to|source|target|prev|next|previous|a|b)$"),
    ("out point (A starts fading)", r"^(out|end|exit|fadeout|outro|stop)$"),
    ("in point (B enters)", r"^(in|start|entry|enter|fadein|intro|cue|begin)$"),
    ("transition length", r"^(duration|length|overlap|len|span)$"),
    ("fade curve / type / style", r"^(curve|shape|type|style|preset|kind|mode|template)$"),
    ("EQ (low/mid/high)", r"^(eq|equalizer|low|mid|high|bass|treble|lows|mids|highs)$"),
    ("filter", r"^(filter|hpf|lpf|cutoff|resonance|highpass|lowpass)$"),
    ("other effects", r"^(effect|effects|fx|echo|reverb|delay|riser|sweep|gain|volume)$"),
]

GQL_DEF_RE = re.compile(r'"([A-Za-z][A-Za-z0-9_]{2,80})"\s*,\s*"(query|mutation|subscription)"\s*,\s*"([0-9a-f]{64})"')
JS_LITERAL_RE = re.compile(r'"([^"\\\n]{3,80})"|\'([^\'\\\n]{3,80})\'')
JS_STRONG_RE = re.compile(r"(?i)(crossfade|transition|mixed|automix|segue|\bmix(?:es|ing)?\b|[a-z]Mix|^Mix)")
CSS_NOISE_RE = re.compile(r"(?i)(transition[-:\s]|transitionend|transition(?:group|duration|delay|timing|property)|"
                          r"\d+m?s\b|ease|cubic-bezier|--|;|\{|\}|mixin|mix-blend|webkit|moz)")


def is_strong(path: str) -> bool:
    return bool(STRONG_TOKENS.intersection(tokenize(path)))


def load_index(run_dir: Path) -> list[dict]:
    idx = run_dir / "index.jsonl"
    if not idx.is_file():
        raise SystemExit(f"ERROR: {idx} not found - is {run_dir} a phase1_discover.py run folder?")
    rows = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                log.warning("Skipping corrupt index line: %s", line[:80])
    return rows


def endpoint_label(row: dict) -> str:
    op = row.get("graphql_operation")
    return f"{row['method']} {row['endpoint']}" + (f"  [GraphQL {op}]" if op else "")


def analyze_bodies(run_dir: Path, rows: list[dict], keywords) -> tuple[dict, list[dict]]:
    """Scan every saved body. Returns per-endpoint stats and all JS bundle rows."""
    endpoints: dict[str, dict] = {}
    js_rows = []
    for row in rows:
        if not row.get("body_file"):
            continue
        path = run_dir / row["body_file"]
        if not path.is_file():
            continue
        ctype = row.get("content_type") or ""
        if "javascript" in ctype:
            js_rows.append(row)
            continue
        label = endpoint_label(row)
        ep = endpoints.setdefault(label, {"endpoint": label, "responses": 0, "key_hits": {}, "value_hits": {},
                                          "string_hits": {}, "context_paths": set(), "strong": 0, "example_file": None,
                                          "example_hit_path": None})
        ep["responses"] += 1
        data = path.read_bytes()
        obj = None
        if path.suffix == ".json" or data[:1] in (b"{", b"["):
            try:
                obj = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                obj = None
        if obj is not None:
            for h in scan_json(obj, keywords):
                bucket = ep["key_hits"] if h.where == "key" else ep["value_hits"]
                g = generalize_path(h.path)
                rec = bucket.setdefault(g, {"keywords": sorted(set(h.keywords)), "count": 0, "preview": h.value_preview})
                rec["count"] += 1
                if h.where == "key" and is_strong(h.path):
                    ep["strong"] += 1
                    if ep["example_file"] is None:
                        ep["example_file"], ep["example_hit_path"] = row["body_file"], h.path
            # Every key under a mix/transition ancestor, keyword or not (e.g. transitions[*].trackUri).
            for p, key, _ in walk(obj):
                if key and is_strong(parent_path(p)):
                    ep["context_paths"].add(generalize_path(p))
        else:
            for s in printable_strings(data):
                kws = match_text(s, keywords)
                if kws:
                    rec = ep["string_hits"].setdefault(s[:120], {"keywords": kws, "count": 0})
                    rec["count"] += 1
                    if STRONG_TOKENS.intersection(tokenize(s)):
                        ep["strong"] += 1
                        ep["example_file"] = ep["example_file"] or row["body_file"]
    return endpoints, js_rows


def sample_structure(run_dir: Path, ep: dict, max_chars: int = 5000) -> str | None:
    """Redacted JSON around the first strong hit: the object that holds it (or its parent)."""
    if not ep.get("example_file") or not ep.get("example_hit_path"):
        return None
    try:
        obj = json.loads((run_dir / ep["example_file"]).read_text(encoding="utf-8"))
        node_path = parent_path(ep["example_hit_path"])
        node = get_at(obj, node_path)
        # Climb one more level if the holder is tiny, to show siblings (track refs etc.).
        if isinstance(node, dict) and len(node) <= 2 and node_path != "$":
            node_path = parent_path(node_path)
            node = get_at(obj, node_path)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        log.debug("Could not build sample for %s: %s", ep["endpoint"], e)
        return None
    text = json.dumps({"path": node_path, "value": redact(node)}, indent=2, ensure_ascii=False)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... (truncated)"


def analyze_ws(run_dir: Path, keywords) -> dict:
    f = run_dir / "ws_frames.jsonl"
    out = {"frames": 0, "hits": defaultdict(lambda: {"count": 0, "keywords": [], "preview": ""}), "strong": 0}
    if not f.is_file():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        out["frames"] += 1
        hits: list[Hit] = []
        if "text" in rec:
            try:
                hits = scan_json(json.loads(rec["text"]), keywords)
            except ValueError:
                hits = [Hit("$", "", "value", match_text(s, keywords), s[:120])
                        for s in [rec["text"]] if match_text(s, keywords)]
        else:
            for s in printable_strings(bytes.fromhex(rec.get("hex", ""))):
                kws = match_text(s, keywords)
                if kws:
                    hits.append(Hit("$", "", "value", kws, s[:120]))
        for h in hits:
            key = f"{rec['ws']} {generalize_path(h.path)} ({h.where})"
            e = out["hits"][key]
            e["count"] += 1
            e["keywords"] = sorted(set(e["keywords"]) | set(h.keywords))
            e["preview"] = e["preview"] or h.value_preview
            if h.where == "key" and is_strong(h.path) or STRONG_TOKENS.intersection(tokenize(h.value_preview)):
                out["strong"] += 1
    out["hits"] = dict(out["hits"])
    return out


def analyze_js(run_dir: Path, js_rows: list[dict]) -> dict:
    """What the web player's code knows about: GraphQL ops and string literals."""
    ops: dict[str, dict] = {}
    literals: dict[str, int] = defaultdict(int)
    for row in js_rows:
        text = (run_dir / row["body_file"]).read_text(encoding="utf-8", errors="replace")
        for name, kind, sha in GQL_DEF_RE.findall(text):
            ops[name] = {"kind": kind, "sha256": sha}
        for a, b in JS_LITERAL_RE.findall(text):
            s = a or b
            if JS_STRONG_RE.search(s) and not CSS_NOISE_RE.search(s):
                literals[s] += 1
    strong_ops = {n: v for n, v in ops.items() if JS_STRONG_RE.search(n) or STRONG_TOKENS.intersection(tokenize(n))}
    top_literals = sorted(literals.items(), key=lambda kv: (-kv[1], kv[0]))[:80]
    return {"bundles": len(js_rows), "graphql_ops_total": len(ops), "graphql_ops_mix_related": strong_ops,
            "literals": top_literals}


def analyze_dom(run_dir: Path) -> dict:
    dom_dir = run_dir / "dom"
    out = {"snapshots": [], "strong_elements": {}, "sliders": {}}
    if not dom_dir.is_dir():
        return out
    for f in sorted(dom_dir.glob("*.json")):
        try:
            frames = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        n_match = n_slider = 0
        for fr in frames:
            for el in fr.get("matches", []):
                sig = " ".join(f"{k}={v}" for k, v in sorted(el["attrs"].items()))
                hay = sig + " " + el.get("text", "")
                if STRONG_TOKENS.intersection(tokenize(hay)):
                    key = f"<{el['tag']}> {sig} :: {el.get('text', '')[:80]}"
                    out["strong_elements"].setdefault(key, []).append(f.stem)
                    n_match += 1
            for sl in fr.get("sliders", []):
                label = sl["attrs"].get("aria-label") or sl.get("ancestor_label") or sl["attrs"].get("data-testid") or "?"
                key = f"<{sl['tag']}> {label}"
                vals = f"now={sl.get('now')} value={sl.get('value')} min={sl.get('min')} max={sl.get('max')} " \
                       f"text={sl['attrs'].get('aria-valuetext')}"
                out["sliders"].setdefault(key, []).append(f"{f.stem}: {vals}")
                n_slider += 1
        out["snapshots"].append({"file": f.name, "strong_elements": n_match, "sliders": n_slider})
    return out


def parameter_coverage(endpoints: dict, ws: dict, dom: dict) -> list[dict]:
    """For each needed parameter, list key paths that look like it inside a mix/transition context."""
    paths: list[tuple[str, str]] = []  # (source, generalized path)
    for ep in endpoints.values():
        for p in set(ep["key_hits"]) | ep["context_paths"]:
            paths.append((ep["endpoint"], p))
    for k in ws["hits"]:
        paths.append(("websocket", k))
    out = []
    for name, rx in PARAMETERS:
        leaf_re = re.compile(rx)
        found = []
        for src, p in paths:
            if not is_strong(p):
                continue
            leaf = re.split(r"[.\[]", p.rstrip("]"))[-1].strip("'\"")
            if any(leaf_re.match(t) for t in tokenize(leaf)):
                found.append(f"{p}  ({src})")
        dom_found = [k for k in dom["sliders"] if any(leaf_re.match(t) for t in tokenize(k))]
        out.append({"parameter": name, "network_candidates": sorted(set(found))[:12], "dom_candidates": dom_found[:8]})
    return out


def holders_of_strong_paths(run_dir: Path, endpoints: dict) -> dict:
    """All sibling keys of the objects holding strong hits (reveals track refs, ids)."""
    out = {}
    for ep in endpoints.values():
        if not ep.get("example_file") or not ep.get("example_hit_path"):
            continue
        try:
            obj = json.loads((run_dir / ep["example_file"]).read_text(encoding="utf-8"))
            holder = get_at(obj, parent_path(ep["example_hit_path"]))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        if isinstance(holder, dict):
            out[ep["endpoint"]] = sorted(map(str, holder.keys()))
    return out


def write_report(run_dir: Path, result: dict) -> Path:
    L: list[str] = []
    w = L.append
    v = result["verdict"]
    w(f"# Phase 1 discovery report\n\nRun: `{run_dir.name}`  \n"
      f"Responses indexed: {result['counts']['responses']}, bodies saved: {result['counts']['bodies']}, "
      f"WebSocket frames: {result['ws']['frames']}, DOM snapshots: {len(result['dom']['snapshots'])}, "
      f"write requests blocked: {result['counts']['blocked']}\n")
    w(f"## Verdict\n\n**{v['headline']}**\n")
    for line in v["details"]:
        w(f"- {line}")
    w("")

    w("## Endpoints with transition-like fields (ranked)\n")
    ranked = result["endpoints_ranked"]
    if not ranked:
        w("_None: no captured response contained a keyword in a key name._\n")
    for ep in ranked[:15]:
        w(f"### {ep['endpoint']}\n\nresponses: {ep['responses']}, strong hits: {ep['strong']}\n")
        if ep["key_hits"]:
            w("| key path | keywords | seen | example value |\n|---|---|---|---|")
            for p, h in sorted(ep["key_hits"].items(), key=lambda kv: (not is_strong(kv[0]), kv[0]))[:25]:
                w(f"| `{p}` | {', '.join(h['keywords'])} | {h['count']} | `{h['preview'].replace('|', '/')}` |")
            w("")
        if ep["string_hits"]:
            w("Binary/protobuf strings: " + "; ".join(f"`{s}`" for s in list(ep["string_hits"])[:15]) + "\n")
        if ep.get("holder_keys"):
            w(f"Keys next to the first strong hit: `{', '.join(ep['holder_keys'][:40])}`\n")
        if ep.get("sample"):
            w("Sample (redacted, lists trimmed to 3):\n\n```json\n" + ep["sample"] + "\n```\n")

    w("## Parameter coverage (heuristic - please sanity-check)\n")
    w("| parameter | network candidates | DOM slider candidates |\n|---|---|---|")
    for c in result["coverage"]:
        net = "<br>".join(f"`{x}`" for x in c["network_candidates"]) or "**not found**"
        dom = "<br>".join(f"`{x}`" for x in c["dom_candidates"]) or "-"
        w(f"| {c['parameter']} | {net} | {dom} |")
    w("")

    js = result["js"]
    w(f"## What the web player code references\n\nJS bundles scanned: {js['bundles']}, "
      f"GraphQL operations defined: {js['graphql_ops_total']}\n")
    if js["graphql_ops_mix_related"]:
        w("Mix/transition-related GraphQL operations:\n")
        for n, d in sorted(js["graphql_ops_mix_related"].items()):
            w(f"- `{n}` ({d['kind']})")
        w("")
    else:
        w("_No GraphQL operation names mention mix/transition/crossfade._\n")
    if js["literals"]:
        w("<details><summary>Mix-related string literals in the code (top 80)</summary>\n")
        for s, n in js["literals"]:
            w(f"- `{s}` x{n}")
        w("\n</details>\n")

    dom = result["dom"]
    w("## DOM\n")
    for s in dom["snapshots"]:
        w(f"- `{s['file']}`: {s['strong_elements']} mix-related elements, {s['sliders']} sliders")
    w("")
    if dom["strong_elements"]:
        w("<details><summary>Mix-related elements</summary>\n")
        for k, snaps in list(dom["strong_elements"].items())[:60]:
            w(f"- `{k.replace('`', '')}` (in {len(set(snaps))} snapshot(s))")
        w("\n</details>\n")
    if dom["sliders"]:
        w("<details><summary>Sliders and their values</summary>\n")
        for k, vals in list(dom["sliders"].items())[:40]:
            w(f"- `{k}`")
            for val in vals[:6]:
                w(f"  - {val}")
        w("\n</details>\n")

    ws = result["ws"]
    if ws["hits"]:
        w("## WebSocket\n")
        for k, h in list(ws["hits"].items())[:30]:
            w(f"- `{k}` x{h['count']}: {', '.join(h['keywords'])} e.g. `{h['preview']}`")
        w("")

    out = run_dir / "report.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def verdict(endpoints_ranked: list[dict], ws: dict, dom: dict, js: dict, counts: dict) -> dict:
    net_strong = sum(ep["strong"] for ep in endpoints_ranked)
    dom_strong = sum(s["strong_elements"] for s in dom["snapshots"])
    sliders = sum(s["sliders"] for s in dom["snapshots"])
    details = [
        f"Strong (mix/transition/crossfade/fade) key hits in network responses: {net_strong}",
        f"Strong hits in WebSocket frames: {ws['strong']}",
        f"Mix-related DOM elements: {dom_strong}; sliders seen: {sliders}",
        f"Mix-related GraphQL operations in the web player code: {len(js['graphql_ops_mix_related'])}",
    ]
    if counts["responses"] == 0:
        return {"headline": "Capture is empty - discovery did not record anything. Re-run phase1_discover.py.",
                "status": "empty", "details": details}
    if net_strong == 0 and ws["strong"] == 0 and dom_strong == 0 and not js["graphql_ops_mix_related"]:
        details.append("Next option: attach to the Spotify desktop app over the Chrome DevTools Protocol "
                       "(see README 'Fallback: desktop app via CDP').")
        return {"headline": "No transition data observed: the web player does not appear to expose "
                            "mixed-playlist transitions.", "status": "not_found", "details": details}
    if net_strong == 0:
        details.append("The UI/code mentions mixing, but no response carried transition fields. Make sure the "
                       "editor was open and each transition was clicked during capture, then re-run.")
        return {"headline": "Transition UI detected, but no transition data found in network traffic yet.",
                "status": "ui_only", "details": details}
    return {"headline": "Transition-like data found in network responses - review the endpoints below.",
            "status": "found", "details": details}


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    return asdict(o)


def latest_run(output_dir: Path) -> Path:
    runs = sorted(p for p in (output_dir / "discovery").glob("*") if (p / "index.jsonl").is_file())
    if not runs:
        raise SystemExit(f"ERROR: no discovery runs found under {output_dir / 'discovery'}. Run phase1_discover.py first.")
    return runs[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="discovery run folder (default: newest)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir).resolve() if args.run_dir else latest_run(cfg.output_dir)
    setup_logging(run_dir / "analyze.log", verbose=args.verbose)
    log.info("Analyzing %s", run_dir)

    keywords = compile_keywords(cfg.discovery.keywords)
    rows = load_index(run_dir)
    blocked_f = run_dir / "blocked_requests.jsonl"
    counts = {
        "responses": len(rows),
        "bodies": sum(1 for r in rows if r.get("body_file")),
        "blocked": len(blocked_f.read_text(encoding="utf-8").splitlines()) if blocked_f.is_file() else 0,
    }
    endpoints, js_rows = analyze_bodies(run_dir, rows, keywords)
    log.info("Scanned %d bodies across %d endpoints (+%d JS bundles)", counts["bodies"], len(endpoints), len(js_rows))
    ws = analyze_ws(run_dir, keywords)
    js = analyze_js(run_dir, js_rows)
    dom = analyze_dom(run_dir)

    holders = holders_of_strong_paths(run_dir, endpoints)
    ranked = sorted((ep for ep in endpoints.values() if ep["key_hits"] or ep["string_hits"]),
                    key=lambda ep: (-ep["strong"], -len(ep["key_hits"]), ep["endpoint"]))
    for ep in ranked[:15]:
        ep["sample"] = sample_structure(run_dir, ep)
        ep["holder_keys"] = holders.get(ep["endpoint"])

    result = {
        "run": run_dir.name,
        "counts": counts,
        "endpoints_ranked": ranked,
        "coverage": parameter_coverage(endpoints, ws, dom),
        "ws": ws,
        "js": js,
        "dom": dom,
    }
    result["verdict"] = verdict(ranked, ws, dom, js, counts)
    (run_dir / "report.json").write_text(json.dumps(result, indent=1, default=_json_default), encoding="utf-8")
    report = write_report(run_dir, result)

    log.info("VERDICT: %s", result["verdict"]["headline"])
    for d in result["verdict"]["details"]:
        log.info("  %s", d)
    log.info("Report written: %s", report)
    log.info("Share report.md (secrets are redacted). Do NOT share network.har or bodies/.")
    return 0 if result["verdict"]["status"] == "found" else 1


if __name__ == "__main__":
    sys.exit(main())
