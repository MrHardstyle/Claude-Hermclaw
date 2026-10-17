#!/usr/bin/env bash
# Build llama-server/llama-bench (llama.cpp, CUDA) for the model host and package them with the CUDA runtime
# libraries they need (D-015). The official llama.cpp releases ship no Linux CUDA binaries, and third-party builds
# are a supply-chain risk, so the binaries are built from the tagged source archive in NVIDIA's CUDA devel image.
#
#   scripts/ops/build-llamacpp.sh [--tag b11361] [--cuda-arch 61] [--out dist]
#
# Output: <out>/llamacpp-<tag>-sm<arch>.tgz (bin/llama-server, bin/llama-bench, lib/libcudart|libcublas|libcublasLt)
# plus a .sha256 file; the ansible role ollama installs it with llamacpp_archive_src / llamacpp_archive_sha256.
# CUDA 12.8 still compiles for Pascal (sm_61, GTX 1080); CUDA 13 dropped it. CPU code targets x86-64-v3 (AVX2/FMA)
# without -march=native, so the build host's CPU does not matter.
set -euo pipefail

TAG=b11361
ARCH=61
OUT=dist
IMAGE=docker.io/nvidia/cuda:12.8.1-devel-ubuntu24.04
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --cuda-arch) ARCH="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$TAG" =~ ^b[0-9]+$ ]] || { echo "tag must look like b11361" >&2; exit 2; }
[[ "$ARCH" =~ ^[0-9]+$ ]] || { echo "cuda arch must be numeric (61 = Pascal)" >&2; exit 2; }
command -v podman >/dev/null || { echo "podman is required" >&2; exit 2; }

mkdir -p "$OUT"
WORK=$(mktemp -d "${TMPDIR:-/var/tmp}/llamacpp-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
curl -fsSL -o "$WORK/src.tar.gz" "https://github.com/ggml-org/llama.cpp/archive/refs/tags/${TAG}.tar.gz"
echo "source ${TAG}: $(sha256sum "$WORK/src.tar.gz" | cut -d' ' -f1)"
tar -xzf "$WORK/src.tar.gz" -C "$WORK"
mkdir -p "$WORK/out"
cat > "$WORK/build.sh" <<EOF
set -euo pipefail
apt-get update -qq && apt-get install -y -qq cmake >/dev/null
cd /work/llama.cpp-${TAG}
cmake -S . -B build -DGGML_CUDA=ON -DGGML_CUDA_NCCL=OFF -DCMAKE_CUDA_ARCHITECTURES=${ARCH} -DGGML_NATIVE=OFF \\
  -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF \\
  -DLLAMA_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_RPATH='\$ORIGIN/../lib' -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON
cmake --build build -j"\$(nproc)" --target llama-server llama-bench
mkdir -p /work/out/bin /work/out/lib
cp build/bin/llama-server build/bin/llama-bench /work/out/bin/
for l in libcudart.so.12 libcublas.so.12 libcublasLt.so.12; do cp -L /usr/local/cuda/lib64/\$l /work/out/lib/; done
echo "${TAG}" > /work/out/VERSION
EOF
nice -n 19 podman run --rm -v "$WORK:/work:Z" "$IMAGE" bash /work/build.sh
ARCHIVE="$OUT/llamacpp-${TAG}-sm${ARCH}.tgz"
tar -czf "$ARCHIVE" -C "$WORK/out" VERSION bin lib
sha256sum "$ARCHIVE" | tee "$ARCHIVE.sha256"
