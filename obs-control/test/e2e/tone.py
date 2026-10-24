# SPDX-License-Identifier: Apache-2.0
"""Check that 48 kHz stereo s16le audio carries a tone: tone.py <raw-file> <expected-hz>.

Looks at the final second of the recording only (a scene switch moments earlier must not count).
Exit 0 and print the estimate when the signal is loud and steady and within 8% of the expected
frequency; exit 1 otherwise (with the reason on stdout).
"""
import struct
import sys

path, expected = sys.argv[1], float(sys.argv[2])
data = open(path, "rb").read()
if len(data) < 48000 * 4:
    print(f"only {len(data)} bytes recorded")
    sys.exit(1)
left = struct.unpack("<%dh" % (len(data) // 2), data)[0::2]
rate, window = 48000, 4800
# Use the final one-second window: assembled takes intentionally have a silent
# still lead-in before their audio-bearing take segment.
left = left[-rate:]
rms = [
    (sum(v * v for v in left[s : s + window]) / window) ** 0.5
    for s in range(0, len(left) - window + 1, window)
]
peak = max(rms)
if peak < 300:
    print(f"silent (peak rms {peak:.0f})")
    sys.exit(1)
# a steady tone: every window is about as loud as the loudest one
if min(rms) < 0.5 * peak:
    print(f"signal is not steady (rms {min(rms):.0f}..{peak:.0f})")
    sys.exit(1)
crossings = sum(1 for a, b in zip(left, left[1:]) if a < 0 <= b)
frequency = crossings / (len(left) / rate)
print(f"{frequency:.0f} Hz (peak rms {peak:.0f})")
sys.exit(0 if abs(frequency - expected) <= expected * 0.08 else 1)
