# SPDX-License-Identifier: Apache-2.0
"""Receive and discard UDP datagrams until killed: udp-drain.py <port>. Keeps OBS's output alive in the smoke test."""
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", int(sys.argv[1])))
while True:
    sock.recv(65535)
