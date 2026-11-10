"""The release gate's task: the runtime imports the platform at the version being released."""
import sys
from importlib.metadata import version

import gcpbricks.mlflow  # noqa: F401 - the [ml] runtime a job's code relies on

print(f"mlops-gate {sys.argv[1:] or ['check']}: gcpbricks {version('gcpbricks')}")
