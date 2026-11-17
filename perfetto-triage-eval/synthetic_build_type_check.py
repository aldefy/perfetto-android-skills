#!/usr/bin/env python3
"""
End-to-end check of 03_build_type.sql on a SYNTHETIC trace.

Builds two tiny Perfetto traces by hand, each holding one PackagesList packet
(the output of the android.packages_list data source) and nothing else: one
with the app marked debuggable, one not. Runs the real triage.py on both
through a real trace_processor and checks health_status.build_type.

What this proves: the SQL is valid for the pinned trace_processor version,
the package_list table has the columns the query reads, and triage.py turns
the row into the right verdict.

What it does not prove: anything about a real capture. These traces contain
no scheduler, slice or frame data, so every other check in the report fails
on them by design. This is plumbing, not a measurement.

    python3 synthetic_build_type_check.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TRIAGE_PY = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts", "triage.py")
PKG = "com.example.synthetic"


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def ld(field, payload):
    """Length-delimited protobuf field."""
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def vi(field, n):
    """Varint protobuf field."""
    return varint(field << 3) + varint(n)


def package_info(name, uid, debuggable, version_code):
    # PackagesList.PackageInfo: name=1, uid=2, debuggable=3,
    # profileable_from_shell=4, version_code=5
    return ld(1, ld(1, name.encode()) + vi(2, uid) + vi(3, int(debuggable))
              + vi(4, 0) + vi(5, version_code))


def trace_bytes(debuggable):
    plist = package_info(PKG, 10234, debuggable, 7) + package_info("com.other.app", 10300, False, 3)
    packet = ld(47, plist)   # TracePacket.packages_list = 47
    return ld(1, packet)     # Trace.packet = 1


def triage(tmp, label, debuggable):
    trace = os.path.join(tmp, f"{PKG}-startup-{label}.pftrace")
    out = os.path.join(tmp, f"out-{label}")
    with open(trace, "wb") as fh:
        fh.write(trace_bytes(debuggable))
    subprocess.run([sys.executable, TRIAGE_PY, trace, "--pkg", PKG, "--out", out, "--no-open"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.path.join(out, "findings.json")) as fh:
        return json.load(fh)


def main():
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for label, debuggable, expect in (("debug", True, "debuggable"),
                                          ("release", False, "release")):
            data = triage(tmp, label, debuggable)
            got = data["health_status"]["build_type"]
            blocker = any(f["severity"] == "blocker" and "Debuggable build" in f["title"]
                          for f in data["findings"])
            ok = got == expect and blocker == debuggable
            failed += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] synthetic {label} trace: "
                  f"build_type={got}, debuggable blocker raised={blocker}")
    print("\nsynthetic traces only: this checks the query and the plumbing, "
          "not a real capture")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
