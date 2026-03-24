#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("Usage: outbox_follower.py <outbox_dir>", file=sys.stderr)
        sys.exit(1)

    outbox_dir = Path(sys.argv[1])
    archive_dir = outbox_dir.parent / "archive"
    offsets_path = outbox_dir.parent / ".outbox_follower_offsets.json"
    offsets = {}

    def load_offsets():
        if not offsets_path.exists():
            return {}
        try:
            data = json.loads(offsets_path.read_text())
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        normalized = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                normalized[key] = {
                    "offset": int(value.get("offset", 0)),
                    "inode": value.get("inode"),
                }
            except Exception:
                continue
        return normalized

    def save_offsets():
        tmp = offsets_path.with_suffix(offsets_path.suffix + ".tmp")
        serializable = {
            path.name: {
                "offset": int(meta.get("offset", 0)),
                "inode": meta.get("inode"),
            }
            for path, meta in offsets.items()
        }
        try:
            tmp.write_text(json.dumps(serializable))
            tmp.replace(offsets_path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    offsets = {
        outbox_dir / name: meta
        for name, meta in load_offsets().items()
    }

    def drain_path(path: Path, start_offset: int) -> int:
        """Stream unread bytes from a JSONL file and return the new offset."""
        with path.open("rb") as f:
            f.seek(start_offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                # Do not consume partial JSONL writes. If a writer has appended
                # bytes without a terminating newline yet, rewind and retry later.
                if not line.endswith(b"\n"):
                    f.seek(line_start)
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
                            drain_path(archived, int(offsets[tracked].get("offset", 0)))
                        except FileNotFoundError:
                            # Best effort: if archive disappears concurrently, drop offset.
                            pass
                    del offsets[tracked]
                    save_offsets()

            for path in files:
                stat = path.stat()
                inode = getattr(stat, "st_ino", None)
                size = int(stat.st_size)
                tracked = offsets.get(path)
                if tracked is None:
                    offsets[path] = {"offset": 0, "inode": inode}
                    tracked = offsets[path]
                else:
                    # If file rotated/truncated in-place, reset offset so new lines
                    # are not skipped forever (critical for approval events).
                    if (
                        tracked.get("inode") != inode
                        or int(tracked.get("offset", 0)) > size
                    ):
                        tracked["offset"] = 0
                    tracked["inode"] = inode

                tracked["offset"] = drain_path(path, int(tracked.get("offset", 0)))
                save_offsets()

            time.sleep(0.1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Follower error: {e}", file=sys.stderr)
            time.sleep(0.2)


if __name__ == "__main__":
    main()
