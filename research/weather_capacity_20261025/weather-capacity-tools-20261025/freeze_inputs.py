"""Bind a completed collector handoff to this machine's file identities."""

import argparse
import json
from pathlib import Path

from inspect_transfer import ROOT
from inspect_transfer import file_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--completion-note", required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT / "tmp") or args.output.exists():
        raise ValueError("Use a new contract file inside the extracted package tmp/")
    value = json.loads(args.template.read_text())
    paths = value.pop("source_paths")
    if not paths or any(not Path(p).is_absolute() for p in paths):
        raise ValueError(
            "List every consumed source/metadata/log as an absolute source_paths entry"
        )
    value.update(
        schema="temporary_weather_frozen_input/v1",
        transfer_status="completed_and_frozen",
        completion_confirmation=args.completion_note,
        source_files=[{"path": p, "identity": file_identity(Path(p))} for p in sorted(set(paths))],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as out:
        json.dump(value, out, indent=2)
        out.write("\n")
    print(args.output)


if __name__ == "__main__":
    main()
