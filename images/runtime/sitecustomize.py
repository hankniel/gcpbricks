# Runs at every interpreter start in the runtime image. A pipeline step deploy gave
# MLOPS_BUNDLE_FILES imports the project's code from that deploy's files
# (gcpbricks.pipelines.use_bundle). Python reports, then ignores, an error raised
# here, so say plainly what failed: the step's own imports fail next.
import os

if os.environ.get("MLOPS_BUNDLE_FILES"):
    try:
        from gcpbricks.pipelines import use_bundle

        use_bundle()
    except Exception as e:  # noqa: BLE001
        import sys

        print(f"mlops: could not put the bundle's files on sys.path: {type(e).__name__}: {e}",
              file=sys.stderr)
