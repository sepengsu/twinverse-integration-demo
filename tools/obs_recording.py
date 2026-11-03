"""Local OBS WebSocket API client; isolated recording profile only."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.environ.get("A2A_RECORDING_DIRECTORY", str(ROOT / "recordings" / "a2a-unity"))).resolve()
PORTABLE = ROOT / "runtime-verification" / "obs-recording"


class ObsClient:
    def __init__(self) -> None:
        config = json.loads((PORTABLE / "config/obs-studio/plugin_config/obs-websocket/config.json").read_text(encoding="utf-8"))
        self.socket = connect(f"ws://127.0.0.1:{config['server_port']}", open_timeout=3)
        hello = json.loads(self.socket.recv())
        data = {"rpcVersion": 1, "eventSubscriptions": 0}
        if "authentication" in hello["d"]:
            challenge = hello["d"]["authentication"]
            secret = base64.b64encode(hashlib.sha256((config["server_password"] + challenge["salt"]).encode()).digest()).decode()
            data["authentication"] = base64.b64encode(hashlib.sha256((secret + challenge["challenge"]).encode()).digest()).decode()
        self.socket.send(json.dumps({"op": 1, "d": data}))
        response = json.loads(self.socket.recv())
        if response["op"] != 2:
            raise RuntimeError("OBS identification failed")

    def call(self, name: str, **data):
        request_id = str(uuid.uuid4())
        self.socket.send(json.dumps({"op": 6, "d": {"requestType": name, "requestId": request_id, "requestData": data}}))
        while True:
            response = json.loads(self.socket.recv(timeout=20))
            if response["op"] == 7 and response["d"]["requestId"] == request_id:
                body = response["d"]
                if not body["requestStatus"]["result"]:
                    raise RuntimeError(f"OBS {name}: {body['requestStatus']}")
                return body.get("responseData", {})

    def close(self):
        self.socket.close()


if __name__ == "__main__":
    import sys
    client = ObsClient()
    try:
        if len(sys.argv) > 1:
            request_data = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
            print(json.dumps(client.call(sys.argv[1], **request_data), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(client.call("GetVersion"), ensure_ascii=False, indent=2))
    finally:
        client.close()
