#!/usr/bin/env python3
"""
Checks for capture.sh that need no phone: a stand-in `adb` answers the
questions the script asks and keeps the trace config it is handed.

What is checked is the config, because a phone's perfetto refuses a config with
a field it does not know and the capture then produces nothing. A real run on
Android 10 failed that way on two fields.

A pass here proves what the script sends. It does not prove a phone accepts it.

    python3 test_capture.py
"""
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts", "capture.sh")
PKG = "com.example.app"

FAKE_ADB = r'''#!/usr/bin/env bash
case "$1" in
  devices) printf 'List of devices attached\nFAKE123\tdevice\n' ;;
  pull) printf 'trace bytes' > "$3" ;;
  shell)
    shift
    case "$*" in
      "getprop ro.build.version.sdk") echo "$FAKE_SDK" ;;
      "getprop ro.product.model") echo "Fake Phone" ;;
      "pm list packages") echo "package:com.example.app" ;;
      dumpsys\ package*) printf '    flags=[ HAS_CODE ALLOW_CLEAR_USER_DATA ]\nDexopt state:\n  [com.example.app]\n      arm64: [status=speed-profile] [reason=bg-dexopt]\n' ;;
      \[\ -d*) exit 1 ;;
      perfetto*) cat > "$FAKE_DIR/config.pbtxt"; echo 4242 ;;
      *) ;;
    esac ;;
  *) ;;
esac
'''


def sandbox(sdk: int) -> tuple[str, dict]:
    work = tempfile.mkdtemp(prefix="capture-")
    adb = os.path.join(work, "adb")
    with open(adb, "w") as fh:
        fh.write(FAKE_ADB)
    os.chmod(adb, os.stat(adb).st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=work + os.pathsep + os.environ["PATH"], FAKE_SDK=str(sdk),
               FAKE_DIR=work, OUTDIR=os.path.join(work, "traces"), TMPDIR=work)
    env.pop("ANDROID_SERIAL", None)
    return work, env


def start(sdk: int) -> tuple[str, str]:
    """Run `capture.sh <pkg> --start` against the stand-in. -> (config, output)"""
    work, env = sandbox(sdk)
    done = subprocess.run(["bash", CAPTURE, PKG, "--start"], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    with open(os.path.join(work, "config.pbtxt")) as fh:
        return fh.read(), done.stdout + done.stderr


def test_android_10_config_drops_the_fields_that_phone_rejects():
    config, said = start(29)
    assert "symbolize_ksyms" not in config and "cpufreq_period_ms" not in config, config[-400:]
    assert "flush_period_ms" not in config, "not tried below Android 12, so not sent"
    assert "write_into_file: true" in config and f'atrace_apps: "{PKG}"' in config
    assert "sched/sched_switch" in config and "android.surfaceflinger.frametimeline" in config
    assert config.count("{") == config.count("}"), "dropping lines must not unbalance the config"
    assert "Late frames cannot be measured" in said


def test_android_14_config_is_complete_and_sets_a_flush_period():
    config, said = start(34)
    assert "symbolize_ksyms: true" in config and "cpufreq_period_ms: 250" in config
    assert "write_into_file: true" in config and "flush_period_ms: 5000" in config
    assert "Late frames cannot be measured" not in said


def test_stop_finds_the_capture_from_another_folder_and_another_outdir():
    """--stop said "no capture is running" when OUTDIR differed from --start."""
    work, env = sandbox(29)
    done = subprocess.run(["bash", CAPTURE, PKG, "--start"], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    elsewhere = tempfile.mkdtemp(prefix="capture-else-")
    stop_env = dict(env, OUTDIR=os.path.join(elsewhere, "other-traces"))
    done = subprocess.run(["bash", CAPTURE, PKG, "--stop"], env=stop_env, cwd=elsewhere,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    traces = [f for f in os.listdir(os.path.join(work, "traces")) if f.endswith(".pftrace")]
    assert len(traces) == 1, "the trace lands where --start said, not where --stop was run"
    again = subprocess.run(["bash", CAPTURE, PKG, "--stop"], env=stop_env, cwd=elsewhere,
                           capture_output=True, text=True, timeout=60)
    assert again.returncode == 6 and "no capture is running" in again.stderr


def test_capture_notes_the_android_version_and_how_the_app_is_compiled():
    """An Android 10 trace does not carry its Android version, and no trace
    says how the app was compiled. capture.sh writes both beside the trace."""
    work, env = sandbox(29)
    done = subprocess.run(["bash", CAPTURE, PKG, "--start"], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    notes = [f for f in os.listdir(os.path.join(work, "traces")) if f.endswith(".capture.txt")]
    assert len(notes) == 1, os.listdir(os.path.join(work, "traces"))
    with open(os.path.join(work, "traces", notes[0])) as fh:
        note = dict(line.strip().split("=", 1) for line in fh if "=" in line)
    assert note["sdk"] == "29" and note["compile_status"] == "speed-profile", note
    assert note["package"] == PKG and note["mode"] == "interactive"
    assert "compiled as: speed-profile" in done.stdout
    sys.path.insert(0, os.path.dirname(CAPTURE))
    import triage
    trace = os.path.join(work, "traces", notes[0].replace(".capture.txt", ".pftrace"))
    assert triage.read_capture_info(trace)["sdk"] == "29", "and triage finds it beside the trace"


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
    print(f"\n{len(CASES) - failed} passed, {failed} failed (a stand-in adb, not a phone)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
