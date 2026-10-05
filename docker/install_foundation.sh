#!/bin/sh
# Install the foundation models' packages into the image's virtualenv, with
# CPU-only PyTorch, and bake in the Chronos-2 weights.
#
# PyPI's Linux PyTorch wheels pull several gigabytes of CUDA libraries that a
# CPU server never uses. So PyTorch comes from the CPU index at the version the
# lockfile pins, and every other package is installed at its locked version.
set -eu

TORCH_INDEX="${1:-https://download.pytorch.org/whl/cpu}"
PYTHON=/app/.venv/bin/python

uv export --frozen --no-dev --extra foundation --no-hashes --no-emit-project --no-header \
    > /tmp/foundation-all.txt
TORCH_VERSION="$(grep -E '^torch==' /tmp/foundation-all.txt | head -1 | sed -E 's/^torch==([^ ;]+).*/\1/')"
grep -vE '^(torch|triton|nvidia-|cuda-)' /tmp/foundation-all.txt > /tmp/foundation.txt

# Everything else at its locked version first, then PyTorch itself (its own
# dependencies are already in place, so nothing is taken from the CPU index but
# the PyTorch wheel).
uv pip install --python "$PYTHON" --no-deps -r /tmp/foundation.txt
uv pip install --python "$PYTHON" --no-deps --index-url "$TORCH_INDEX" "torch==${TORCH_VERSION}"
rm -f /tmp/foundation-all.txt /tmp/foundation.txt

# Download the weights now, so the running service never fetches them.
"$PYTHON" - <<'PY'
from vaticore.forecasting.foundation import CHRONOS_MODEL
from chronos import BaseChronosPipeline

BaseChronosPipeline.from_pretrained(CHRONOS_MODEL, device_map="cpu")
print(f"baked in {CHRONOS_MODEL}")
PY
