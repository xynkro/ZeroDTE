#!/bin/zsh
# ZeroDTE backend launcher — fixes numpy OpenBLAS hang on macOS.

export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export PYTHONDONTWRITEBYTECODE=1

cd /Users/xynkro/Documents/Trading/ZeroDTE

# Wait for port 8765 to be free
for i in {1..10}; do
  if ! lsof -ti:8765 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

exec /Users/xynkro/Documents/Trading/ZeroDTE/.venv/bin/python \
  -m uvicorn backend.app.api:app \
  --host 0.0.0.0 \
  --port 8765
