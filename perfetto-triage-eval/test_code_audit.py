#!/usr/bin/env python3
"""
Checks for code_audit.py: which code was on the profiled path, and what in it
matches what the trace measured.

The audit is a pattern match over source, so the tests are about its restraint
as much as its hits: only files on the path are read, a check runs only when a
finding asks for it, comments and other platforms are ignored, long strings do
not leave the repo, and every item stays a candidate.

The app in here is written by the test into a temporary folder. Nothing in this
file is a measurement.

    python3 test_code_audit.py
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


cm, ca, fp, vr = load("code_map"), load("code_audit"), load("fix_plan"), load("visual_report")

SRC = "app/src/main/java/com/example/shop/"
FILES = {
    "app/src/main/AndroidManifest.xml": """
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
  <!-- <application android:name=".OldApp"> -->
  <application android:name=".ShopApp" android:label="Shop">
    <activity android:name=".MainActivity" android:exported="true">
      <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
      </intent-filter>
    </activity>
    <activity android:name=".OtherActivity" />
    <provider android:name="androidx.startup.InitializationProvider" android:authorities="x">
      <meta-data android:name="com.example.shop.AnalyticsInitializer" android:value="androidx.startup" />
    </provider>
  </application>
</manifest>
""",
    SRC + "ShopApp.kt": """package com.example.shop

class ShopApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Firebase.initialize(this)
        // Sentry.init(this) is switched off
        val prefs = getSharedPreferences("settings", 0)
        val config = runBlocking { loadConfig("https://config.example.com/v1/a/long/secret/path") }
        initDependencies()
    }
}
""",
    SRC + "AnalyticsInitializer.kt": """package com.example.shop

class AnalyticsInitializer : Initializer<Unit> {
    override fun create(context: Context) {
        Mixpanel.getInstance(context, "0123456789abcdef0123456789abcdef")
    }
}
""",
    SRC + "MainActivity.kt": """package com.example.shop

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val tm = getSystemService(TelephonyManager::class.java)
        val country = tm.networkCountryIso
        setContent { ShopRoot() }
    }
}
""",
    SRC + "OtherActivity.kt": """package com.example.shop
class OtherActivity : ComponentActivity() {
    fun slow() { Thread.sleep(500) }
}
""",
    SRC + "ui/Root.kt": """package com.example.shop.ui

@Composable
fun ShopRoot() {
    HomeScreen()
}
""",
    SRC + "ui/HomeScreen.kt": """package com.example.shop.ui

@Composable
fun HomeScreen() {
    val pulse = rememberInfiniteTransition(label = "pulse")
    LazyColumn {
        items(products) { ProductRow(it) }
        items(
            offers,
            key = { it.id },
        ) { OfferRow(it) }
        items(banners, key = { it.id }) { BannerRow(it) }
    }
}
""",
    SRC + "ui/CheckoutScreen.kt": """package com.example.shop.ui

@Composable
fun CheckoutScreen() {
    val spin = animateFloat(animationSpec = infiniteRepeatable(tween(900)))
}
""",
    SRC + "di/Deps.kt": """package com.example.shop.di

fun initDependencies() {
    scope.launch(Dispatchers.IO) {
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
    }
}
""",
    "app/src/iosMain/kotlin/IosHome.kt": """package com.example.shop
fun HomeScreenIos() { rememberInfiniteTransition() }
""",
    "benchmark/src/main/java/com/example/shop/StartupBenchmark.kt": """package com.example.shop
class StartupBenchmark { fun run() { Thread.sleep(1000); ShopRoot() } }
""",
    "app/build.gradle.kts": """plugins { id("com.android.application") }
android {
    defaultConfig { applicationId = "com.example.shop" }
    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}
""",
}


def repo(extra=None, drop=()):
    root = tempfile.mkdtemp(prefix="audit-repo-")
    for rel, text in dict(FILES, **(extra or {})).items():
        if rel in drop:
            continue
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    return root


def finding(kind, title=None, severity="medium"):
    return {"kind": kind, "severity": severity, "area": "x",
            "title": title or f"finding {kind}", "detail": "d", "action": "a"}


ALL = [finding(k) for k in ("startup_waiting", "main_binder", "no_ttfd", "no_trace_sections",
                            "jit_compile", "handoff_compose-expert")]


def run(findings=None, mode="startup", flow=None, servers=("system_server",), root=None, pkg=""):
    index = cm.scan_repo(root or repo())
    return ca.audit(index, ALL if findings is None else findings, mode, flow, None,
                    list(servers), pkg)


def checks(a, name=None):
    return [it for it in a["items"] if name is None or it["check"] == name]


def base(it):
    return os.path.basename(it["file"] or "")


# ---- the path ------------------------------------------------------------

def test_entry_points_come_from_the_manifest():
    a = run()
    roles = {os.path.basename(f["file"]): f["role"] for f in a["path"]["files"]}
    assert roles["ShopApp.kt"] == "Application class"
    assert roles["MainActivity.kt"] == "launcher Activity"
    assert roles["AnalyticsInitializer.kt"] == "startup initializer"
    assert "OtherActivity.kt" not in roles, "an Activity that is not the launcher is not on the path"
    assert "OldApp" not in str(a["path"]), "a commented-out manifest entry must be ignored"


def test_path_follows_references_two_steps_and_says_how_far():
    a = run()
    by = {os.path.basename(f["file"]): f for f in a["path"]["files"]}
    assert by["Root.kt"]["depth"] == 1 and "MainActivity.kt" in by["Root.kt"]["role"]
    assert by["HomeScreen.kt"]["depth"] == 2
    assert by["Deps.kt"]["depth"] == 1, "a lower-case top-level function is followed too"
    assert "CheckoutScreen.kt" not in by, "a screen nothing on the path refers to is not on it"


def test_other_platforms_and_benchmark_modules_are_never_read():
    a = run()
    names = [os.path.basename(f["file"]) for f in a["path"]["files"]]
    assert "IosHome.kt" not in names and "StartupBenchmark.kt" not in names
    assert not [it for it in a["items"] if base(it) in ("IosHome.kt", "StartupBenchmark.kt")]


def test_flow_names_add_screens_and_report_the_ones_not_found():
    a = run(mode="interactive", flow=["Checkout", "Wishlist"])
    by = {os.path.basename(f["file"]): f for f in a["path"]["files"]}
    assert by["CheckoutScreen.kt"]["role"] == "flow: Checkout"
    assert a["path"]["unmatched_flow"] == ["Wishlist"]
    assert any("Wishlist" in h for h in a["path"]["how"])


def test_interactive_capture_with_no_flow_says_the_screens_are_unknown():
    a = run(mode="interactive")
    assert any("--flow" in h for h in a["path"]["how"])


def test_with_two_apps_in_a_repo_the_traced_package_picks_the_entry_points():
    extra = {
        "catalog/src/main/AndroidManifest.xml": FILES["app/src/main/AndroidManifest.xml"]
        .replace(".ShopApp", ".CatalogApp").replace(".MainActivity", ".CatalogActivity"),
        "catalog/src/main/java/com/example/catalog/CatalogActivity.kt":
            "package com.example.catalog\nclass CatalogActivity { fun onCreate() { Thread.sleep(9) } }\n",
    }
    a = run(root=repo(extra), pkg="com.example.shop.staging")
    names = [os.path.basename(f["file"]) for f in a["path"]["files"]]
    assert "MainActivity.kt" in names and "CatalogActivity.kt" not in names


# ---- the audit -----------------------------------------------------------

def test_startup_set_up_is_listed_with_file_and_line():
    hits = checks(run(), "sdk_init_on_start")
    assert {(base(h), h["line"]) for h in hits} == {("ShopApp.kt", 6), ("AnalyticsInitializer.kt", 5)}
    assert all(h["confidence"] == "candidate" for h in hits)


def test_a_commented_out_line_is_not_a_hit():
    assert not [h for h in run()["items"] if "Sentry" in (h.get("code") or "")]


def test_long_strings_are_blanked_before_a_line_leaves_the_repo():
    a = run()
    text = str(a)
    assert "secret" not in text and "0123456789abcdef" not in text
    assert any('"…"' in (h.get("code") or "") for h in a["items"])
    assert any('"settings"' in (h.get("code") or "") for h in a["items"]), "short strings stay readable"


def test_a_line_under_a_background_dispatcher_is_kept_but_flagged():
    hit = next(h for h in checks(run(), "system_call") if base(h) == "Deps.kt")
    assert "off the main thread" in hit["note"]
    main = next(h for h in checks(run()) if base(h) == "MainActivity.kt" and h["line"] == 6)
    assert "note" not in main


def test_telephony_check_needs_the_trace_to_show_calls_to_the_phone_process():
    assert not checks(run(servers=("system_server",)), "telephony_call")
    hits = checks(run(servers=("system_server", "com.android.phone")), "telephony_call")
    assert {h["line"] for h in hits} == {6, 7}
    assert not [h for h in checks(run(servers=("com.android.phone",)), "system_call")
                if base(h) == "MainActivity.kt" and h["line"] == 6], "one line, one candidate"


def test_a_check_only_runs_when_a_finding_asks_for_it():
    a = run(findings=[finding("no_ttfd")], mode="interactive")
    assert {it["check"] for it in a["items"]} == {"no_report_drawn"}
    quiet = run(findings=[], mode="interactive")
    assert quiet["items"] == []


def test_cold_start_lists_what_is_set_up_even_without_a_startup_finding():
    a = run(findings=[], mode="startup")
    assert {it["check"] for it in a["items"]} == {"sdk_init_on_start"}
    assert all(it["because"] is None for it in a["items"])


def test_endless_animation_on_the_path_wins_over_the_rest_of_the_repo():
    hits = checks(run(), "endless_animation")
    assert [base(h) for h in hits] == ["HomeScreen.kt"]


def test_endless_animation_falls_back_to_the_repo_and_says_so():
    root = repo(drop=(SRC + "ui/HomeScreen.kt",))
    hits = checks(run(findings=[finding("handoff_compose-expert")], mode="interactive", root=root),
                  "endless_animation")
    assert [base(h) for h in hits] == ["CheckoutScreen.kt"]
    assert "not known to be on the path" in hits[0]["on_path"]


def test_lazy_list_key_on_the_same_or_a_following_line_is_not_flagged():
    hits = checks(run(), "lazy_list_without_key")
    assert [(base(h), h["line"]) for h in hits] == [("HomeScreen.kt", 7)]


def test_missing_baseline_profile_is_reported_only_when_it_is_missing():
    assert checks(run(), "no_baseline_profile")
    root = repo({"app/src/main/baseline-prof.txt": "Lcom/example/shop/ShopApp;\n"})
    a = run(root=root)
    assert not checks(a, "no_baseline_profile") and checks(a, "baseline_profile_present")


def test_report_fully_drawn_present_but_not_reached_is_a_different_item():
    assert checks(run(), "no_report_drawn")[0]["on_path"] == "launcher Activity"
    root = repo({SRC + "ui/Root.kt": FILES[SRC + "ui/Root.kt"].replace(
        "    HomeScreen()", "    ReportDrawnWhen { ready }\n    HomeScreen()")})
    a = run(root=root)
    assert not checks(a, "no_report_drawn")
    hit = checks(a, "report_drawn_not_reached")[0]
    assert base(hit) == "Root.kt" and hit["line"] == 5


def test_trace_section_suggestions_point_at_entry_points():
    hits = checks(run(), "where_to_trace")
    assert {(base(h), h["line"]) for h in hits} == {
        ("ShopApp.kt", 4), ("AnalyticsInitializer.kt", 4), ("MainActivity.kt", 4)}


def test_r8_off_is_flagged_only_on_an_explicit_false_or_no_setting_at_all():
    assert checks(run(), "release_not_minified")[0]["line"] == 5
    on = repo({"app/build.gradle.kts": FILES["app/build.gradle.kts"].replace("= false", "= true")})
    assert not checks(run(root=on), "release_not_minified")
    elsewhere = repo({
        "app/build.gradle.kts": FILES["app/build.gradle.kts"].replace("isMinifyEnabled = false", ""),
        "build-logic/convention/build.gradle.kts": "// isMinifyEnabled is set by the convention plugin\n"})
    assert not checks(run(root=elsewhere), "release_not_minified"), \
        "a convention plugin may set it, so an empty release block is not proof"


def test_items_tied_to_a_finding_come_first_and_name_it():
    a = run(findings=[finding("main_binder", "Main-thread binder: calls to system_server cost 79 ms")])
    tied = [it for it in a["items"] if it["because"]]
    assert tied and a["items"][0]["because"].startswith("Main-thread binder")
    assert a["items"].index(tied[-1]) < len(tied)


def test_a_repo_with_no_manifest_says_so_instead_of_guessing():
    root = tempfile.mkdtemp(prefix="audit-empty-")
    with open(os.path.join(root, "main.dart"), "w") as fh:
        fh.write("void main() {}\n")
    a = run(root=root)
    assert a["path"]["files"] == []
    assert "could not be worked out" in a["summary"]
    assert not [it for it in a["items"] if it.get("line")]


def test_audit_survives_a_missing_repo():
    a = ca.audit(cm.scan_repo("/no/such/place"), ALL, "startup")
    assert a["path"]["files"] == [] and isinstance(a["summary"], str)


# ---- into the fix plan and the reports -----------------------------------

def test_fix_plan_carries_candidates_under_the_matching_suggestion():
    f = [finding("main_binder", "Main-thread binder: calls to system_server cost 79 ms")]
    a = run(findings=f)
    plan = fp.build(f, {"verdict": "trusted"}, None, None, a)
    item = next(i for i in plan["items"] if i["kind"] == "main_binder")
    assert [os.path.basename(c["file"]) for c in item["candidates"]][0] == "MainActivity.kt", \
        "the line with no background dispatcher above it comes first"
    md = fp.to_markdown(plan)
    assert "MainActivity.kt:6" in md and "candidates: read each before acting" in md
    assert "does not name the interface" in md


def test_fix_plan_without_an_audit_is_unchanged():
    f = [finding("main_binder", "Main-thread binder: IFoo costs 79 ms")]
    plan = fp.build(f, {"verdict": "trusted"})
    assert "candidates" not in plan["items"][0] and plan["items"][0]["where"] == ["IFoo"]


def test_markdown_and_html_show_the_path_and_escape_the_code():
    root = repo({SRC + "ShopApp.kt": FILES[SRC + "ShopApp.kt"].replace(
        "Firebase.initialize(this)", "Firebase.initialize(this) <script>alert(1)</script>")})
    f = [finding("startup_waiting", "Startup is waiting-bound: 70% waiting")]
    a = run(findings=f, root=root)
    md = ca.to_markdown(a)
    assert "How the path was worked out" in md and "ShopApp.kt" in md
    plan = fp.build(f, {"verdict": "trusted"}, None, None, a)
    html = vr.fix_section(plan, a)
    assert "Which code was read, and why" in html and "ShopApp.kt:6" in html
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {str(e)[:400]}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed (a made-up app, not a capture)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
