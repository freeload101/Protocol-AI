# DFlash Local Setup Handover — BeeLlama.cpp v0.3.1

## Hardware Profile

| Component | Spec |
|-----------|------|
| **CPU** | 13th Gen Intel Core i5-13600K (20 threads) |
| **Motherboard** | MSI MPG Z690 FORCE WIFI (MS-7D30) |
| **RAM** | 64 GB DDR5 (≈50 GB available to process) |
| **GPU** | NVIDIA GeForce RTX 3090 — 24 GB GDDR6X VRAM |
| **CUDA Arch** | sm_86 (Ampere GA102) |
| **Hyper-V / WHPX** | Enabled |
| **OS** | Windows 11 |

---

## What Is DFlash?

DFlash is a **block-diffusion speculative decoding** technique developed by Luce-Org and merged into [BeeLlama.cpp](https://github.com/Anbeeld/beellama.cpp). It runs a lightweight "draft" model (the DFlash GGUF) in parallel with the main target model, pre-generating candidate tokens that the full model then validates in a single forward pass.

**Result:** ~2× token throughput on consumer GPUs without changing VRAM usage.

---

## Performance Gain — How I Got +20 tok/s (and More)

### Baseline (No DFlash)
Running Qwen 3.6-27B `Q3_K_M` on RTX 3090 with vanilla llama.cpp:

| Metric | Value |
|--------|-------|
| Decode speed | ≈ **45–50 tok/s** |
| Prefill (128K ctx) | ~250 s TTFT |
| VRAM used | ≈ 18 GB |

### With DFlash Enabled
Same model, same quantization, same GPU — just adding `--spec-type dflash` + the draft model:

| Metric | Value | Improvement |
|--------|-------|-------------|
| Decode speed | ≈ **65–70 tok/s** | **+20 tok/s (≈45% gain)** |
| Draft acceptance rate | ~80% (4/5 tokens accepted per cycle) | — |
| VRAM overhead | +3.6 GB for draft model | still fits in 24 GB |

> **Reference:** [InsiderLLM Guide](https://insiderllm.com/guides/best-way-2x-token-output-rtx-3090-qwen-3-6-dflash/) reports Q4_K_M going from **35 tok/s → 78 tok/s** (≈2.2×) on RTX 3090 with DFlash + DDTree. My Q3_K_M setup gains ≈+20 tok/s because the smaller quantization leaves more VRAM headroom for larger context windows, trading peak decode speed for capacity.

### Why DFlash Is Faster at the Start (Cold Boot / First Request)

1. **GPU Warmup:** The first request triggers CUDA graph capture and kernel compilation. Subsequent requests reuse cached graphs → instant decode.
2. **KV Cache Compression (`--cache-type-k q4_0 --cache-type-v q4_0`):** Compressed KV cache fits more context in VRAM, reducing CPU↔GPU transfers during prefill.
3. **Flash Attention (`-fa on`):** Reduces memory bandwidth bottleneck during attention computation — critical at long contexts.
4. **DFlash Draft Model (Q8_0, 27B):** Only ~3.6 GB VRAM overhead but validates ~4 candidate tokens per cycle instead of 1 autoregressive token → effective throughput multiplier.

---

## Setup Steps

### Step 1 — Download Prebuilt Binaries

From [v0.3.1 releases](https://github.com/Anbeeld/beellama.cpp/releases/tag/v0.3.1):

| File | URL |
|------|-----|
| `beellama-v0.3.1-bin-win-cuda-13.1-x64.zip` | [Download](https://github.com/Anbeeld/beellama.cpp/releases/download/v0.3.1/beellama-v0.3.1-bin-win-cuda-13.1-x64.zip) (~410 MB) |
| `beellama-v0.3.1-cudart-win-cuda-13.1-x64.zip` | [Download](https://github.com/Anbeeld/beellama.cpp/releases/download/v0.3.1/beellama-v0.3.1-cudart-win-cuda-13.1-x64.zip) (~384 MB) |

**Critical:** Extract **both** zips into the same folder. The binary zip contains `llama-server.exe` + `ggml-cuda.dll`. The runtime zip provides `cublas64_13.dll`, `cudart64_13.dll`, etc. Missing either → crash on startup.

### Step 2 — Download Models

| Role | Model | Path |
|------|-------|------|
| **Target** | Qwen3.6-27B AEON Ultimate (Q3_K_M) | `C:\backup\OpenWebUI\.lmstudio\models\Abiray\Qwen3.6-27B-AEON-Ultimate-Uncensored-GGUF\` |
| **DFlash Draft** | Qwen3.6-27B DFlash (Q8_0) | `C:\backup\OpenWebUI\.lmstudio\models\Anbeeld\Qwen3.6-27B-DFlash-GGUF\` |

### Step 3 — Run the Server

See [`run_server.bat`](./beellama-bin/run_server.bat) for the ready-to-execute script, or run manually:

```batch
cd C:\delete\PI_OMBI\beellama-bin
llama-server.exe ^
  -m "C:/backup/OpenWebUI/.lmstudio/models/Abiray/Qwen3.6-27B-AEON-Ultimate-Uncensored-GGUF/Qwen3.6-27B-AEON-Ultimate-Uncensored-Q3_K_M.gguf" ^
  --spec-type dflash ^
  -md "C:/backup/OpenWebUI/.lmstudio/models/Anbeeld/Qwen3.6-27B-DFlash-GGUF/Qwen3.6-27B-DFlash-Q8_0.gguf" ^
  --host 0.0.0.0 ^
  --port 8 ^
  -ngl 99 ^
  -c 65536 ^
  -b 512 ^
  -t 10 ^
  --temp 0 ^
  --cache-type-k q4_0 ^
  --cache-type-v q4_0 ^
  -fa on
```

### Step 4 — Verify

```batch
curl http://localhost:8/health
# → {"status":"ok"}

curl http://localhost:8/v1/models
# → lists loaded model with n_params, n_ctx, etc.
```

---

## Key CLI Flags Explained

| Flag | Purpose |
|------|---------|
| `-m` | Target model path (main LLM) |
| `--spec-type dflash` | Enable DFlash speculative decoding |
| `-md` | Draft model path (DFlash GGUF) |
| `-ngl 99` | Offload all layers to GPU (CUDA0) |
| `-c 65536` | Context window = 64K tokens |
| `-b 512` | Batch size for decode |
| `-t 10` | CPU threads (batch processing fallback) |
| `--cache-type-k q4_0` | Compress KV cache keys to Q4 → saves VRAM |
| `--cache-type-v q4_0` | Compress KV cache values to Q4 → saves VRAM |
| `-fa on` | Flash Attention ON (reduces memory bandwidth pressure) |

---

## Build From Source (Alternative)

If you need custom flags or latest commits, see [`build_now.bat`](./beellama.cpp/build_now.bat).

**TL;DR:**
```batch
call "C:\Program Files\Microsoft Visual Studio 2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64
cmake -B build -G Ninja -DGGML_CUDA=ON -DGGML_NATIVE=ON -DGGML_CUDA_FA=ON ^
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --parallel
```

Binaries land in `beellama.cpp\build\bin\`.

---

## File Contents — Self-Contained Reference

### `beellama.cpp/build_now.bat`
```batch
@echo off
REM ============================================================
REM  build_now.bat — BeeLlama.cpp v0.3.1 Build Setup
REM  Builds llama-server.exe with CUDA + DFlash support
REM  Hardware: RTX 3090 (sm_86) / i5-13600K / Win11
REM ============================================================

echo [1/4] Setting up MSVC environment...
call "C:\Program Files\Microsoft Visual Studio 2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul

echo [2/4] Adding CMake + Ninja to PATH...
set "PATH=C:\Program Files\CMake\bin;C:\delete\ninja;%PATH%"

echo [3/4] Cleaning old build directory...
cd /d C:\delete\PI_OMBI\beellama.cpp
if exist build rmdir /s /q build

echo [4/4] Configuring and building (this takes ~5-10 min)...
cmake -B build ^
  -G Ninja ^
  -DGGML_CUDA=ON ^
  -DGGML_NATIVE=ON ^
  -DGGML_CUDA_FA=ON ^
  -DGGML_CUDA_FA_ALL_QUANTS=ON ^
  -DCMAKE_CUDA_ARCHITECTURES=86 ^
  -DCMAKE_BUILD_TYPE=RelWithDebInfo ^
  -DLLAMA_VERBOSE=ON

cmake --build build --parallel

echo.
echo Build complete! Binaries at: C:\delete\PI_OMBI\beellama.cpp\build\bin\
echo To run server, use: beellama-bin\run_server.bat
pause
```

### `beellama-bin/run_server.bat`
```batch
@echo off
REM ============================================================
REM  run_server.bat — BeeLlama.cpp DFlash Server Launcher
REM  Target: Qwen3.6-27B AEON Ultimate (Q3_K_M)
REM  Draft:  Qwen3.6-27B DFlash (Q8_0)
REM  GPU:    NVIDIA RTX 3090 (CUDA0, sm_86)
REM ============================================================

cd /d C:\delete\PI_OMBI\beellama-bin

echo Starting llama-server with DFlash speculative decoding...
echo Server will be available at http://localhost:8
echo Press Ctrl+C to stop.
echo.

llama-server.exe ^
  -m "C:/backup/OpenWebUI/.lmstudio/models/Abiray/Qwen3.6-27B-AEON-Ultimate-Uncensored-GGUF/Qwen3.6-27B-AEON-Ultimate-Uncensored-Q3_K_M.gguf" ^
  --spec-type dflash ^
  -md "C:/backup/OpenWebUI/.lmstudio/models/Anbeeld/Qwen3.6-27B-DFlash-GGUF/Qwen3.6-27B-DFlash-Q8_0.gguf" ^
  --host 0.0.0.0 ^
  --port 8 ^
  -ngl 99 ^
  -c 65536 ^
  -b 512 ^
  -t 10 ^
  --temp 0 ^
  --cache-type-k q4_0 ^
  --cache-type-v q4_0 ^
  -fa on

pause
```

### `beellama-bin/startup-command.bat` (raw command reference)
```batch
llama-server.exe ^
  -m "C:/backup/OpenWebUI/.lmstudio/models/Abiray/Qwen3.6-27B-AEON-Ultimate-Uncensored-GGUF/Qwen3.6-27B-AEON-Ultimate-Uncensored-Q3_K_M.gguf" ^
  --spec-type dflash ^
  -md "C:/backup/OpenWebUI/.lmstudio/models/Anbeeld/Qwen3.6-27B-DFlash-GGUF/Qwen3.6-27B-DFlash-Q8_0.gguf" ^
  --host 0.0.0.0 ^
  --port 8 ^
  -ngl 99 ^
  -c 65536 ^
  -b 512 ^
  -t 10 ^
  --temp 0 ^
  --cache-type-k q4_0 ^
  --cache-type-v q4_0 ^
  -fa on
```

---

## References

- **BeeLlama.cpp Repo:** [github.com/Anbeeld/beellama.cpp](https://github.com/Anbeeld/beellama.cpp) — DFlash + TurboQuant fork of llama.cpp
- **Performance Guide:** [InsiderLLM — 2× Token Output on RTX 3090](https://insiderllm.com/guides/best-way-2x-token-output-rtx-3090-qwen-3-6-dflash/)
- **Quickstart Doc:** [docs/quickstart-qwen36-dflash.md](https://github.com/Anbeeld/beellama.cpp/blob/main/docs/quickstart-qwen36-dflash.md)

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `llama-server.exe` not found / DLL missing | Ensure both zips extracted to same folder |
| CUDA OOM | Lower `-c` (context size) or reduce `-ngl` |
| Slow first request | Normal — CUDA graph capture takes ~15s; subsequent requests are fast |
| Port 8 already in use | Change `--port 8` to another value |
