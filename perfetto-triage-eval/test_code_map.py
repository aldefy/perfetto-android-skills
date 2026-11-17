#!/usr/bin/env python3
"""
Checks for code_map.py: is a hot slice the app's own code?

The cost of a wrong answer is lopsided. Calling a framework slice "yours"
sends someone to read the wrong file, so every positive needs evidence, and
these tests spend most of their effort on the names that must NOT match.

    python3 test_code_map.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = load("code_map")
vr = load("visual_report")
PKG = "com.example.app"


def tree(files: dict) -> str:
    root = tempfile.mkdtemp(prefix="cm-test-")
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    return root


APP = {
    "app/src/main/java/com/example/app/FeedRepo.kt":
        "package com.example.app\n"
        "class FeedRepo {\n"
        "    suspend fun load(page: Int) = trace(\"Feed.load\") { api.get(page) }\n"
        "    fun stage(name: String) = trace(\"v3.$name\") { }\n"
        "    fun decode() { Trace.beginSection(\"decodeImages\"); Trace.endSection() }\n"
        "}\n",
    "app/src/main/java/com/example/app/MainActivity.kt":
        "package com.example.app\nclass MainActivity : ComponentActivity() {\n"
        "    override fun onCreate(b: Bundle?) { val label = \"animation\" }\n}\n",
    "app/src/main/java/com/example/app/HomeScreen.kt":
        "package com.example.app\n@Composable fun HomeScreen() { }\n",
    # must be ignored: tests and build output are not the shipped app
    "app/src/test/java/com/example/app/FakeTest.kt": "fun t() = trace(\"waitForever\") { }\n",
    "app/build/generated/Gen.kt": "fun g() = trace(\"traversal\") { }\n",
}


def owner(name, index, pkg=PKG):
    return cm.classify(name, index, pkg)


def test_trace_sections_are_found_with_file_and_line():
    index = cm.scan_repo(tree(APP))
    r = owner("Feed.load", index)
    assert r == {"owner": "app", "how": "trace section",
                 "evidence": "app/src/main/java/com/example/app/FeedRepo.kt:3"}, r
    assert owner("decodeImages", index)["how"] == "trace section"
    assert owner("decodeImages 12", index)["owner"] == "app", "trailing args are ignored"
    assert len(index["sections"]) == 3, index["sections"]


def test_template_sections_match_their_runtime_names():
    index = cm.scan_repo(tree(APP))
    assert owner("v3.orb", index)["evidence"].endswith("FeedRepo.kt:4")
    assert owner("v3.keyboard hiding", index)["owner"] == "app"
    assert owner("v3.", index)["owner"] == "other", "the variable part must not be empty"
    assert owner("v4.orb", index)["owner"] == "other"


def test_file_reference_and_class_method():
    index = cm.scan_repo(tree(APP))
    r = owner("com.example.app.HomeScreen (HomeScreen.kt:42)", index)
    assert r["how"] == "source file" and r["evidence"].endswith("HomeScreen.kt:42"), r
    r = owner("FeedRepo.load", index)
    assert r["how"] == "class.method" and r["evidence"].endswith("FeedRepo.kt:3"), r
    assert owner("FeedRepo.missing", index)["owner"] == "other", "method must exist in the file"
    assert owner("Other (Other.kt:7)", index)["owner"] == "other", "file must exist in the repo"


def test_app_class_inside_a_framework_slice():
    index = cm.scan_repo(tree(APP))
    r = owner("performCreate:com.example.app.MainActivity", index)
    assert r["how"] == "app class" and r["evidence"].endswith("MainActivity.kt"), r
    assert owner("performCreate:com.other.app.MainActivity", index)["owner"] == "other"
    assert owner("performCreate:com.example.app.MainActivity", index, pkg="*")["owner"] == "other"


def test_framework_names_are_never_claimed():
    index = cm.scan_repo(tree(APP))
    for name in ("traversal", "waitForever", "postAndWait", "Choreographer#doFrame 26045026",
                 "draw-VRI[MainActivity]", "Compose:recompose", "Recomposer:animation",
                 "animation",                       # a plain string in the app is not a section
                 "EmojiCompat.MetadataRepo.create",  # a library class the app does not define
                 "AIDL::java::android.app.IActivityManager::attachApplication::server",
                 "Drawing  0.00  0.00 1080.00 2424.00", "binder transaction", ""):
        assert owner(name, index)["owner"] == "other", name


def test_no_repo_and_empty_results_are_fine():
    index = cm.scan_repo("/nonexistent/path")
    assert index["scanned"] == 0 and owner("Feed.load", index)["owner"] == "other"
    out = cm.build({}, "/nonexistent/path", PKG)
    assert out["checked"] == 0 and cm.findings(out) == []


def results(names):
    return {"40_slice_hotspots": (["name", "thread_name", "n", "total_ms", "self_ms"],
                                  [[n, "main", "1", str(ms), str(ms)] for n, ms in names]),
            "13_startup_top_slices": (["startup_id", "slice_name", "n", "total_ms", "max_ms"],
                                      [["1", n, "1", str(ms), str(ms)] for n, ms in names[:2]])}


def test_finding_when_nothing_is_the_apps():
    repo = tree({"app/src/main/java/com/example/app/Plain.kt": "class Plain\n"})
    out = cm.build(results([("traversal", 900), ("waitForever", 400)]), repo, PKG)
    assert out["app_owned"] == 0 and out["sections_declared"] == 0 and out["checked"] == 2, out
    f = cm.findings(out)
    assert len(f) == 1 and f[0]["severity"] == "medium" and f[0]["area"] == "code", f
    assert f[0]["title"] == "The app has no trace sections of its own"
    assert "androidx.tracing" in f[0]["action"] and f[0]["directional_only"] is False
    assert f[0]["source"] == ["13_startup_top_slices", "40_slice_hotspots"], f[0]["source"]


def test_finding_when_sections_exist_but_do_not_rank():
    out = cm.build(results([("traversal", 900)]), tree(APP), PKG)
    f = cm.findings(out)
    assert f[0]["title"] == "None of the app's trace sections shows up in the rankings", f
    assert "declares 3 trace sections" in f[0]["detail"], f[0]["detail"]


def test_finding_lists_the_apps_own_slices():
    out = cm.build(results([("traversal", 900), ("Feed.load", 300), ("v3.orb", 120)]),
                   tree(APP), PKG)
    assert out["app_owned"] == 2 and out["named_after_app_code"] == 2, out
    assert [s["name"] for s in out["slices"]] == ["traversal", "Feed.load", "v3.orb"]
    f = cm.findings(out)
    assert f[0]["severity"] == "info", f
    assert f[0]["title"] == "2 of 3 hot slices are named after the app's own code"
    assert "Feed.load (300 ms, app/src/main/java/com/example/app/FeedRepo.kt:3)" in f[0]["detail"]


def test_lifecycle_only_match_still_asks_for_trace_sections():
    repo = tree({"app/src/main/java/com/example/app/MainActivity.kt": "class MainActivity\n"})
    out = cm.build(results([("performCreate:com.example.app.MainActivity", 40),
                            ("traversal", 900)]), repo, PKG)
    f = cm.findings(out)
    assert f[0]["title"] == "The app has no trace sections of its own", f
    assert "framework lifecycle slices: performCreate:com.example.app.MainActivity" in f[0]["detail"]


def test_report_marks_the_apps_slices_and_escapes_them():
    import json
    out_dir = tempfile.mkdtemp(prefix="cm-out-")
    os.makedirs(os.path.join(out_dir, "csv"))
    with open(os.path.join(out_dir, "csv", "40_slice_hotspots.csv"), "w") as fh:
        fh.write("name,thread_name,n,total_ms,self_ms\ntraversal,main,1,900,900\n"
                 "Feed.load,main,1,300,300\n")
    code_map = cm.build(cm.load_results(out_dir), tree(APP), PKG)
    code_map["slices"][1]["evidence"] = "<b>x</b>.kt:3"
    json.dump({"trace": "t.pftrace", "pkg": PKG, "findings": cm.findings(code_map),
               "code_map": code_map}, open(os.path.join(out_dir, "findings.json"), "w"))
    page = vr.render(out_dir)
    assert page.count('<span class="own">your code</span>') == 2, "one on the bar, one in the list"
    assert vr.esc("1 of these 2 names leads into the app's own code and is marked") in page
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;.kt:3" in page
    assert '<span class="tag">inference</span><b>What it means.</b>' in page


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {str(e)[:300]}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
