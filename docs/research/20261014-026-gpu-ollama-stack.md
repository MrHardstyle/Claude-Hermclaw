# 20261014-026 – GPU stack on .224: NVIDIA driver on Debian 13, Ollama release and Pascal support

## Question
1. Which Ollama release is current (2026-10-14), does it still run on Pascal / compute capability 6.1
   (GTX 1080 8 GB), which CUDA runner libraries does the Linux build ship, which driver does it need,
   is Pascal deprecated, and which minimum Ollama version do `qwen3.8` and `gemma4` need?
2. Which NVIDIA driver can Debian 13 "trixie" install for a GTX 1080 (trixie main, trixie-backports,
   NVIDIA's own CUDA repository `debian13`)? Is 580 the last branch with Pascal? Exact packages, apt
   components, Secure Boot/DKMS, pinning against a Pascal-less branch.
3. Ollama Linux install methods (install script vs. pinned release archive with checksum), systemd unit
   and the server environment variables for one 8 GB GPU with RAM offload.
4. Re-check of the six model tags (size, minimum Ollama version) and RAM+VRAM needs with partial offload.
5. ffmpeg NVENC on Pascal with Debian's own `ffmpeg` package.

## Why needed
P32 (deployment playbook roles `nvidia` and `ollama`), INSTALLATION.md (model worker `.224`), runbook
`docs/runbooks/upgrade.md`. The roles were written from research R-007/R-017 (2026-10-08); this
re-check found that two of their assumptions no longer hold on Debian 13 (see Decision).

## Sources (accessed 2026-10-14)
Access notes: `ollama.com`, `registry.ollama.ai`, `*.debian.org` (packages, tracker, wiki, deb mirrors,
qa/madison, salsa), `docs.nvidia.com`, `developer.nvidia.com`, `download.nvidia.com` and the NVIDIA forums
were **not reachable** from this build sandbox (DNS failure or egress-policy 403). GitHub (web pages,
`raw.githubusercontent.com`, release downloads) was reachable; everything about Ollama below was read
directly from the official repository at tag `v0.40.4` or from its GitHub release pages. Debian/NVIDIA
facts come from search-engine snippets of the primary pages unless stated otherwise.

Primary (read directly):
- [P] Ollama releases (list, v0.40.4, v0.40.0, v0.35.x, v0.34.x, v0.33.x, v0.32.12, v0.30.0, v0.30.3, v0.20.0):
  https://github.com/ollama/ollama/releases , https://github.com/ollama/ollama/releases/tag/v0.40.4 ,
  asset list https://github.com/ollama/ollama/releases/expanded_assets/v0.40.4
- [P] Release checksums: https://github.com/ollama/ollama/releases/download/v0.40.4/sha256sum.txt (also v0.40.3, v0.32.12)
- [P] Release archive `ollama-linux-amd64.tar.zst` v0.40.4, downloaded and stream-listed in the sandbox
  (sha256 recomputed, contents listed – nothing executed)
- [P] Ollama docs at v0.40.4: https://github.com/ollama/ollama/blob/v0.40.4/docs/gpu.mdx ,
  `docs/linux.mdx`, `docs/faq.mdx`, `docs/context-length.mdx`; older `docs/gpu.mdx` at v0.20.0, v0.22.0, v0.30.0, v0.32.12
- [P] Ollama source at v0.40.4: `scripts/install.sh`, `envconfig/config.go`, `discover/cuda_compat.go`,
  `discover/runner.go`, `discover/types.go`, `ml/device.go`, `ml/path.go`, `llm/server.go`,
  `llm/llama_server.go`, `server/sched.go`, `Dockerfile`, `scripts/build_linux.sh`,
  `llama/server/CMakePresets.json`, `LLAMA_CPP_VERSION` (https://github.com/ollama/ollama/tree/v0.40.4)
- [P] llama.cpp `ggml/src/ggml-cuda/fattn.cu` at `b11521` (the revision pinned by Ollama v0.40.4):
  https://github.com/ggml-org/llama.cpp/blob/b11521/ggml/src/ggml-cuda/fattn.cu
- [P] Ollama issue #18825 "Failed to pull `embeddinggemma-2:740m` on linux" (opened 2026-10-06, open):
  https://github.com/ollama/ollama/issues/18825 ; PR #18820 "model: add multimodal embeddings" (MLX runner):
  https://github.com/ollama/ollama/pull/18820
- [P] NVIDIA cuda-repo-management issue #38 "Debian 13 Repository Missing Older Drivers" (opened 2026-02-01,
  open, last comment 2026-10-14) and #40 (duplicate): https://github.com/NVIDIA/cuda-repo-management/issues/38 ,
  https://github.com/NVIDIA/cuda-repo-management/issues/40
- [P] NVIDIA open-gpu-kernel-modules tags (driver version/date list): https://github.com/NVIDIA/open-gpu-kernel-modules/tags
- [P] Debian ffmpeg changelog (`/usr/share/doc/ffmpeg/changelog.Debian.gz` of the Ubuntu 24.04 build host,
  which carries the Debian entries): "ffmpeg (7:4.4.1-2) unstable – Add Build-Depends on libffmpeg-nvenc-dev
  on amd64, arm64 and i386 (Closes: #925941)"; `ffmpeg -encoders` of that Debian-derived build lists
  `h264_nvenc`, `hevc_nvenc`, `av1_nvenc`
- [P] Launchpad mirror of the Debian sid changelog of `nvidia-graphics-drivers`:
  https://launchpad.net/debian/sid/+source/nvidia-graphics-drivers/+changelog

Primary pages seen only through search snippets (not fetchable from here):
- [P/snippet] packages.debian.org: `libnvidia-encode1` 550.163.01-2 (trixie, non-free); source
  `nvidia-graphics-drivers` 550.163.01-2 (trixie); `nvidia-driver` 550.163.01-4~bpo13+1 (trixie-backports);
  `libffmpeg-nvenc-dev` 12.1.14.0-1 (trixie); ffmpeg source 7.1.x in trixie:
  https://packages.debian.org/trixie/libnvidia-encode1 , https://packages.debian.org/trixie-backports/nvidia-driver ,
  https://packages.debian.org/trixie/libffmpeg-nvenc-dev , https://packages.debian.org/source/stable/ffmpeg
- [P/snippet] tracker.debian.org news "Accepted ffmpeg 7:7.1.5-0+deb13u1 into stable-security" (2026-06-22):
  https://tracker.debian.org/news/1766379/accepted-ffmpeg-7715-0deb13u1-source-into-stable-security/
- [P/snippet] Debian wiki NvidiaGraphicsDrivers (trixie: 550 series, backports note, kernel coupling):
  https://wiki.debian.org/NvidiaGraphicsDrivers
- [P/snippet] NVIDIA data-center release notes 580.173.02: https://docs.nvidia.com/datacenter/tesla/tesla-release-notes-580-173-02/index.html
- [P/snippet] NVIDIA CUDA Features Archive (Maxwell/Pascal/Volta removed from offline compilation in CUDA 13.0):
  https://docs.nvidia.com/cuda/cuda-features-archive/index.html
- [P/snippet] NVIDIA driver installation guide, kernel modules (proprietary flavour required for Maxwell/Pascal/Volta):
  https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/kernel-modules.html
- [P/snippet] Arch Linux news "NVIDIA 590 driver drops Pascal support":
  https://archlinux.org/news/nvidia-590-driver-drops-pascal-support-main-packages-switch-to-open-kernel-modules/

Secondary (marked as such wherever used):
- [S] Phoronix "NVIDIA Confirms 580 Linux Driver Is The Last For Maxwell / Pascal / Volta":
  https://www.phoronix.com/news/NVIDIA-580-Linux-Driver-Last-HW
- [S] GamingOnLinux / 9to5Linux on 580.173.02 (June 2026):
  https://www.gamingonlinux.com/2026/06/nvidia-580-173-02-released-for-linux-for-those-on-older-gpus/ ,
  https://9to5linux.com/nvidia-580-173-02-linux-graphics-driver-released-for-geforce-10-series
- [S] Guru3D (580 security updates "through 2028"): https://www.guru3d.com/story/nvidia-ends-linux-driver-support-for-gtx-900-and-10-series-gpus/
- [S] Proxmox forum "NVIDIA 580.x + Kernel 7.0 incompatibility on Trixie":
  https://forum.proxmox.com/threads/nvidia-580-x-kernel-7-0-incompatibility-on-trixie.183421/
- [S] FFmpeg-cvslog "avcodec/nvenc: update minimum driver version list" (API 12.1 → Linux 530.41.03, 12.2 → 550.54.14):
  https://ffmpeg.org/pipermail/ffmpeg-cvslog/2024-March/141811.html ; Gentoo ebuild nv-codec-headers 12.1.14.0
- [S] Ollama library pages via search index (sizes): https://ollama.com/library/gemma4/tags ,
  https://ollama.com/library/gemma4:12b , https://ollama.com/library/embeddinggemma-2:740m ;
  OrcaRouter blog on `qwen3.8:27b` sizes https://www.orcarouter.ai/blog/qwen-3-8-27b-ollama ;
  agenticwire / localllm.in on `qwen3:8b`; datallmlab / morphllm on `qwen3-coder:30b`
- [S] Wikipedia NVENC (Pascal: no HEVC B-frames): https://en.wikipedia.org/wiki/NVENC

## Relevant facts

### Ollama release and Pascal support

| # | Fact | Source |
|---|---|---|
| O1 | Latest release **v0.40.4**, published 2026-10-12 19:00, marked "Latest"; previous v0.40.3 (2026-10-11), v0.40.2 (10-08), v0.40.1 (10-07), v0.40.0 (09-25). | [P] GitHub releases |
| O2 | Linux amd64 asset is **`ollama-linux-amd64.tar.zst`** (1,436,408,810 bytes); `ollama-linux-amd64.tgz` no longer exists for v0.40.4 (HTTP 404). Separate `-rocm` and `-mlx` archives. `sha256sum.txt` lists `15f31e9576d15a481e099b4145dba6e9f8c1f295927d6b8895300ae30ed3740a  ./ollama-linux-amd64.tar.zst`; recomputed in the sandbox: **identical**. v0.40.3: `ce6f8b671a699bf1ebafb28d60dbbabf4f37b30b499145fa308a8ba72de021d6` (from sha256sum.txt, not recomputed). | [P] release assets, sha256sum.txt, own download |
| O3 | Archive layout: `bin/ollama`, `lib/ollama/{cuda_v12,cuda_v13,vulkan}`, CPU variants `libggml-cpu-*.so`, `llama-server`. `cuda_v12` = CUDA runtime **12.8.90**, cuBLAS 12.8.5.5 (1.20 GiB); `cuda_v13` = CUDA 13.0.96 / cuBLAS 13.1.1.3. No MLX in the base archive. Extracted 2.10 GiB. | [P] own tar listing |
| O4 | Build presets v0.40.4 (identical in v0.32.12 and v0.40.3): `llama_cuda_v12_linux` `CMAKE_CUDA_ARCHITECTURES=50-virtual;52-virtual;60;61;70;75;80;86;89;90;90a;100;120` (native **sm_61** cubins); `llama_cuda_v13_linux` starts at `75-virtual` (no Pascal). Dockerfile: `CUDA12VERSION=12.8`, `CUDA13VERSION=13.0`. | [P] `llama/server/CMakePresets.json`, `Dockerfile` |
| O5 | GTX 1080 therefore runs on the **cuda_v12** runner only. CUDA 13.0 removed offline compilation/library support for Maxwell/Pascal/Volta. | [P] O4; [P/snippet] CUDA Features Archive |
| O6 | Docs v0.40.4: "Ollama supports Nvidia GPUs with compute capability 5.0+ and driver version 550 and newer. **Nvidia GPUs with compute capability 5.0 through 6.2 require driver version 570 or newer.**" GTX 1080 listed under CC 6.1. The 570 sentence first appears in `docs/gpu.mdx` at v0.30.0 (absent at v0.22.0, which said "531 and newer"). | [P] docs/gpu.mdx at v0.20.0/v0.22.0/v0.30.0/v0.32.12/v0.40.4 |
| O7 | Enforced in code: `discover/cuda_compat.go` `filterOldCUDADriver` drops every CUDA device with compute major < 7 from the cuda_v12 runner when the NVIDIA kernel driver branch is < 570 and the runtime is CUDA ≥ 12.8 (log: `NVIDIA driver too old … required_driver="570 or newer"`). Result with a 550 driver: **GTX 1080 is not used, inference falls back to CPU**. | [P] cuda_compat.go v0.40.4 (identical to v0.40.3) |
| O8 | No Pascal deprecation notice in docs or release notes v0.32.12…v0.40.4; risk lies in a future removal of the cuda_v12 runner (cf. O5). | [P] releases pages 1–3, docs |
| O9 | All GGUF models are served via the bundled upstream `llama-server` subprocess (llama.cpp **b11521** in v0.40.4, b11351 in v0.40.3). MLX models use a separate MLX runner whose Linux build is `mlx_cuda_v13` (CUDA 13 + cuDNN 9) → **MLX-only models cannot run on Pascal**. | [P] `llm/server.go`, `Dockerfile`, `LLAMA_CPP_VERSION` |
| O10 | Minimum versions: `gemma4:26b` (and e2b/e4b/31b) added in **v0.20.0** (2026-04-02); `gemma4:12b` added in **v0.30.3** (2026-06-03); `qwen3.8:27b` added in **v0.32.12** (2026-08-14, "This release adds the support of Qwen 3.8 27B", `ollama run qwen3.8:27b` and `…:27b-mlx`). Hermclaw minimum therefore **≥ 0.32.12**, which already needs driver ≥ 570 on Pascal (O6). | [P] release notes |
| O11 | `embeddinggemma-2` is implemented **only on the MLX runner** (PR #18820, merged 2026-10-06). Issue #18825 (Linux, v0.40.0): `Error: this model requires MLX support, but the MLX runtime is not available`; maintainer (pdevine, 2026-10-06): "not yet available on Linux and Windows. We'll add support soon." Still open; reports for `:270m`, `:740m-bf16`, `…-mxfp8-text`, `…-nvfp4-text`. v0.40.4 "Multimodal embeddings are supported through llama-server" does not close the issue. Even with the `-mlx` archive the MLX runner is CUDA 13 → no Pascal (O9). | [P] PR #18820, issue #18825, v0.40.4 notes |

### NVIDIA driver on Debian 13

| # | Fact | Source |
|---|---|---|
| N1 | R580 is the last driver branch for Maxwell, Pascal and Volta; 590 and later dropped them (Arch: "with the update to driver version 590, the NVIDIA driver no longer supports Pascal (GTX 10xx)"). Newer branches on the market: 595.x, 610.x, 615.x (615.78.08 tagged 2026-10-07). | [S] Phoronix; [P/snippet] Arch news; [P] open-gpu-kernel-modules tags |
| N2 | Latest 580 releases: **580.173.02** (tag 2026-06-25; GeForce legacy bug-fix release for GTX 900/10 series) and 580.178.04 (tag 2026-08-03; links to data-center release notes). No 580 release after 2026-08-03 found. | [P] open-gpu-kernel-modules tags; [S] GamingOnLinux/9to5Linux |
| N3 | NVIDIA states the 580 branch continues to get new-kernel support and critical fixes during its support period; "security updates through 2028" only from secondary sources. | [S] Guru3D, 9to5Linux; issue #38 comment |
| N4 | Open kernel modules need GSP (Turing+); **Pascal requires the proprietary module flavour**. | [P/snippet] NVIDIA kernel-modules guide |
| N5 | **Debian trixie main** ships `nvidia-graphics-drivers` **550.163.01-2** (e.g. `libnvidia-encode1` 550.163.01-2, non-free). **trixie-backports**: `nvidia-driver` 550.163.01-4~bpo13+1. **sid**: 550.163.01-5.1 (2026-06-11, NMU for Linux 7.0). No 580 upload to Debian found (sid changelog shows 580/595 only as sources of back-ported kernel-compat patches). | [P/snippet] packages.debian.org; [P] Launchpad mirror of the sid changelog |
| N6 | **NVIDIA's CUDA repo `debian13` has no 580 packages** – only 590+ (Pascal-less). Issue #38 (open since 2026-02-01): NVIDIA maintainer 2026-03-09 "Still no update on getting 580 packages", 2026-04-17 a trixie-specific 580 build "is still waiting for momentum"; last user comment 2026-10-14 expects no fix. The `debian12` repo has 580 but is SHA-1 signed, which trixie's apt (sqv) rejects since 2026-02-01. | [P] cuda-repo-management #38/#40 |
| N7 | Consequence: **no apt source provides a driver ≥ 570 for Pascal on trixie**. Debian's 550 is too old for Ollama ≥ 0.30 (O6/O7); NVIDIA's apt repo has no Pascal-capable branch. The only supported way to a 580 driver is NVIDIA's `.run` installer (with DKMS). | N5, N6, O7 |
| N8 | Kernel coupling: vanilla drivers < 580 do not build on kernels ≥ 6.16 (Debian patches 550 for that); 580.x reportedly failed against Linux 7.0 (Proxmox kernel) – trixie's stock kernel is 6.12 LTS. Do not use backports kernels on .224. | [P/snippet] Debian wiki; [S] NVIDIA forum, Proxmox forum |
| N9 | Debian package names (for reference / a future Debian 580 package): `nvidia-driver` (full stack incl. X), headless subset `nvidia-kernel-dkms`, `nvidia-smi`, `libcuda1`, `libnvidia-encode1`, `libnvcuvid1`, `nvidia-persistenced` (contrib); firmware `firmware-misc-nonfree` (non-free-firmware; not needed by the proprietary driver on Pascal); headers `linux-headers-amd64`; components `contrib non-free non-free-firmware`. | [P/snippet] packages.debian.org; Debian wiki |

### Ollama server configuration (v0.40.4 source)

| # | Fact | Source |
|---|---|---|
| E1 | `OLLAMA_HOST` default `127.0.0.1:11434`. | [P] envconfig |
| E2 | `OLLAMA_KEEP_ALIVE` default 5m; negative = forever; API `keep_alive` overrides it; `keep_alive: 0` unloads. | [P] envconfig, faq |
| E3 | `OLLAMA_MAX_LOADED_MODELS` default 0 = automatic (3 × GPUs); `OLLAMA_NUM_PARALLEL` default 1 (memory scales with NUM_PARALLEL × context); `OLLAMA_MAX_QUEUE` 512. With GPU inference **an additional model is loaded concurrently only if it fits with the loaded ones, otherwise one is evicted** ("new models must be able to completely fit in VRAM"). | [P] envconfig, faq, `server/sched.go` |
| E4 | `OLLAMA_CONTEXT_LENGTH` default 0 → 4k context below 24 GiB VRAM (32k at 24–48 GiB, 256k ≥ 48 GiB); per-request `num_ctx` overrides; `ollama ps` shows the allocated CONTEXT and the CPU/GPU split. | [P] envconfig, docs/context-length.mdx |
| E5 | `OLLAMA_FLASH_ATTENTION` is tri-state: unset → automatic; `llama-server` gets `--flash-attn auto` when all devices "support" FA, `on`/`off` only if the variable is set. `cudaFlashAttentionSupported` = CUDA, compute ≥ 6 (not 7.2), CUDA driver major ≥ 7 → **GTX 1080 counts as FA-capable, so leaving the variable unset means FA auto/on, not off.** | [P] `llm/llama_server.go`, `ml/device.go` |
| E6 | llama.cpp b11521: without tensor cores (Pascal) FA uses the generic **TILE** kernel (prompt processing) or the **VEC** kernel (decode, also with quantized K/V); TILE converts quantized K/V to f16 temporarily. | [P] `fattn.cu` b11521 |
| E7 | `OLLAMA_KV_CACHE_TYPE` (`f16` default, `q8_0` ≈ ½, `q4_0` ≈ ¼); global for all models; only effective with flash attention; passed as `--cache-type-k/--cache-type-v`. | [P] faq, `llm/llama_server.go` |
| E8 | `OLLAMA_MODELS` default `$HOME/.ollama/models` (official installer: user home `/usr/share/ollama`); the `ollama` user needs rw. | [P] envconfig, faq |
| E9 | `OLLAMA_ORIGINS`: always allows localhost/127.0.0.1/0.0.0.0 (+ app/file/tauri/vscode schemes); extra origins comma-separated. Irrelevant for non-browser clients (LiteLLM, model worker). | [P] envconfig, faq |
| E10 | Further relevant variables: `OLLAMA_LOAD_TIMEOUT` (5m), `OLLAMA_GPU_OVERHEAD` (bytes reserved per GPU), `OLLAMA_VULKAN` (Vulkan discovery, default on), `OLLAMA_LLM_LIBRARY` (bypass autodetection, e.g. `cuda_v12`), `OLLAMA_NO_CLOUD` ("Disable Ollama cloud features (remote inference and web search)"), `OLLAMA_NOPRUNE`, `CUDA_VISIBLE_DEVICES` (UUID from `nvidia-smi -L`), `LLAMA_ARG_FIT` (llama.cpp automatic fit, default on). | [P] envconfig |
| E11 | Libraries are found relative to the resolved executable (`<exe>/../lib/ollama`), so `/opt/ollama/<ver>/bin/ollama` + `/opt/ollama/<ver>/lib/ollama` works. | [P] `ml/path.go` |
| E12 | GPU discovery logs `inference compute … library=CUDA compute=6.1 … libdirs=ollama,cuda_v12 driver=… total=… available=…`; CPU fallback logs `id=cpu library=cpu`. | [P] `discover/types.go` |
| E13 | Linux suspend/resume: "sometimes Ollama will fail to discover your NVIDIA GPU, and fallback to running on the CPU"; workaround `rmmod nvidia_uvm && modprobe nvidia_uvm`. | [P] docs/gpu.mdx |
| E14 | Install script (`install.sh` v0.40.4): downloads `https://ollama.com/download/ollama-linux-amd64.tar.zst?version=$OLLAMA_VERSION` (needs `zstd`), no checksum check, installs to `/usr/local` (deletes `/usr/local/lib/ollama`), **always rewrites `/etc/systemd/system/ollama.service`** (`WantedBy=default.target`), creates user `ollama` (home `/usr/share/ollama`) in `render`/`video`; **if `nvidia-smi` is missing it adds NVIDIA's `debian13` CUDA repo and installs `cuda-drivers` (= newest, Pascal-less branch)**. | [P] scripts/install.sh |
| E15 | Manual install per docs: `curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst \| sudo tar x -C /usr`, remove old `/usr/lib/ollama` first; sample unit `ExecStart=/usr/bin/ollama serve`, `User=ollama`, `Restart=always`. | [P] docs/linux.mdx |

### Models (default tags are GGUF → llama-server cuda_v12; sizes are download sizes)

| Role | Tag | Size / context | Min. Ollama | Status | Source |
|---|---|---|---|---|---|
| Planner | `gemma4:26b` (MoE 26B/≈4B active) | ≈18 GB (tags page range 16–19 GB), 256K | 0.20.0 | listed | [P] v0.20.0 notes; [S] tags page via search |
| Planner fallback | `gemma4:12b` | ≈7.6 GB (`12b-it-qat` ≈7.2 GB, `12b-it-bf16` ≈24 GB, separate `12b-mlx`) | 0.30.3 | listed | [P] v0.30.3 notes; [S] library pages via search |
| Coder | `qwen3-coder:30b` (MoE 30B/3.3B active) | ≈19 GB Q4_K_M, 256K | older than 0.20 | unchanged | [S] datallmlab, morphllm |
| Heavy reviewer | `qwen3.8:27b` (= Q4_K_M, incl. ≈0.93 GB vision encoder) | ≈18 GB, 256K | **0.32.12** | listed (R-025) | [P] v0.32.12 notes; [S] OrcaRouter |
| Fast router | `qwen3:8b` | 5.2 GB, 40K | old | unchanged | [S] agenticwire, localllm.in |
| Embedding | `embeddinggemma-2:740m` | ≈1.3 GB | — | **MLX-only, not runnable on Linux/Pascal** (O11) | [P] issue #18825 |
| Embedding fallback | `embeddinggemma:300m` (GGUF, 768 dim) | ≈622 MB, **2K context** | 0.11.10 | runnable | [S] library listing via search; R-025 |

Estimated RAM+VRAM with 8 GB VRAM (≈7.5 GiB usable after the CUDA context) and partial offload — weights
are split by llama.cpp's automatic fit, layers that do not fit stay in (mmap'd) system RAM; KV per token
from the published architectures where known, otherwise marked as assumption:

| Profile (`config/models.example.yaml`) | Weights | KV f16 at profile context | KV q8_0 | Total RAM+VRAM (f16 → q8_0) | GPU share | yaml `memory_gb` |
|---|---|---|---|---|---|---|
| fast-router `qwen3:8b`, 16K | 5.2 GB | ≈2.3 GB (36 layers × 8 KV heads × 128 × 2 × 2 B) | ≈1.1 GB | ≈8.0 → ≈6.8 GB | 100 % GPU only with q8_0 (or smaller ctx) | 7 |
| planner `gemma4:26b`, 32K | ≈18 GB | ≈0.8 GB (R-008, sliding-window layers) | ≈0.4 GB | ≈20 GB | ≈35 % GPU | 19 → raise to 20 |
| planner fallback `gemma4:12b`, 32K | ≈7.6 GB | ≈1–3 GB (assumption) | half | ≈9–11 GB | mostly GPU | 10 |
| coder `qwen3-coder:30b`, 32K | ≈19 GB | ≈3.0 GB (≈96 KB/token, R-011) | ≈1.5 GB | ≈23 → ≈21.5 GB | ≈33 % GPU | 23 |
| heavy `qwen3.8:27b`, 24K | ≈18 GB | ≈6 GB if 64 layers × 8 KV heads dense (assumption; less if hybrid attention) | ≈3 GB | ≈25 → ≈22 GB | ≈30 % GPU, dense → slow | 25 |
| embedding `embeddinggemma:300m` | ≈0.6 GB | small | – | ≈1 GB | GPU or CPU | 2 |

Disk for all six tags ≈ 70 GB plus ≈ 2.1 GiB per extracted Ollama version.

### ffmpeg NVENC on Pascal

| # | Fact | Source |
|---|---|---|
| F1 | Debian's own `ffmpeg` is built with NVENC since 7:4.4.1-2 (Build-Depends `libffmpeg-nvenc-dev` on amd64; NVENC loaded via `dlopen`, no `--enable-nonfree` needed). A Debian-derived 6.1 build lists `h264_nvenc`, `hevc_nvenc`, `av1_nvenc`. | [P] Debian changelog; own `ffmpeg -encoders` |
| F2 | trixie: ffmpeg **7:7.1.5-0+deb13u1** (trixie-security, accepted 2026-06-22), `libffmpeg-nvenc-dev` **12.1.14.0-1** → NVENC API 12.1 → Linux driver **≥ 530.41.03** (580 and 550 both satisfy it). | [P/snippet] tracker.debian.org, packages.debian.org; [S] FFmpeg-cvslog, Gentoo ebuild |
| F3 | Runtime libraries: `libnvidia-encode.so.1` + `libcuda.so.1` (Debian: `libnvidia-encode1`, `libcuda1`; NVDEC: `libnvcuvid1`) – installed by the NVIDIA `.run` installer as well. | [P/snippet] packages.debian.org |
| F4 | Pascal (GP104) NVENC: H.264 and HEVC (8/10-bit), **no HEVC B-frames**, **no AV1** encode (`av1_nvenc` is listed by ffmpeg but fails on Pascal). GeForce has a driver-enforced concurrent-session limit (exact current value not verifiable from here; irrelevant with one video lease). | [S] Wikipedia NVENC; R-017/R-024 |

## Decision / recommendation for Hermclaw

Roles stay unchanged (Gemma planner, Qwen3-Coder 30B, Qwen3.8 27B, Qwen3 8B, EmbeddingGemma); only the
GPU/Ollama installation path changes.

1. **NVIDIA driver: R580 via NVIDIA's `.run` installer with DKMS**, version **580.173.02** (latest GeForce
   580 release found; any later 580.x is acceptable, never 590+). Debian's 550 (main/backports) is *not*
   sufficient (Ollama ≥ 0.30 requires ≥ 570 for CC 6.1, O6/O7) and NVIDIA's `debian13` apt repository has no
   580 packages (N6), so the current role default `nvidia_source: cuda` + `nvidia-driver-pinning-580` fails on
   trixie ("Unable to locate package") and `nvidia_source: debian` installs 550 (the role's branch assert then
   fails – correct, but a dead end). Switch to the `cuda` source only once issue #38 is resolved and
   `apt-cache policy nvidia-driver-pinning-580` shows a candidate on `.224`.
   - Kernel module flavour: **proprietary** (open modules do not support Pascal, N4).
   - Kernel: trixie stock `linux-image-amd64` (6.12 LTS) + `linux-headers-amd64`; no backports kernel (N8).
   - Secure Boot: disable on `.224` (simplest, home LAN), or enrol the DKMS MOK key
     (`mokutil --import /var/lib/dkms/mok.pub`, Debian dkms signs modules with `/var/lib/dkms/mok.key`;
     Debian wiki, not re-fetched).
   - Pin: no NVIDIA/CUDA apt repo on `.224`; apt preferences block every apt-provided NVIDIA driver package
     (`Pin: version *`, `Pin-Priority: -1`) so neither Debian's 550 nor a 590+ package can replace the runfile
     driver; the runfile version is pinned in Ansible (`nvidia_runfile_version`, sha256 recorded on first
     download — NVIDIA publishes no checksum file that could be checked from here).
2. **Ollama v0.40.4** via the pinned release archive `ollama-linux-amd64.tar.zst`, sha256
   `15f31e9576d15a481e099b4145dba6e9f8c1f295927d6b8895300ae30ed3740a` (verified, O2); keep the tarball
   method (`/opt/ollama/<version>`, symlink `current`, checksum from `sha256sum.txt`). v0.40.3 (current role
   pin) remains a valid fallback (same CUDA presets and driver rule; older llama.cpp b11351). Never install
   via `install.sh` on `.224` (E14). Floor: 0.32.12 (qwen3.8). Do not install the `-mlx` / `-rocm` archives.
3. **Embedding model on `.224`: `embeddinggemma:300m`** (768 dim, **2048-token context**) instead of
   `embeddinggemma-2:740m` until Ollama ships EmbeddingGemma 2 for llama-server/CUDA 12 *and* a smoke test on
   `.224` passes (O11). `vector(768)` stays valid; chunks must stay ≤ ~1,800 tokens (R-013's ≤ ~1,500 already
   fits). `scripts/ops/hermclaw-model-pull` already falls back, but the example config should default to the
   fallback so INSTALLATION.md is not built on an unrunnable tag.
4. **Ollama environment (systemd drop-in `ollama.service.d/hermclaw.conf`)**:

   | Variable | Value | Reason |
   |---|---|---|
   | `OLLAMA_HOST` | `0.0.0.0:11434` | LiteLLM on .225 must reach it; restrict with the host firewall to .225 + localhost (no auth in Ollama) |
   | `OLLAMA_KEEP_ALIVE` | `5m` | default; Hermclaw unloads explicitly with `keep_alive: 0` |
   | `OLLAMA_MAX_LOADED_MODELS` | `2` | one large model + embedding |
   | `OLLAMA_NUM_PARALLEL` | `1` | context memory is multiplied by this value |
   | `OLLAMA_CONTEXT_LENGTH` | unset | Hermclaw sends `num_ctx`; verify with `ollama ps` (CONTEXT column), the server default would be 4096 |
   | `OLLAMA_FLASH_ATTENTION` | `1` after the smoke test, otherwise **explicitly `0`** | unset = auto, which on CC 6.1 already enables FA (E5); the role's "false" currently means auto |
   | `OLLAMA_KV_CACHE_TYPE` | `q8_0` (with FA=1), `f16` if FA=0 | keeps `qwen3:8b`@16K 100 % on GPU, saves 1.5–3 GB on the large models |
   | `OLLAMA_MODELS` | `/var/lib/ollama/.ollama/models` (role default) on a disk with ≥ 100 GB free | ≈70 GB of models |
   | `OLLAMA_ORIGINS` | unset | no browser client |
   | `OLLAMA_LOAD_TIMEOUT` | `10m` | 18–19 GB partial-offload loads on a cold cache |
   | `OLLAMA_NO_CLOUD` | `1` | no accidental remote inference/web search from a local-only system |
   | `OLLAMA_VULKAN` | `0` | deterministic CUDA path; a filtered CUDA device must not silently fall back to Vulkan |
   | `OLLAMA_LLM_LIBRARY` | optional `cuda_v12` (only after test) | skips cuda_v13/vulkan probing |

5. Embedding requests on `.224` should run on the CPU (`options.num_gpu: 0` in `/api/embed`) so that an embed
   call does not force eviction of the partially offloaded large model (E3). Verify on the host; if LiteLLM
   cannot pass the option, the model worker's direct `/api/embed` fallback should.
6. **ffmpeg**: Debian `ffmpeg` (7:7.1.5-0+deb13u1) is sufficient; use `h264_nvenc` / `hevc_nvenc` with
   `-bf 0` for HEVC; never `av1_nvenc` on the GTX 1080.

### Install commands for `.224` (Debian 13, run as root; to be mirrored by the roles)

```sh
# 1  prerequisites (stock trixie kernel; contrib/non-free not needed for the runfile path)
apt-get update
apt-get install -y linux-headers-amd64 build-essential dkms pkg-config libglvnd-dev zstd pciutils mokutil
mokutil --sb-state                 # expected: "SecureBoot disabled" (or enrol the DKMS MOK, see Decision 1)

# 2  keep apt from ever installing a driver over the runfile (Debian 550 or NVIDIA 590+)
cat >/etc/apt/preferences.d/hermclaw-nvidia.pref <<'EOF'
Package: /^(nvidia|libnvidia|libcuda|libnvcuvid|libnvoptix|xserver-xorg-video-nvidia|cuda-drivers|firmware-nvidia)/
Pin: version *
Pin-Priority: -1
EOF

# 3  disable nouveau, reboot
printf 'blacklist nouveau\noptions nouveau modeset=0\n' >/etc/modprobe.d/hermclaw-blacklist-nouveau.conf
update-initramfs -u && systemctl reboot

# 4  NVIDIA 580 (proprietary modules, DKMS) – URL pattern of NVIDIA's download server (not reachable from the
#    build sandbox); verify the flags with "--advanced-options" of the exact runfile
cd /var/cache/hermclaw
curl -fLO https://download.nvidia.com/XFree86/Linux-x86_64/580.173.02/NVIDIA-Linux-x86_64-580.173.02.run
sha256sum NVIDIA-Linux-x86_64-580.173.02.run          # record in the inventory (nvidia_runfile_sha256)
sh NVIDIA-Linux-x86_64-580.173.02.run --check
sh NVIDIA-Linux-x86_64-580.173.02.run --silent --dkms --kernel-module-type=proprietary --no-x-check --disable-nouveau
systemctl reboot
nvidia-smi --query-gpu=driver_version,name,memory.total,compute_cap --format=csv,noheader
#   expected: 580.173.02, NVIDIA GeForce GTX 1080, 8192 MiB, 6.1
dkms status                                            # nvidia/580.173.02, <kernel>, x86_64: installed

# 5  Ollama 0.40.4 (pinned archive, checksum-verified)
V=0.40.4
curl -fLo /var/cache/hermclaw/ollama-$V-linux-amd64.tar.zst \
  https://github.com/ollama/ollama/releases/download/v$V/ollama-linux-amd64.tar.zst
echo "15f31e9576d15a481e099b4145dba6e9f8c1f295927d6b8895300ae30ed3740a  /var/cache/hermclaw/ollama-$V-linux-amd64.tar.zst" | sha256sum -c -
install -d -m 0755 /opt/ollama/$V
tar --zstd --no-same-owner -xf /var/cache/hermclaw/ollama-$V-linux-amd64.tar.zst -C /opt/ollama/$V
ln -sfn /opt/ollama/$V /opt/ollama/current
# user/unit/drop-in: role `ollama` (unit ExecStart=/opt/ollama/current/bin/ollama serve, drop-in = table above)
systemctl daemon-reload && systemctl enable --now ollama
curl -s http://127.0.0.1:11434/api/version            # {"version":"0.40.4"}

# 6  GPU smoke test (gate for every driver/Ollama upgrade)
journalctl -u ollama -b --no-pager | grep -E 'inference compute|NVIDIA driver too old'
#   must show library=CUDA compute=6.1 libdirs=ollama,cuda_v12; must NOT show "driver too old" or id=cpu
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen3:8b","prompt":"ok","stream":false,"think":false,"options":{"num_ctx":16384,"num_predict":8}}'
ollama ps                                              # qwen3:8b: PROCESSOR "100% GPU", CONTEXT 16384
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen3:8b","keep_alive":0}'

# 7  models + ffmpeg
scripts/ops/hermclaw-model-pull                        # embeddinggemma-2 fails → falls back to embeddinggemma:300m
apt-get install -y ffmpeg
ffmpeg -hide_banner -encoders | grep nvenc
ffmpeg -hide_banner -f lavfi -i testsrc2=size=1920x1080:rate=30 -t 5 -c:v h264_nvenc -preset p4 -b:v 6M -f null -
ffmpeg -hide_banner -f lavfi -i testsrc2=size=1920x1080:rate=30 -t 5 -c:v hevc_nvenc -preset p4 -bf 0 -f null -
```

## Implementation consequences

INSTALLATION.md (model worker `.224`) must:
- state that Debian's `nvidia-driver` (550) and NVIDIA's `debian13` repo are **not** usable for the GTX 1080
  with Hermclaw's Ollama version, and document the runfile procedure above (Secure Boot, nouveau blacklist,
  two reboots, DKMS rebuild on kernel updates via `linux-headers-amd64`);
- never run `curl … install.sh | sh` on `.224` (it would replace `ollama.service` and, without `nvidia-smi`,
  install the newest Pascal-less `cuda-drivers`);
- include the GPU smoke test (step 6) as a hard gate, plus `ollama ps` checks per role model;
- set the embedding profile to `embeddinggemma:300m` with `context_tokens: 2048`;
- add ffmpeg + NVENC check (step 7) for the media pipeline;
- note that after suspend/resume the GPU may disappear for Ollama (E13): unload models before suspend and run
  `rmmod nvidia_uvm && modprobe nvidia_uvm && systemctl restart ollama` after resume.

Deployment playbook (`infra/ansible/roles/nvidia`, `roles/ollama`) must (recommended changes, not made here):
- `nvidia`: add `nvidia_source: runfile` (new default) with `nvidia_runfile_version: "580.173.02"`,
  `nvidia_runfile_sha256` (inventory), `nvidia_runfile_url`; install prerequisites, nouveau blacklist +
  initramfs, run the installer with `--silent --dkms --kernel-module-type=proprietary`, skip when
  `nvidia-smi` already reports the pinned version; keep the 580-branch assert;
- `nvidia`: in runfile mode render a blocking apt preference (`Pin: version *`, priority -1) instead of the
  current `580.*`=1001 pin; keep `cuda` mode for when NVIDIA backfills 580 (then
  `nvidia-driver-pinning-580` + `cuda-drivers`), and make `debian` mode fail early with a clear message
  (550 < 570 required by Ollama ≥ 0.30);
- `nvidia`: the runfile installs `/usr/bin/nvidia-persistenced` but no systemd unit — the current
  "enable nvidia-persistenced" task would fail; ship a unit (NVIDIA sample
  `/usr/share/doc/NVIDIA_GLX-1.0/samples/nvidia-persistenced-init.tar.bz2`) or use a oneshot `nvidia-smi -pm 1`;
- `ollama`: bump `ollama_version` to `0.40.4` (or keep 0.40.3 until the smoke test ran), archive name stays
  `ollama-linux-amd64.tar.zst`;
- `ollama`: make `ollama_flash_attention` tri-state (`auto|true|false`) and render `OLLAMA_FLASH_ATTENTION=0`
  for `false`; add `OLLAMA_KV_CACHE_TYPE`, `OLLAMA_NO_CLOUD=1`, `OLLAMA_VULKAN=0`, `OLLAMA_LOAD_TIMEOUT=10m`
  (all pass the existing `ollama_extra_env` allowlist);
- `ollama`: after start, assert the journal shows `library=CUDA` for the GTX 1080 (fail on
  `NVIDIA driver too old` / `id=cpu`);
- `scripts/ops/hermclaw-model-pull`: besides `ollama show`, do a functional check per tag (1-token generate
  with `keep_alive: 0`, one `/api/embed`) because MLX-only tags can be listed but not run on Linux.
- Firewall on `.224`: allow TCP 11434 only from `.225` (and localhost).

## Open risks
- **No apt-managed Pascal driver on trixie**: runfile installs bypass Debian security updates; 580 updates
  (security/kernel compat) must be followed manually (NVIDIA legacy release notes) and re-run by Ansible.
  NVIDIA may never backfill 580 into `debian13` (issue #38).
- Runfile flag names (`--kernel-module-type`, `--disable-nouveau`) and the download URL could not be verified
  against NVIDIA's README from the sandbox; check `--advanced-options` on the host.
- 580 end of support (secondary: security updates until ~2028); after that `.224` needs a newer GPU.
- Future Ollama releases may drop the `cuda_v12` runner (as CUDA 13 cannot target Pascal) → keep the version
  pinned; read release notes for "cuda_v12"/"compute capability" before every bump.
- `embeddinggemma-2:740m` unusable on Linux/Pascal until Ollama supports it outside MLX; even then MLX on Linux
  needs CUDA 13 (sm_75+), so only a llama-server/GGUF build would help. The 300m fallback has only 2K context.
- Flash attention / q8_0 KV on Pascal is supported by llama.cpp's TILE/VEC kernels but not measured on this
  GPU; prompt processing may be slower than without FA — decide with the smoke-test numbers.
- Concurrent embedding + partially offloaded large model may cause evictions (E3); `num_gpu: 0` for embeddings
  is the mitigation, to be verified.
- Memory figures for `qwen3.8:27b` KV and `gemma4:12b` KV are assumptions (architecture not verified from a
  primary source); tag sizes come from secondary sources because ollama.com was unreachable — confirm with
  `ollama show <tag>` / `ollama ps` on `.224`.
- Suspend/resume of `.224` can leave Ollama on CPU (E13) – needs the resume hook.
- Debian package versions (550.163.01-x, ffmpeg 7.1.5-0+deb13u1, libffmpeg-nvenc-dev 12.1.14.0-1) were taken
  from search snippets of packages.debian.org/tracker.debian.org; confirm with `apt-cache policy` on the host.
