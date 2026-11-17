#!/usr/bin/env bash
# Capture a Perfetto trace from an app that is ALREADY INSTALLED on a connected
# device. No source, no Gradle, no build required.
#
#   ./capture.sh com.example.app                 # cold start capture
#   ./capture.sh com.example.app --interactive 20 # capture while you drive the app
#   ./capture.sh com.example.app --start          # begin tracing, return at once
#   ./capture.sh com.example.app --stop           # end tracing, pull the trace
#   ./capture.sh com.example.app --allow-debug    # capture a debuggable build anyway
#   ./capture.sh com.example.app --serial 4A121FDAQ0001B   # pick one of several devices
#
# Refuses a debuggable build by default: it runs 2 to 5x slower than release and
# its numbers are not real. --allow-debug captures it anyway; triage.py reads the
# build type back out of the trace and marks every finding directional only.
#
# --start/--stop is for a flow whose length you don't know in advance (a script
# or an agent driving several screens). The trace then holds exactly the flow,
# with no guessed duration cutting it short or padding it with dead time. A
# started capture stops by itself after --max seconds (default 600).
#
# Output: ./traces/<pkg>-<mode>-<n>.pftrace
set -euo pipefail

USAGE="usage: capture.sh <package> [--interactive SECONDS | --start [--max SECONDS] | --stop] [--allow-debug] [--serial SERIAL]"
PKG="${1:?$USAGE}"
shift || true
MODE="startup"
SECS=12
ALLOW_DEBUG=0
ACTION="run"      # run: fixed window. start / stop: you decide when it ends.
MAX=600
while (( $# > 0 )); do
  case "$1" in
    --interactive)
      MODE="interactive"
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then SECS="$2"; shift; else SECS=20; fi
      ;;
    --start) ACTION="start"; MODE="interactive" ;;
    --stop)  ACTION="stop";  MODE="interactive" ;;
    --max)
      if [[ ! "${2:-}" =~ ^[0-9]+$ ]]; then echo "[capture] ERROR: --max needs a number of seconds" >&2; exit 64; fi
      MAX="$2"; shift
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
OUTDIR="$(cd "$OUTDIR" && pwd)"   # absolute, so --stop can pull into it from anywhere
STAMP=$(date +%H%M%S)
OUT="$OUTDIR/${PKG}-${MODE}-${STAMP}.pftrace"
DEV="/data/misc/perfetto-traces/triage-${STAMP}.pftrace"
# Written by --start, read by --stop. It lives outside OUTDIR on purpose: a
# --stop run with a different OUTDIR, or from another folder, must still find
# the capture it is meant to stop.
STATE_DIR="${TMPDIR:-/tmp}"
STATE="${STATE_DIR%/}/perfetto-triage-capture-${PKG}.session"
OLD_STATE="$OUTDIR/.capture-${PKG}.session"   # where older versions kept it

# Pull the finished trace off the device and report it. Used by every mode.
finish() {
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
}

if [[ "$ACTION" == "stop" ]]; then
  if [[ ! -f "$STATE" && -f "$OLD_STATE" ]]; then STATE="$OLD_STATE"; fi
  if [[ ! -f "$STATE" ]]; then
    echo "[capture] ERROR: no capture is running for $PKG. Start one with --start." >&2
    exit 6
  fi
  PID=""; STARTED=0; SAVED_SERIAL=""
  while IFS='=' read -r key value; do
    case "$key" in
      PID) PID="$value" ;; DEV) DEV="$value" ;; OUT) OUT="$value" ;;
      STARTED) STARTED="$value" ;; SERIAL) SAVED_SERIAL="$value" ;;
    esac
  done < "$STATE"
  if [[ -z "${ANDROID_SERIAL:-}" && -n "$SAVED_SERIAL" ]]; then export ANDROID_SERIAL="$SAVED_SERIAL"; fi
  if [[ ! "$PID" =~ ^[0-9]+$ ]]; then
    echo "[capture] ERROR: $STATE is damaged (no pid). Remove it and start again." >&2
    exit 6
  fi
  # SIGTERM makes perfetto flush and close the file. It may already be gone if
  # the --max safety limit was reached, which is fine: the trace is complete.
  adb shell "kill -TERM $PID" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    adb shell "[ -d /proc/$PID ]" >/dev/null 2>&1 || break
    sleep 1
  done
  if adb shell "[ -d /proc/$PID ]" >/dev/null 2>&1; then
    echo "[capture] ERROR: tracing (pid $PID) did not stop within 30 s." >&2
    exit 7
  fi
  rm -f "$STATE"
  echo "[capture] stopped after $(( $(date +%s) - STARTED ))s"
  finish
  exit 0
fi

if [[ "$ACTION" == "start" && -f "$STATE" ]]; then
  echo "[capture] ERROR: a capture is already running for $PKG." >&2
  echo "[capture] Stop it with: capture.sh $PKG --stop" >&2
  exit 6
fi

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
  echo "[capture] WARNING: API $SDK is older than Android 12 (API 31), which is the" \
       "first release that records frame deadlines. Late frames cannot be measured" \
       "on this phone. Startup, thread state and binder analysis still work."
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

# What the trace itself may not say. Android 10 traces carry no Android version,
# and no trace says how the app was compiled. triage.py reads this file when it
# sits beside the trace: it is how the report knows that frame deadlines cannot
# exist on this phone, and whether heavy JIT means "no profile" or "stale profile".
COMPILE=$(grep -oE '\[status=[a-z-]+\]' <<<"$DUMP" | tr -d '[]' | cut -d= -f2 | sort -u | paste -sd, - || true)
# Why it reads that way. reason=install on a build installed with adb means the
# label came from install time, before profileinstaller copied the Baseline
# Profile in, so speed-profile can sit on top of an empty profile.
REASON=$(grep -oE '\[reason=[a-z-]+\]' <<<"$DUMP" | tr -d '[]' | cut -d= -f2 | sort -u | paste -sd, - || true)
{
  echo "sdk=$SDK"
  echo "model=$MODEL"
  echo "package=$PKG"
  echo "mode=$MODE"
  echo "compile_status=${COMPILE:-unknown}"
  echo "compile_reason=${REASON:-unknown}"
} > "${OUT%.pftrace}.capture.txt"
echo "[capture] compiled as: ${COMPILE:-unknown} (reason: ${REASON:-unknown})"
if [[ "$COMPILE" == *speed-profile* && "$REASON" == install ]]; then
  echo "[capture] NOTE: speed-profile with reason=install. On a build installed with adb the Baseline" \
       "Profile is usually not applied yet. See SKILL.md, \"Prove the profile is applied\"."
fi

# Keep the screen on and awake; a dozing device produces garbage traces.
adb shell svc power stayon usb >/dev/null 2>&1 || true
adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true

# The perfetto on the phone rejects a config field it does not know, and the
# capture then fails with no trace at all. Seen on Android 10 for these two.
# Neither is needed by any query: CPU frequency also arrives as an ftrace event.
fit_to_device() {
  if (( SDK < 31 )); then grep -Ev 'symbolize_ksyms|cpufreq_period_ms'; else cat; fi
}

if [[ "$ACTION" == "start" ]]; then SECS="$MAX"; fi
sed "s/atrace_apps: \"\\*\"/atrace_apps: \"$PKG\"\\n      atrace_apps: \"system_server\"/" "$CFG" \
  | sed "s/duration_ms: 20000/duration_ms: $((SECS * 1000))/" \
  | fit_to_device > /tmp/triage-cfg.pbtxt

if [[ "$ACTION" == "start" ]]; then
  # A flow of unknown length can outgrow the ring buffer, which would silently
  # overwrite its own beginning. Stream the buffer to the file as we go.
  printf 'write_into_file: true\nfile_write_period_ms: 2500\n' >> /tmp/triage-cfg.pbtxt
  # With a flush period trace_processor can read the file in pieces. Without
  # one it reports config_write_into_file_no_flush and loads it all at once.
  # Left off below Android 12, where the field has not been tried.
  if (( SDK >= 31 )); then printf 'flush_period_ms: 5000\n' >> /tmp/triage-cfg.pbtxt; fi

  echo "[capture] starting trace (stops by itself after ${MAX}s)"
  # shellcheck disable=SC2002
  PERF_OUT=$(cat /tmp/triage-cfg.pbtxt | adb shell perfetto -c - --txt -o "$DEV" -d 2>&1 | tr -d '\r' || true)
  PID=$(grep -E '^[0-9]+$' <<<"$PERF_OUT" | tail -1 || true)
  if [[ -z "$PID" ]]; then
    echo "[capture] ERROR: perfetto did not report a pid, so the trace cannot be stopped later:" >&2
    echo "$PERF_OUT" >&2
    exit 7
  fi
  printf 'PID=%s\nDEV=%s\nOUT=%s\nSTARTED=%s\nSERIAL=%s\n' \
    "$PID" "$DEV" "$OUT" "$(date +%s)" "${ANDROID_SERIAL:-}" > "$STATE"
  sleep 3  # same attach delay as the fixed-window path
  echo "[capture] tracing $PKG. Run your flow, then: capture.sh $PKG --stop"
  exit 0
fi

if [[ "$MODE" == "interactive" ]] && (( SECS > 20 )); then
  echo "[capture] NOTE: a fixed ${SECS}s window is a guess. If the flow is scripted," \
       "use --start and --stop so the trace holds exactly the flow."
fi

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

finish
