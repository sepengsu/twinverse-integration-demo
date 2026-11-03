"""Prepare an isolated, silent OBS profile for the requested two-window video."""
from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "runtime-verification/obs-recording"
CONFIG = PORTABLE / "config/obs-studio"
OUTPUT = ROOT / "recordings/a2a-unity"
OUTPUT.mkdir(parents=True, exist_ok=True)
profile = CONFIG / "basic/profiles/A2ARecording"
scenes = CONFIG / "basic/scenes"
plugin = CONFIG / "plugin_config/obs-websocket"
for directory in (profile, scenes, plugin):
    directory.mkdir(parents=True, exist_ok=True)

(CONFIG / "global.ini").write_text("""[General]
FirstRun=false
LicenseAccepted=true
Language=en-US
InfoIncrement=-1
LastVersion=536870913

[Basic]
Profile=A2ARecording
ProfileDir=A2ARecording
SceneCollection=A2ARecording
SceneCollectionFile=A2ARecording
ConfigOnNewProfile=false

[BasicWindow]
PreviewEnabled=true
""", encoding="utf-8")
(profile / "basic.ini").write_text(f"""[General]
Name=A2ARecording

[Video]
BaseCX=1920
BaseCY=1080
OutputCX=1920
OutputCY=1080
FPSType=0
FPSCommon=30
ColorFormat=NV12
ColorSpace=709
ColorRange=Partial

[Output]
Mode=Simple

[SimpleOutput]
FilePath={OUTPUT.as_posix()}
RecFormat2=mkv
RecFormat=mkv
RecQuality=Small
RecEncoder=x264
UseAdvanced=false

[Audio]
SampleRate=48000
ChannelSetup=Stereo
""", encoding="utf-8")
websocket_config = {"first_load": False, "server_enabled": True, "server_port": 4457,
                    "alerts_enabled": False, "auth_required": True, "server_password": secrets.token_urlsafe(24)}
(plugin / "config.json").write_text(json.dumps(websocket_config), encoding="utf-8")
scene = {"name": "A2ARecording", "current_scene": "A2A Demo", "current_program_scene": "A2A Demo",
         "scene_order": [{"name": "A2A Demo"}], "sources": [{"id": "scene", "versioned_id": "scene",
         "name": "A2A Demo", "uuid": str(uuid.uuid4()), "settings": {"items": []}}],
         "groups": [], "quick_transitions": [], "transitions": [{"name": "Cut", "id": "cut_transition", "settings": {}}],
         "current_transition": "Cut", "transition_duration": 300}
for key in ("DesktopAudioDevice1", "DesktopAudioDevice2", "AuxAudioDevice1", "AuxAudioDevice2", "AuxAudioDevice3", "AuxAudioDevice4"):
    scene[key] = None
(scenes / "A2ARecording.json").write_text(json.dumps(scene), encoding="utf-8")
print("Isolated OBS profile prepared: 1920x1080, 30fps, two requested windows, no microphone/audio.")
