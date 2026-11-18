"""images/runtime/Dockerfile: a GPU machine's driver is on the loader path, or every CUDA wheel
in an environment built on it finds no driver and its job runs on the CPU, attached GPU idle."""
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "images" / "runtime" / "Dockerfile"


def test_the_runtime_puts_vertex_gpu_driver_on_the_loader_path():
    text = DOCKERFILE.read_text()
    env = text[text.index("ENV LD_LIBRARY_PATH="):].split("\n\n")[0]
    assert "/usr/local/nvidia/lib64" in env and "/usr/local/nvidia/bin" in env
