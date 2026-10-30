# Chromium worker seccomp profile

`chromium-seccomp.json` derives from Microsoft's Playwright profile at commit
`cfe490d941f992655948eee8fd4fd94e1c4234bf`, the `v1.64.1` release.

- Source file is [utils/docker/seccomp_profile.json](https://github.com/microsoft/playwright/blob/cfe490d941f992655948eee8fd4fd94e1c4234bf/utils/docker/seccomp_profile.json).
- Upstream SHA-256 is `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`.
- Trellis SHA-256 is `9c86a46bf3b2ef2c7cfece632b4ece1127698810857ef98914f87d99fdc74a27`.
- The upstream Apache-2.0 license is preserved in `chromium-seccomp.LICENSE`.

Trellis makes two changes. `clone3` returns `ENOSYS` (38) and remains denied.
This lets libc fall back to the existing permitted `clone` call. The `chroot`
syscall is permitted without the upstream container-capability condition.
Chromium needs it inside its new user namespace to enter an empty directory.
The worker still has no container capabilities, so the kernel rejects `chroot`
outside a user namespace that grants the required namespace-local capability.
Chromium's [namespace sandbox implementation](https://chromium.googlesource.com/chromium/src/+/lkgr/sandbox/linux/services/credentials.cc)
performs this operation before dropping its namespace capabilities.
The profile otherwise preserves the upstream default deny policy and the user
namespace permissions for `clone`, `setns`, and `unshare`.
The current [Moby profile](https://github.com/moby/profiles/blob/main/seccomp/default.json)
also returns `ENOSYS` for `clone3` when `CAP_SYS_ADMIN` is absent.

The Docker launcher supplies this file through `--security-opt seccomp=<path>`
alongside `no-new-privileges` and drops all capabilities. The image label records
the profile digest. A matching label is only a configuration check. Actual
Chromium sandbox startup must succeed in the restricted worker before rendering
is advertised. Do not grant `SYS_ADMIN`, disable seccomp, or disable Chromium's
sandbox to make the probe pass.

The worker keeps Playwright 1.64.1 and its Chromium build inside the immutable
`/ms-playwright` image directory. Its Python wheels and dependencies have hashes
for standard CPython 3.14 on Linux ARM64 and x86-64 in
`worker-requirements.txt`. Playwright's installer selects the browser revision
bundled with that pinned package and installs system packages at image build
time. Chromium downloads and apt repositories are upstream build inputs. The
operator's immutable worker image ID pins the resulting artifact for execution.
The launcher selects the full Chromium headless channel. The separate headless
shell lacks the `chrome://sandbox` diagnostics required by the readiness probe.

The worker directs `HOME` and XDG configuration/cache paths to its private bounded
`/tmp` filesystem. Chromium crash handling also needs writable configuration
[storage](https://chromium.googlesource.com/experimental/chromium/src/+/HEAD/docs/linux/debugging_minidump.md).
A read-only root filesystem must not send those writes to `/root` or a host home.
