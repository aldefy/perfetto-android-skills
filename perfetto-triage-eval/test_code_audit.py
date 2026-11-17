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


# ---- what two real runs got wrong ----------------------------------------

KMP = {
    "composeApp/src/androidMain/AndroidManifest.xml": FILES["app/src/main/AndroidManifest.xml"]
    .replace(".ShopApp", ".AndroidApp").replace(".MainActivity", ".AppActivity"),
    "composeApp/src/androidMain/kotlin/com/example/shop/App.android.kt": """package com.example.shop

class AndroidApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (BuildConfig.DEBUG) DebugLog.init(filesDir)
        Sentry.init(this)
    }
}

class AppActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { App() }
    }
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/App.kt": """package com.example.shop

@Composable
fun App() {
    AppNavigation()
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/AppNavigation.kt": """package com.example.shop

@Composable
fun AppNavigation() {
    if (loading) CircularProgressIndicator()
    LinearProgressIndicator(progress = { done })
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/AppViewModel.kt":
        "package com.example.shop\nclass AppViewModel\n",
    "composeApp/src/commonMain/kotlin/com/example/shop/setup/GuidedSetupScreen.kt":
        "package com.example.shop.setup\n@Composable\nfun GuidedSetupScreen() {}\n",
    "composeApp/src/commonMain/kotlin/com/example/shop/setup/PairingDeviceScreen.kt":
        "package com.example.shop.setup\n@Composable\nfun PairingDeviceScreen() {}\n",
    "composeApp/src/commonMain/kotlin/com/example/shop/maps/MapStage.kt":
        "package com.example.shop.maps\n@Composable\nfun MapStage() { rememberInfiniteTransition() }\n",
    "composeApp/src/androidDebug/kotlin/com/example/shop/DebugMenu.kt":
        "package com.example.shop\nfun debugMenu() { Thread.sleep(50); App() }\n",
}


def kmp(extra=None):
    root = tempfile.mkdtemp(prefix="audit-kmp-")
    for rel, text in dict(KMP, **(extra or {})).items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    return root


def test_entry_points_are_found_where_the_class_is_defined_not_by_file_name():
    """A Kotlin Multiplatform app kept its Application and Activity classes
    together in App.android.kt. Looking for AppActivity.kt found nothing."""
    a = run(root=kmp())
    roles = {os.path.basename(f["file"]): f["role"] for f in a["path"]["files"]}
    assert roles["App.android.kt"] == "Application class, launcher Activity", roles
    assert "App.kt" in roles and "AppNavigation.kt" in roles, "and the path is followed from there"
    assert any("Entry points from AndroidManifest.xml" in h for h in a["path"]["how"])


def test_debug_source_sets_are_not_read_and_debug_guarded_lines_are_marked():
    a = run(root=kmp())
    assert "DebugMenu.kt" not in [os.path.basename(f["file"]) for f in a["path"]["files"]]
    hits = checks(a, "sdk_init_on_start")
    assert [h["line"] for h in hits] == [7], "the debug-only line is not a startup cost in release"
    assert [(os.path.basename(h["file"]), h["line"]) for h in a["skipped_debug"]] == [("App.android.kt", 6)]
    assert "Left out because they sit behind a debug check" in ca.to_markdown(a)
    kept = ca.audit(cm.scan_repo(kmp()), ALL, "startup", debug_build=True)
    assert [h["line"] for h in checks(kept, "sdk_init_on_start")] == [7, 6] or \
        sorted(h["line"] for h in checks(kept, "sdk_init_on_start")) == [6, 7], \
        "on a debuggable capture that code did run, so it stays"


def test_debug_only_code_is_recognised_in_the_shapes_it_is_written_in():
    """Two lines a real run flagged as startup cost both sat behind a debug check."""
    text = """class App {
    fun onCreate() {
        if (BuildConfig.DEBUG) {
            Timber.plant(Tree())
            val manager = getSystemService(NotificationManager::class.java)
        } else {
            Sentry.init(this)
        }
        if (BuildConfig.DEBUG) LogStore.init(dir)
        Analytics.init(this)
        if (!BuildConfig.DEBUG) {
            Crash.init(this)
        }
        setUpInspector()
    }

    private fun setUpInspector() {
        if (!BuildConfig.DEBUG) return
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    private fun debugMenu() {
        Menu.init(this)
    }

    fun other() {
        if (BuildConfig.DEBUG) {
            debugMenu()
        }
        Maps.init(this)
    }
}
"""
    marked = {n + 1 for n in ca.debug_lines(text)}
    for line, why in ((4, "inside a debug block"), (5, "inside a debug block"),
                      (9, "a single-line debug if"), (19, "after if (!DEBUG) return"),
                      (20, "after if (!DEBUG) return"),
                      (24, "a private function only called from a debug block")):
        assert line in marked, f"line {line} is debug-only: {why}"
    for line, why in ((7, "the else branch ships"), (10, "plain code"),
                      (12, "inside if (!DEBUG): release only"), (14, "a plain call"),
                      (31, "after the debug block has closed")):
        assert line not in marked, f"line {line} is not debug-only: {why}"


def test_an_exact_screen_name_wins_over_names_that_only_contain_it():
    """--flow App matched 13 files: everything with App in its name."""
    index = cm.scan_repo(kmp())
    names = sorted(os.path.basename(f) for f in ca.match_name(index, "app"))
    assert names == ["App.android.kt", "App.kt", "AppViewModel.kt"], \
        f"App, and App plus a screen suffix, but not AppNavigation: {names}"
    assert [os.path.basename(f) for f in ca.match_name(index, "appnav")] == ["AppNavigation.kt"]
    assert [os.path.basename(f) for f in ca.match_name(index, "stage")] == ["MapStage.kt"]


def test_a_described_flow_is_split_into_the_screens_it_names():
    """Free text matched only its first words."""
    index = cm.scan_repo(kmp())
    hits, missed = ca.match_flow(index, "then guided setup to Pairing Device")
    assert sorted(os.path.basename(f) for f, _ in hits) == [
        "GuidedSetupScreen.kt", "PairingDeviceScreen.kt"], hits
    assert missed == []
    hits, missed = ca.match_flow(index, "open the Wishlist then Map Stage")
    assert [os.path.basename(f) for f, _ in hits] == ["MapStage.kt"] and missed == ["Wishlist"]
    assert ca.match_flow(index, "Wishlist") == ([], ["Wishlist"])


def test_code_the_trace_names_joins_the_path():
    """Screens that ran were not audited, because nothing on the path referred
    to them. A slice that names an app class is direct evidence it ran."""
    names = ["performCreate:com.example.shop.AppActivity",
             "com.example.shop.maps.MapStage (MapStage.kt:3)",
             "Lcom/other/lib/Thing;", "Choreographer#doFrame"]
    index = cm.scan_repo(kmp())
    a = ca.audit(index, [finding("always_animating")], "interactive", None, None, [], "", names)
    roles = {os.path.basename(f["file"]): f["role"] for f in a["path"]["files"]}
    assert roles["MapStage.kt"] == "named in the trace", roles
    assert "named in the trace" in roles["App.android.kt"]
    assert not any("--flow" in h for h in a["path"]["how"]), "the trace already said what ran"
    hit = checks(a, "endless_animation")
    assert [base(h) for h in hit] == ["MapStage.kt"] and hit[0]["on_path"] == "named in the trace"
    quiet = ca.audit(index, [finding("always_animating")], "interactive", None, None, [], "", [])
    assert any("does not say which screens ran" in h for h in quiet["path"]["how"])


NAV = {
    "composeApp/src/commonMain/kotlin/com/example/shop/AppNavigation.kt": """package com.example.shop

@Composable
fun AppNavigation() {
    NavHost(nav, startDestination = Routes.Home) {
        composable(Routes.Home) { HomeScreen() }
        composable(Routes.Search) { SearchScreen() }
    }
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/home/HomeScreen.kt": """package com.example.shop.home

@Composable
fun HomeScreen() {
    HomeHeader()
    WeatherBadge()
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/home/HomeHeader.kt": """package com.example.shop.home

@Composable
fun HomeHeader() {
    val t = rememberInfiniteTransition(label = "sky")
    val drift by t.animateFloat(0f, 1f, infiniteRepeatable(tween(4000)))
    Canvas(Modifier.fillMaxWidth()) {
        drawCircle(Color.White, radius = 4f, center = Offset(size.width * drift, 20f))
    }
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/home/WeatherBadge.kt": """package com.example.shop.home

@Composable
fun WeatherBadge() {
    val pulse = rememberInfiniteTransition(label = "pulse")
    val scale by pulse.animateFloat(1f, 1.1f, infiniteRepeatable(tween(900)))
    Text("Rain", fontSize = (14 * scale).sp)
}
""",
    "composeApp/src/commonMain/kotlin/com/example/shop/search/SearchScreen.kt":
        "package com.example.shop.search\n@Composable\nfun SearchScreen() { val vm = SearchViewModel() }\n",
    "composeApp/src/commonMain/kotlin/com/example/shop/search/SearchViewModel.kt": """package com.example.shop.search

class SearchViewModel {
    fun retryWhenOnline() = scope.launch {
        while (true) {
            if (online()) break
            delay(2000)
        }
    }
}
""",
    "other/src/commonMain/kotlin/dev/another/app/Shimmer.kt":
        "package dev.another.app\n@Composable\nfun Shimmer() { rememberInfiniteTransition() }\n",
}


def test_the_first_screen_is_read_from_the_navigation_set_up():
    """The always-on animation was not located: the entry files of a large app
    refer to more files than fit, and the first screen was never reached."""
    a = run(findings=[finding("always_animating")], root=kmp(NAV))
    roles = {os.path.basename(f["file"]): f["role"] for f in a["path"]["files"]}
    assert roles["HomeScreen.kt"] == "first screen: Home", roles
    assert roles["HomeHeader.kt"].startswith("in the first screen's tree, 1 step from HomeScreen.kt")
    assert any(h.startswith("First screen: Home, read from the navigation set-up in AppNavigation.kt")
               for h in a["path"]["how"]), a["path"]["how"]
    hits = checks(a, "endless_animation")
    assert {base(h) for h in hits} == {"HomeHeader.kt", "WeatherBadge.kt"}, \
        "candidates come from the first screen, not from another app in the same repo"
    assert all("first screen" in h["on_path"] for h in hits)


def test_a_background_loop_with_a_delay_is_not_an_animation():
    """`while (true) { ...; delay() }` in a ViewModel was flagged as redrawing every frame."""
    a = run(findings=[finding("always_animating")], root=kmp(NAV))
    assert not [it for it in a["items"] if base(it) == "SearchViewModel.kt"], \
        [it["check"] for it in a["items"] if base(it) == "SearchViewModel.kt"]


def test_when_frames_draw_without_recomposing_the_draw_block_candidate_comes_first():
    f = dict(finding("always_animating"), animation_phase="draw")
    hits = checks(run(findings=[f], root=kmp(NAV)), "endless_animation")
    assert base(hits[0]) == "HomeHeader.kt" and "draw block" in hits[0]["note"], hits[0]
    assert "note" not in [h for h in hits if base(h) == "WeatherBadge.kt"][0]
    plain = checks(run(findings=[finding("always_animating")], root=kmp(NAV)), "endless_animation")
    assert not any("note" in h for h in plain), "no clue from the trace, no claim"


def test_the_repo_wide_fallback_prefers_the_traced_apps_own_package():
    extra = {k: v for k, v in NAV.items() if "/home/" not in k and "AppNavigation" not in k}
    extra["composeApp/src/commonMain/kotlin/com/example/shop/promo/Banner.kt"] = \
        "package com.example.shop.promo\n@Composable\nfun Banner() { rememberInfiniteTransition() }\n"
    hits = checks(run(findings=[finding("always_animating")], root=kmp(extra)), "endless_animation")
    assert base(hits[0]) == "Banner.kt", [base(h) for h in hits]
    assert all("not known to be on the path" in h["on_path"] for h in hits)


def test_indeterminate_progress_is_a_candidate_and_a_determinate_one_is_not():
    a = run(findings=[finding("always_animating")], root=kmp())
    hits = checks(a, "redraws_while_shown")
    assert [(base(h), h["line"]) for h in hits] == [("AppNavigation.kt", 5)], hits
    plan = fp.build([finding("always_animating")], {"verdict": "trusted"}, None, None, a)
    cands = plan["items"][0]["candidates"]
    assert cands[0]["file"].endswith("AppNavigation.kt"), "code on the path comes first"
    assert "not known to be on the path" in cands[-1]["on_path"]


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
