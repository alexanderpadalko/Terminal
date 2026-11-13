"""Exercise to_thread and localhost DNS through the actual frozen Finam worker."""
import json
from pathlib import Path
import subprocess
import sys


exe = Path(sys.argv[1]).resolve()
output = Path("finam-runtime-exe-result.json").resolve()
subprocess.run([str(exe), "--check-finam-runtime", str(output)], check=True, timeout=30,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
result = json.loads(output.read_text(encoding="utf-8"))
assert result["ok"] and result["frozen"], result
print("PACKAGED_FINAM_RUNTIME_OK", json.dumps(result), flush=True)
