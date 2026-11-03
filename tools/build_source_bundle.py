"""Build a role-separated source ZIP from the current project tree."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "dist" / "super-agent-source"
ARCHIVE = ROOT / "dist" / "super-agent-source.zip"
SKIP_PARTS = {".venv", ".uv-cache", "__pycache__"}


def add_file(files: dict[str, Path], section: str, relative: str) -> None:
    source = ROOT / relative
    if not source.is_file() or not source.resolve().is_relative_to(ROOT.resolve()):
        raise FileNotFoundError(source)
    destination = f"{section}/{relative}"
    if destination in files:
        raise ValueError(f"Duplicate bundle path: {destination}")
    files[destination] = source


def add_tree(files: dict[str, Path], section: str, relative: str, suffixes: set[str]) -> None:
    for source in sorted((ROOT / relative).rglob("*")):
        if source.is_file() and source.suffix in suffixes and not SKIP_PARTS.intersection(source.parts):
            add_file(files, section, source.relative_to(ROOT).as_posix())


def collect_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for relative in (".env.example", ".gitignore", "README.md", "pyproject.toml", "uv.lock", "config/agents.yaml"):
        add_file(files, "00_common", relative)
    add_tree(files, "00_common", "src/agent_shared", {".py"})
    add_tree(files, "10_skku_supervisor", "src/super_agent", {".py", ".js", ".html", ".css"})
    add_tree(files, "10_skku_supervisor", "src/ui", {".py", ".js", ".html", ".css"})
    add_tree(files, "11_skku_path_planning", "src/skku", {".py"})
    add_tree(files, "20_cau_process_agent", "src/cau", {".py"})
    add_file(files, "20_cau_process_agent", "config/fixtures/cau_process_plan.yaml")
    add_tree(files, "20_cau_process_agent", "deployment/cau-process-agent", {".py", ".ps1", ".md", ".toml", ".yaml"})
    add_file(files, "20_cau_process_agent", "deployment/cau-process-agent/.python-version")
    add_file(files, "20_cau_process_agent", "deployment/cau-process-agent/uv.lock")
    add_tree(files, "30_unist_unity", "src/unist", {".py", ".cs"})
    for relative in ("tools/run_twinverse_http.py", "tools/twinverse_anchor_patch.py"):
        add_file(files, "30_unist_unity", relative)
    add_tree(files, "40_tests", "tests", {".py"})
    add_tree(files, "60_docs", "docs", {".md", ".svg", ".pptx"})
    add_tree(files, "70_poc", "poc", {".py"})
    return files


README = """# Super Agent 소스 패키지

현재 프로젝트의 소스를 기관과 역할별로 분리한 공유용 묶음입니다. 각 폴더 안에서는 원본 프로젝트의 상대 경로를 유지합니다.

| 폴더 | 내용 |
| --- | --- |
| `00_common` | 공통 설정과 `src/agent_shared` |
| `10_skku_supervisor` | `src/super_agent/supervisor`와 `src/ui` 대시보드 |
| `11_skku_path_planning` | `src/skku` 로봇 A2A Agent |
| `20_cau_process_agent` | `src/cau` 공정 A2A Agent와 원격 설치 파일 |
| `30_unist_unity` | `src/unist` Unity 연동 스크립트 |
| `40_tests` | 테스트 |
| `60_docs` | 문서와 설계 자료 |
| `70_poc` | 개념 검증 코드 |

UNIST A2A Agent는 아직 구현되지 않았습니다. 현재 `src/unist`에는 Unity 미리보기 스크립트만 있습니다.
전체 프로젝트를 실행할 때는 `00_common`, `10_skku_supervisor`, `11_skku_path_planning`, `20_cau_process_agent`, `30_unist_unity`의 내용을 한 폴더로 합친 뒤 루트 `README.md`의 명령을 따르세요.
CAU만 별도 PC에 설치할 때는 독립 설치 설정이 들어 있는 `cau-process-agent.zip`을 사용합니다.
촬영·검증·로컬 패키징용 `tools` 전체는 공유하지 않습니다. `30_unist_unity/tools`의 두 스크립트는 현재 Twinverse 시연 환경을 그대로 재현할 때만 필요합니다.
"""


def main() -> None:
    files = collect_files()
    if STAGE.exists():
        if STAGE.is_symlink() or STAGE.is_junction() or not STAGE.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"Unsafe bundle staging path: {STAGE}")
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for relative, source in sorted(files.items()):
        target = STAGE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (STAGE / "README.md").write_text(README, encoding="utf-8")
    manifest = {
        "package": "super-agent-source",
        "files_sha256": {
            relative: hashlib.sha256((STAGE / relative).read_bytes()).hexdigest()
            for relative in sorted([*files, "README.md"])
        },
    }
    (STAGE / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(ARCHIVE, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(STAGE.rglob("*")):
            if source.is_file():
                archive.write(source, f"super-agent-source/{source.relative_to(STAGE).as_posix()}")
    with zipfile.ZipFile(ARCHIVE) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Source ZIP integrity check failed")
    checksum = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    (ROOT / "dist/super-agent-source.zip.sha256").write_text(f"{checksum}  super-agent-source.zip\n", encoding="ascii")
    print(f"Built {ARCHIVE} with {len(files)} source files")


if __name__ == "__main__":
    main()
