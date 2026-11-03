"""Remux the completed recording and export frames for visual verification."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from obs_recording import OUTPUT


def main():
    result_file = OUTPUT / "recording-result.json"
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if not result.get("success") or result.get("workflow_status") != "completed":
        raise RuntimeError("Refusing to finalize an incomplete demonstration")
    source = Path(result["recording"]).resolve()
    if not source.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError("Recording must be in the task output directory")
    destination = OUTPUT / "a2a-web-unity-demo.mp4"
    binaries = Path.home() / "AppData/Local/Microsoft/WinGet/Links"
    ffmpeg = str(binaries / "ffmpeg.exe")
    ffprobe = str(binaries / "ffprobe.exe")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                    "-map", "0:v:0", "-c:v", "copy", "-movflags", "+faststart", str(destination)], check=True)
    metadata = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-show_streams", "-show_format",
                                                   "-of", "json", str(destination)], text=True, encoding="utf-8"))
    stream = metadata["streams"][0]
    duration = float(metadata["format"]["duration"])
    phases = json.loads((OUTPUT / "recording-phases.json").read_text(encoding="utf-8"))
    if stream["width"] != 1920 or stream["height"] != 1080 or not phases or duration < phases[-1]["seconds"] + 3:
        raise RuntimeError("Unexpected video dimensions or duration")
    frames = OUTPUT / "verification-frames"
    frames.mkdir(exist_ok=True)
    timestamps = sorted({round(value,2) for value in [1, 3.5, 8, 13, 25, 50, 75, duration-2] if 0 <= value < duration})
    for index, timestamp in enumerate(timestamps):
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(timestamp),
                        "-i", str(destination), "-frames:v", "1", str(frames / f"frame-{index}.png")], check=True)
    (frames / "timestamps.json").write_text(json.dumps(timestamps), encoding="utf-8")
    result["video_path"] = str(destination)
    result["video"] = {"duration_seconds": duration, "width": stream["width"], "height": stream["height"],
                       "frame_rate": stream["avg_frame_rate"], "codec": stream["codec_name"],
                       "size_bytes": int(metadata["format"]["size"]), "audio": False}
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT / "video-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps({"file": str(destination), **result["video"]}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
