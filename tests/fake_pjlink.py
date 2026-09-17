#!/usr/bin/env python3
"""
A pretend PJLink projector for testing without the real one.

    python3 tests/fake_pjlink.py [--port 4352] [--password secret]

Answers POWR (with a short warm-up / cool-down), INPT and LAMP, the same way
the Hitachi does. Also importable: FakeProjector(port).start() runs a thread.
"""

import argparse
import hashlib
import socket
import threading
import time


class FakeProjector:
    def __init__(self, port=0, password="", warm_seconds=2.0):
        self.port = port
        self.password = password
        self.warm_seconds = warm_seconds
        self.power = "0"           # 0 off, 1 on, 2 cooling, 3 warming
        self.input = "31"
        self.lamp_hours = 1234
        self.muted = False
        self.erst = "000000"       # fan, lamp, temperature, cover, filter, other
        self.commands = []         # everything received, for assertions
        self._until = 0
        self._sock = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def _tick(self):
        if self.power in ("2", "3") and time.time() >= self._until:
            self.power = "1" if self.power == "3" else "0"

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            conn.settimeout(5)
            seed = "498e4a67"
            if self.password:
                conn.sendall(("PJLINK 1 %s\r" % seed).encode())
            else:
                conn.sendall(b"PJLINK 0\r")
            try:
                data = b""
                while not data.endswith(b"\r"):
                    chunk = conn.recv(256)
                    if not chunk:
                        return
                    data += chunk
            except OSError:
                return
            line = data.decode(errors="replace").strip()
            if self.password:
                digest = hashlib.md5((seed + self.password).encode()).hexdigest()
                if not line.startswith(digest):
                    conn.sendall(b"PJLINK ERRA\r")
                    return
                line = line[len(digest):]
            self.commands.append(line)
            conn.sendall((self._answer(line) + "\r").encode())

    def _answer(self, line):
        self._tick()
        if not line.startswith("%1"):
            return "%1XXXX=ERR1"
        body = line[2:]
        cmd, _, arg = body.partition(" ")
        arg = arg.strip()
        if cmd == "POWR":
            if arg == "?":
                return "%1POWR=" + self.power
            if arg in ("0", "1"):
                if self.power in ("2", "3"):
                    return "%1POWR=ERR3"
                if arg == "1" and self.power == "0":
                    self.power = "3"
                    self._until = time.time() + self.warm_seconds
                elif arg == "0" and self.power == "1":
                    self.power = "2"
                    self._until = time.time() + self.warm_seconds
                return "%1POWR=OK"
            return "%1POWR=ERR2"
        if cmd == "INPT":
            if arg == "?":
                return "%1INPT=" + self.input
            if self.power != "1":
                return "%1INPT=ERR3"
            self.input = arg
            return "%1INPT=OK"
        if cmd == "AVMT":
            if arg == "?":
                return "%%1AVMT=%s" % ("31" if self.muted else "30")
            if self.power != "1":
                return "%1AVMT=ERR3"
            if arg in ("30", "31", "11", "21", "10", "20"):
                self.muted = arg.endswith("1")
                return "%1AVMT=OK"
            return "%1AVMT=ERR2"
        if cmd == "ERST":
            return "%%1ERST=%s" % self.erst
        if cmd == "LAMP":
            return "%%1LAMP=%d %s" % (self.lamp_hours, "1" if self.power == "1" else "0")
        if cmd == "NAME":
            return "%1NAME=Fake CP-BW301WN"
        return "%1" + cmd + "=ERR1"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4352)
    parser.add_argument("--password", default="")
    args = parser.parse_args()
    fake = FakeProjector(args.port, args.password).start()
    print("Fake projector listening on 127.0.0.1:%d" % fake.port)
    while True:
        time.sleep(3600)
