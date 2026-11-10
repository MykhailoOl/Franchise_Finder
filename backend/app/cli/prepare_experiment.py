import argparse
import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg

from app.core.config import BACKEND_DIRECTORY, Settings
from app.core.experiment import CONFIG_PATH, canonical_bytes, content_hash, load_experiment
from app.db.experiment_readiness import run_readiness

LOCAL_DIRECTORY = BACKEND_DIRECTORY.parent / ".local"


def producer_identity() -> dict:
    files = (
        "app/core/experiment.py",
        "app/db/experiment_readiness.py",
        "app/cli/prepare_experiment.py",
        "app/core/data_contract.py",
    )
    source_hashes = {
        name: hashlib.sha256((BACKEND_DIRECTORY / name).read_bytes()).hexdigest() for name in files
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        worktree_status = subprocess.run(
            ["git", "status", "--porcelain", "--", *files],
            cwd=BACKEND_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        commit = None
        worktree_status = None
    return {
        "source_sha256": source_hashes,
        "code_sha256": content_hash(source_hashes),
        "git_commit": commit,
        "producer_worktree_status": worktree_status,
        "python_version": platform.python_version(),
    }


def save_artifacts(
    output_root: Path, report: dict, candidates: list[dict], memberships: list[dict]
) -> Path:
    output_root = output_root.resolve()
    if not output_root.is_relative_to(LOCAL_DIRECTORY.resolve()):
        raise ValueError(
            "Experiment reports must remain inside the ignored project .local directory"
        )
    producer = producer_identity()
    identifier = "_".join(
        (report["config_sha256"][:16], report["dataset_sha256"][:16], producer["code_sha256"][:12])
    )
    destination = output_root / identifier
    if destination.exists():
        existing = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        if (
            existing["config_sha256"] != report["config_sha256"]
            or existing["dataset_sha256"] != report["dataset_sha256"]
            or existing["producer"]["code_sha256"] != producer["code_sha256"]
        ):
            raise ValueError("Existing immutable artifact identity does not match")
        for name, expected in existing["artifacts"].items():
            if hashlib.sha256((destination / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Existing immutable artifact has changed: {name}")
        return destination
    output_root.mkdir(parents=True, exist_ok=True)
    staging = output_root / (".pending-" + uuid4().hex)
    staging.mkdir(exist_ok=False)
    artifacts = {}
    for name, records in (("candidates.jsonl", candidates), ("memberships.jsonl", memberships)):
        digest = hashlib.sha256()
        with (staging / name).open("xb") as output:
            for row in records:
                line = canonical_bytes(row) + b"\n"
                output.write(line)
                digest.update(line)
        artifacts[name] = digest.hexdigest()
    report_payload = (
        canonical_bytes(
            {**report, "generated_at": datetime.now(UTC).isoformat(), "producer": producer}
        )
        + b"\n"
    )
    (staging / "readiness.json").write_bytes(report_payload)
    artifacts["readiness.json"] = hashlib.sha256(report_payload).hexdigest()
    manifest = {
        "config_sha256": report["config_sha256"],
        "dataset_sha256": report["dataset_sha256"],
        "producer": producer,
        "artifacts": artifacts,
    }
    (staging / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    staging.rename(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze a read-only presence experiment readiness snapshot"
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-root", type=Path, default=LOCAL_DIRECTORY / "experiments")
    arguments = parser.parse_args()
    try:
        definition = load_experiment(arguments.config)
        report, candidates, memberships = run_readiness(Settings(), definition)
        destination = save_artifacts(arguments.output_root, report, candidates, memberships)
    except (OSError, ValueError, psycopg.Error) as error:
        parser.exit(1, f"Experiment readiness failed: {error}\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "artifact_directory": str(destination),
                "category_support": [
                    {key: row[key] for key in ("pool", "category", "status")}
                    for row in report["summary"]["category_support"]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
