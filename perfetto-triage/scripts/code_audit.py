#!/usr/bin/env python3
"""
What can be fixed in the code that ran while the trace was recorded.

  ./code_audit.py triage-out --app-repo ~/code/app
  ./code_audit.py triage-out --app-repo ~/code/app --flow Home,TripDetail

triage.py runs this itself when it is given --app-repo. A trace says what was
slow. It does not say which line caused it. This reads the app's source for the
second half, in two steps:

  1. The path. Which code ran during the capture?
       cold start    the Application class, startup initializers and the
                     launcher Activity from AndroidManifest.xml, then the app
                     code those files refer to, two steps out
       a driven flow the screens the person or agent names with --flow, matched
                     to source files by name
       either        any app class or file the trace itself names
  2. The audit. Each file on the path is read for patterns that match what the
     trace measured: main-thread calls into system services when the trace has
     main-thread binder time, animations that never stop when Compose work runs
     on every frame, a missing Baseline Profile when the JIT was busy, and so on.

Every item is a candidate, not a verdict. A pattern match cannot see which
thread a line runs on or whether it runs at all. The agent reads the file,
drops what does not apply, and only then suggests the fix.

Code lines are copied into the report with long string literals blanked, so a
key or URL in the source does not travel with it.

No third-party dependencies. Reads source files, never runs them.
"""

import argparse
import csv
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OTHER_PLATFORMS = {"iosMain", "jsMain", "wasmJsMain", "desktopMain", "macosMain", "nativeMain",
                   "appleMain", "ios", "macos", "web", "windows", "linux"}
MAX_PATH_FILES = 80
MAX_PER_CHECK = 8
STARTUP_KINDS = ["slow_cold_start", "slow_start", "startup_dominated", "startup_waiting",
                 "startup_compute", "startup_mixed"]
JANK_KINDS = ["jank_rate", "jank_ui", "jank_split", "layout_cost", "handoff_compose-expert"]

SYMBOL = re.compile(
    r"^[ \t]*(?:@[\w.]+(?:\([^)\n]*\))?[ \t\n]*)*"
    r"(?:(?:public|private|internal|protected|actual|expect|open|abstract|override|suspend|"
    r"inline|data|sealed|final|static|enum|annotation|value)[ \t]+)*"
    r"(?:fun[ \t]+(?:<[^>\n]*>[ \t]*)?(?:[\w.]+\.)?([A-Z]\w*)[ \t]*\(|"
    r"(?:class|object|interface)[ \t]+([A-Z]\w*))", re.M)
# Top-level lower-case functions and values (initKoin(), appModule): the glue
# between an entry point and the rest of the app in many Kotlin projects.
TOP_LEVEL = re.compile(
    r"^(?:(?:public|internal|private|actual|expect|suspend|inline|const)[ \t]+)*"
    r"(?:fun[ \t]+(?:<[^>\n]*>[ \t]*)?([a-z]\w{4,})[ \t]*\(|va[lr][ \t]+([a-z]\w{4,})\b)", re.M)
IDENT = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b")
LONG_STRING = re.compile(r'"(?:[^"\\\n]|\\.){13,}"')
OFF_MAIN = re.compile(r"Dispatchers\.(IO|Default)|\bwithContext\(|\.subscribeOn\(|Executors?\.|"
                      r"\bthread\s*[({]|WorkManager|CoroutineWorker|\bAsyncTask\b")

# Each check: where it looks, what it matches, which findings make it relevant,
# and what to say. scope: seed (the entry files only), path (every file on the
# path), repo (anywhere, used when the path has no hit and a finding asks).
CHECKS = [
    {"id": "sdk_init_on_start", "scope": "seed", "kinds": STARTUP_KINDS, "always_in": "startup",
     "pattern": re.compile(
         r"\b(?:startKoin|Firebase\w*\.\w+|Sentry\w*\.init|Mapbox\w*\.\w+|Mixpanel\w*\.\w+|"
         r"Amplitude\w*|AppsFlyerLib|Adjust\.\w+|Branch\.\w+|OneSignal\.\w+|MobileAds\.initialize|"
         r"Timber\.plant|WorkManager\.initialize|FacebookSdk\.\w+|Purchases\.configure|"
         r"Intercom\.\w+|PaymentConfiguration\.init|Places\.initialize|MapsInitializer\.\w+|"
         r"Bugsnag\.\w+|Datadog\.\w+|Embrace\.\w+|NewRelic\.\w+|Instabug\w*|CleverTap\w*|"
         r"MoEngage\w*|UXCam\.\w+|Smartlook\w*|Napier\.base|[A-Z]\w*\.(?:init|initialize|install|"
         r"configure|setup)\w*)\s*[({]"),
     "title": "Something is set up on the startup path",
     "why": "Everything the Application class, an initializer or the launcher Activity sets up "
            "runs before the first frame, whether or not the first screen needs it.",
     "fix": "Keep what the first screen needs. Move the rest behind a lazy, a background "
            "dispatcher, or androidx.startup with lazy initialisation.",
     "plan": ["startup_compute", "startup_mixed", "startup_waiting"]},
    {"id": "blocking_call", "scope": "path", "kinds": STARTUP_KINDS + ["jank_ui", "main_binder"],
     "pattern": re.compile(r"\brunBlocking\b|\bThread\.sleep\s*\(|\.blockingGet\(\)|\.blockingFirst\(\)|"
                           r"\.get\(\)\s*//\s*block|CountDownLatch\(|\.join\(\)"),
     "title": "A call that blocks the thread it runs on",
     "why": "On the main thread this is time the app spends waiting, not working. It shows in "
            "the trace as the main thread asleep.",
     "fix": "Make it a suspend call on a background dispatcher, or move it past the first frame.",
     "plan": ["startup_waiting", "startup_mixed"]},
    {"id": "system_call", "scope": "path", "kinds": ["main_binder", "startup_waiting"],
     "pattern": re.compile(
         r"getSystemService\s*[(<]|\bpackageManager\.\w+|getPackageInfo\(|getInstalledPackages\(|"
         r"checkSelfPermission\(|contentResolver\.query\(|Settings\.(?:Secure|Global|System)\.get\w*|"
         r"\bregisterReceiver\(|\.activeNetwork\b|getNetworkCapabilities\(|isProviderEnabled\(|"
         r"\.getRunningAppProcesses|AccountManager\.get|NotificationManagerCompat\.from"),
     "title": "A call into a system service",
     "why": "These look local but are binder calls to another process. On the main thread each "
            "one is the app frozen until the reply comes back.",
     "fix": "Call it from a background dispatcher, cache the result, or batch repeated calls.",
     "plan": ["main_binder", "startup_waiting", "startup_mixed"]},
    {"id": "telephony_call", "scope": "path", "kinds": ["main_binder"], "server": "com.android.phone",
     "pattern": re.compile(r"TelephonyManager|SubscriptionManager|telephonyManager\.\w+|"
                           r"\.simOperator\w*|\.networkOperator\w*|simCountryIso|networkCountryIso|"
                           r"PhoneNumberUtils|SmsManager"),
     "title": "A call into the telephony service",
     "why": "The trace shows main-thread binder calls to com.android.phone. Telephony and "
            "subscription APIs are served by that process.",
     "fix": "Read the value once on a background dispatcher and keep it, instead of asking on "
            "the main thread.",
     "plan": ["main_binder"]},
    {"id": "disk_or_parse_on_start", "scope": "seed", "kinds": STARTUP_KINDS,
     "pattern": re.compile(r"\.readText\(\)|\.readBytes\(\)|BitmapFactory\.decode\w+\(|"
                           r"Json\.decodeFromString|\.fromJson\(|ObjectInputStream\(|"
                           r"getSharedPreferences\(|\.edit\(\).*\.commit\(\)|openFileInput\(|"
                           r"\bassets\.open\("),
     "title": "Disk read or parsing on the startup path",
     "why": "Reading or decoding a file before the first frame adds its whole duration to "
            "startup, and it is slowest on a cold device.",
     "fix": "Load it off the main thread and show the first screen without it, or defer it "
            "until it is needed.",
     "plan": ["startup_compute", "startup_mixed", "startup_waiting"]},
    {"id": "endless_animation", "scope": "path", "repo_fallback": True,
     "kinds": ["handoff_compose-expert", "jank_ui", "jank_rate"],
     "pattern": re.compile(r"rememberInfiniteTransition|infiniteRepeatable|withFrameNanos|"
                           r"withFrameMillis|IterateForever|iterations\s*=\s*Int\.MAX_VALUE|"
                           r"repeatCount\s*=\s*\w*\.?INFINITE|[Ss]himmer\w*\s*\("),
     "title": "An animation that runs until something stops it",
     "why": "An endless animation recomposes or redraws on every frame for as long as it is "
            "in the composition, including while it is covered or off screen.",
     "fix": "Run it only while it is visible and needed: take it out of the composition when "
            "loading ends, and stop it when the screen is covered or in the background.",
     "plan": ["handoff_compose-expert", "jank_ui"]},
    {"id": "lazy_list_without_key", "scope": "path", "kinds": JANK_KINDS,
     "pattern": re.compile(r"\bitems(?:Indexed)?\s*\((?![^\n]*\bkey\b)"),
     "title": "A lazy list with no item key",
     "why": "Without a stable key, inserting or moving one item makes Compose treat the rows "
            "after it as new, and it recomposes them all.",
     "fix": "Pass key = { it.id } with an id that is stable and unique.",
     "plan": ["handoff_compose-expert", "jank_ui"], "lookahead": 2},
]


def _load_code_map():
    spec = importlib.util.spec_from_file_location("code_map", os.path.join(HERE, "code_map.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOT_THE_APP = re.compile(r"benchmark|baseline-?profile|/lint|/build-logic|/buildSrc", re.I)


def _android(rel: str) -> bool:
    """True for source that can run in the Android app: not another platform's
    source set, and not a benchmark, profile-generator or build-logic module.
    Only the module part of the path is judged, the part before /src/, because
    package folders can be called anything."""
    path = "/" + rel.replace("\\", "/")
    module = path.split("/src/", 1)[0]
    return not (set(path.split("/")) & OTHER_PLATFORMS) and not NOT_THE_APP.search(module)


def redact(line: str, width: int = 150) -> str:
    """A source line that is safe to copy into a report."""
    text = LONG_STRING.sub('"…"', line.strip())
    return text if len(text) <= width else text[:width - 1] + "…"


# --------------------------------------------------------------------------
# the path
# --------------------------------------------------------------------------

def _manifests(repo: str) -> list[str]:
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in ("build", ".gradle", ".git", "node_modules", ".idea",
                                                "intermediates", "androidTest", "test", "debug")
                   and not d.startswith(".")]
        if "AndroidManifest.xml" in files and ("src" in root.split(os.sep) or root.endswith("android/app")):
            found.append(os.path.join(root, "AndroidManifest.xml"))
        if len(found) >= 40:
            break
    return found


def _attr(tag: str, name: str) -> str | None:
    m = re.search(r'android:' + name + r'\s*=\s*"([^"]+)"', tag)
    return m.group(1) if m else None


def read_manifests(repo: str) -> dict:
    """{'application': [names], 'launcher': [names], 'initializers': [names]}.
    Names are as written: .MainActivity, com.example.App, App."""
    out = {"application": [], "launcher": [], "initializers": [], "manifests": []}
    for path in _manifests(repo):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        out["manifests"].append(os.path.relpath(path, repo))
        m = re.search(r"<application\b[^>]*>", text, re.S)
        if m and _attr(m.group(0), "name"):
            out["application"].append(_attr(m.group(0), "name"))
        for block in re.finditer(r"<(activity|activity-alias)\b[^>]*?(?:/>|>.*?</\1>)", text, re.S):
            body = block.group(0)
            if "android.intent.action.MAIN" in body and "android.intent.category.LAUNCHER" in body:
                head = body.split(">", 1)[0]
                name = _attr(head, "targetActivity") or _attr(head, "name")
                if name:
                    out["launcher"].append(name)
        for block in re.finditer(r"<provider\b[^>]*?(?:/>|>.*?</provider>)", text, re.S):
            body = block.group(0)
            head = body.split(">", 1)[0]
            name = _attr(head, "name") or ""
            if "androidx.startup" in name:
                for meta in re.finditer(r"<meta-data\b[^>]*>", body):
                    if "androidx.startup" in (_attr(meta.group(0), "value") or ""):
                        out["initializers"].append(_attr(meta.group(0), "name") or "")
            elif name:
                out["initializers"].append(name)
    for key in ("application", "launcher", "initializers"):
        out[key] = [n for n in dict.fromkeys(out[key]) if n]
    return out


def _files_for(index: dict, name: str) -> list[str]:
    """Source files for a manifest class name. With several files of that name,
    the one whose package line agrees with the manifest name wins."""
    cls = name.rsplit(".", 1)[-1]
    files = [f for f in index.get("all_files", {}).get(cls, []) if _android(f)]
    if len(files) > 1 and "." in name.strip("."):
        package = name.rsplit(".", 1)[0].lstrip(".")
        exact = [f for f in files if re.search(
            r"^\s*package\s+[\w.]*" + re.escape(package) + r"\s*;?\s*$",
            _text(index, f), re.M)]
        files = exact or files
    return files[:2]


def _text(index: dict, rel: str) -> str:
    cache = index.setdefault("_text", {})
    if rel not in cache:
        try:
            path = os.path.join(index["root"], rel)
            cache[rel] = ("" if os.path.getsize(path) > 1_000_000
                          else open(path, encoding="utf-8", errors="replace").read())
        except OSError:
            cache[rel] = ""
    return cache[rel]


def symbol_index(index: dict) -> dict:
    """{Name: [files]} for every class, object and capitalised function the app
    defines. Composables are capitalised functions, and they rarely sit in a
    file of the same name, so the file index alone cannot find them."""
    if "_symbols" in index:
        return index["_symbols"]
    symbols: dict[str, list[str]] = {}
    for files in index.get("all_files", {}).values():
        for rel in files:
            if not _android(rel) or not rel.endswith((".kt", ".java")):
                continue
            text = _text(index, rel)
            for m in list(SYMBOL.finditer(text)) + list(TOP_LEVEL.finditer(text)):
                name = m.group(1) or m.group(2)
                bucket = symbols.setdefault(name, [])
                if rel not in bucket:
                    bucket.append(rel)
    index["_symbols"] = symbols
    return symbols


def _refs(index: dict, rel: str) -> list[str]:
    """App files this file refers to by name. A name defined in more than three
    files is too common to follow."""
    symbols = symbol_index(index)
    out = []
    for name in dict.fromkeys(IDENT.findall(_text(index, rel))):
        files = symbols.get(name)
        if not files or len(files) > (3 if name[0].isupper() else 1):
            continue
        for f in files:
            if f != rel and f not in out:
                out.append(f)
    return out


def _shared_segments(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("."), b.split(".")):
        if x != y:
            break
        n += 1
    return n


def build_path(index: dict, mode: str, flow: list[str] | None, code_map: dict | None,
               pkg: str = "") -> dict:
    """Which files ran during the capture, and how each one was chosen."""
    files: dict[str, dict] = {}        # rel -> {"role", "depth", "seed"}
    how, unmatched = [], []

    def add(rel, role, depth, seed=False):
        if rel in files or not _android(rel):
            return False
        if len(files) >= MAX_PATH_FILES and not seed:
            return False
        files[rel] = {"role": role, "depth": depth, "seed": seed}
        return True

    manifest = read_manifests(index["root"])
    found = []
    for key, role in (("application", "Application class"), ("initializers", "startup initializer"),
                      ("launcher", "launcher Activity")):
        for name in manifest[key]:
            for rel in _files_for(index, name):
                found.append((rel, role))
    # A repo can hold more than one app (a catalog, a demo). Keep the entry
    # points whose package is closest to the package that was traced.
    if pkg and pkg != "*" and len({role for _, role in found}) < len(found):
        score = {}
        for rel, _ in found:
            m = re.search(r"^\s*package\s+([\w.]+)", _text(index, rel), re.M)
            score[rel] = _shared_segments(m.group(1) if m else "", pkg.rstrip("*").rstrip("."))
        best = max(score.values())
        if best >= 2:
            found = [(rel, role) for rel, role in found if score[rel] == best]
    entry = []
    for rel, role in found:
        if add(rel, role, 0, seed=True):
            entry.append((rel, role))
    if entry:
        how.append("Entry points from AndroidManifest.xml: " + ", ".join(
            f"{os.path.basename(rel)} ({role})" for rel, role in entry) + ".")
    elif manifest["manifests"]:
        how.append("AndroidManifest.xml names no Application class or launcher Activity that "
                   "is a source file in this repo.")
    else:
        how.append("No AndroidManifest.xml was found in the source tree, so the entry points "
                   "are not known.")

    named = []
    for name in flow or []:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if not key:
            continue
        hits = []
        if "/" in name or name.endswith((".kt", ".java")):
            base = os.path.splitext(os.path.basename(name))[0]
            same = index.get("all_files", {}).get(base, [])
            hits = [f for f in same if f.replace("\\", "/").endswith(name)] or same
        else:
            for stem, rels in index.get("all_files", {}).items():
                if key in re.sub(r"[^a-z0-9]", "", stem.lower()):
                    hits.extend(rels)
            for sym, rels in symbol_index(index).items():
                if key == sym.lower() or sym.lower() in (key + "screen", key + "route", key + "page"):
                    hits.extend(rels)
        hits = [h for h in dict.fromkeys(hits) if _android(h)][:12]
        if not hits:
            unmatched.append(name)
        for rel in hits:
            if add(rel, f"flow: {name}", 0, seed=True):
                named.append(rel)
            elif rel in files and not files[rel]["role"].startswith("flow"):
                files[rel]["role"] += f", flow: {name}"
    if flow:
        how.append(f"Screens named with --flow: {', '.join(flow)} matched {len(named)} "
                   f"file{'' if len(named) == 1 else 's'}."
                   + (f" No file matched: {', '.join(unmatched)}." if unmatched else ""))

    traced = []
    for s in (code_map or {}).get("slices", []):
        ev = s.get("evidence")
        if s.get("owner") == "app" and ev:
            rel = ev.rsplit(":", 1)[0] if re.search(r":\d+$", ev) else ev
            if add(rel, "named in the trace", 0, seed=True):
                traced.append(rel)
    if traced:
        how.append(f"{len(traced)} file{'' if len(traced) == 1 else 's'} named by slices in the trace.")

    # What the entry files refer to, two steps out. A reference is not proof the
    # code ran, so each file keeps how far it is from an entry point.
    frontier = [rel for rel, meta in files.items() if meta["seed"]]
    for depth in (1, 2):
        nxt = []
        for rel in frontier:
            origin = os.path.basename(rel)
            for ref in _refs(index, rel):
                label = (f"used by {origin}" if depth == 1
                         else f"two steps from an entry point, via {origin}")
                if add(ref, label, depth):
                    nxt.append(ref)
        frontier = nxt
    reached = sum(1 for m in files.values() if not m["seed"])
    if reached:
        how.append(f"{reached} more file{'' if reached == 1 else 's'} that those refer to, up to "
                   f"two steps out. A reference is not proof the code ran.")
    if mode != "startup" and not flow:
        how.append("This was not a cold-start capture and no screens were named, so the "
                   "screens that were driven are not known. Pass --flow with their names.")
    return {"files": files, "how": how, "flow": flow or [], "unmatched_flow": unmatched,
            "manifests": manifest["manifests"],
            "capped": len(files) >= MAX_PATH_FILES}


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------

def _scan(index: dict, rel: str, check: dict) -> list[dict]:
    hits = []
    lines = _text(index, rel).split("\n")
    in_block_comment = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_block_comment:
            in_block_comment = "*/" not in stripped
            continue
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            continue
        if not stripped or stripped.startswith(("//", "*", "import ", "package ")):
            continue
        code = stripped.split("//", 1)[0] if "//" in stripped and "://" not in stripped else stripped
        m = check["pattern"].search(code)
        if not m:
            continue
        if check.get("lookahead"):
            # items( ... ) often puts key = on the next line
            following = " ".join(lines[i + 1:i + 1 + check["lookahead"]])
            if re.search(r"\bkey\s*=", following):
                continue
        hit = {"file": rel, "line": i + 1, "code": redact(stripped), "match": m.group(0)[:60]}
        before = " ".join(lines[max(0, i - 6):i + 1])
        if check["id"] in ("blocking_call", "system_call", "telephony_call",
                           "disk_or_parse_on_start") and OFF_MAIN.search(before):
            hit["note"] = ("A background dispatcher or thread is named just above, so this "
                           "may already be off the main thread.")
        hits.append(hit)
    return hits


def _gradle_files(repo: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in ("build", ".gradle", ".git", "node_modules", ".idea")
                   and not d.startswith(".")]
        for f in files:
            if f.endswith((".gradle", ".gradle.kts", ".toml")) or f == "baseline-prof.txt":
                out.append(os.path.join(root, f))
        if len(out) > 400:
            break
    return out


def _read(path: str) -> str:
    try:
        return "" if os.path.getsize(path) > 2_000_000 else open(path, encoding="utf-8",
                                                                 errors="replace").read()
    except OSError:
        return ""


def repo_checks(index: dict, kinds: set, mode: str, path: dict) -> list[dict]:
    """Checks about something that is missing, which no single line can show."""
    repo, items = index["root"], []
    gradle = _gradle_files(repo)
    rel = lambda p: os.path.relpath(p, repo)

    if kinds & {"jit_compile", "class_loading"} or (mode == "startup" and kinds & set(STARTUP_KINDS)):
        profiles = [rel(p) for p in gradle if os.path.basename(p) == "baseline-prof.txt"]
        plugin = [rel(p) for p in gradle if not p.endswith("baseline-prof.txt")
                  and re.search(r"androidx\.baselineprofile|baselineprofile|profileinstaller",
                                _read(p), re.I)]
        because = next((k for k in ("jit_compile", "class_loading") if k in kinds), None)
        if not profiles and not plugin:
            items.append({
                "check": "no_baseline_profile", "title": "No Baseline Profile in this repo",
                "file": None, "line": None, "code": "",
                "why": "No baseline-prof.txt, no androidx.baselineprofile plugin and no "
                       "profileinstaller dependency were found. Without a profile the app's "
                       "startup code is interpreted and JIT-compiled on every fresh install.",
                "fix": "Add a baseline profile module that records the startup path with "
                       "BaselineProfileRule, and the profileinstaller dependency in the app.",
                "plan": ["jit_compile", "class_loading"], "kind": because,
                "on_path": "whole repo"})
        else:
            where = (profiles or plugin)[0]
            items.append({
                "check": "baseline_profile_present", "title": "A Baseline Profile is set up",
                "file": where, "line": None, "code": "",
                "why": "The repo has a profile or the plugin that generates one"
                       + (f" ({len(profiles)} baseline-prof.txt)" if profiles else "")
                       + ". If the trace still shows heavy JIT or class loading, the profile "
                         "is stale, does not cover this path, or was not compiled on this install.",
                "fix": "Check adb shell dumpsys package dexopt for speed-profile, then "
                       "regenerate the profile over the path that was captured.",
                "plan": ["jit_compile", "class_loading"], "kind": because,
                "on_path": "whole repo"})

    if "no_ttfd" in kinds:
        found = None
        for files in index.get("all_files", {}).values():
            for f in files:
                m = re.search(r"reportFullyDrawn\s*\(|\bReportDrawn\w*\s*[({]", _text(index, f))
                if m and _android(f):
                    found = (f, _text(index, f).count("\n", 0, m.start()) + 1)
                    break
            if found:
                break
        launcher = next((r for r, meta in path["files"].items()
                         if meta["role"].startswith("launcher")), None)
        if found:
            items.append({
                "check": "report_drawn_not_reached", "title": "reportFullyDrawn is in the code but did not fire",
                "file": found[0], "line": found[1], "code": redact(
                    _text(index, found[0]).split("\n")[found[1] - 1]),
                "why": "The app calls it here, yet the trace has no time-to-full-display. The "
                       "call was not reached on this path, or its condition never became true.",
                "fix": "Make sure the first screen reaches this call once its content is drawn.",
                "plan": ["no_ttfd"], "kind": "no_ttfd", "on_path": "whole repo"})
        else:
            items.append({
                "check": "no_report_drawn", "title": "Nothing in the repo reports full draw",
                "file": launcher, "line": None, "code": "",
                "why": "No reportFullyDrawn() or ReportDrawn call was found anywhere in the "
                       "app's source.",
                "fix": "In the first screen's composable add ReportDrawnWhen { contentIsReady }, "
                       "or call reportFullyDrawn() from the launcher Activity once content is on screen.",
                "plan": ["no_ttfd"], "kind": "no_ttfd",
                "on_path": "launcher Activity" if launcher else "whole repo"})

    if kinds & {"no_trace_sections", "sections_not_ranked"}:
        for r, meta in path["files"].items():
            if not meta["seed"]:
                continue
            text = _text(index, r)
            entry_point = not meta["role"].startswith(("flow", "named in"))
            m = (re.search(r"override\s+fun\s+(?:onCreate|create)\s*\(", text) if entry_point
                 else re.search(r"^[ \t]*(?:\w+[ \t]+)*fun[ \t]+[A-Z]\w*\s*\(", text, re.M))
            if not m or sum(1 for it in items if it["check"] == "where_to_trace") >= 6:
                continue
            line = text.count("\n", 0, m.start()) + 1
            items.append({
                "check": "where_to_trace", "title": "A place for a trace section",
                "file": r, "line": line, "code": redact(text[m.start():].split("\n")[0]),
                "why": f"This is the {meta['role']}. A section here puts its cost in the next "
                       f"trace under a name of your own.",
                "fix": "Wrap the body: trace(\"Name\") { ... } from androidx.tracing.",
                "plan": ["no_trace_sections", "sections_not_ranked"],
                "kind": "no_trace_sections" if "no_trace_sections" in kinds else "sections_not_ranked",
                "on_path": meta["role"]})

    if mode == "startup" and kinds & (set(STARTUP_KINDS) | {"jit_compile"}):
        for p in gradle:
            if not p.endswith((".gradle", ".gradle.kts")):
                continue
            text = _read(p)
            if "applicationId" not in text and "com.android.application" not in text:
                continue
            m = re.search(r"\brelease\s*\{|getByName\(\s*\"release\"\s*\)\s*\{|named\(\s*\"release\"\s*\)\s*\{", text)
            if not m:
                continue
            depth, end = 0, m.end() - 1
            for end in range(m.end() - 1, min(len(text), m.end() + 4000)):
                depth += text[end] == "{"
                depth -= text[end] == "}"
                if depth == 0:
                    break
            block = text[m.start():end + 1]
            off = re.search(r"(?:isMinifyEnabled\s*=\s*false|minifyEnabled\s+false|minifyEnabled\s*=\s*false)", block)
            # Not set in the block is only a finding when no build file sets it
            # at all: a convention plugin or a Gradle property may turn it on.
            anywhere = any("inifyEnabled" in _read(g) for g in gradle
                           if g.endswith((".gradle", ".gradle.kts", ".kt")))
            if off or not anywhere:
                items.append({
                    "check": "release_not_minified", "title": "The release build is not shrunk by R8",
                    "file": rel(p), "line": text.count("\n", 0, m.start()) + 1,
                    "code": redact(off.group(0) if off else "no isMinifyEnabled in any build file"),
                    "why": "Without R8 the app ships and loads every class and method it "
                           "depends on, which makes cold start and class loading slower.",
                    "fix": "Set isMinifyEnabled = true (and isShrinkResources = true) for "
                           "release, then test the release build before shipping.",
                    "plan": ["startup_compute", "class_loading", "jit_compile"],
                    "kind": next((k for k in ("jit_compile", "class_loading") if k in kinds),
                                 next(iter(kinds & set(STARTUP_KINDS)), None)),
                    "on_path": "build configuration"})
            break
    return items


def audit(index: dict, findings: list, mode: str = "unknown", flow: list[str] | None = None,
          code_map: dict | None = None, binder_servers: list[str] | None = None,
          pkg: str = "") -> dict:
    """findings as triage.py makes them -> the audit. Never raises on odd input."""
    by_kind = {}
    for f in findings or []:
        by_kind.setdefault(f.get("kind") or "other", f)
    kinds = set(by_kind)
    path = build_path(index, mode, flow, code_map, pkg)
    files = path["files"]
    seeds = [r for r, m in files.items() if m["seed"]]
    order = sorted(files, key=lambda r: (files[r]["depth"], not files[r]["seed"]))
    items, clean = [], []

    for check in CHECKS:
        relevant = next((k for k in check["kinds"] if k in kinds), None)
        if check.get("server") and check["server"] not in (binder_servers or []):
            continue
        if not relevant and check.get("always_in") != mode:
            continue
        targets = seeds if check["scope"] == "seed" else order
        hits = []
        for rel in targets:
            for h in _scan(index, rel, check):
                h["on_path"] = files[rel]["role"]
                hits.append(h)
        widened = False
        if not hits and check.get("repo_fallback") and relevant:
            for group in index.get("all_files", {}).values():
                for rel in group:
                    if rel in files or not _android(rel) or not rel.endswith((".kt", ".java")):
                        continue
                    for h in _scan(index, rel, check):
                        h["on_path"] = "elsewhere in the repo, not known to be on the path"
                        hits.append(h)
            widened = True
        if not hits:
            clean.append(check["title"])
            continue
        # likely-on-main first, then nearest to an entry point
        hits.sort(key=lambda h: ("note" in h, files.get(h["file"], {"depth": 9})["depth"]))
        for h in hits[:MAX_PER_CHECK]:
            items.append({"check": check["id"], "title": check["title"], **h,
                          "why": check["why"], "fix": check["fix"], "plan": check["plan"],
                          "kind": relevant})
        if len(hits) > MAX_PER_CHECK:
            items[-1]["more"] = len(hits) - MAX_PER_CHECK
        if widened:
            items[-1]["widened"] = True

    # One line, one candidate: the telephony reading is the more specific one.
    specific = {(it["file"], it["line"]) for it in items if it["check"] == "telephony_call"}
    items = [it for it in items
             if not (it["check"] == "system_call" and (it["file"], it["line"]) in specific)]
    items += repo_checks(index, kinds, mode, path)
    for it in items:
        it["confidence"] = "candidate"
        it["because"] = by_kind[it["kind"]]["title"] if it.get("kind") in by_kind else None
    # items tied to a finding in this trace first
    items.sort(key=lambda it: it["because"] is None)
    tied = sum(1 for it in items if it["because"])
    if not files:
        summary = ("The code on the path could not be worked out, so nothing was audited. "
                   + " ".join(path["how"]))
    elif not items:
        summary = (f"Read {len(files)} files on the path and found nothing that matches what "
                   f"the trace measured.")
    else:
        summary = (f"Read {len(files)} file{'' if len(files) == 1 else 's'} on the path. "
                   f"{tied} candidate{'' if tied == 1 else 's'} in the code "
                   f"{'is' if tied == 1 else 'are'} tied to a finding in this trace"
                   + (f", and {len(items) - tied} more sit{'s' if len(items) - tied == 1 else ''} on the path."
                      if len(items) > tied else "."))
    return {
        "repo": index.get("repo", ""), "mode": mode, "summary": summary,
        "path": {"how": path["how"], "flow": path["flow"], "unmatched_flow": path["unmatched_flow"],
                 "files": [{"file": r, "role": files[r]["role"], "depth": files[r]["depth"]}
                           for r in order],
                 "capped": path["capped"]},
        "items": items, "clean": clean,
        "limits": "Pattern matches in source. They cannot see which thread a line runs on, or "
                  "whether it ran in this capture. Read each one before acting on it.",
    }


def to_markdown(a: dict, heading: str = "## Code on the profiled path") -> str:
    out = [heading, "", a["summary"], ""]
    out.append("How the path was worked out:")
    out.append("")
    out.extend(f"- {line}" for line in a["path"]["how"])
    out.append("")
    if not a["items"]:
        return "\n".join(out)
    out.append(a["limits"])
    out.append("")
    current = object()
    for it in a["items"]:
        group = it["because"]
        if group != current:
            current = group
            out.append(f"### {'Because: ' + group if group else 'Also on this path'}")
            out.append("")
            if not group:
                out.append("No finding in this trace points at these. They are listed because "
                           "they sit on the path.")
                out.append("")
        where = (f"`{it['file']}:{it['line']}`" if it.get("file") and it.get("line")
                 else f"`{it['file']}`" if it.get("file") else "the repo")
        out.append(f"- **{it['title']}**, {where} ({it['on_path']})")
        if it.get("code"):
            out.append(f"  - Code: `{it['code']}`")
        out.append(f"  - Why it matters: {it['why']}")
        out.append(f"  - Suggested fix: {it['fix']}")
        if it.get("note"):
            out.append(f"  - Note: {it['note']}")
        if it.get("more"):
            out.append(f"  - {it['more']} more matches of this kind are not listed.")
    out.append("")
    if a.get("clean"):
        out.append("Looked for and not found on the path: " + "; ".join(
            t[0].lower() + t[1:] for t in a["clean"]) + ".")
        out.append("")
    return "\n".join(out)


def plan_candidates(a: dict | None) -> dict:
    """{plan kind: [audit items]} for fix_plan.py to attach to its suggestions."""
    out: dict[str, list] = {}
    for it in (a or {}).get("items", []):
        for kind in it.get("plan", []):
            out.setdefault(kind, []).append(it)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="a directory written by triage.py")
    ap.add_argument("--app-repo", required=True, help="the app's source tree")
    ap.add_argument("--flow", default="", help="screens that were driven during the capture, "
                                                "comma-separated: Home,TripDetail")
    ap.add_argument("--json", action="store_true", help="print the audit as JSON")
    args = ap.parse_args()
    path = os.path.join(args.out_dir, "findings.json")
    if not os.path.exists(path):
        sys.exit(f"no findings.json in {args.out_dir}. Run triage.py first.")
    with open(path) as fh:
        data = json.load(fh)
    servers = []
    calls = os.path.join(args.out_dir, "csv", "30_binder_main_thread.csv")
    if os.path.exists(calls):
        with open(calls, newline="") as fh:
            servers = [r.get("server_process", "") for r in csv.DictReader(fh)]
    index = _load_code_map().scan_repo(args.app_repo)
    flow = [x.strip() for x in args.flow.split(",") if x.strip()]
    result = audit(index, data.get("findings") or [], data.get("mode", "unknown"), flow,
                   data.get("code_map"), servers)
    print(json.dumps(result, indent=2) if args.json else to_markdown(result, "# Code on the profiled path"))


if __name__ == "__main__":
    main()
