#!/usr/bin/env bash
# Measure cold-start time N times, with the compilation state controlled, and
# print median + spread. No Gradle, no benchmark module, works on any installed
# APK. Use this for the before/after loop when you cannot add Macrobenchmark.
#
#   ./ab_startup.sh com.example.app .MainActivity 10 none
#   ./ab_startup.sh com.example.app .MainActivity 10 profile
#
# modes:
#   none     reset compiled state before each run  -> worst case, fresh install
#   profile  speed-profile compile once, then run  -> what a warmed-up user sees
#   asis     do not touch compilation at all
set -euo pipefail

PKG="${1:?usage: ab_startup.sh <package> <.Activity> [iterations] [none|profile|asis]}"
ACT="${2:?activity, e.g. .MainActivity or com.example.ui.MainActivity}"
N="${3:-10}"
MODE="${4:-none}"

SDK=$(adb shell getprop ro.build.version.sdk | tr -d '\r')

reset_compilation() {
  if (( SDK >= 34 )); then
    # --reset is unreliable on API 34+: ART partly compiles after first launch.
    adb shell cmd package compile -f -m verify "$PKG" >/dev/null
    adb shell pm art clear-app-profiles "$PKG" >/dev/null 2>&1 || true
  else
    adb shell cmd package compile --reset "$PKG" >/dev/null
  fi
}

case "$MODE" in
  profile) adb shell cmd package compile -m speed-profile -f "$PKG" >/dev/null ;;
  none)    : ;;
  asis)    : ;;
  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

echo "[ab] $PKG  mode=$MODE  iterations=$N  api=$SDK"
adb shell dumpsys package dexopt 2>/dev/null | grep -A2 "\[$PKG\]" | head -3 || true

times=()
for i in $(seq 1 "$N"); do
  [[ "$MODE" == "none" ]] && reset_compilation
  adb shell am force-stop "$PKG"
  sleep 1
  out=$(adb shell am start-activity -W -n "$PKG/$ACT" \
          -a android.intent.action.MAIN -c android.intent.category.LAUNCHER 2>&1 | tr -d '\r')
  t=$(echo "$out" | awk -F': *' '/^TotalTime/{print $2}')
  if [[ -z "$t" ]]; then
    echo "[ab] run $i FAILED:"; echo "$out" | head -5; continue
  fi
  times+=("$t")
  printf '[ab] run %2d  TotalTime %5s ms\n' "$i" "$t"
  adb shell am force-stop "$PKG"
done

printf '%s\n' "${times[@]}" | sort -n | awk -v n="${#times[@]}" '
  { a[NR]=$1; s+=$1 }
  END {
    if (NR==0) { print "no successful runs"; exit 1 }
    med = (NR%2) ? a[(NR+1)/2] : (a[NR/2]+a[NR/2+1])/2;
    printf "\n[ab] n=%d  median %.0f ms  mean %.0f ms  min %d  max %d\n", NR, med, s/NR, a[1], a[NR];
    printf "[ab] report the MEDIAN. Startup distributions have a long right tail;\n";
    printf "[ab] a mean moved by one thermal outlier is how people fake wins.\n";
  }'
