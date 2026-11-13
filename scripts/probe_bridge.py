"""Read-only probe of the existing Codex daemon through its byte proxy.

Does not resume, queue, start a turn, or change configuration. Uses only stdlib.
"""
import argparse
import base64
import hashlib
import json
import os
import queue
import struct
import subprocess
import threading
import time
import uuid


def probe_native(thread_id):
    """Read only App thread metadata using the inherited desktop tools pipe."""
    pipe_path = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    caller_thread_id = os.environ.get("CODEX_THREAD_ID")
    if not pipe_path or not caller_thread_id:
        raise RuntimeError("Run inside the desktop App environment; pipe and caller thread are required")
    with open(pipe_path, "r+b", buffering=0) as stream:
        def read(count):
            result = bytearray()
            while len(result) < count:
                data = stream.read(count - len(result))
                if not data:
                    raise EOFError("Desktop App pipe closed")
                result.extend(data)
            return bytes(result)

        def call(request_id, method, params):
            data = json.dumps({"jsonrpc": "2.0", "id": request_id,
                               "method": method, "params": params}).encode()
            stream.write(struct.pack("<I", len(data)) + data)
            size = struct.unpack("<I", read(4))[0]
            if size > 64 * 1024 * 1024:
                raise ValueError("Oversized desktop App response")
            response = json.loads(read(size))
            if "error" in response:
                raise RuntimeError(json.dumps(response["error"]))
            return response["result"]

        catalog = call(1, "tools/list", {"threadStartKind": "all"})
        read_tool = next(tool for tool in catalog["tools"] if tool["name"] == "read_thread")
        result = call(2, "tools/call", {
            "arguments": {"threadId": thread_id, "turnLimit": 1, "includeOutputs": False,
                          "maxOutputCharsPerItem": 0},
            "callerSource": "codex", "callId": "probe-" + str(uuid.uuid4()),
            "namespace": read_tool["namespace"], "threadId": caller_thread_id,
            "tool": "read_thread", "turnId": "mcp-turn-readonly-probe",
        })
        summary = {"transport": "desktop-native-pipe", "success": result.get("success"),
                   "targetThreadId": thread_id}
        for item in result.get("contentItems", []):
            if item.get("type") != "inputText":
                continue
            try:
                content = json.loads(item["text"])
            except ValueError:
                continue
            if isinstance(content, dict):
                summary["resultKeys"] = list(content)
                if isinstance(content.get("thread"), dict):
                    content = content["thread"]
                for key in ("id", "threadId", "hostId", "status"):
                    if key in content:
                        summary[key] = content[key]
        print(json.dumps(summary, ensure_ascii=False, indent=2))


class ProxyClient:
    def __init__(self, executable="codex", timeout=10):
        self.timeout = timeout
        self.process = subprocess.Popen(
            [executable, "app-server", "proxy"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.chunks = queue.Queue()
        self.buffer = bytearray()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                data = os.read(self.process.stdout.fileno(), 65536)
                self.chunks.put(data)
                if not data:
                    return
        except OSError:
            self.chunks.put(b"")

    def read(self, count):
        while len(self.buffer) < count:
            data = self.chunks.get(timeout=self.timeout)
            if not data:
                raise EOFError("Codex proxy closed")
            self.buffer.extend(data)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def connect(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
            "\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.process.stdin.write(request.encode())
        self.process.stdin.flush()
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            header.extend(self.read(1))
            if len(header) > 65536:
                raise ValueError("Oversized upgrade response")
        lines = header.decode().split("\r\n")
        if lines[0].split()[1] != "101":
            raise ValueError("WebSocket upgrade failed: " + lines[0])
        headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        headers = {k.lower(): v.strip() for k, v in headers.items()}
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ValueError("Invalid WebSocket accept hash")

    def frame(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        header = bytes([128 | opcode])
        if size < 126:
            header += bytes([128 | size])
        elif size <= 65535:
            header += bytes([254]) + struct.pack("!H", size)
        else:
            header += bytes([255]) + struct.pack("!Q", size)
        self.process.stdin.write(header + mask + bytes(
            value ^ mask[i % 4] for i, value in enumerate(data)
        ))
        self.process.stdin.flush()

    def send(self, message):
        self.frame(json.dumps(message).encode())

    def receive(self):
        message = bytearray()
        while True:
            header = self.read(2)
            opcode = header[0] & 15
            size = header[1] & 127
            if size == 126:
                size = struct.unpack("!H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read(8))[0]
            if size > 64 * 1024 * 1024:
                raise ValueError("Oversized WebSocket frame")
            mask = self.read(4) if header[1] & 128 else None
            data = self.read(size)
            if mask:
                data = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
            if opcode == 8:
                raise EOFError("WebSocket closed")
            if opcode == 9:
                self.frame(data, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise ValueError("Unexpected WebSocket opcode")
            message.extend(data)
            if header[0] & 128:
                return json.loads(message)

    def call(self, request_id, method, params):
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = self.receive()
            if response.get("id") == request_id:
                if "error" in response:
                    raise RuntimeError(json.dumps(response["error"]))
                return response["result"]
        raise TimeoutError(method)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--native", action="store_true", help="Probe the actual desktop App tools pipe")
    args = parser.parse_args()
    if not args.thread:
        parser.error("--thread or CODEX_THREAD_ID is required")
    if args.native:
        probe_native(args.thread)
        return
    client = ProxyClient(args.codex)
    try:
        client.connect()
        initialization = client.call(1, "initialize", {
            "clientInfo": {"name": "codex-monitor-readonly-probe", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        client.send({"method": "initialized", "params": {}})
        loaded_ids = []
        cursor = None
        request_id = 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            page = client.call(request_id, "thread/loaded/list", params)
            loaded_ids.extend(page["data"])
            cursor = page.get("nextCursor")
            request_id += 1
            if not cursor:
                break
        thread = client.call(request_id, "thread/read", {
            "threadId": args.thread, "includeTurns": False,
        })["thread"]
        print(json.dumps({
            "userAgent": initialization.get("userAgent"),
            "threadId": thread["id"], "loadedInDaemon": args.thread in loaded_ids,
            "status": thread.get("status"), "source": thread.get("source"),
            "cwd": thread.get("cwd"), "loadedThreadCount": len(loaded_ids),
        }, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
