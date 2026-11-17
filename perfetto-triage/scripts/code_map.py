#!/usr/bin/env python3
"""
Whose code is each hot slice? A first pass at Step 4 of the skill.

  ./code_map.py triage-out --app-repo ~/code/app

triage.py runs this itself when it is given --app-repo. It takes the slice
names from the startup, late-frame and self-time rankings and checks each one
against the app's source. A slice is called the app's own only on evidence:

  trace section   the name is the string passed to a tracing call in the app
                  (androidx.tracing trace("..."), Trace.beginSection("..."),
                  Dart Timeline, React Native Systrace), including names built
                  from a template such as trace("screen.$name")
  source file     the slice names a file and line that exists in the app, which
                  is what Compose composition tracing writes: Foo (Foo.kt:42)
  class.method    the slice reads Class.method, the app has Class.kt or
                  Class.java, and that file defines the method
  app class       a framework slice that names a class under the app's package,
                  such as performCreate:com.example.app.MainActivity

Everything else is reported as "not in your source". That is a statement about
the search, not proof the slice is framework code: a name built at runtime, or
code in a dependency, will not be found.

No third-party dependencies. Reads source files, never runs them.
"""

import argparse
import csv
import json
import os
import re
import sys

SKIP_DIRS = {"build", ".gradle", ".git", ".idea", "node_modules", "out", "dist", "Pods",
             ".cxx", "intermediates", "generated", ".kotlin", "DerivedData",
             "test", "androidTest", "androidUnitTest", "commonTest", "benchmark", "__tests__"}
SOURCE_EXT = (".kt", ".java", ".dart", ".js", ".jsx", ".ts", ".tsx")
MAX_FILES = 25_000
MAX_BYTES = 1_000_000

# A string literal handed to a tracing call. Kotlin/Java use double quotes,
# Dart and JS may use single.
TRACE_CALL = re.compile(
    r"""\b(?:trace|traceAsync|traceSection|beginSection|beginAsyncSection|
            Trace\.\w+|TraceCompat\.\w+|
            Timeline\.(?:startSync|timeSync|instantSync)|TimelineTask\(\)\.start|
            Systrace\.(?:beginEvent|beginAsyncEvent|measure\w*))
        \s*\(\s*(?:\w+\s*=\s*)?
        (?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')""",
    re.X)
TEMPLATE_PART = re.compile(r"\\\$\\\{[^}]*\\\}|\\\$\w+")
FILE_REF = re.compile(r"\b([A-Za-z_]\w*)\.(kt|java|dart|tsx?|jsx?):(\d+)")
CLASS_METHOD = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.([a-z_][A-Za-z0-9_]*)\b")
TRAILING_ARGS = re.compile(r"[\s:]+[-\d.\s,x]+$")
NAMED = ("trace section", "source file", "class.method")
STEMS = (("40_slice_hotspots", "name", "self_ms"),
         ("23_jank_hot_slices", "name", "total_ms"),
         ("13_startup_top_slices", "slice_name", "total_ms"))


def _walk(repo: str):
    seen = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(SOURCE_EXT):
                seen += 1
                if seen > MAX_FILES:
                    return
                yield os.path.join(root, f)


def scan_repo(repo: str) -> dict:
    """One pass over the app's source: which class files exist, and every
    string the app hands to a tracing call. Never raises on odd input."""
    index = {"repo": os.path.basename(os.path.abspath(repo)) if repo else "",
             "root": repo, "files": {}, "sections": [], "scanned": 0}
    if not repo or not os.path.isdir(repo):
        return index
    for path in _walk(repo):
        rel = os.path.relpath(path, repo)
        stem = os.path.splitext(os.path.basename(path))[0]
        index["files"].setdefault(stem, rel)
        index["scanned"] += 1
        try:
            if os.path.getsize(path) > MAX_BYTES:
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "race" not in text and "Timeline" not in text:
            continue
        for m in TRACE_CALL.finditer(text):
            literal = m.group(1) if m.group(1) is not None else m.group(2)
            if not literal or len(literal) > 200:
                continue
            line = text.count("\n", 0, m.start()) + 1
            pattern = None
            if "$" in literal:
                body = TEMPLATE_PART.sub(".+", re.escape(literal))
                if body.replace(".+", "").strip():      # a template that is all variable matches nothing
                    pattern = re.compile(body)
                else:
                    continue
            index["sections"].append({"literal": literal, "pattern": pattern,
                                      "where": f"{rel}:{line}"})
    return index


def _sql_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _glob_escape(text: str) -> str:
    """Make *, ? and [ literal in a GLOB pattern."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def section_match_sql(index: dict, column: str = "s.name", limit: int = 400) -> str | None:
    """A SQL condition that is true for slices named after one of the app's
    trace sections. None when the app declares none."""
    literals, globs = [], []
    for sec in index.get("sections", []):
        lit = sec["literal"]
        if "\n" in lit or "\x00" in lit:
            continue
        if sec["pattern"] is None:
            if lit not in literals:
                literals.append(lit)
        else:
            # trace("checkout.$name") -> GLOB 'checkout.*'
            parts = re.split(r"\$\{[^}]*\}|\$\w+", lit)
            pattern = "*".join(_glob_escape(p) for p in parts)
            if pattern.replace("*", "") and pattern not in globs:
                globs.append(pattern)
    literals, globs = literals[:limit], globs[:limit]
    clauses = []
    if literals:
        clauses.append(f"{column} IN ({', '.join(_sql_str(x) for x in literals)})")
    clauses.extend(f"{column} GLOB {_sql_str(g)}" for g in globs)
    return " OR ".join(clauses) if clauses else None


def section_findings(rows: list[dict]) -> list[dict]:
    """rows: 24_jank_by_app_section as dicts. What to say about them."""
    if not rows:
        return []

    def n(row, key):
        try:
            return float(row.get(key) or 0)
        except ValueError:
            return 0.0
    all_late = n(rows[0], "all_late_frames")
    inside = n(rows[0], "late_frames_in_any_section")
    if not all_late:
        return []
    src = ["24_jank_by_app_section"]
    top = max(rows, key=lambda r: n(r, "late_frames"))
    if not n(top, "late_frames"):
        return [{
            "severity": "info", "area": "jank",
            "title": "No late frame overlaps any of the app's trace sections",
            "detail": f"{len(rows)} of the app's sections ran in this trace and "
                      f"{all_late:.0f} frames were late, but never at the same time.",
            "action": "The late frames come from work that is not inside a section yet. "
                      "Wrap the screens or transitions you suspect and capture again.",
            "source": src}]
    share = 100.0 * n(top, "late_frames") / all_late
    others = [r for r in rows if r is not top and n(r, "late_frames")][:3]
    more = ("" if not others else " Next: " + ", ".join(
        f"{r['section']} ({n(r, 'late_frames'):.0f})" for r in others) + ".")
    return [{
        "severity": "medium" if share >= 25 else "info", "area": "jank",
        "title": f"{n(top, 'late_frames'):.0f} of {all_late:.0f} late frames overlap {top['section']}",
        "detail": f"That is {share:.0f}% of the late frames, worst {n(top, 'worst_overrun_ms'):.0f} ms "
                  f"over. {inside:.0f} of {all_late:.0f} late frames overlap at least one of the "
                  f"app's sections." + more,
        "action": "Look at this section first: it is the app's own code running while frames "
                  "were late. Overlap is not proof it caused them, since a section that is "
                  "simply long overlaps more frames. Narrow it with smaller sections inside it.",
        "source": src}]


def _defines(index: dict, stem: str, method: str) -> str | None:
    """'path:line' where the class file defines the method, else None."""
    rel = index["files"].get(stem)
    if not rel:
        return None
    cache = index.setdefault("_text", {})
    if rel not in cache:
        try:
            cache[rel] = open(os.path.join(index["root"], rel), encoding="utf-8",
                              errors="replace").read()
        except OSError:
            cache[rel] = ""
    m = re.search(r"\b(?:fun\s+(?:<[^>]*>\s*)?(?:\w+\.)?|[\w<>\[\], ?]+\s+)" + re.escape(method)
                  + r"\s*\(", cache[rel])
    if not m:
        return None
    return f"{rel}:{cache[rel].count(chr(10), 0, m.start()) + 1}"


def classify(name: str, index: dict, pkg: str = "") -> dict:
    """{'owner': 'app'|'other', 'how': str, 'evidence': 'path:line'|None}"""
    clean = (name or "").strip()
    bare = TRAILING_ARGS.sub("", clean)
    for sec in index["sections"]:
        if sec["pattern"] is None:
            if sec["literal"] in (clean, bare):
                return {"owner": "app", "how": "trace section", "evidence": sec["where"]}
        elif sec["pattern"].fullmatch(clean) or sec["pattern"].fullmatch(bare):
            return {"owner": "app", "how": "trace section", "evidence": sec["where"]}
    m = FILE_REF.search(clean)
    if m and m.group(1) in index["files"]:
        return {"owner": "app", "how": "source file",
                "evidence": f"{index['files'][m.group(1)]}:{m.group(3)}"}
    for m in CLASS_METHOD.finditer(clean):
        where = _defines(index, m.group(1), m.group(2))
        if where:
            return {"owner": "app", "how": "class.method", "evidence": where}
    base = (pkg or "").rstrip("*").rstrip(".")
    if base and "." in base and base in clean:
        tail = clean[clean.index(base) + len(base):]
        m = re.search(r"\.([A-Z][A-Za-z0-9_]*)", tail)
        if m and m.group(1) in index["files"]:
            return {"owner": "app", "how": "app class", "evidence": index["files"][m.group(1)]}
    return {"owner": "other", "how": "not in your source", "evidence": None}


def build(results: dict, repo: str, pkg: str = "", index: dict | None = None) -> dict:
    """results: {stem: (header, rows)} as triage.py holds them."""
    index = index if index is not None else scan_repo(repo)
    merged: dict[str, dict] = {}
    for stem, name_col, ms_col in STEMS:
        header, rows = results.get(stem, ([], []))
        if name_col not in header or ms_col not in header:
            continue
        ni, mi = header.index(name_col), header.index(ms_col)
        for row in rows:
            try:
                value = float(row[mi])
            except (ValueError, IndexError):
                continue
            entry = merged.setdefault(row[ni], {"name": row[ni], "ms": 0.0, "stems": []})
            entry["ms"] = max(entry["ms"], value)
            if stem not in entry["stems"]:
                entry["stems"].append(stem)
    slices = []
    for entry in sorted(merged.values(), key=lambda e: -e["ms"]):
        entry.update(classify(entry["name"], index, pkg))
        entry["ms"] = round(entry["ms"], 3)
        slices.append(entry)
    return {"repo": index["repo"], "files_scanned": index["scanned"],
            "sections_declared": len(index["sections"]), "checked": len(slices),
            "app_owned": sum(1 for s in slices if s["owner"] == "app"),
            "named_after_app_code": sum(1 for s in slices if s["how"] in NAMED),
            "slices": slices}


def findings(cm: dict) -> list[dict]:
    """What the code map adds to the report, as findings."""
    if not cm or not cm.get("checked"):
        return []
    stems = sorted({s for x in cm["slices"] for s in x["stems"]})
    scope = (f"Checked {cm['checked']} slice names from the startup, late-frame and "
             f"self-time rankings against {cm['repo']} ({cm['files_scanned']} source files). ")
    named = [s for s in cm["slices"] if s["how"] in NAMED]
    lifecycle = [s for s in cm["slices"] if s["how"] == "app class"]

    def listed(items):
        return "; ".join(f"{s['name']} ({s['ms']:.0f} ms, {s['evidence']})" for s in items[:5])

    if named:
        return [{
            "severity": "info", "area": "code",
            "title": f"{len(named)} of {cm['checked']} hot slices are named after the app's own code",
            "detail": scope + "These point at a file: " + listed(named) + ".",
            "action": "Start with these. Every other name in the rankings was not found in "
                      "the app's source, so it is framework or library work the app triggered.",
            "source": stems, "directional_only": False}]
    tail = (" The only names that lead into the app are framework lifecycle slices: "
            + listed(lifecycle) + ".") if lifecycle else ""
    if cm["sections_declared"] == 0:
        title = "The app has no trace sections of its own"
        why = "None is named after a function or screen in the app, and the app declares no trace sections."
    else:
        title = "None of the app's trace sections shows up in the rankings"
        why = (f"The app declares {cm['sections_declared']} trace sections and none of them "
               f"ranks, so the slow paths are not the instrumented ones.")
    return [{
        "severity": "medium", "area": "code", "title": title,
        "detail": scope + why + tail,
        "action": "Add androidx.tracing sections, trace(\"Name\") { ... }, around the code "
                  "paths you suspect and capture again. For Compose, turn on composition "
                  "tracing so recomposition slices carry composable names. Until then this "
                  "report can say which framework stage is slow, not which screen or "
                  "function caused it.",
        "source": stems, "directional_only": False}]


def load_results(out_dir: str) -> dict:
    results = {}
    for stem, _, _ in STEMS:
        path = os.path.join(out_dir, "csv", stem + ".csv")
        if os.path.exists(path):
            with open(path, newline="") as fh:
                rows = list(csv.reader(fh))
            results[stem] = (rows[0] if rows else [], rows[1:])
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="a directory written by triage.py")
    ap.add_argument("--app-repo", required=True, help="the app's source tree")
    ap.add_argument("--pkg", default="", help="the app's package, to spot its classes "
                                             "inside framework slice names")
    ap.add_argument("--json", action="store_true", help="print the whole map as JSON")
    args = ap.parse_args()
    cm = build(load_results(args.out_dir), args.app_repo, args.pkg)
    if args.json:
        print(json.dumps(cm, indent=2))
        return
    print(f"{cm['app_owned']} of {cm['checked']} hot slices found in {cm['repo']} "
          f"({cm['files_scanned']} source files, {cm['sections_declared']} trace sections declared)")
    for s in cm["slices"][:30]:
        mark = "APP  " if s["owner"] == "app" else "     "
        print(f"  {mark}{s['ms']:9.1f} ms  {s['name'][:60]:60s} {s['evidence'] or ''}")
    if not cm["checked"]:
        sys.exit("no slice rankings found in " + args.out_dir)


if __name__ == "__main__":
    main()
