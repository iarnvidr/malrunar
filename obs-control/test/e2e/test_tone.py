#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline regression for the finite assembled-take tone detector."""
import array
import math
import subprocess
import sys
import tempfile
from pathlib import Path

checker = Path(__file__).with_name("tone.py")
rate = 48000

def check(samples, expected, success):
    with tempfile.NamedTemporaryFile() as audio:
        audio.write(array.array("h", (value for sample in samples for value in (sample, sample))).tobytes())
        audio.flush()
        result = subprocess.run([sys.executable, "-I", str(checker), audio.name, str(expected)], capture_output=True, text=True)
    assert (result.returncode == 0) == success, result.stdout + result.stderr

silence = [0] * rate
signal = [int(10000 * math.sin(2 * math.pi * 660 * i / rate)) for i in range(rate)]
# Assembled clip begins with one silent second, then an audible take segment.
check(silence + signal, 660, True)
check(silence + silence, 660, False)
