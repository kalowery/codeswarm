#!/usr/bin/env python3
import sys
import time
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("Usage: outbox_follower.py <outbox_dir>", file=sys.stderr)
        sys.exit(1)

    outbox_dir = Path(sys.argv[1])
    offsets = {}

    while True:
        try:
            files = sorted(outbox_dir.glob("*.jsonl"))
            current_files = set(files)

            # Remove offsets for files that no longer exist (e.g., archived)
            for tracked in list(offsets.keys()):
                if tracked not in current_files:
                    del offsets[tracked]

            for path in files:
                if path not in offsets:
                    offsets[path] = 0

                with path.open("rb") as f:
                    f.seek(offsets[path])
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        sys.stdout.buffer.write(line)
                        sys.stdout.flush()
                    offsets[path] = f.tell()

            time.sleep(0.5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Follower error: {e}", file=sys.stderr)
            time.sleep(1)


if __name__ == "__main__":
    main()
