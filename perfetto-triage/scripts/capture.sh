#!/usr/bin/env bash
# Capture a Perfetto trace from an app that is ALREADY INSTALLED on a connected
# device. No source, no Gradle, no build required.
#
#   ./capture.sh com.example.app                 # cold start capture
#   ./capture.sh com.example.app --interactive 20 # capture while you drive the app
#   ./capture.sh com.example.app --allow-debug    # capture a debuggable build anyway
#   ./capture.sh com.example.app --serial 4A121FDAQ0001B   # pick one of several devices
#
# Refuses a debuggable build by default: it runs 2 to 5x slower than release and
# its numbers are not real. --allow-debug captures it anyway; triage.py reads the
# build type back out of the trace and marks every finding directional only.
#
# Output: ./traces/<pkg>-<mode>-<n>.pftrace
set -euo pipefail

USAGE="usage: capture.sh <package> [--interactive SECONDS] [--allow-debug] [--serial SERIAL]"
PKG="${1:?$USAGE}"
shift || true
MODE="startup"
SECS=12
ALLOW_DEBUG=0
while (( $# > 0 )); do
  case "$1" in
    --interactive)
      MODE="interactive"
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then SECS="$2"; shift; else SECS=20; fi
      ;;
    --allow-debug) ALLOW_DEBUG=1 ;;
    --serial)
      if [[ -z "${2:-}" ]]; then echo "[capture] ERROR: --serial needs a value" >&2; exit 64; fi
      export ANDROID_SERIAL="$2"; shift
      ;;
    *) echo "[capture] ERROR: unknown argument '$1'" >&2; echo "$USAGE" >&2; exit 64 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$HERE/../assets/startup_jank.pbtxt"
OUTDIR="${OUTDIR:-./traces}"
mkdir -p "$OUTDIR"
STAMP=$(date +%H%M%S)
OUT="$OUTDIR/${PKG}-${MODE}-${STAMP}.pftrace"
DEV="/data/misc/perfetto-traces/triage-${STAMP}.pftrace"

# One device, chosen on purpose. The same phone connected over USB and Wi-Fi
# shows up as two entries, and every adb call below would fail with "more than
# one device". Say which devices there are and stop, instead of failing later.
if [[ -z "${ANDROID_SERIAL:-}" ]]; then
  DEVICES=$(adb devices | tr -d '\r' | awk 'NR > 1 && $2 == "device" { print $1 }')
  COUNT=$(grep -c . <<<"$DEVICES" || true)
  if (( COUNT > 1 )); then
    echo "[capture] ERROR: $COUNT devices are connected. Pick one with --serial" >&2
    echo "[capture] (or set ANDROID_SERIAL). A phone on both USB and Wi-Fi counts twice:" >&2
    sed 's/^/[capture]   /' <<<"$DEVICES" >&2
    exit 4
  fi
else
  echo "[capture] using device $ANDROID_SERIAL"
fi

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

# Build type. dumpsys prints the ApplicationInfo flags as `flags=[ ... ]` or
# `pkgFlags=[ ... ]` depending on the Android version; DEBUGGABLE is in that list
# for a debuggable APK.
DUMP=$(adb shell dumpsys package "$PKG" 2>/dev/null | tr -d '\r' || true)
FLAG_LINES=$(grep -E '^[[:space:]]*(pkgFlags|flags)=\[' <<<"$DUMP" || true)
if [[ -z "$FLAG_LINES" ]]; then
  echo "[capture] WARNING: could not read package flags for $PKG, so the build" \
       "type is NOT verified. triage.py will check it again from the trace."
elif grep -qw DEBUGGABLE <<<"$FLAG_LINES"; then
  if (( ALLOW_DEBUG )); then
    echo "[capture] WARNING: $PKG is a DEBUGGABLE build. Capturing anyway" \
         "(--allow-debug). Every number from this trace is directional only."
  else
    echo "[capture] ERROR: $PKG is a DEBUGGABLE build. It runs 2 to 5x slower than" >&2
    echo "[capture] release, so a trace of it does not represent what users get." >&2
    echo "[capture] Install a release or release-like build, or pass --allow-debug" >&2
    echo "[capture] to capture it anyway and have the report flagged." >&2
    exit 3
  fi
else
  echo "[capture] build type: not debuggable"
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

# adb pull writes its progress line to stderr, so silence both streams and
# report a failure ourselves.
if ! adb pull "$DEV" "$OUT" >/dev/null 2>&1; then
  echo "[capture] ERROR: could not pull $DEV from the device." >&2
  exit 5
fi
adb shell rm -f "$DEV" || true
adb shell svc power stayon false >/dev/null 2>&1 || true

SIZE=$(wc -c < "$OUT")
echo "[capture] wrote $OUT ($((SIZE / 1024)) KB)"
if (( SIZE < 50000 )); then
  echo "[capture] WARNING: trace is suspiciously small. The app may not have" \
       "launched, or tracing was denied. Check 'adb logcat -d | grep -i perfetto'."
fi
echo "$OUT"
