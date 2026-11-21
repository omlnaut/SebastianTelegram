#!/usr/bin/env python3
"""Stand-in for the real `pi` executable in adapter tests.

Replays a fixture JSONL stream on stdout, writes stderr, records its argv and
environment, and exits with a chosen status. Everything is driven by
FAKE_PI_* environment variables so each test can describe one run.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    record = os.environ.get("FAKE_PI_RECORD")
    if record:
        Path(record).write_text(
            json.dumps(
                {
                    "argv": sys.argv,
                    "env": dict(os.environ),
                    "pgrp": os.getpgrp(),
                    "pid": os.getpid(),
                }
            )
        )

    fixture = os.environ.get("FAKE_PI_FIXTURE")
    if fixture:
        for line in Path(fixture).read_text().splitlines(keepends=True):
            sys.stdout.write(line)
            sys.stdout.flush()

    stderr = os.environ.get("FAKE_PI_STDERR")
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()

    child_pid_file = os.environ.get("FAKE_PI_CHILD_PID")
    if child_pid_file:
        # A tool subprocess: same process group, outliving this process unless
        # the group as a whole is signalled.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(child_pid_file).write_text(str(child.pid))

    delay = float(os.environ.get("FAKE_PI_SLEEP", "0"))
    if delay:
        time.sleep(delay)

    return int(os.environ.get("FAKE_PI_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
