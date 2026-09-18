"""Local JSONL transport peer; it never imports an SDK or accesses the network."""

import json
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
starts = Path(sys.argv[2])
generation = int(starts.read_text()) + 1 if starts.exists() else 1
starts.write_text(str(generation))

for line in sys.stdin:
    message = json.loads(line)
    if generation == 1 and mode == "eof":
        sys.exit(1)
    if generation == 1 and mode == "timeout":
        # Outlast the real transport deadline, then attempt to send stale data.
        time.sleep(8)
    print(json.dumps({"status": "ok", "request": message["request"],
                      "timeout_ms": message["timeout_ms"], "pid": os.getpid(),
                      "generation": generation}), flush=True)
