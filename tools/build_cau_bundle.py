"""Build an allowlisted CAU-only source ZIP; never include local secrets/runtime."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "deployment/cau-process-agent"
STAGE = ROOT / "dist/cau-process-agent"
SOURCE_FILES = [
    "src/agent_shared/__init__.py",
    "src/agent_shared/config.py",
    "src/agent_shared/a2a.py",
    "src/agent_shared/demo_pacing.py",
    "src/cau/__init__.py",
    "src/cau/contracts.py",
    "src/cau/backend.py",
    "src/cau/dummy_backend.py",
    "src/cau/a2a_executor.py",
    "src/cau/a2a_server.py",
    "config/fixtures/cau_process_plan.yaml",
]


def stage() -> list[str]:
    # Replace staged source files so old package paths cannot survive a refactor.
    staged_source = STAGE / "src"
    if staged_source.exists():
        if not staged_source.resolve().is_relative_to(STAGE.resolve()):
            raise RuntimeError(f"Staged source escapes bundle directory: {staged_source}")
        shutil.rmtree(staged_source)
    files = []
    for file in sorted(TEMPLATES.rglob("*")):
        if file.is_file() and (file.suffix in {".py", ".ps1", ".md", ".toml", ".yaml"} or file.name in {".python-version", "uv.lock"}):
            relative = file.relative_to(TEMPLATES)
            target = STAGE / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            files.append(relative.as_posix())
    for relative in SOURCE_FILES:
        target = STAGE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
        files.append(relative)
    return sorted(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    files = stage()
    if args.stage_only:
        print(STAGE)
        return
    if not (STAGE / "uv.lock").is_file():
        raise SystemExit("Create deployment/cau-process-agent/uv.lock with uv lock before packaging.")
    manifest = {name: hashlib.sha256((STAGE / name).read_bytes()).hexdigest() for name in sorted(files)}
    (STAGE / "MANIFEST.json").write_text(json.dumps({"package": "cau-process-agent", "backend": "dummy", "files_sha256": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append("MANIFEST.json")
    archive = ROOT / "dist/cau-process-agent.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for relative in sorted(files):
            zipped.write(STAGE / relative, f"cau-process-agent/{relative}")
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.namelist():
            if any(part in {".env", ".venv", ".twinverse-venv", ".uv-cache", "__pycache__", "runtime-verification"} for part in Path(item).parts):
                raise RuntimeError(f"Forbidden file in ZIP: {item}")
        if zipped.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed.")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ROOT / "dist/cau-process-agent.zip.sha256").write_text(f"{checksum}  cau-process-agent.zip\n", encoding="ascii")
    print(json.dumps({"zip": str(archive), "bytes": archive.stat().st_size, "files": len(files), "sha256": checksum}, indent=2))


if __name__ == "__main__":
    main()
