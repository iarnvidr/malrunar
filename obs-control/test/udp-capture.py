# SPDX-License-Identifier: Apache-2.0
"""Save UDP datagrams to a file for a while: udp-capture.py <file> <port> <give-up-seconds>.

Writes <file>.done when it has at least ~6 seconds of data (the output stream starts a few
seconds after the container starts), or when it gives up. Used by the smoke test.
"""
import socket
import sys
import time

path, port, give_up = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", port))
sock.settimeout(1)
deadline = time.monotonic() + give_up
first = None
with open(path, "wb") as out:
    while time.monotonic() < deadline:
        try:
            data = sock.recv(65535)
        except socket.timeout:
            continue
        first = first or time.monotonic()
        out.write(data)
        if time.monotonic() - first > 6:
            break
with open(path + ".done", "w") as done:
    done.write("ok\n" if first else "no data\n")
