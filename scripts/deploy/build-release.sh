#!/usr/bin/env bash
# Hermclaw Next – build a deployable release for Debian 13 / CPython 3.13 (P32 32.1, 32.2, 32.5).
#
# Usage: scripts/deploy/build-release.sh [--source DIR] [--out DIR] [--python BIN] [--version VER]
#                                        [--constraints FILE] [--offline-from DIR]... [--wheel-cache DIR]
#                                        [--ui-dist DIR] [--force]
#
#   --source DIR        checkout to build (default: this repository), e.g. a `git worktree` of a release tag
#   --out DIR           output root (default <repo>/dist); the release is written to DIR/release-<version>/
#   --python BIN        CPython 3.13 used for the self-test and the build tools (default: python3.13, else python3)
#   --version VER       build another version than pyproject.toml's (rehearsal/hotfix builds; written into the
#                       temporary build copy only, never into the checkout)
#   --constraints FILE  pip constraints for the runtime dependencies (reproduce a tested lock, e.g. the
#                       constraints.txt of an earlier release)
#   --offline-from DIR  resolve every wheel (runtime, LiteLLM, setuptools/wheel for the build) from DIR only,
#                       without an index (air-gapped builds; repeatable). Default: the configured package index.
#   --wheel-cache DIR   copy every wheel this build used into DIR (a local mirror for later --offline-from builds)
#   --ui-dist DIR       use an already built UI bundle (index.html + assets/) instead of npm ci && npm run build
#   --force             replace an existing DIR/release-<version>/
#
# Output (dist/release-<version>/, verified by playbooks/deploy.yml before anything is copied):
#   release.json                     version, CPython/platform target, Alembic head, LiteLLM requirement (read from the
#                                    `litellm-proxy` extra of pyproject.toml), payload names and lock checksums
#   hermclaw-runtime-<v>.tar         wheelhouse/ (the hermclaw wheel incl. share/hermclaw/{alembic.ini,migrations}
#                                    and every runtime dependency), requirements.lock (name==version --hash=sha256:…),
#                                    constraints.txt, SHA256SUMS, release.json, sandbox-images/ (Containerfiles)
#   hermclaw-litellm-<v>.tar         wheelhouse/ + requirements.lock + constraints.txt + SHA256SUMS for the proxy venv
#   hermclaw-ui-<v>.tar.gz           static UI build (ui/: npm ci && npm run build)
#   config/*.example.yaml            templates the deployment renders /etc/hermclaw/*.yaml from
#   scripts/ops/*                    operator scripts the deployment installs on the hosts
#   SHA256SUMS                       sha256 of every file above
#
# Wheels are resolved for CPython 3.13 (cp313/abi3), manylinux x86_64 up to glibc 2.41 (Debian 13), binary only.
# The self-test installs the runtime offline with --require-hashes into a fresh venv of --python, runs `pip check`,
# `hermclaw --version`, loads every console entry point, reads the Alembic head and validates the shipped example
# configuration with `hermclaw config-check`; the LiteLLM lock is resolved offline (pip --dry-run) and its wheels are
# scanned for the litellm_init.pth supply-chain implant (research 20261014-027 L4).
# Exit codes: 0 ok, 1 build/self-test failure, 2 usage, 69 missing tool.
set -euo pipefail
umask 022

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "${SCRIPT_DIR}/../.." && pwd)
OUT="${REPO}/dist"
SOURCE=""
PYTHON=""
VERSION=""
CONSTRAINTS=""
WHEEL_CACHE=""
UI_DIST=""
FORCE=0
OFFLINE=()
# manylinux tags accepted on Debian 13 (glibc 2.41), most specific first: pip does not widen a single tag (R-029 B5)
PLATFORMS=(manylinux_2_41_x86_64 manylinux_2_39_x86_64 manylinux_2_36_x86_64 manylinux_2_34_x86_64
    manylinux_2_31_x86_64 manylinux_2_28_x86_64 manylinux_2_27_x86_64 manylinux_2_26_x86_64 manylinux_2_24_x86_64
    manylinux_2_17_x86_64 manylinux2014_x86_64 manylinux_2_12_x86_64 manylinux2010_x86_64 manylinux_2_5_x86_64
    manylinux1_x86_64)
PY_TARGET="3.13"
ABI="cp313"

log() { printf '[build-release] %s\n' "$*" >&2; }
die() { printf '[build-release] ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }
usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) [[ $# -ge 2 ]] || usage; SOURCE="$2"; shift 2 ;;
        --out) [[ $# -ge 2 ]] || usage; OUT="$2"; shift 2 ;;
        --python) [[ $# -ge 2 ]] || usage; PYTHON="$2"; shift 2 ;;
        --version) [[ $# -ge 2 ]] || usage; VERSION="$2"; shift 2 ;;
        --constraints) [[ $# -ge 2 ]] || usage; CONSTRAINTS="$2"; shift 2 ;;
        --offline-from) [[ $# -ge 2 ]] || usage; OFFLINE+=("$2"); shift 2 ;;
        --wheel-cache) [[ $# -ge 2 ]] || usage; WHEEL_CACHE="$2"; shift 2 ;;
        --ui-dist) [[ $# -ge 2 ]] || usage; UI_DIST="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage ;;
        *) printf '[build-release] unknown argument: %s\n' "$1" >&2; usage ;;
    esac
done

# --------------------------------------------------------------------------------------------- tools
if [[ -n "$SOURCE" ]]; then
    [[ -f "${SOURCE}/pyproject.toml" && -d "${SOURCE}/hermclaw" ]] || die "--source ${SOURCE} is not a Hermclaw checkout" 2
    SOURCE=$(cd "$SOURCE" && pwd)
else
    SOURCE="$REPO"
fi
for tool in tar gzip sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || die "${tool} not found" 69
done
if [[ -z "$PYTHON" ]]; then
    if command -v python3.13 >/dev/null 2>&1; then PYTHON=python3.13; else PYTHON=python3; fi
fi
command -v "$PYTHON" >/dev/null 2>&1 || die "${PYTHON} not found (Debian 13: apt install python3 python3-venv)" 69
"$PYTHON" -c "import sys; sys.exit(0 if sys.implementation.name == 'cpython' and '%d.%d' % sys.version_info[:2] == '${PY_TARGET}' else 1)" \
    || die "${PYTHON} is not CPython ${PY_TARGET} (the release targets Debian 13's python3; use --python)" 69
for dir in "${OFFLINE[@]}"; do
    [[ -d "$dir" ]] || die "--offline-from ${dir}: no such directory" 2
done
[[ -z "$CONSTRAINTS" || -f "$CONSTRAINTS" ]] || die "--constraints ${CONSTRAINTS}: no such file" 2
# the nginx site sends a Content-Security-Policy without 'unsafe-inline' (roles/nginx nginx_csp): an inline <script>,
# <style> or style="" attribute in index.html would be blocked in the browser
check_ui_bundle() {  # dir
    [[ -f "$1/index.html" && -d "$1/assets" ]] || die "UI bundle $1 has no index.html + assets/" 2
    "$PYTHON" -I - "$1/index.html" <<'PY' || die "UI bundle $1: index.html is not compatible with the Content-Security-Policy" 2
import html.parser, sys
class Check(html.parser.HTMLParser):
    problems, script = [], None
    def handle_starttag(self, tag, attrs):
        names = {k for k, _ in attrs}
        if "style" in names or any(k.startswith("on") for k in names):
            self.problems.append(f"inline attribute in <{tag}>")
        if tag == "style":
            self.problems.append("inline <style>")
        if tag == "script":
            self.script = "src" in names
            if not self.script:
                self.problems.append("inline <script>")
    def handle_data(self, data):
        if self.script and data.strip():
            self.problems.append("<script src> with inline code")
    def handle_endtag(self, tag):
        if tag == "script":
            self.script = None
c = Check()
c.feed(open(sys.argv[1], encoding="utf-8").read())
if c.problems:
    sys.exit("; ".join(sorted(set(c.problems))))
PY
}
if [[ -n "$UI_DIST" ]]; then
    check_ui_bundle "$UI_DIST"
else
    command -v npm >/dev/null 2>&1 || die "npm not found (Node.js >= 22.12 builds the UI; or pass --ui-dist)" 69
    node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=12)?0:1)' \
        || die "Node.js $(node --version 2>/dev/null || echo '?') is older than 22.12 (ui/package.json engines)" 69
fi

# --------------------------------------------------------------------------------------------- version
PROJECT_VERSION=$("$PYTHON" -I -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
    "${SOURCE}/pyproject.toml")
INIT_VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${SOURCE}/hermclaw/__init__.py")
[[ "$PROJECT_VERSION" == "$INIT_VERSION" ]] \
    || die "pyproject.toml version ${PROJECT_VERSION} != hermclaw/__init__.py __version__ ${INIT_VERSION}"
VERSION="${VERSION:-$PROJECT_VERSION}"
# canonical PEP 440 (what pip reports and what hermclaw_version must be): 0.1.0rc1, 0.2.0, 1.0.0.post1
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$ ]] \
    || die "version '${VERSION}' is not a canonical PEP 440 version (e.g. 0.1.0rc1)" 2

RELEASE="${OUT}/release-${VERSION}"
if [[ -e "$RELEASE" && $FORCE -eq 0 ]]; then
    die "${RELEASE} exists (use --force to rebuild it)" 2
fi
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
RELEASE="${OUT}/release-${VERSION}"
STAGE=$(mktemp -d "${OUT}/.release-${VERSION}.XXXXXX")
WORK=$(mktemp -d)
cleanup() { rm -rf "$WORK" "$STAGE"; }
trap cleanup EXIT

INDEX_ARGS=()
if [[ ${#OFFLINE[@]} -gt 0 ]]; then
    INDEX_ARGS+=(--no-index)
    for dir in "${OFFLINE[@]}"; do INDEX_ARGS+=(--find-links "$(cd "$dir" && pwd)"); done
fi
TARGET_ARGS=(--only-binary=:all: --python-version "$PY_TARGET" --implementation cp --abi "$ABI")
for plat in "${PLATFORMS[@]}"; do TARGET_ARGS+=(--platform "$plat"); done
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_ROOT_USER_ACTION=ignore PYTHONDONTWRITEBYTECODE=1

log "release ${VERSION} -> ${RELEASE} (python $("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# build tools: pip from the target interpreter's own ensurepip (independent of the host's pip)
"$PYTHON" -m venv "${WORK}/tools" || die "${PYTHON} -m venv failed (Debian: apt install python3-venv)" 69
TOOLS_PIP="${WORK}/tools/bin/pip"

# --------------------------------------------------------------------------------------------- sources
SRC="${WORK}/src"
mkdir -p "$SRC"
tar -C "$SOURCE" --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' -cf - \
    pyproject.toml README.md alembic.ini migrations hermclaw worker | tar -C "$SRC" -xf -
if [[ "$VERSION" != "$PROJECT_VERSION" ]]; then
    log "version override ${PROJECT_VERSION} -> ${VERSION} (build copy only)"
    "$PYTHON" -I - "$SRC" "$VERSION" <<'PY'
import pathlib, re, sys
src, version = pathlib.Path(sys.argv[1]), sys.argv[2]
for rel, pattern, repl in (("pyproject.toml", r'(?m)^version = "[^"]*"$', f'version = "{version}"'),
                           ("hermclaw/__init__.py", r'(?m)^__version__ = "[^"]*"$', f'__version__ = "{version}"')):
    path = src / rel
    text, n = re.subn(pattern, repl, path.read_text(encoding="utf-8"), count=1)
    if n != 1:
        sys.exit(f"cannot set the version in {rel}")
    path.write_text(text, encoding="utf-8")
PY
fi

# --------------------------------------------------------------------------------------------- wheel
log "building the hermclaw wheel"
"$TOOLS_PIP" wheel --quiet --no-deps --wheel-dir "${WORK}/dist" "${INDEX_ARGS[@]}" "$SRC" >&2 \
    || die "pip wheel failed (offline builds need setuptools>=69 and wheel in --offline-from)"
WHEEL="${WORK}/dist/hermclaw-${VERSION}-py3-none-any.whl"
[[ -f "$WHEEL" ]] || die "expected ${WHEEL##*/}, got: $(ls "${WORK}/dist")"
"$PYTHON" -I - "$WHEEL" "$VERSION" <<'PY' || die "the wheel lacks the migrations or entry points"
import sys, zipfile
wheel, version = sys.argv[1], sys.argv[2]
names = zipfile.ZipFile(wheel).namelist()
data = f"hermclaw-{version}.data/data/share/hermclaw/"
need = [data + "alembic.ini", data + "migrations/env.py"]
missing = [n for n in need if n not in names]
if missing or not any(n.startswith(data + "migrations/versions/") and n.endswith(".py") for n in names):
    sys.exit(f"missing data files: {missing or 'migrations/versions/*.py'}")
if not any(n.endswith(".dist-info/entry_points.txt") for n in names):
    sys.exit("no entry_points.txt")
PY

# --------------------------------------------------------------------------------------------- wheelhouses
RUNTIME="${STAGE}/payload-runtime"
LITELLM="${STAGE}/payload-litellm"
mkdir -p "${RUNTIME}/wheelhouse" "${LITELLM}/wheelhouse" "${WORK}/build-deps"
CONSTRAINT_ARGS=()
[[ -z "$CONSTRAINTS" ]] || CONSTRAINT_ARGS=(--constraint "$CONSTRAINTS")

log "resolving the runtime wheelhouse (CPython ${PY_TARGET}, manylinux x86_64)"
"$TOOLS_PIP" download --quiet "${TARGET_ARGS[@]}" "${INDEX_ARGS[@]}" "${CONSTRAINT_ARGS[@]}" \
    --dest "${RUNTIME}/wheelhouse" "$WHEEL" >&2 || die "pip download of the runtime dependencies failed"
cp -f "$WHEEL" "${RUNTIME}/wheelhouse/"

# single source of truth for the proxy version: the litellm-proxy extra of pyproject.toml
LITELLM_LIST=$("$PYTHON" -I -c '
import sys, tomllib
for req in tomllib.load(open(sys.argv[1], "rb"))["project"].get("optional-dependencies", {}).get("litellm-proxy", []):
    print(req)' "${SRC}/pyproject.toml") || die "cannot read the litellm-proxy extra of pyproject.toml"
[[ -n "$LITELLM_LIST" ]] || die "pyproject.toml has no litellm-proxy extra"
mapfile -t LITELLM_REQS <<< "$LITELLM_LIST"
log "resolving the LiteLLM wheelhouse: ${LITELLM_REQS[*]}"
"$TOOLS_PIP" download --quiet "${TARGET_ARGS[@]}" "${INDEX_ARGS[@]}" --dest "${LITELLM}/wheelhouse" \
    "${LITELLM_REQS[@]}" >&2 || die "pip download of ${LITELLM_REQS[*]} failed"

if [[ -n "$WHEEL_CACHE" ]]; then
    # build requirements too, so a later --offline-from build can build the wheel without an index
    "$TOOLS_PIP" download --quiet "${INDEX_ARGS[@]}" --dest "${WORK}/build-deps" "setuptools>=69" wheel >&2 \
        || die "pip download of the build requirements failed"
    mkdir -p "$WHEEL_CACHE"
    for dir in "${RUNTIME}/wheelhouse" "${LITELLM}/wheelhouse" "${WORK}/build-deps"; do
        cp -f "$dir"/*.whl "$WHEEL_CACHE/"   # one cp per directory: the wheelhouses share many files
    done
    rm -f "${WHEEL_CACHE}/${WHEEL##*/}"
fi

# --------------------------------------------------------------------------------------------- locks
# requirements.lock: every wheel pinned with its sha256 (pip --require-hashes); constraints.txt: the same pins
lock() {  # payload dir, top-level requirement(s)...
    "$PYTHON" -I - "$@" <<'PY'
import hashlib, pathlib, re, sys
payload, tops = pathlib.Path(sys.argv[1]), sys.argv[2:]
canon = lambda n: re.sub(r"[-_.]+", "-", n).lower()
extras = {}
for top in tops:
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?", top)
    if not m:
        sys.exit(f"cannot parse requirement {top!r}")
    extras[canon(m.group(1))] = m.group(2) or ""
pins = {}
for wheel in sorted((payload / "wheelhouse").glob("*.whl")):
    name, version = wheel.name.split("-")[:2]
    key = canon(name)
    if key in pins and pins[key][0] != version:
        sys.exit(f"two versions of {key} in the wheelhouse: {pins[key][0]} and {version}")
    pins.setdefault(key, (version, []))[1].append(hashlib.sha256(wheel.read_bytes()).hexdigest())
missing = [t for t in extras if t not in pins]
if missing:
    sys.exit(f"top-level requirement(s) not in the wheelhouse: {missing}")
lock_lines = [f"{k}{extras.get(k, '')}=={v} " + " ".join(f"--hash=sha256:{h}" for h in sorted(hs))
              for k, (v, hs) in sorted(pins.items())]
(payload / "requirements.lock").write_text(
    "# Generated by scripts/deploy/build-release.sh - install with: pip install --no-index --find-links wheelhouse "
    "--only-binary=:all: --require-hashes -r requirements.lock\n" + "\n".join(lock_lines) + "\n", encoding="utf-8")
(payload / "constraints.txt").write_text("".join(f"{k}=={v}\n" for k, (v, _) in sorted(pins.items())), encoding="utf-8")
print(len(pins))
PY
}
RUNTIME_WHEELS=$(lock "$RUNTIME" "hermclaw")
LITELLM_WHEELS=$(lock "$LITELLM" "${LITELLM_REQS[@]}")
log "locked ${RUNTIME_WHEELS} runtime and ${LITELLM_WHEELS} LiteLLM distributions"

"$PYTHON" -I - "${LITELLM}/wheelhouse" <<'PY' || die "supply-chain check of the LiteLLM wheelhouse failed"
import pathlib, sys, zipfile
bad = [f"{w.name}: {n}" for w in sorted(pathlib.Path(sys.argv[1]).glob("*.whl"))
       for n in zipfile.ZipFile(w).namelist() if n.rsplit("/", 1)[-1] == "litellm_init.pth"]
if bad:
    sys.exit("litellm_init.pth found (GHSA-5mg7-485q-xm76 credential stealer): " + ", ".join(bad))
PY

# --------------------------------------------------------------------------------------------- self-test
log "self-test: offline install into a fresh CPython ${PY_TARGET} venv"
"$PYTHON" -m venv "${WORK}/selftest"
ST="${WORK}/selftest/bin"
"${ST}/pip" install --quiet --no-index --find-links "${RUNTIME}/wheelhouse" --only-binary=:all: --no-cache-dir \
    --require-hashes -r "${RUNTIME}/requirements.lock" >&2 || die "offline install of the runtime lock failed"
"${ST}/pip" check >&2 || die "pip check failed"
got=$("${ST}/hermclaw" --version) || die "hermclaw --version failed"
[[ "$got" == "hermclaw ${VERSION}" ]] || die "hermclaw --version printed '${got}', expected 'hermclaw ${VERSION}'"
EP_LIST=$("${ST}/python" -I -c '
import importlib.metadata as md, sys
eps = sorted((e for e in md.distribution("hermclaw").entry_points if e.group == "console_scripts"), key=lambda e: e.name)
failed = []
for ep in eps:
    try:
        ep.load()
    except Exception as exc:
        failed.append(f"{ep.name} = {ep.value}: {type(exc).__name__}: {exc}")
    print(ep.name)
if failed:
    sys.exit("console entry points that cannot be loaded (the units start them):\n  " + "\n  ".join(failed))
') || die "entry point check failed"
mapfile -t ENTRY_POINTS <<< "$EP_LIST"
ALEMBIC_HEAD=$("${ST}/python" -I -c 'from hermclaw.persistence.migrate import head_revision; print(head_revision())') \
    || die "cannot read the Alembic head from the installed release"
[[ "$ALEMBIC_HEAD" =~ ^[0-9A-Za-z_]+$ ]] || die "unexpected Alembic head '${ALEMBIC_HEAD}'"
mkdir -p "${STAGE}/config"
cp "${SOURCE}"/config/*.example.yaml "${STAGE}/config/"
HERMCLAW_CONFIG_DIR="${STAGE}/config" "${ST}/hermclaw" config-check >/dev/null \
    || die "hermclaw config-check rejects the shipped example configuration"
"$PYTHON" -m venv "${WORK}/selftest-litellm"
"${WORK}/selftest-litellm/bin/pip" install --quiet --dry-run --ignore-installed --no-index \
    --find-links "${LITELLM}/wheelhouse" --only-binary=:all: --require-hashes -r "${LITELLM}/requirements.lock" >&2 \
    || die "the LiteLLM lock does not resolve offline"

# --------------------------------------------------------------------------------------------- UI
UI_OUT="${WORK}/ui-dist"
NODE_VERSION=""
if [[ -n "$UI_DIST" ]]; then
    log "UI: using the prebuilt bundle ${UI_DIST}"
    mkdir -p "$UI_OUT"
    cp -a "${UI_DIST}/." "$UI_OUT/"
    UI_SOURCE="prebuilt"
else
    log "UI: npm ci && npm run build"
    mkdir -p "${WORK}/ui"
    tar -C "${SOURCE}/ui" --exclude=./node_modules --exclude=./dist --exclude=./test-results --exclude=./tests/.runtime \
        --exclude=./playwright-report -cf - . | tar -C "${WORK}/ui" -xf -
    (cd "${WORK}/ui" && npm ci --no-audit --no-fund --loglevel=error >&2 && npm run build >&2) || die "UI build failed"
    cp -a "${WORK}/ui/dist" "$UI_OUT"
    NODE_VERSION=$(node --version)
    UI_SOURCE="npm run build"
fi
check_ui_bundle "$UI_OUT"

# --------------------------------------------------------------------------------------------- metadata
mkdir -p "${STAGE}/scripts/ops"
for f in "${SOURCE}"/scripts/ops/*; do
    [[ -f "$f" ]] && install -m 0755 "$f" "${STAGE}/scripts/ops/"
done
mkdir -p "${RUNTIME}/sandbox-images"
cp -a "${SOURCE}/infra/sandbox-images/." "${RUNTIME}/sandbox-images/"
mapfile -t TOOLCHAINS < <(find "${RUNTIME}/sandbox-images" -mindepth 2 -maxdepth 2 -name Containerfile -printf '%h\n' \
    | xargs -n1 basename | sort)

"$PYTHON" -I - "$STAGE" "$VERSION" "$ALEMBIC_HEAD" "$NODE_VERSION" "$UI_SOURCE" "$RUNTIME_WHEELS" "$LITELLM_WHEELS" \
    "$(printf '%s,' "${ENTRY_POINTS[@]}")" "$(printf '%s,' "${TOOLCHAINS[@]}")" "$(printf '%s,' "${PLATFORMS[@]}")" \
    "$(printf '%s\n' "${LITELLM_REQS[@]}")" <<'PY'
import datetime, hashlib, json, pathlib, platform, re, socket, sys
(stage, version, head, node, ui_source, n_runtime, n_litellm, eps, toolchains, platforms, llm_reqs) = sys.argv[1:12]
stage = pathlib.Path(stage)
split = lambda s: [x for x in s.split(",") if x]
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
reqs = [r for r in llm_reqs.splitlines() if r]
main = next((r for r in reqs if re.match(r"^litellm(\[[^\]]+\])?==", r)), None)
if main is None:
    sys.exit("the litellm-proxy extra must pin litellm exactly (litellm[proxy]==X.Y.Z)")
lock_sha = sha(stage / "payload-litellm" / "requirements.lock")
meta = {
    "name": "hermclaw",
    "version": version,
    "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "build_host": socket.gethostname(),
    "python": {"implementation": "cp", "version": "3.13", "abi": "cp313", "platforms": split(platforms),
               "selftest_python": platform.python_version()},
    "alembic_head": head,
    "entry_points": split(eps),
    "sandbox_toolchains": split(toolchains),
    "runtime": {"payload": f"hermclaw-runtime-{version}.tar", "distributions": int(n_runtime),
                "lock_sha256": sha(stage / "payload-runtime" / "requirements.lock")},
    "litellm": {"payload": f"hermclaw-litellm-{version}.tar", "requirements": reqs,
                "version": main.split("==", 1)[1], "distributions": int(n_litellm), "lock_sha256": lock_sha,
                "venv_name": f"venv-{main.split('==', 1)[1]}-{lock_sha[:12]}"},
    "ui": {"payload": f"hermclaw-ui-{version}.tar.gz", "source": ui_source, "node": node or None},
}
text = json.dumps(meta, indent=2, sort_keys=True) + "\n"
(stage / "release.json").write_text(text, encoding="utf-8")
(stage / "payload-runtime" / "release.json").write_text(text, encoding="utf-8")
PY

# payload checksums (verified on the host after unpacking), then deterministic archives
checksums() {  # dir: SHA256SUMS over every regular file below it (relative paths, sorted)
    local sums="${WORK}/SHA256SUMS.$$"
    (cd "$1" && find . -type f ! -name SHA256SUMS -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' sha256sum) > "$sums"
    mv "$sums" "$1/SHA256SUMS"
}
for payload in "$RUNTIME" "$LITELLM"; do
    checksums "$payload"
done
TAR_OPTS=(--sort=name --owner=0 --group=0 --numeric-owner "--mode=u=rwX,go=rX" --mtime=@0)
tar "${TAR_OPTS[@]}" -C "$RUNTIME" -cf "${STAGE}/hermclaw-runtime-${VERSION}.tar" .
tar "${TAR_OPTS[@]}" -C "$LITELLM" -cf "${STAGE}/hermclaw-litellm-${VERSION}.tar" .
tar "${TAR_OPTS[@]}" -C "$UI_OUT" -cf - . | gzip -n -9 > "${STAGE}/hermclaw-ui-${VERSION}.tar.gz"
cp "${RUNTIME}/constraints.txt" "${STAGE}/constraints.txt"
cp "${LITELLM}/constraints.txt" "${STAGE}/litellm-constraints.txt"
rm -rf "$RUNTIME" "$LITELLM"
checksums "$STAGE"
chmod -R u=rwX,go=rX "$STAGE"

# --------------------------------------------------------------------------------------------- publish
if [[ -e "$RELEASE" ]]; then
    rm -rf "$RELEASE"
fi
mv "$STAGE" "$RELEASE"
log "done: ${RELEASE} (alembic head ${ALEMBIC_HEAD}, ${RUNTIME_WHEELS} runtime / ${LITELLM_WHEELS} LiteLLM distributions)"
printf '%s\n' "$RELEASE"
