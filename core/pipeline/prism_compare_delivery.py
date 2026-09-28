from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from core.backtest.prism_compare_engine import CELL_FILES
from core.pipeline.prism_compare_config import STRATEGIES, file_sha256, write_json
from core.pipeline.prism_compare_report import ROOT_FILES
from core.technical_v2.contracts import ContractError

MIB = 1024 * 1024


def _allowed_names() -> set[str]:
    names = set(ROOT_FILES) | {"artifact_manifest.json", "regression_evidence.json", "run_identity.json",
        "validation_complete.json", "test_started.json", "test_complete.json", "smoke_diagnostic.json"}
    names.update(f"{strategy}/{split}/{cost}/{name}" for strategy in STRATEGIES
                 for split in ("validation", "test") for cost in ("base", "stress")
                 for name in (*CELL_FILES, "cell_manifest.json"))
    return names


def _verify_files(directory: Path, records: dict) -> None:
    allowed = _allowed_names()
    for name, record in records.items():
        if name not in allowed:
            raise ContractError(f"file is outside the derived-result whitelist: {name}")
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ContractError("derived-result path is not an ordinary contained file")
        if path.stat().st_size != record["bytes"] or file_sha256(path) != record["sha256"]:
            raise ContractError(f"derived-result bytes/SHA256 mismatch: {name}")


def _verify_parts(path: Path, receipt: dict) -> list[Path]:
    if not 0 < receipt["part_bytes_limit"] <= 40 * MIB:
        raise ContractError("archive part limit must be at most40MiB")
    digest, size, paths = hashlib.sha256(), 0, []
    for record in receipt["parts"]:
        name = record["file"]
        if Path(name).name != name or name in {".", ".."}:
            raise ContractError("archive part filename is not contained")
        part = path.parent / name
        if part.is_symlink() or part.stat().st_size != record["bytes"] or record["bytes"] > receipt["part_bytes_limit"]:
            raise ContractError("archive part bytes/limit differs")
        part_digest = hashlib.sha256()
        with part.open("rb") as source:
            for chunk in iter(lambda: source.read(MIB), b""):
                digest.update(chunk)
                part_digest.update(chunk)
                size += len(chunk)
        if part_digest.hexdigest() != record["sha256"]:
            raise ContractError("archive part SHA256 differs")
        paths.append(part)
    if size != receipt["archive"]["bytes"] or digest.hexdigest() != receipt["archive"]["sha256"]:
        raise ContractError("reassembled archive bytes/SHA256 differs")
    return paths


def package(artifact_dir: str | Path, output_dir: str | Path, *, part_mib: int | None = None) -> dict:
    root, output = Path(artifact_dir).resolve(), Path(output_dir).resolve()
    manifest_path = root / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("raw_vendor_database_included") is not False:
        raise ContractError("raw vendor databases cannot be packaged")
    records = dict(manifest["files"])
    records["artifact_manifest.json"] = {"bytes": manifest_path.stat().st_size, "sha256": file_sha256(manifest_path)}
    _verify_files(root, records)
    config = json.loads((root / "parameters.json").read_text())
    maximum = config["delivery"]["max_archive_part_mib"]
    limit = (maximum if part_mib is None else part_mib) * MIB
    if not isinstance(limit, int) or not 0 < limit <= min(maximum, 40) * MIB:
        raise ContractError("archive part limit exceeds the configured maximum40MiB")
    receipt_path = output / "delivery_manifest.json"
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text())
        if previous["artifact_manifest_sha256"] != records["artifact_manifest.json"]["sha256"] or previous["part_bytes_limit"] != limit:
            raise ContractError("existing delivery belongs to different artifacts/part limit; preserve it and choose a fresh directory")
        _verify_parts(receipt_path, previous)
        return {"status": "PACKAGED", "manifest_path": str(receipt_path), "reused": True}
    if output.exists() and any(output.iterdir()):
        raise ContractError("incomplete delivery preserved; choose a fresh output directory")
    output.mkdir(parents=True, exist_ok=True)
    parts = []
    with tempfile.TemporaryDirectory(prefix="prism-package-", dir=output) as temporary:
        archive = Path(temporary) / "results.tar.gz"
        with archive.open("wb") as destination, gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            with tarfile.open(mode="w", fileobj=compressed) as bundle:
                for name, record in sorted(records.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = record["bytes"], 0o600, 0
                    with (root / name).open("rb") as source:
                        bundle.addfile(info, source)
        with archive.open("rb") as source:
            index = 1
            for chunk in iter(lambda: source.read(limit), b""):
                name = f"prism-v2-results.tar.gz.part{index:03d}"
                (output / name).write_bytes(chunk)
                parts.append({"file": name, "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
                index += 1
        receipt = {"schema_version": "prism-v2-delivery.v1", "status": "PACKAGED",
            "source_commit": manifest["source_commit"], "artifact_manifest_sha256": records["artifact_manifest.json"]["sha256"],
            "part_bytes_limit": limit, "archive": {"bytes": archive.stat().st_size, "sha256": file_sha256(archive)},
            "parts": parts, "files": records, "raw_vendor_database_included": False,
            "restore_command": "python -m scripts.package_prism_v2 restore --manifest PATH/delivery_manifest.json --output-dir NEW_EMPTY_DIRECTORY"}
        _verify_parts(receipt_path, receipt)
        write_json(receipt_path, receipt, immutable=True)
    return {"status": "PACKAGED", "manifest_path": str(receipt_path), "reused": False}


def restore(delivery_manifest: str | Path, output_dir: str | Path) -> dict:
    path, output = Path(delivery_manifest).resolve(), Path(output_dir).resolve()
    receipt = json.loads(path.read_text())
    if receipt["raw_vendor_database_included"] is not False:
        raise ContractError("raw vendor delivery cannot be restored by this tool")
    if not set(receipt["files"]).issubset(_allowed_names()):
        raise ContractError("delivery contains files outside the derived-result whitelist")
    parts = _verify_parts(path, receipt)
    if output.exists() and any(output.iterdir()):
        raise ContractError("restore target is not empty; do not overwrite existing files")
    with tempfile.TemporaryDirectory(prefix="prism-restore-", dir=path.parent) as temporary:
        archive, restored = Path(temporary) / "results.tar.gz", Path(temporary) / "restored"
        restored.mkdir()
        with archive.open("wb") as destination:
            for part in parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, destination, length=MIB)
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) != len(receipt["files"]) or {member.name for member in members} != set(receipt["files"]):
                raise ContractError("archive members differ from the hash-bound whitelist")
            for member in members:
                if not member.isfile():
                    raise ContractError("archive contains a non-file member")
                target = restored / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination, length=MIB)
        _verify_files(restored, receipt["files"])
        if file_sha256(restored / "artifact_manifest.json") != receipt["artifact_manifest_sha256"]:
            raise ContractError("restored artifact manifest SHA256 differs")
        output.mkdir(parents=True, exist_ok=True)
        for name in receipt["files"]:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(restored / name, target)
        _verify_files(output, receipt["files"])
    return {"status": "VERIFIED_RESTORED", "output_dir": str(output),
            "verified_file_count": len(receipt["files"]), "archive_sha256": receipt["archive"]["sha256"]}
