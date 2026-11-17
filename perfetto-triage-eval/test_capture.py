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
  shell)
    shift
    case "$*" in
      "getprop ro.build.version.sdk") echo "$FAKE_SDK" ;;
      "getprop ro.product.model") echo "Fake Phone" ;;
      "pm list packages") echo "package:com.example.app" ;;
      dumpsys\ package*) echo "    flags=[ HAS_CODE ALLOW_CLEAR_USER_DATA ]" ;;
      perfetto*) cat > "$FAKE_DIR/config.pbtxt"; echo 4242 ;;
      *) ;;
    esac ;;
  *) ;;
esac
'''


def start(sdk: int) -> tuple[str, str]:
    """Run `capture.sh <pkg> --start` against the stand-in. -> (config, output)"""
    work = tempfile.mkdtemp(prefix="capture-")
    adb = os.path.join(work, "adb")
    with open(adb, "w") as fh:
        fh.write(FAKE_ADB)
    os.chmod(adb, os.stat(adb).st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=work + os.pathsep + os.environ["PATH"], FAKE_SDK=str(sdk),
               FAKE_DIR=work, OUTDIR=os.path.join(work, "traces"))
    env.pop("ANDROID_SERIAL", None)
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
