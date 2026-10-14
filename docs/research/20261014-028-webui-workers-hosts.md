# 20261014-028 – Web UI, execution worker and host basics: nginx SSE, Node 22, rootless Podman 5 with cgroup delegation, Wake-on-LAN

## Question
1. **nginx on `.223`**: which nginx version does Debian 13 "trixie" ship, which reverse-proxy settings does
   Server-Sent Events (SSE) need (`proxy_buffering`, `proxy_http_version`, `Connection`, `proxy_read_timeout`,
   `X-Accel-Buffering`, gzip), what is a strict Content-Security-Policy (CSP) for the static Vite/React build,
   and how should HTTPS work in a LAN with a self-signed or internal CA?
2. **Node.js 22 for the UI build**: current NodeSource deb-repo setup for Debian 13, Debian's own `nodejs` in
   trixie, the Node 22 support window, and Vite 8's Node requirement.
3. **Rootless Podman on trixie (`.222`, SearXNG on `.225`)**: Podman version, required packages, `/etc/subuid`,
   lingering, cgroup v2 controller delegation for `user@.service` and how to verify it, the sandbox flags
   (`--read-only --network=none --userns=keep-id --pids-limit --memory --cpus`) as an unprivileged user, and
   where rootless Quadlet units go.
4. **Wake-on-LAN (`.222`, `.224`)**: persistent WoL on Debian 13 (systemd `.link` `WakeOnLan=magic` vs. an
   `ethtool -s … wol g` unit vs. NetworkManager), BIOS prerequisites, verifying with `ethtool`, sending magic
   packets (wakeonlan/etherwake, broadcast address), `systemctl suspend` and waking from suspend.

## Why needed
P01/P32 roles `nginx`, `nodejs`, `podman`, `wol_target` and `systemd_skeleton` (SearXNG Quadlet), INSTALLATION.md
(web UI `.223`, execution worker `.222`, model worker `.224`), P10 idle sleep / wake, P18 sandbox, P36 security.
The roles were written from R-001, R-005, R-014, R-016 and R-018 (2026-10-08). This re-check confirms most of
them, but found three things that need action: Podman 5.4.2 in trixie is affected by an unfixed CVE, NodeSource
had a signing-key problem with trixie's apt in 2026, and the nginx template has gaps (TLS/CSP missing, header
inheritance, token in logs, health probe vs. redirect). See Decision.

## Sources (accessed 2026-10-14)
Access notes. These were **not reachable** from this build sandbox (DNS failure or egress-policy 403):
`*.debian.org` (packages, tracker, security-tracker, wiki, deb mirrors, sources), `launchpadlibrarian.net`,
`nginx.org`, `docs.podman.io`, `freedesktop.org`, `deb.nodesource.com`, `vite.dev`, `wiki.archlinux.org`,
`rfc-editor.org`, `support.apple.com`, `endoflife.date`. `ollama.com` was not used. **Reachable:**
`raw.githubusercontent.com` (official source/doc repositories at pinned tags), `nodejs.org`, `registry.npmjs.org`,
`launchpad.net` (its Debian mirror only carries sid/bookworm data, **no trixie data**), and GitHub web pages via
WebFetch. Debian trixie package versions therefore come from **search-engine snippets of the primary Debian pages**
(marked `[P/snippet]`) and are not verified directly. Run `apt-cache policy <pkg>` on the hosts to confirm them.

Primary, read directly (`[P]`):
- nginx reference docs (source of nginx.org, `main` = 1.29.x mainline, with per-directive version notes):
  https://github.com/nginx/nginx.org/blob/main/xml/en/docs/http/ngx_http_proxy_module.xml , `ngx_http_gzip_module.xml`,
  `ngx_http_headers_module.xml`, `ngx_http_v2_module.xml`, `ngx_http_upstream_module.xml`, `ngx_http_ssl_module.xml`,
  `ngx_http_log_module.xml` (`combined` format), `ngx_http_core_module.xml` (`$request`, `$uri`)
- Vite 8.3.4 docs: https://github.com/vitejs/vite/blob/v8.3.4/docs/guide/features.md (section "Content Security Policy"),
  https://github.com/vitejs/vite/blob/v8.3.4/docs/config/build-options.md ; npm metadata
  https://registry.npmjs.org/vite/8.3.4 (`engines`), https://registry.npmjs.org/vite (dist-tags, publish time)
- MDN (reference docs, mdn/content): CSP `style-src`
  https://github.com/mdn/content/blob/main/files/en-us/web/http/reference/headers/content-security-policy/style-src/index.md ,
  `frame-ancestors`, `connect-src`; EventSource https://github.com/mdn/content/blob/main/files/en-us/web/api/eventsource/index.md
- Node.js release schedule https://raw.githubusercontent.com/nodejs/Release/main/schedule.json ; release index
  https://nodejs.org/dist/index.json ; https://nodejs.org/dist/v22.23.3/SHASUMS256.txt (+ `.sig` present)
- NodeSource: https://github.com/nodesource/distributions/blob/master/scripts/deb/setup_22.x ,
  https://github.com/nodesource/distributions/blob/master/DEV_README.md , README.md (now only points to DEV_README);
  issue #1920 "Debian repositories still using SHA1, which policy no longer allows as of 02/01/26"
  https://github.com/nodesource/distributions/issues/1920
- Podman v5.4.2: `docs/tutorials/rootless_tutorial.md`, `troubleshooting.md` (§17 lingering, §26 resource-limit
  delegation), `docs/source/markdown/podman-systemd.unit.5.md`, `docs/source/markdown/options/{read-only,read-only-tmpfs,userns.container,pids-limit,memory,network}.md`,
  `libpod/define/info.go` (https://github.com/containers/podman/tree/v5.4.2); `RELEASE_NOTES.md` on `main`
  (6.1.0 … 4.x) https://github.com/containers/podman/blob/main/RELEASE_NOTES.md
- GHSA-4hq8-gpf5-8p68 / CVE-2026-57231 (Podman env leak):
  https://github.com/podman-container-tools/podman/security/advisories/GHSA-4hq8-gpf5-8p68
- systemd v257 (trixie's major version): `units/user@.service.in`, `network/99-default.link`, `man/systemd.link.xml`,
  `man/systemd-sleep.conf.xml`, `man/systemd.resource-control.xml`, `man/systemd-suspend.service.xml`
  (https://github.com/systemd/systemd/tree/v257)
- NetworkManager 1.52.0: `src/libnm-core-impl/nm-setting-wired.c`, `src/core/devices/nm-device-ethernet.c`
  (https://github.com/NetworkManager/NetworkManager/tree/1.52.0)
- Linux v6.12 (trixie's kernel line): `Documentation/admin-guide/pm/sleep-states.rst`,
  `Documentation/ABI/testing/sysfs-devices-power` (https://github.com/torvalds/linux/tree/v6.12)
- wakeonlan 0.50 (upstream of Debian's `wakeonlan`): https://github.com/jpoliv/wakeonlan/blob/master/wakeonlan
- Ollama v0.40.4 `docs/gpu.mdx` ("Linux Suspend Resume"): https://github.com/ollama/ollama/blob/v0.40.4/docs/gpu.mdx
- Launchpad mirror of the Debian archive, publishing history (sid/experimental/bookworm only):
  https://launchpad.net/debian/+source/nginx/+publishinghistory , …/podman/…, …/nodejs/…
- Repo state read for this re-check: `infra/ansible/roles/{nginx,nodejs,podman,wol_target,common}`,
  `infra/systemd/{hermclaw-searxng.container.j2,hermclaw-exec-worker.service.j2,hermclaw-wol@.service}`,
  `config/hosts.example.yaml`, `ui/package.json`, `ui/vite.config.ts`, `ui/dist/index.html`, `ui/src/lib/sse.ts`,
  `ui/src/components/Artifacts.tsx`, `hermclaw/api/routes.py`, `hermclaw/api/app.py`, `hermclaw/wol/probes.py`,
  `worker/execution/sandbox.py`

Primary pages seen only through search snippets (`[P/snippet]`, not fetchable from here):
- Debian Package Tracker / changelog nginx 1.26.3-3+deb13u9 (2026-09-15, trixie-security) and DSA-6496-1/-2:
  https://tracker.debian.org/pkg/src:nginx , https://tracker.debian.org/media/packages/n/nginx/changelog-1.26.3-3deb13u9 ,
  https://security-tracker.debian.org/tracker/DSA-6496-1 ; LWN advisory list https://lwn.net/Articles/1094644/
- Debian tracker podman (trixie 5.4.2+ds1-2) and Security Tracker CVE-2026-57231 (trixie "vulnerable", bug #1141502):
  https://tracker.debian.org/pkg/src:podman , https://security-tracker.debian.org/tracker/CVE-2026-57231
- piuparts log podman 5.4.2+ds1-2+b1 (Depends/Recommends): https://piuparts.debian.org/trixie2next/pass/podman-remote_5.4.2+ds1-2+b1.log
- Debian tracker nodejs (trixie 20.19.2+dfsg-1+deb13u2): https://tracker.debian.org/pkg/src:nodejs
- packages.debian.org crun trixie 1.21-1: https://packages.debian.org/uk/trixie/i386/crun ; catatonit 0.2.1-2: https://sources.debian.org/patches/catatonit
- Apple "Requirements for trusted certificates in iOS 13 and macOS 10.15": https://support.apple.com/103769

Secondary (`[S]`, marked wherever used):
- ioBroker forum posts on trixie + NodeSource (apt log `node_22.x nodistro … 22.19.0-1nodesource1`, SHA1 rejection, March 2026):
  https://forum.iobroker.net/topic/81187/upgrade-von-debian-12-bookworm-auf-debian-13-trixie/113 , https://forum.iobroker.net/topic/84314
- linuxcapable / computingforgeeks Node.js on Debian 13 guides (Debian ships 20.19.2; do not mix Debian `npm`):
  https://linuxcapable.com/how-to-install-node-js-on-debian-linux/
- ArchWiki Wake-on-LAN (systemd.link must copy 99-default.link; BIOS "PCI power up", ErP off): https://wiki.archlinux.org/title/Wake-on-LAN
- Older copy of Debian's stock `nginx.conf` (`gzip on;`, `gzip_types` commented): https://gist.github.com/didinkaj/791fe6092761f6e4b4bc6ea3a383ff45
- Tenable plugin 323664 (CVE-2026-57231 "unpatched"): https://www.tenable.com/plugins/nessus/323664
- LWN trixie overview (systemd 257, kernel 6.12): https://lwn.net/Articles/1033740/

## Relevant facts

### A. nginx and SSE (`.223`)

| # | Fact | Source |
|---|---|---|
| A1 | trixie ships the **nginx 1.26.3** branch with backported fixes. Current stable-security build **1.26.3-3+deb13u9** (changelog 2026-09-15, DSA-6496-2 dated 2026-09-16, a regression fix for DSA-6496-1 = deb13u8: CVE-2026-42533, CVE-2026-56434, CVE-2026-60005). The tracker still lists 4 open security issues for trixie. Upstream is at 1.30.4 (sid 1.30.4-7, 2026-09-16). | [P/snippet] tracker, DSA; [P] Launchpad sid history |
| A2 | `proxy_buffering` (default `on`): when off, "the response is passed to a client synchronously, immediately as it is received". Buffering "can also be enabled or disabled by passing 'yes' or 'no' in the X-Accel-Buffering response header field" unless `proxy_ignore_headers` disables it. | [P] proxy module doc |
| A3 | `proxy_http_version`: "**Since 1.29.7, version 1.1 is used by default. Before 1.29.7, version 1.0** was used". On trixie's 1.26 it must be set to `1.1` explicitly (needed for upstream keepalive and chunked streaming). | [P] proxy module doc |
| A4 | Upstream `keepalive`: enabled by default only **since 1.29.7**; on 1.26 it needs an explicit `keepalive N;` plus `proxy_http_version 1.1` and `proxy_set_header Connection "";`. | [P] upstream module doc |
| A5 | `proxy_read_timeout` default **60s**, measured "only between two successive read operations"; the connection is closed if the upstream sends nothing in that time. The 15 s SSE heartbeat (config `events.heartbeat_seconds: 15`) keeps any value > 15 s alive. | [P] proxy module doc; repo config |
| A6 | `gzip_types` default is `text/html` only. `text/event-stream` is compressed only if it is listed (or `*`). Debian's stock `nginx.conf` has `gzip on;` with `gzip_types` commented out, so `gzip off;` in the SSE location is defensive but correct. | [P] gzip module doc; [S] older Debian nginx.conf copy |
| A7 | `add_header` directives "are inherited from the previous configuration level **if and only if there are no add_header directives defined on the current level**". The `add_header_inherit` directive exists only since **1.29.3**, so it is **not available in 1.26**: every location that sets its own `add_header` must repeat the CSP and security headers. | [P] headers module doc |
| A8 | `http2 on;` (server level) is available since 1.25.1. The `ssl_protocols` default is `TLSv1.2 TLSv1.3` since 1.23.4. A server-level `ssl_protocols` may be taken from the port's default server. | [P] v2/ssl module docs |
| A9 | Over HTTP/1.1, browsers allow only **6 connections per browser+host**, across all tabs (Chrome/Firefox "won't fix"). With HTTP/2 the limit is the number of negotiated streams (default 100). The UI opens up to two EventSources per tab (`/jobs/{id}/events/stream`, `/events/stream`), so without HTTP/2 a few tabs block all further API requests. | [P] MDN EventSource; repo `ui/src/lib/useEvents.ts` |
| A10 | Vite CSP guidance: without `html.cspNonce`, nothing inline is needed in a production build. Small assets are inlined as `data:` URIs unless `build.assetsInlineLimit: 0`. "Do not allow `data:` for `script-src`". The modulepreload polyfill is injected into the entry chunk (not an inline script). | [P] Vite 8.3.4 features.md, build-options.md |
| A11 | Repo build check: `ui/vite.config.ts` already sets `assetsInlineLimit: 0`. `ui/dist/index.html` has only `<script type="module" crossorigin src="/assets/…js">` and a stylesheet link, with **no inline script/style**. The bundle has no `eval`/`new Function`. Images and videos are shown from **`blob:` URLs** (`URL.createObjectURL`, `Artifacts.tsx`), so `img-src`/`media-src` need `blob:`. React sets styles through the `style` property (CSSOM), which `style-src 'self'` does not block (MDN: properties "set directly on the element's style property will not be blocked"). | [P] repo; MDN style-src |
| A12 | `frame-ancestors` "is not supported in the `<meta>` element", so the CSP must be sent as an HTTP header by nginx. | [P] MDN frame-ancestors |
| A13 | The API middleware already sends `X-Accel-Buffering: no` and `Cache-Control: no-cache` for `text/event-stream` (`hermclaw/api/routes.py`). The SSE location regex `^/api/(jobs/[^/]+/)?events/stream$` covers both stream routes (`hermclaw/api/events.py`). | [P] repo |
| A14 | EventSource cannot send headers, so the UI passes the bearer token as **`?access_token=`** on `/stream` URLs (`ui/src/lib/sse.ts`). nginx's predefined `combined` format logs `"$request"`, which is the "full original request line" including the query string, so tokens land in `/var/log/nginx/hermclaw-next.access.log`. `$uri` is the "current URI in request, normalized" (no arguments; it may change on internal redirects). nginx error-log lines (e.g. upstream timeouts) also contain the request line (operational knowledge, not re-verified). | [P] repo; [P] nginx log and core module docs |
| A15 | The orchestrator's HTTP readiness probe uses `follow_redirects=False` and accepts only **2xx** (`hermclaw/wol/probes.py`). `config/hosts.example.yaml` probes nginx at `port: 80, http_path: /`. Once port 80 redirects to HTTPS, that probe fails. | [P] repo |
| A16 | `hermclaw/api/app.py` starts uvicorn with `proxy_headers=True, forwarded_allow_ips="*"`, so any LAN client that reaches `:8000` directly can spoof `X-Forwarded-For`/`-Proto`. | [P] repo |
| A17 | `infra/nginx/` contains only `.gitkeep`. The role template comment says "the deployment phase replaces it with infra/nginx/hermclaw-next.conf (TLS, rate limits, headers)", but that file does not exist yet. | [P] repo |
| A18 | Apple platforms (iOS 13+/macOS 10.15+) reject TLS server certs issued after 2019-07-01 unless they meet all of these: validity **≤ 825 days**, EKU **id-kp-serverAuth**, DNS name in **SAN** (CN is not trusted), RSA ≥ 2048 bits, SHA-2. | [P/snippet] Apple 103769 |

### B. Node.js 22 / Vite 8 (UI build)

| # | Fact | Source |
|---|---|---|
| B1 | Node 22 "Jod": LTS since 2024-10-29, **maintenance since 2025-10-21, end-of-life 2027-04-30**. Node 24 "Krypton": active LTS until 2026-10-20, EOL 2028-04-30. Node 26: LTS from 2026-10-28, EOL 2029-04-30. **Node 20 reached EOL on 2026-04-30.** | [P] nodejs/Release schedule.json |
| B2 | Latest 22.x is **v22.23.3** (2026-09-23, npm 10.9.9). The last 22.x **security** release is **v22.23.2** (2026-07-28). Tarball `node-v22.23.3-linux-x64.tar.xz` sha256 `df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de` (SHASUMS256.txt, signed `.sig` available). | [P] nodejs.org dist index, SHASUMS256.txt |
| B3 | **Vite 8.3.4** (npm `latest`, published 2026-10-08) declares `engines.node: "^20.19.0 \|\| >=22.12.0"`. `ui/package.json` pins `vite 8.3.4` and `engines.node >=22.12.0`. | [P] npm registry; repo |
| B4 | Debian trixie's own nodejs is **20.19.2+dfsg-1+deb13u2** (stable and stable-security), with 11 open security issues listed for trixie. That is an upstream-EOL line (B1). It technically satisfies Vite's `^20.19.0` but not the repo's `>=22.12.0`. No nodejs in trixie-backports was found. sid now has 24.21.0. | [P/snippet] tracker; [P] Launchpad sid history |
| B5 | NodeSource `setup_22.x` (current master) does this: installs `apt-transport-https ca-certificates curl gnupg`, writes the key from `https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key` via `gpg --dearmor` to `/usr/share/keyrings/nodesource.gpg`, writes the deb822 file `/etc/apt/sources.list.d/nodesource.sources` (`URIs: https://deb.nodesource.com/node_22.x`, `Suites: nodistro`, `Components: main`, `Architectures: amd64\|arm64`, `Signed-By:`), and pins `nodejs` and `nsolid` with `Pin: origin deb.nodesource.com` / `Pin-Priority: 600`. Documented usage: `curl -fsSL https://deb.nodesource.com/setup_22.x -o nodesource_setup.sh && bash nodesource_setup.sh && apt install -y nodejs`. | [P] setup_22.x, DEV_README |
| B6 | NodeSource's own support table still lists Debian 10–12 only (no Debian 13 row). The `nodistro` suite is distribution-independent ("install Node.js on almost any distro"). A trixie user log shows `node_22.x nodistro … nodejs 22.19.0-1nodesource1` installing fine. | [P] DEV_README; [S] ioBroker forum |
| B7 | Since **2026-02-01**, trixie's apt (Sequoia `sqv`) rejects SHA1 binding signatures. NodeSource repos failed with "Signing key on 6F71F525282841EEDAF851B42F59B5F99B1BE0B4 is not bound … SHA1 is not considered secure since 2026-02-01". Issue #1920 was closed 2026-02-05; users confirm that **re-downloading the current key** fixes it ("You shouldn't need to update that nodesource.sources file at all. Just the key."). An old cached key still gives a "will reject signature within a year" warning. | [P] nodesource issue #1920; [S] ioBroker |
| B8 | Installing Debian's `npm` package on top of NodeSource's `nodejs` (which bundles npm) causes a package conflict. | [S] linuxcapable |

### C. Rootless Podman 5 (`.222` sandbox, `.225` SearXNG)

| # | Fact | Source |
|---|---|---|
| C1 | trixie: **podman 5.4.2+ds1-2** (binNMU `+b1`/`+b2`), no `+deb13u` update found, no trixie-backports podman found. Upstream: 5.8.6 / 6.1.x. Debian sid: 5.8.6+ds1-3 (2026-10-12), experimental 6.1.1+ds1-1. | [P/snippet] tracker, piuparts; [P] Launchpad sid history |
| C2 | podman 5.4.2+ds1-2 **Depends**: conmon, `crun \| runc`, golang-github-containers-common, netavark, libsubid5, …; **Recommends**: buildah, ca-certificates, `catatonit \| tini \| dumb-init`, containers-storage, criu, dbus-user-session, **passt, slirp4netns, uidmap**. With `--no-install-recommends` (as the role installs), these must be listed explicitly. trixie crun is **1.21-1**, catatonit 0.2.1-2. | [P/snippet] piuparts log, packages.debian.org |
| C3 | **CVE-2026-57231** (GHSA-4hq8-gpf5-8p68, CVSS 7.5, published 2026-06-24): an image `Env` entry with a key and no value makes `podman run` copy that variable's **host value** into the container (`*` copies all). Affects 1.8.1–5.8.3, fixed in **5.8.4 / 6.0.0**. Workaround: do not run untrusted images, and check `podman image inspect --format '{{.Config.Env}}'` for entries without `=`. Debian Security Tracker lists trixie 5.4.2+ds1-2 as **vulnerable** (unfixed, bug #1141502). The tracker also lists CVE-2026-55686 (WORKDIR symlink, ≤ 5.7.1) as open for trixie. | [P] GHSA; [P/snippet] security tracker; [S] Tenable |
| C4 | pasta (package `passt`) is the default rootless network since Podman 5.0 (`default_rootless_network_cmd = "pasta"` in containers.conf `[network]`). **Podman 6.0 removed slirp4netns, cgroup v1, CNI, iptables and BoltDB**. This does not matter on trixie (5.4.2), but it does for the next Debian. | [P] rootless tutorial; RELEASE_NOTES 6.0.0 |
| C5 | `/etc/subuid` and `/etc/subgid` need a unique, non-overlapping range per user, e.g. `usermod --add-subuids 100000-165535 --add-subgids 100000-165535 <user>`. After changing them, run `podman system migrate` as that user (it stops the user's containers and the pause process). | [P] rootless tutorial |
| C6 | Without lingering, rootless containers exit when the user's session ends. Fix: `loginctl enable-linger <user>`. | [P] troubleshooting §17 |
| C7 | systemd **v257 `user@.service` ships `Delegate=pids memory cpu`** (plus `DelegateSubgroup=init.scope`), so `--cpus`, `--memory` and `--pids-limit` work rootless without a drop-in. **`cpuset` and `io` are not delegated by default**, which is what `--cpuset-cpus`, `--cpuset-mems`, `--device-*-bps`/`-iops` and `--blkio-weight` need. Podman's documented fix is `/etc/systemd/system/user@.service.d/delegate.conf` with `[Service]` + `Delegate=memory pids cpu cpuset`, then log out and back in. Without delegation, crun reports "the requested cgroup controller `cpu` is not available". | [P] systemd v257 unit; Podman troubleshooting §26 |
| C8 | `Delegate=` "Takes either a boolean argument or a (possibly empty) list of control group controller names … Assigning the empty string will enable delegation, but reset the list". It is only safe on the unified (v2) hierarchy. | [P] systemd.resource-control(5) v257 |
| C9 | Check delegation with `cat "/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers"`. `podman info` exposes `host.cgroupVersion`, `host.cgroupControllers`, `host.cgroupManager`, `host.security.rootless`, `host.rootlessNetworkCmd` (Go-template names `.Host.CgroupsVersion`, `.Host.CgroupControllers`, `.Host.CgroupManager`, `.Host.Security.Rootless`, `.Host.RootlessNetworkCmd`). | [P] troubleshooting §26; `libpod/define/info.go` v5.4.2 |
| C10 | `--read-only-tmpfs` defaults to **true**: with `--read-only` Podman still mounts writable tmpfs on `/dev`, `/dev/shm`, `/run`, `/tmp`, `/var/tmp`. Only `--read-only-tmpfs=false` makes the container fully read-only. `--pids-limit` defaults to 2048 when the pids controller is available. `--network=none` creates a netns "but do[es] not configure network interfaces". `--userns=keep-id` maps the host UID to the same UID in the container (rootless). | [P] Podman 5.4.2 option docs |
| C11 | The repo sandbox already uses `--read-only --read-only-tmpfs=false --tmpfs /tmp… --tmpfs /dev/shm… --network=none --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges --pids-limit --memory --memory-swap=<same> --cpus --http-proxy=false --log-driver=none`. When it needs network, its auto mode picks pasta for rootless and falls back to slirp4netns. **`engine_env` inherits the full `os.environ` of the worker** whenever passthrough variables are used (`worker/execution/sandbox.py:983`), and the podman process inherits it otherwise. | [P] repo |
| C12 | Rootless Quadlet search path: `$XDG_RUNTIME_DIR/containers/systemd/`, `$XDG_CONFIG_HOME/containers/systemd/` (or `~/.config/containers/systemd/`), **`/etc/containers/systemd/users/$(UID)`**, `/etc/containers/systemd/users/`. Files in `users/` apply to all users and files in `users/${UID}/` only to that UID. Generated units are transient; only `Alias`, `WantedBy` and `RequiredBy` from `[Install]` are applied by the generator. `/etc/containers/systemd/users` has been supported since 4.6.0, and the subfolder bug was fixed in 5.4.0. The `podman quadlet install/list/rm` CLI exists only **since 5.6.0** (not in trixie). | [P] podman-systemd.unit(5) v5.4.2; RELEASE_NOTES |
| C13 | The image pull on Quadlet start can exceed systemd's default 90 s start timeout, so pre-pull the image or set `TimeoutStartSec=` (the repo template uses 300 s). | [P] podman-systemd.unit(5) |

### D. Wake-on-LAN and suspend (`.222`, `.224`)

| # | Fact | Source |
|---|---|---|
| D1 | `.link` files are sorted across `/usr/lib`, `/usr/local/lib`, `/run` and `/etc/systemd/network`. "**The first (in lexical order) of the link files that matches a given device is applied**". systemd ships `99-default.link`, so a user `.link` must sort earlier. Drop-ins `foo.link.d/*.conf` are supported and take precedence over the main file. Diagnose with `SYSTEMD_LOG_LEVEL=debug udevadm test-builtin net_setup_link /sys/class/net/<if>`. | [P] systemd.link(5) v257 |
| D2 | `WakeOnLan=` accepts `off` or a list of `phy unicast multicast broadcast arp magic secureon`. It "Defaults to unset, and the device's default will be used". | [P] systemd.link(5) v257 |
| D3 | `99-default.link` (v257) contains `[Match] OriginalName=*` and `[Link] NamePolicy=keep kernel database onboard slot path`, `AlternativeNamesPolicy=database onboard slot path mac`, `MACAddressPolicy=persistent`. A custom full `.link` that omits these changes the interface naming. ArchWiki: "The content of the default link file … has to be included, otherwise the interface might be misconfigured". | [P] systemd v257; [S] ArchWiki |
| D4 | NetworkManager 1.52: `802-3-ethernet.wake-on-lan` defaults to `default` (use the global `ethernet.wake-on-lan`). If that is also unset, NM falls back to **`ignore`, so it does not touch the NIC's WoL setting**. To make NM manage it: `nmcli connection modify <profile> 802-3-ethernet.wake-on-lan magic`. | [P] NM source nm-setting-wired.c, nm-device-ethernet.c |
| D5 | `/sys/devices/…/power/wakeup` "allows the user space to check if the device is enabled to wake up the system from sleep states, such as … suspend to RAM … and hibernation". `/sys/power/mem_sleep` lists `s2idle`, `shallow`, `deep`; the default is usually `deep`, otherwise `s2idle`. | [P] kernel v6.12 ABI doc, sleep-states.rst |
| D6 | systemd-sleep: suspend is allowed by default if the kernel supports it (`AllowSuspend=`). `MemorySleepMode=` writes `/sys/power/mem_sleep` (default empty = kernel default). Executables in `/usr/lib/systemd/system-sleep/` run with `pre`/`post` and the action, and **`user.slice` is frozen while they run**. | [P] systemd-sleep.conf(5), systemd-suspend.service(8) v257 |
| D7 | BIOS/UEFI: WoL must be enabled (vendor names such as "PCI power up", "Power On By PCI-E"), and **ErP/EuP must be off**, otherwise the NIC is unpowered in S5. | [S] ArchWiki; R-016 (Thomas-Krenn) |
| D8 | `wakeonlan` 0.50 (upstream of Debian's package): default target `255.255.255.255`, default port `discard`/9, uses `SO_BROADCAST`; options `-i <ip>` and `-p <port>`. Hermclaw's own sender (`hermclaw/wol/magic.py`) does the same with `SO_BROADCAST`. `hosts.yaml` uses the directed broadcast `192.168.178.255`. | [P] wakeonlan source; repo |
| D9 | `.224`: "after a suspend/resume cycle, sometimes Ollama will fail to discover your NVIDIA GPU, and fallback to running on the CPU". Workaround: `rmmod nvidia_uvm && modprobe nvidia_uvm`. | [P] Ollama docs/gpu.mdx v0.40.4 |
| D10 | Repo: `wol_target` installs `hermclaw-wol@.service` (oneshot `ethtool -s %I wol g`, `BindsTo` the device, `Before=network-pre.target`), asserts `Supports Wake-on` contains `g` and `Wake-on: g`. `ethtool` is in `common_base_packages`. The sudoers allow-list for the runtime user is `/usr/bin/systemctl suspend` on workers. Nothing re-applies WoL after resume, and `power/wakeup` is not checked. | [P] repo |

## Decision / recommendation for Hermclaw

### 1. nginx on `.223` (package `nginx` from trixie main + trixie-security)
- Install `nginx` from Debian. Require **≥ 1.26.3-3+deb13u9** and keep `trixie-security` enabled.
  The nginx.org mainline packages are not needed: all directives used below exist in 1.26.
- Write the deployment config as `infra/nginx/hermclaw-next.conf` (or extend the role template) with the
  following content. `{{ name }}` is the LAN DNS name that actually resolves, e.g. `hermclaw.fritz.box` on a
  FRITZ!Box (`192.168.178.0/24` is the FRITZ!Box default net – assumption) or `hermclaw.home.arpa`.

```nginx
# /etc/nginx/sites-available/hermclaw-next.conf  (top level of a site file = http context on Debian)
log_format hermclaw_noquery '$remote_addr - $remote_user [$time_local] "$request_method $uri $server_protocol" '
                            '$status $body_bytes_sent "$http_referer" "$http_user_agent" $request_time';

upstream hermclaw_api {
    server 192.168.178.225:8000;
    keepalive 16;                      # 1.26: no implicit keepalive (A4)
}

server {                               # port 80: health + redirect only
    listen 80;
    listen [::]:80;
    server_name {{ name }} 192.168.178.223;
    server_tokens off;
    location = /healthz { access_log off; default_type text/plain; return 200 "ok\n"; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;                          # >= 1.25.1; lifts the 6-connections limit for SSE (A9)
    server_name {{ name }} 192.168.178.223;

    ssl_certificate     /etc/nginx/tls/hermclaw-web.crt;
    ssl_certificate_key /etc/nginx/tls/hermclaw-web.key;   # root:root 0600
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:hermclaw_tls:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    root /var/www/hermclaw-next;
    index index.html;
    server_tokens off;
    client_max_body_size 64m;
    access_log /var/log/nginx/hermclaw-next.access.log hermclaw_noquery;   # never log ?access_token= (A14)
    error_log  /var/log/nginx/hermclaw-next.error.log warn;

    include /etc/nginx/snippets/hermclaw-headers.conf;

    location ~ ^/api/(jobs/[^/]+/)?events/stream$ {          # SSE
        proxy_pass http://hermclaw_api;
        proxy_http_version 1.1;                                # 1.26 default is 1.0 (A3)
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;                                   # API also sends X-Accel-Buffering: no
        proxy_cache off;
        gzip off;
        proxy_read_timeout 1h;                                 # > 15 s heartbeat (A5)
        proxy_send_timeout 1h;
    }

    location /api/ {
        proxy_pass http://hermclaw_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location / { try_files $uri $uri/ /index.html; }

    location = /index.html {
        include /etc/nginx/snippets/hermclaw-headers.conf;     # 1.26: add_header is not merged (A7)
        add_header Cache-Control "no-cache" always;
    }

    location /assets/ {
        include /etc/nginx/snippets/hermclaw-headers.conf;
        add_header Cache-Control "public, max-age=31536000, immutable" always;   # one header, no "expires"
    }
}
```

```nginx
# /etc/nginx/snippets/hermclaw-headers.conf
add_header Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; upgrade-insecure-requests" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "same-origin" always;
add_header X-Frame-Options "DENY" always;
add_header Cross-Origin-Opener-Policy "same-origin" always;
add_header Cross-Origin-Resource-Policy "same-origin" always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), usb=()" always;
# add_header Strict-Transport-Security "max-age=31536000" always;   # only once every client trusts the CA and uses the DNS name
```

  - CSP rationale: A10–A12. `blob:` only for `img-src`/`media-src` (artifact previews). No `'unsafe-inline'`,
    no `data:`, no `'unsafe-eval'`. If `<meta name="hermclaw-api-base">` is ever set to another origin, add that
    origin to `connect-src`. Trusted Types (`require-trusted-types-for 'script'`) should first be trialled as
    `Content-Security-Policy-Report-Only`.
  - Deploy gate: `nginx -t`, then `curl -sI https://{{ name }}/ | grep -i content-security-policy` and an SSE smoke test:
    `curl -N -H 'Accept: text/event-stream' "https://{{ name }}/api/events/stream?access_token=…"` must print
    `: heartbeat`-style comments about every 15 s without stalling.
- **TLS with an internal CA** (OpenSSL 3.x from trixie). Keep the CA key encrypted and offline (admin workstation,
  backed up with the secrets backup). Issue the leaf cert for ≤ **397 days**, ECDSA P-256, with SAN DNS + IP and EKU
  serverAuth (A18):

```bash
umask 077; mkdir -p ~/hermclaw-ca && cd ~/hermclaw-ca
cat > ca.cnf <<'EOF'
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = Hermclaw LAN Root CA
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
openssl req -x509 -config ca.cnf -newkey ec -pkeyopt ec_paramgen_curve:P-256 -sha256 -days 3650 \
  -keyout ca.key -out ca.crt                       # prompts for a CA key passphrase
cat > web.cnf <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = hermclaw.home.arpa
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:hermclaw.home.arpa, IP:192.168.178.223
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF
openssl req -new -config web.cnf -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc \
  -keyout hermclaw-web.key -out hermclaw-web.csr
openssl x509 -req -in hermclaw-web.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 397 -sha256 \
  -extfile web.cnf -extensions v3_leaf -out hermclaw-web.crt
openssl verify -CAfile ca.crt hermclaw-web.crt
openssl x509 -in hermclaw-web.crt -noout -subject -enddate -ext subjectAltName,extendedKeyUsage
# on .223
sudo install -d -m 0755 /etc/nginx/tls
sudo install -m 0644 hermclaw-web.crt /etc/nginx/tls/ && sudo install -m 0600 hermclaw-web.key /etc/nginx/tls/
sudo nginx -t && sudo systemctl reload nginx
# Debian/Ubuntu clients and .225 (if it ever calls the UI over HTTPS)
sudo install -m 0644 ca.crt /usr/local/share/ca-certificates/hermclaw-lan-ca.crt && sudo update-ca-certificates
```
  Firefox uses its own certificate store, so import `ca.crt` there or distribute it by enterprise policy. Browsers
  on other OSes import it into their OS trust store. These client steps were not re-verified in this session.
- `.223` → `.225:8000` stays plain HTTP inside the LAN (unchanged architecture). Restrict uvicorn's
  `forwarded_allow_ips` to `192.168.178.223,127.0.0.1` (A16), and allow `:8000` on `.225` only from `.223` and the
  workers (host firewall), so that `X-Forwarded-*` cannot be spoofed and the bypass of nginx headers is closed.

### 2. Node.js 22 for the UI build
- **Do not use Debian's `nodejs` 20.19.2** (B1, B4). Keep the `nodejs` role: NodeSource `node_22.x` / `nodistro`
  / `main`, `amd64`, pin `Pin: origin deb.nodesource.com` / `600`. Install only `nodejs` (it includes npm and
  corepack), **never Debian's `npm`** (B8).
- Raise the role minimum from `22.12.0` to **`22.23.2`** (last 22.x security release, B2). The current target is
  22.23.3. Vite 8.3.4 is satisfied (B3).
- Equivalent manual commands for INSTALLATION.md (same layout as `setup_22.x`, but with the key under
  `/etc/apt/keyrings`, and fetching the key **fresh**, which avoids the SHA1-binding failure B7):

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
sudo chmod 0644 /etc/apt/keyrings/nodesource.gpg
sudo tee /etc/apt/sources.list.d/nodesource.sources >/dev/null <<'EOF'
Types: deb
URIs: https://deb.nodesource.com/node_22.x
Suites: nodistro
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/nodesource.gpg
EOF
printf 'Package: nodejs\nPin: origin deb.nodesource.com\nPin-Priority: 600\n' | sudo tee /etc/apt/preferences.d/hermclaw-nodesource.pref
sudo apt-get update 2>&1 | tee /tmp/apt-update.log
grep -Ei 'sha1|policy|not bound|NO_PUBKEY' /tmp/apt-update.log && echo "fix the NodeSource key first"
apt-cache policy nodejs            # candidate must be 22.x from deb.nodesource.com
sudo apt-get install -y nodejs
node --version && npm --version    # v22.23.x, 10.9.x
```
- Fallback without NodeSource (if the repo or its key breaks again): use the official tarball, checksum-verified (B2):
  `curl -fsSLO https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz && curl -fsSLO https://nodejs.org/dist/v22.23.3/SHASUMS256.txt && grep ' node-v22.23.3-linux-x64.tar.xz$' SHASUMS256.txt | sha256sum -c - && sudo tar -xJf node-v22.23.3-linux-x64.tar.xz -C /opt && sudo ln -sfn /opt/node-v22.23.3-linux-x64 /opt/node22`
  (then `PATH=/opt/node22/bin:$PATH` for the build user).
- Node 22 is in maintenance and reaches EOL on **2027-04-30**. Record a follow-up to move the build to Node 24
  (EOL 2028-04-30) before then. Vite 8 accepts `>=22.12.0`, so the `engines` field stays valid.

### 3. Rootless Podman on trixie
- Packages (keep the role list; `--no-install-recommends` makes them explicit, C2):
  `podman crun uidmap passt slirp4netns fuse-overlayfs catatonit dbus-user-session systemd-container` from trixie main
  (podman 5.4.2+ds1-2, crun 1.21-1). `slirp4netns` stays only as the sandbox fallback; never rely on it for new
  features (C4).
- Sub-IDs: one unique range per Podman user, e.g. `hermclaw:100000:65536` in `/etc/subuid` and `/etc/subgid`.
  After any change, run `podman system migrate` as that user (C5).
- Lingering: `loginctl enable-linger hermclaw` (C6).
- Delegation drop-in (keep): `/etc/systemd/system/user@.service.d/delegate.conf` with `[Service]` +
  `Delegate=cpu cpuset io memory pids`. On trixie, `pids memory cpu` are already the default (C7); the drop-in adds
  `cpuset io` and makes the intent explicit. Apply with `systemctl daemon-reload` and a restart of `user@<uid>`
  (or a reboot) before the first container runs.
- Verification (playbook and `hermclaw doctor`):

```bash
U=$(id -u hermclaw)
cat /sys/fs/cgroup/user.slice/user-$U.slice/user@$U.service/cgroup.controllers   # expect: cpuset cpu io memory pids
cd / && sudo -u hermclaw env XDG_RUNTIME_DIR=/run/user/$U podman info --format \
  '{{.Host.CgroupsVersion}} {{.Host.CgroupManager}} {{.Host.CgroupControllers}} rootless={{.Host.Security.Rootless}} net={{.Host.RootlessNetworkCmd}}'
# expect: v2 systemd [cpuset cpu io memory pids] rootless=true net=pasta
cd / && sudo -u hermclaw env XDG_RUNTIME_DIR=/run/user/$U podman run --rm --network=none --read-only --read-only-tmpfs=false \
  --tmpfs /tmp:rw,size=64m --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=256 --memory=512m --memory-swap=512m --cpus=1 localhost/hermclaw-sandbox-generic:1 \
  sh -c 'id -u; cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.max; cat /proc/net/dev; touch /x 2>&1 || true'
# expect: host uid, 536870912, 256, "100000 100000", only "lo" in /proc/net/dev, "Read-only file system"
```
- Quadlet (SearXNG on `.225`): keep `/etc/containers/systemd/users/<uid>/hermclaw-searxng.container`. It is
  root-owned, applies to that UID only, and is supported in 5.4.2 (C12). Activate and check with
  `sudo systemctl --user -M hermclaw@ daemon-reload && sudo systemctl --user -M hermclaw@ start hermclaw-searxng.service`.
  Dry-run with `/usr/libexec/podman/quadlet -dryrun -user` (or `/usr/lib/podman/quadlet`; the repo test
  handles both). Do **not** document `podman quadlet list`, which only exists from 5.6.0.
- **CVE-2026-57231 mitigation (trixie podman is vulnerable, C3)**:
  1. Run only `localhost/hermclaw-sandbox-*` images built from the repo's Containerfiles; never pull and run
     arbitrary registry images in the sandbox.
  2. Before every run (or once at image build/registration), refuse images whose `podman image inspect --format '{{json .Config.Env}}'`
     contains an entry without `=`.
  3. Start the podman process with a **minimal environment** (`PATH`, `HOME`, `LANG`, `XDG_RUNTIME_DIR`,
     `DBUS_SESSION_BUS_ADDRESS` + the explicit passthrough names), not the worker's full `os.environ`.
  4. Re-check `apt-cache policy podman` and the tracker at every point release, and switch to a fixed Debian build
     as soon as one appears.

### 4. Wake-on-LAN and suspend
- BIOS/UEFI on `.222`/`.224`: enable Wake-on-LAN / "Power On By PCI-E", **disable ErP/EuP** (D7). If the board
  separates them, enable "wake from S5". Use the onboard wired NIC.
- Persistence: **keep the existing `hermclaw-wol@<if>.service` oneshot**. It works the same under ifupdown,
  systemd-networkd and NetworkManager, because NM's default is `ignore` (D4). It does not risk interface renaming
  the way a hand-written full `.link` file does (D1, D3). Accepted alternative (document only): a drop-in
  `/etc/systemd/network/99-default.link.d/50-hermclaw-wol.conf` with `[Link]` + `WakeOnLan=magic`. It keeps
  `NamePolicy`, but applies to every interface that `99-default.link` matches. If NM manages the NIC, either leave
  it at `ignore` or set `nmcli connection modify <profile> 802-3-ethernet.wake-on-lan magic`, never `default`
  together with a global override that disables WoL.
- Add a resume re-apply unit: some drivers drop the WoL setting across suspend, and the oneshot runs only at
  boot. The hardware behaviour could not be tested here.

```ini
# /etc/systemd/system/hermclaw-wol-resume@.service
[Unit]
Description=Hermclaw: re-apply Wake-on-LAN on %I after resume
After=suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ethtool -s %I wol g

[Install]
WantedBy=suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
```
- Verification on each worker:
  `ethtool <if> | grep -E 'Supports Wake-on|Wake-on'` must show `g` in both lines;
  `cat /sys/class/net/<if>/device/power/wakeup` must show `enabled`; `cat /sys/power/mem_sleep` should show
  `[deep]` (if only `s2idle` exists, WoL-from-suspend must be tested explicitly). Then run
  `sudo systemctl suspend`, send the packet from `.225`, and measure the time to SSH/worker-API readiness
  against `ping_timeout_seconds`/`service_timeout_seconds` in `hosts.yaml`.
- Sending: Hermclaw's own UDP sender to `192.168.178.255:9` (unchanged). Manual tests use `apt-get install wakeonlan`
  and `wakeonlan -i 192.168.178.255 -p 9 <MAC>` (D8). `etherwake` (raw Ethernet, root) is an optional second tool.
  Its `-i <if> -b` usage was not re-verified.
- Suspend stays `sudo /usr/bin/systemctl suspend` via the sudoers allow-list (unchanged).
- `.224` (GPU): after resume, Ollama may silently fall back to CPU (D9). Add a sleep hook that stops `ollama`
  before sleep and reloads `nvidia_uvm` and starts `ollama` after resume. This must be **tested on `.224`** and
  coordinated with R-026 (driver install method). Example:

```ini
# /etc/systemd/system/hermclaw-ollama-sleep.service
[Unit]
Description=Hermclaw: stop Ollama before sleep, reload nvidia_uvm after resume
Before=sleep.target
StopWhenUnneeded=yes

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/systemctl stop ollama.service
ExecStop=-/usr/sbin/modprobe -r nvidia_uvm
ExecStop=/usr/sbin/modprobe nvidia_uvm
ExecStop=/usr/bin/systemctl start --no-block ollama.service

[Install]
WantedBy=sleep.target
```
  The model worker's readiness check after wake must also assert GPU use (e.g. `ollama ps` PROCESSOR = GPU, or the
  `inference compute … library=CUDA` log line from R-026 E12), not only `/api/version`.

## Implementation consequences
- **INSTALLATION.md – web UI `.223`**: install `nginx`; create `/etc/nginx/tls/` with cert and key from the internal CA
  (commands above); install the site and `snippets/hermclaw-headers.conf`; run `nginx -t`; reload; verify the CSP header,
  the HTTP/2 negotiation (`curl -sI --http2 https://…` shows `HTTP/2 200`) and the SSE heartbeat via nginx. Install
  Node 22 (NodeSource commands or role), then `npm ci && npm run build` in `ui/` and rsync `ui/dist/` to
  `/var/www/hermclaw-next/`. Distribute `ca.crt` to client browsers, and add a calendar reminder to renew the leaf
  cert before day 397.
- **INSTALLATION.md – execution worker `.222`**: packages, sub-IDs, linger, delegation drop-in, reboot or `user@` restart,
  the `podman info`/smoke-test block above, building the sandbox images as the **same user** the worker runs as,
  and the CVE-2026-57231 note (local images only).
- **INSTALLATION.md – WoL (`.222`, `.224`)**: BIOS checklist (WoL on, ErP off), real MACs into the inventory and
  `hosts.yaml`, the verification block, and one suspend/wake cycle per host recorded as an acceptance step.
- **Deployment playbook (P32)**:
  - `nginx` role: TLS variables (`nginx_tls_enabled`, `nginx_tls_cert_path`, `nginx_tls_key_path`), the headers snippet
    included in every location that has its own `add_header`, `log_format hermclaw_noquery`, `http2 on`, a port-80
    `/healthz` + redirect server, and `nginx -t` as the gate (already present).
  - `config/hosts.yaml` (`hosts.example.yaml`): nginx service probe → `{name: nginx, port: 80, http_path: /healthz}`,
    because the probe accepts 2xx only, without redirects (A15). Optionally add a 443 TCP check.
  - `nodejs` role: `nodejs_min_version: "22.23.2"`; fail on sqv/SHA1 warnings in `apt-get update` output.
  - `podman` role: run `podman system migrate` as the user when a sub-ID range was added. Also assert
    `cgroup.controllers` and `rootlessNetworkCmd == pasta` (the existing `podman info` assert stays).
  - `wol_target` role: install and enable `hermclaw-wol-resume@<if>.service`. Assert
    `/sys/class/net/<if>/device/power/wakeup == enabled` (warn only, because some NICs expose no such file).
  - Model worker (`.224`): `hermclaw-ollama-sleep.service` after it has been validated on the hardware.
- **Runtime code (outside this doc's write scope, for the owning engineers)**: minimal podman environment and the
  image-`Env` check in `worker/execution/sandbox.py`; `forwarded_allow_ips` restricted in `hermclaw/api/app.py`;
  replace `?access_token=` on SSE with a short-lived, single-use stream ticket (the error log still contains the
  request line, A14).

## Open risks
- **Debian versions are snippet-level evidence only.** packages.debian.org, the tracker and the mirrors were
  unreachable, so nginx 1.26.3-3+deb13u9, podman 5.4.2+ds1-2, crun 1.21-1 and nodejs 20.19.2+dfsg-1+deb13u2 must be
  confirmed with `apt-cache policy` on the hosts before INSTALLATION.md is frozen.
- **CVE-2026-57231 is unfixed in trixie's podman** (tracker snippet; Tenable even flags "unpatched"). The fix in
  Debian may never come for 5.4.2. The mitigations above reduce but do not remove the risk. CVE-2026-55686 is also open.
- nginx 1.26 in trixie has 4 open security issues on the tracker. Keep unattended security updates on `.223`.
- **Node 22 EOL 2027-04-30**. NodeSource does not officially list Debian 13, and its signing key already broke
  once with trixie's sqv policy (2026-02). The tarball fallback is documented. The `sqv` policy may tighten further.
- **Access token in SSE query strings**: the access log is fixed by `hermclaw_noquery`, but error-log request lines
  and browser history still carry it until the API offers stream tickets.
- **HTTPS with a private CA** depends on every client importing the CA; certificate expiry (≤ 397 days) needs
  monitoring (e.g. a `hermclaw doctor` check for `notAfter`). HSTS has no effect when the UI is opened by IP
  (RFC 6797 – not re-fetched here), so only enable it for the DNS name.
- **WoL is hardware-dependent** and could not be tested from this sandbox: NIC drivers may reset WoL on resume or
  link-down, `s2idle`-only platforms may not wake by LAN, switch-port power saving (EEE) can interfere, and ErP
  defaults differ per board. Acceptance needs one real wake cycle per host.
- **`.224` suspend with NVIDIA**: GPU loss after resume (Ollama docs); the sleep-hook pattern is unvalidated on the
  Pascal + 580 `.run` driver setup from R-026. Restarting `user@<uid>` (delegation changes) kills user services
  such as SearXNG, so do it only during deployment.
- **Upstream Podman 6** has removed slirp4netns/cgroup v1/CNI/iptables. Debian 14 will need pasta-only configs.
  The sandbox's slirp4netns fallback is dead code there.
- The sandbox README uses user `hermclaw-exec` while `group_vars` and the podman role use `hermclaw`. If they
  differ on `.222`, sub-IDs, linger and delegation checks run for the wrong user. This must be aligned (not
  verified which one INSTALLATION.md will use).
