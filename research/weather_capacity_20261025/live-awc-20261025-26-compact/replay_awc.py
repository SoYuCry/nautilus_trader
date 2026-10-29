"""Read the offline compact AWC bundle using only Python's standard library."""

import argparse
from collections.abc import Iterator
from decimal import Decimal
import hashlib
import json
from pathlib import Path

DAYS = ("2026-10-25", "2026-10-26")
BASE = Path(__file__).resolve().parent


def load_observations(directory: Path) -> list[str]:
    """Load exact JSON text fragments indexed by observation ID."""
    values = []
    with (directory / "observations.jsonl").open(encoding="utf-8") as file:
        for line in file:
            row = json.loads(line)
            if row["id"] != len(values) or not isinstance(row["json"], str):
                raise ValueError("Invalid observation dictionary")
            values.append(row["json"])
    return values


def iter_raw_lines(
    day: str,
    directory: Path = BASE,
    observations: list[str] | None = None,
) -> Iterator[bytes]:
    """Yield the exact original exported JSONL lines, including their line endings."""
    if day not in DAYS:
        raise ValueError("Unsupported day")
    if observations is None:
        observations = load_observations(directory)
    with (directory / f"events-{day}.jsonl").open(encoding="utf-8") as file:
        for line in file:
            event = json.loads(line)
            if "literal" in event:
                yield event["literal"].encode("utf-8")
                continue
            ids = event["row_ids"]
            if not ids or any(type(i) is not int or not 0 <= i < len(observations) for i in ids):
                raise ValueError("Invalid observation reference")
            values = [observations[i] for i in ids]
            if "separators" in event:
                separators = event["separators"]
                if len(separators) != len(values) - 1:
                    raise ValueError("Invalid separator list")
                parts = [values[0]]
                for separator, value in zip(separators, values[1:]):
                    parts.extend((separator, value))
                body = "".join(parts)
            else:
                body = event["separator"].join(values)
            yield (event["prefix"] + body + event["suffix"]).encode("utf-8")


def iter_events(day: str, directory: Path = BASE) -> Iterator[dict]:
    """Yield original export envelopes, preserving integers and decimal JSON values."""
    for line in iter_raw_lines(day, directory):
        yield json.loads(line, parse_float=Decimal)


def verify(directory: Path = BASE) -> dict:
    """Verify compact member hashes and byte-exact reconstruction of both originals."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files_sha256"].items():
        if Path(name).name != name:
            raise ValueError("Invalid member name")
        digest = hashlib.sha256()
        with (directory / name).open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"Compact file hash mismatch: {name}")
    observations = load_observations(directory)
    if len(observations) != manifest["unique_observations"]:
        raise ValueError("Observation count mismatch")
    checked = []
    for day in DAYS:
        digest = hashlib.sha256()
        size = count = 0
        for line in iter_raw_lines(day, directory, observations):
            digest.update(line)
            size += len(line)
            count += 1
        expected = manifest["original_exports"][day]
        if (digest.hexdigest(), size, count) != (
            expected["sha256"], expected["bytes"], expected["records"]
        ):
            raise ValueError(f"Original export reconstruction mismatch: {day}")
        checked.append({"day": day, "records": count, "sha256": digest.hexdigest()})
    return {"passed": True, "unique_observations": len(observations), "originals": checked}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Check hashes and reconstruction")
    parser.add_argument("--restore", type=Path, help="Write both original exports to this directory")
    args = parser.parse_args()
    result = verify()
    print(json.dumps(result, indent=2))
    if args.restore is not None:
        args.restore.mkdir(parents=True, exist_ok=True)
        targets = [args.restore / f"live-awc-{day}.jsonl" for day in DAYS]
        if any(path.exists() for path in targets):
            raise FileExistsError("Refusing to overwrite an existing restored export")
        observations = load_observations(BASE)
        for day, target in zip(DAYS, targets):
            with target.open("xb") as file:
                for line in iter_raw_lines(day, BASE, observations):
                    file.write(line)
            print(f"Restored: {target.resolve()}")


if __name__ == "__main__":
    main()
