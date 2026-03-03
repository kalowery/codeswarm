#!/usr/bin/env python3
import sys
import time
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("Usage: outbox_follower.py <outbox_dir>", file=sys.stderr)
        sys.exit(1)

    outbox_dir = Path(sys.argv[1])
    archive_dir = outbox_dir.parent / "archive"
    offsets = {}

    def drain_path(path: Path, start_offset: int) -> int:
        """Stream unread bytes from a JSONL file and return the new offset."""
        with path.open("rb") as f:
            f.seek(start_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
            return f.tell()

    while True:
        try:
            files = sorted(outbox_dir.glob("*.jsonl"))
            current_files = set(files)

            # Files can be atomically moved to archive by the worker on shutdown.
            # Drain any unread tail from archive before forgetting offsets.
            for tracked in list(offsets.keys()):
                if tracked not in current_files:
                    archived = archive_dir / tracked.name
                    if archived.exists():
                        try:
                            drain_path(archived, offsets[tracked])
                        except FileNotFoundError:
                            # Best effort: if archive disappears concurrently, drop offset.
                            pass
                    del offsets[tracked]

            for path in files:
                if path not in offsets:
                    offsets[path] = 0

                offsets[path] = drain_path(path, offsets[path])

            time.sleep(0.5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Follower error: {e}", file=sys.stderr)
            time.sleep(1)


if __name__ == "__main__":
    main()
