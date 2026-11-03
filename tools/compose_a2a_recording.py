"""Compose only the explicitly requested dashboard and Unity windows in OBS."""
from __future__ import annotations

import json
import time

from obs_recording import ObsClient, OUTPUT


def main():
    client = ObsClient()
    scene = "A2A Demo"
    try:
        existing = {item["inputName"] for item in client.call("GetInputList")["inputs"]}

        def create(name, kind, settings):
            if name in existing:
                client.call("SetInputSettings", inputName=name, inputSettings=settings, overlay=True)
                return client.call("GetSceneItemId", sceneName=scene, sourceName=name)["sceneItemId"]
            return client.call("CreateInput", sceneName=scene, inputName=name, inputKind=kind,
                               inputSettings=settings, sceneItemEnabled=True)["sceneItemId"]

        def transform(item, x, y, **settings):
            client.call("SetSceneItemTransform", sceneName=scene, sceneItemId=item,
                        sceneItemTransform={"positionX": x, "positionY": y, "alignment": 5, **settings})

        background = create("Recording Background", "color_source_v3",
                            {"color": 0xFF21150D, "width": 1920, "height": 1080})
        client.call("SetSceneItemIndex", sceneName=scene, sceneItemId=background, sceneItemIndex=0)
        for name, matcher, x, y, width, height in [
            ("A2A Web", lambda title: "A2A Console" in title, 24, 110, 1038, 856),
            ("Unity Runtime", lambda title: "A2A Unity Preview" in title, 1086, 110, 810, 856),
        ]:
            windows = client.call("GetInputPropertiesListPropertyItems", inputName=name, propertyName="window")["propertyItems"]
            matches = [item for item in windows if matcher(item.get("itemName", "")) and item.get("itemEnabled")]
            if len(matches) != 1:
                raise RuntimeError(f"Expected one authorized {name} window, found {len(matches)}")
            client.call("SetInputSettings", inputName=name,
                        inputSettings={"window": matches[0]["itemValue"], "method": 2, "client_area": True,
                                       "cursor": True, "capture_audio": False}, overlay=True)
            item = client.call("GetSceneItemId", sceneName=scene, sourceName=name)["sceneItemId"]
            client.call("SetSceneItemEnabled", sceneName=scene, sceneItemId=item, sceneItemEnabled=True)
            transform(item, x, y, cropTop=80 if name == "A2A Web" else 0, boundsType="OBS_BOUNDS_SCALE_INNER", boundsAlignment=5,
                      boundsWidth=width, boundsHeight=height)

        def label(name, text, x, y, size=24, color=0xE6EAF1):
            item = create(name, "text_gdiplus_v3", {"text": text, "font": {"face": "Malgun Gothic", "size": size},
                                                    "color": color, "opacity": 100})
            transform(item, x, y)
            return item

        label("Recording Title", "에이전트 협업 시연  |  A2A × Twinverse", 24, 20, 32)
        label("Web Label", "WEB  ·  네트워크 / 요청·응답 / 실제 JSON", 24, 75, 21, 0xEEE67C)
        label("Unity Label", "UNITY  ·  실제 수신 경로 / 로컬 재생", 1086, 75, 21, 0xEEE67C)
        label("Demo Scope", "CAU Dummy · Kafka 제외 · 충돌 미검증", 1430, 30, 17)
        if "Demo Flow" in existing:
            old_item = client.call("GetSceneItemId", sceneName=scene, sourceName="Demo Flow")["sceneItemId"]
            client.call("SetSceneItemEnabled", sceneName=scene, sceneItemId=old_item, sceneItemEnabled=False)
        label("Recording Caption", "1. 웹에서 작업을 실행하고 에이전트 간 통신을 확인합니다.", 24, 995, 27)
        client.call("SetCurrentProgramScene", sceneName=scene)
        time.sleep(3)
        details = {name: client.call("GetSceneItemTransform", sceneName=scene,
                                    sceneItemId=client.call("GetSceneItemId", sceneName=scene, sourceName=name)["sceneItemId"])["sceneItemTransform"]
                   for name in ["A2A Web", "Unity Runtime"]}
        (OUTPUT / "capture-sources.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
        client.call("SaveSourceScreenshot", sourceName=scene, imageFormat="png",
                    imageFilePath=str(OUTPUT / "recording-preview.png"), imageWidth=1920, imageHeight=1080)
        print("OBS composition saved: recording-preview.png")
        print(json.dumps({name: {key: detail[key] for key in ["sourceWidth", "sourceHeight"]}
                          for name, detail in details.items()}))
    finally:
        client.close()


if __name__ == "__main__":
    main()
