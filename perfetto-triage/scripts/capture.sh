#!/usr/bin/env bash
# Capture a Perfetto trace from an app that is ALREADY INSTALLED on a connected
# device. No source, no Gradle, no build required.
#
#   ./capture.sh com.example.app                 # cold start capture
#   ./capture.sh com.example.app --interactive 20 # capture while you drive the app
#
# Output: ./traces/<pkg>-<mode>-<n>.pftrace
set -euo pipefail

PKG="${1:?usage: capture.sh <package> [--interactive SECONDS]}"
shift || true
MODE="startup"
SECS=12
if [[ "${1:-}" == "--interactive" ]]; then MODE="interactive"; SECS="${2:-20}"; fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$HERE/../assets/startup_jank.pbtxt"
OUTDIR="${OUTDIR:-./traces}"
mkdir -p "$OUTDIR"
STAMP=$(date +%H%M%S)
OUT="$OUTDIR/${PKG}-${MODE}-${STAMP}.pftrace"
DEV="/data/misc/perfetto-traces/triage-${STAMP}.pftrace"

adb wait-for-device >/dev/null
SDK=$(adb shell getprop ro.build.version.sdk | tr -d '\r')
MODEL=$(adb shell getprop ro.product.model | tr -d '\r')
echo "[capture] device: $MODEL (API $SDK)"
if (( SDK < 31 )); then
  echo "[capture] WARNING: API $SDK < 31. FrameTimeline is unavailable, so jank" \
       "attribution will be empty. Startup and binder analysis still work."
fi
if ! adb shell pm list packages | tr -d '\r' | grep -x "package:$PKG" >/dev/null; then
  echo "[capture] ERROR: $PKG is not installed on this device." >&2
  echo "[capture] Installed packages matching:" >&2
  adb shell pm list packages | tr -d '\r' | grep -i "${PKG%%.*}" >&2 || true
  exit 2
fi

# Keep the screen on and awake; a dozing device produces garbage traces.
adb shell svc power stayon usb >/dev/null 2>&1 || true
adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true

sed "s/atrace_apps: \"\\*\"/atrace_apps: \"$PKG\"\\n      atrace_apps: \"system_server\"/" "$CFG" \
  | sed "s/duration_ms: 20000/duration_ms: $((SECS * 1000))/" > /tmp/triage-cfg.pbtxt

echo "[capture] starting trace (${SECS}s)"
# shellcheck disable=SC2002
cat /tmp/triage-cfg.pbtxt | adb shell perfetto -c - --txt -o "$DEV" -d >/dev/null
sleep 3  # give tracing time to fully attach before we force-stop+relaunch,
         # or android_startups can miss the launch entirely (race, not a bug
         # in the config — see deck slide on this)

if [[ "$MODE" == "startup" ]]; then
  echo "[capture] cold-starting $PKG"
  adb shell am force-stop "$PKG"
  adb shell am kill "$PKG" >/dev/null 2>&1 || true
  sleep 1
  adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
else
  echo "[capture] DRIVE THE APP NOW — scroll the screen you care about. ${SECS}s..."
fi

sleep "$((SECS + 2))"
# Wait for the tracing service to flush.
for _ in $(seq 1 30); do
  adb shell 'pgrep -f "perfetto -c -" >/dev/null' || break
  sleep 1
done

adb pull "$DEV" "$OUT" >/dev/null
adb shell rm -f "$DEV" || true
adb shell svc power stayon false >/dev/null 2>&1 || true

SIZE=$(wc -c < "$OUT")
echo "[capture] wrote $OUT ($((SIZE / 1024)) KB)"
if (( SIZE < 50000 )); then
  echo "[capture] WARNING: trace is suspiciously small. The app may not have" \
       "launched, or tracing was denied. Check 'adb logcat -d | grep -i perfetto'."
fi
echo "$OUT"
