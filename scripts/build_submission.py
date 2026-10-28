"""Package only reviewable source/materials, excluding local credentials and caches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {"src", "tests", "docs", "data", "examples", "scripts"}
FILES = {"README.md", "SUBMISSION.md", "requirements.txt", ".env.example", ".gitignore"}


def included(path):
    relative = path.relative_to(ROOT)
    return (path.is_file() and not path.is_symlink()
            and (relative.parts[0] in DIRECTORIES or str(relative) in FILES)
            and not any(p in {"__pycache__", ".pytest_cache", ".git", ".venv"} for p in relative.parts)
            and not path.name.endswith((".pyc", ".lock", ".tmp"))
            and not (path.name.startswith(".env") and path.name != ".env.example"))


def package(output):
    paths = sorted(p for p in ROOT.rglob("*") if included(p))
    secrets = []
    if os.getenv("DEEPSEEK_API_KEY"):
        secrets.append(os.environ["DEEPSEEK_API_KEY"].encode())
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().endswith(("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
                value = value.strip().strip('"').strip("'")
                if len(value) >= 8:
                    secrets.append(value.encode())
    manifest = []
    for path in paths:
        data = path.read_bytes()
        if any(value in data for value in secrets) or re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b", data):
            raise ValueError(f"疑似密钥出现在提交文件中：{path.relative_to(ROOT)}；已停止打包（不输出密钥）")
        manifest.append({"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(data).hexdigest()})
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, "material-schedule-agent/" + path.relative_to(ROOT).as_posix())
        archive.writestr("material-schedule-agent/MANIFEST.json", json.dumps(manifest, indent=2))
    print(json.dumps({"archive": str(output), "files": len(paths),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "material-schedule-agent-submission.zip")
    package(parser.parse_args().output.resolve())
