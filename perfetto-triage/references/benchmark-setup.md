# Adding Macrobenchmark + Baseline Profiles to a brownfield app

Verified against the current androidx APIs. Where the official docs are wrong,
it is called out — there are two live typos in Google's own samples that will
not compile.

## Versions

| Artifact | Stable | Notes |
|---|---|---|
| `androidx.benchmark:benchmark-macro-junit4` | `1.4.1` | `1.5.0-beta01` if you are on AGP 9.x |
| `androidx.baselineprofile` Gradle plugin | `1.4.1` | same train as the library |
| `androidx.benchmark` Gradle plugin | `1.4.1` | |
| `androidx.profileinstaller:profileinstaller` | `1.4.1` | 1.3+ is the hard minimum |
| `androidx.test.uiautomator:uiautomator` | `2.4.0` | needed for 1.5.x idioms |

**AGP 9.x caution.** AGP 9.0 flipped `android.newDsl` to `true` by default, and
the benchmark 1.5.0-alpha01 notes say the plugin "no longer requires
`newDsl=false` in AGP 9.0" — implying 1.4.1 *does*. On AGP 9.x, either put
`android.newDsl=false` in `gradle.properties` or move to `1.5.0-beta01`. The
`newDsl` escape hatch is removed in AGP 10, so `1.5.0-beta01` is the more
durable choice.

## Module layout

Use a separate `com.android.test` module. Do **not** use `testBuildType` — it is
absent from all current docs and both official samples, and is only relevant to
the legacy in-app-module layout.

```
app/                 the app under test
macrobenchmark/      com.android.test module, targets :app
```

### `macrobenchmark/build.gradle.kts`

```kotlin
plugins {
    id("com.android.test")
    id("org.jetbrains.kotlin.android")
    id("androidx.baselineprofile")
}

android {
    namespace = "com.example.macrobenchmark"
    compileSdk = 36

    defaultConfig {
        minSdk = 28          // Baseline Profile floor; below this apps are fully AOT
        targetSdk = 36
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    targetProjectPath = ":app"
    experimentalProperties["android.experimental.self-instrumenting"] = true
}

baselineProfile {
    useConnectedDevices = true   // connected devices need root, or API 33+
}

dependencies {
    implementation("androidx.benchmark:benchmark-macro-junit4:1.4.1")
    implementation("androidx.test.ext:junit:1.2.1")
    implementation("androidx.test.uiautomator:uiautomator:2.4.0")
}
```

If the app has product flavors, add to the benchmark module's `defaultConfig`:

```kotlin
missingDimensionStrategy("environment", "production")
```

### `app/build.gradle.kts`

```kotlin
plugins {
    id("com.android.application")
    id("androidx.baselineprofile")
}

android {
    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
        // Only needed if you cannot sign release locally.
        create("benchmark") {
            initWith(getByName("release"))
            signingConfig = signingConfigs.getByName("debug")
            matchingFallbacks += listOf("release")   // required in multi-module apps
        }
    }
}

dependencies {
    implementation("androidx.profileinstaller:profileinstaller:1.4.1")
    baselineProfile(project(":macrobenchmark"))
}
```

If you add a `benchmark` build type, add the same `create("benchmark") { … }`
block with `matchingFallbacks` to the `:macrobenchmark` module too.

### `app/src/main/AndroidManifest.xml`

```xml
<application ...>
    <profileable android:shell="true" />
</application>
```

Requires API 29+ on device. Without it, Macrobenchmark cannot read detailed
trace data from the app. (`isProfileable` as a build-type DSL property is not
in any current doc — use the manifest tag.)

## The A/B startup benchmark

This is the pair that proves a Baseline Profile win: `None()` versus
`Partial(BaselineProfileMode.Require)`.

```kotlin
package com.example.macrobenchmark

import androidx.benchmark.macro.BaselineProfileMode
import androidx.benchmark.macro.CompilationMode
import androidx.benchmark.macro.StartupMode
import androidx.benchmark.macro.StartupTimingMetric
import androidx.benchmark.macro.junit4.MacrobenchmarkRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

private const val TARGET = "com.example.app"

@LargeTest
@RunWith(AndroidJUnit4::class)
class StartupBenchmark {

    @get:Rule val benchmarkRule = MacrobenchmarkRule()

    @Test fun startupNoCompilation() = startup(CompilationMode.None())

    @Test fun startupBaselineProfile() =
        startup(CompilationMode.Partial(baselineProfileMode = BaselineProfileMode.Require))

    private fun startup(mode: CompilationMode) = benchmarkRule.measureRepeated(
        packageName = TARGET,
        metrics = listOf(StartupTimingMetric()),
        compilationMode = mode,
        startupMode = StartupMode.COLD,
        iterations = 10,
        setupBlock = { pressHome() },
    ) {
        startActivityAndWait()
    }
}
```

Three API facts that bite:

1. `iterations` has **no default and sits after two defaulted parameters** —
   it must be passed as a named argument. There is no positional call.
2. `startupMode` defaults to **`null`**, not `COLD`. Pass it explicitly.
3. `measureRepeated` returns `Unit`. Results go to the JSON output, not a
   return value.

Use `BaselineProfileMode.Require`, not `UseIfAvailable`: `Require` throws if the
profile or ProfileInstaller is missing, so a broken setup fails loudly instead
of quietly measuring an unprofiled app and reporting "no improvement".

## The scroll / jank benchmark

Written for **1.4.1** (`device` + `By` + `Direction`). The samples currently on
developer.android.com use `uiAutomator { onElement { … } }`, which is
UiAutomator 2.4 / benchmark 1.5 only and will not compile on 1.4.1.

```kotlin
@Test
fun scrollFeed() = benchmarkRule.measureRepeated(
    packageName = TARGET,
    metrics = listOf(
        FrameTimingMetric(),
        TraceSectionMetric("FeedItemBind", mode = TraceSectionMetric.Mode.Sum),
    ),
    startupMode = StartupMode.WARM,
    iterations = 10,
    setupBlock = {
        pressHome()
        startActivityAndWait()
        device.wait(Until.hasObject(By.res("feed_list")), 10_000)
    },
) {
    val list = device.findObject(By.res("feed_list"))
    list.setGestureMargin(device.displayWidth / 5)   // stay clear of gesture nav
    repeat(3) {
        list.fling(Direction.DOWN)
        device.waitForIdle()
    }
}
```

For Compose, `Modifier.testTag("feed_list")` surfaces as `viewIdResourceName`;
on older Compose you may need `Modifier.semantics { testTagsAsResourceId = true }`
on an ancestor.

## Generating the Baseline Profile

```kotlin
@RunWith(AndroidJUnit4::class)
class BaselineProfileGenerator {
    @get:Rule val rule = BaselineProfileRule()

    @Test fun generate() = rule.collect(
        packageName = TARGET,
        includeInStartupProfile = true,
    ) {
        pressHome()
        startActivityAndWait()
        // Extend with the critical user journey: scroll the feed, open a detail screen.
    }
}
```

```bash
./gradlew :app:generateBaselineProfile
# or, skipping the macrobenchmarks:
./gradlew :app:generateBaselineProfile \
  -Pandroid.testInstrumentationRunnerArguments.androidx.benchmark.enabledRules=BaselineProfile
```

Output lands in `app/src/<variant>/generated/baselineProfiles/baseline-prof.txt`
(plural `baselineProfiles` — the codelab's singular `baselineProfile` is stale),
with `startup-prof.txt` beside it. The compiled profile must stay under 1.5 MB.

## Running and collecting traces

```bash
./gradlew :macrobenchmark:connectedCheck
./gradlew :macrobenchmark:connectedCheck \
  -Pandroid.testInstrumentationRunnerArguments.class=com.example.macrobenchmark.StartupBenchmark#startupNoCompilation
```

Macrobenchmark writes **one `.perfetto-trace` per measured iteration**, next to
`benchmarkData.json`. Gradle copies them to:

```
macrobenchmark/build/outputs/connected_android_test_additional_output/**/
```

On device they live in
`/storage/emulated/0/Android/media/<benchmark.package>/`. Pull them with:

```bash
adb shell find /storage/emulated/0/Android/media/com.example.macrobenchmark \
  -name "*.perfetto-trace" | tr -d '\r' | xargs -n1 adb pull
```

Then feed straight into `scripts/triage.py`.

### Useful instrumentation args

| Arg | Use |
|---|---|
| `androidx.benchmark.dryRunMode.enable=true` | one loop, to check the wiring compiles and runs |
| `androidx.benchmark.enabledRules=BaselineProfile` | generate profiles only |
| `androidx.benchmark.compilation.enabled=false` | skip reinstall+compile between iterations |
| `androidx.benchmark.suppressErrors=EMULATOR,LOW-BATTERY` | smoke tests only — never for numbers you report |
| `androidx.benchmark.fullTracing.enable=true` | Compose composition tracing (names composables in slices) |
| `additionalTestOutputDir=/sdcard/Download/` | redirect output; add `no-isolated-storage=true` on API 29+ |

## CompilationMode reference

| Mode | Means |
|---|---|
| `None()` | profile reset, everything JITs — worst case, fresh install with no profile |
| `Partial(baselineProfileMode, warmupIterations)` | what a real install with a profile looks like |
| `Full()` | everything AOT — not realistic, but low-variance |
| `DEFAULT` | `Partial(UseIfAvailable, 0)` — note this is **not** the same as `Partial()`, whose own default is `Require` |

The parameter is **`warmupIterations`** (plural). Google's
`measure-baselineprofile` doc page writes `warmupIteration` — that is a typo and
will not compile.

Published reference numbers for Now in Android on a Pixel 7: no compilation
324.8 ms, full 315 ms, partial 312 ms, **baseline profile 229.0 ms**.

## Without Gradle changes

If the repo cannot take a new module, `scripts/ab_startup.sh` gives a
defensible before/after from adb alone. The compilation-state commands it wraps:

```bash
adb shell cmd package compile -m speed-profile -f <pkg>   # AOT compile current profile
adb shell cmd package compile -r bg-dexopt <pkg>          # simulate overnight dexopt
adb shell cmd package compile --reset <pkg>               # API ≤33, needs root
adb shell cmd package compile -f -m verify <pkg>          # API 34+ path
adb shell pm art clear-app-profiles <pkg>                 # API 34+ path
adb shell dumpsys package dexopt | grep -A2 <pkg>         # what is actually compiled
```

On API 34+, `--reset` alone does not clear state — ART now partly compiles apps
after first launch. Use the `verify` + `clear-app-profiles` pair.
