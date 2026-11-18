# Roadmap

The goal is reliable setup and recovery that opens **vanilla Minecraft**.
These are acceptance gates, not promised dates or platform guarantees.

| Next step | Done when |
| --- | --- |
| Qualify source setup | Follow the [fresh-install recipe](CLEAN_INSTALL.md) on an independently recorded Apple Silicon Mac; record prerequisites, exact source, setup/download/retry and save/reopen. A same-Mac smoke test or unbound M1 Air report alone does not close this gate. |
| Verify the compiled party fix | The exact rebuilt WinHTTP artifact passes Minecraft party creation/leave/recreate, invite reception, party joining and friend-world joining. The temporary in-game correction and isolated regression tests are supporting evidence. |
| Qualify a distributable app | A specific app passes first-run, update/recovery, save/restore, multiplayer and extended-session checks; corresponding source/notices, signing, notarization and downloaded-app checks are complete. |
| Establish a macOS Windows 11 ARM path | Verify an official ARM64 Bedrock client and a maintainable VM/graphics stack, preferring OSS components where practical; preserve macOS 27 Wine unless the entire vanilla game path passes acceptance. |
| Remove Python from the player path | Replace the frozen Python worker and player-facing setup, download, preparation, monitoring and lifecycle with Rust where feasible; demonstrate macOS 27 parity and a Python-free packaged/source player path before removing the current implementation. |

The [source preview](SOURCE_PREVIEW.md) gives contributors a versioned baseline.
[Release readiness](RELEASE.md) tracks binary gates; [compatibility](COMPATIBILITY.md)
separates source behavior, historical gameplay and current-candidate evidence.
Mullion ray tracing and native ARM game execution remain separate research tracks.

## macOS 28 Windows ARM migration

This migration is limited to macOS and the native Windows ARM Bedrock client.
Prefer open-source launcher, VM, translation, graphics, and integration
components where practical. Proprietary vendor components are allowed only
under the user's own licenses and after explicit, default-disabled acceptance
in `EULA.md`; that setting never bypasses Microsoft ownership verification.
No supporting VM code is implemented because no complete game path has been
validated.

Next gates:

1. Verify with Microsoft whether the user's owned game is available as a
   native ARM64 Windows package. The current repository downloader explicitly
   accepts an AMD64 executable; x64 emulation is not a substitute for this
   scope.
2. Test whether QEMU TCG can host an ARM64 Windows guest, and whether UTM's
   experimental DXMT DirectX 11 path works with that configuration. If a
   licensed Apple virtualization or graphics API is needed, identify it in
   `EULA.md`; verify the exact guest-driver source/license and the user's
   applicable vendor terms. Do not use Parallels.
3. If those prerequisites pass, build only a minimal VM/game launch handoff
   for Windows 11 ARM and the unmodified vanilla client. Preserve macOS 27
   Wine behavior and user data; do not add visual modifications or Linux
   support.
4. Require a real Apple Silicon macOS 28 test of ownership/install, native ARM
   client execution, DirectX 11 graphics, save/reopen, VM lifecycle and clean
   setup before integrating or claiming support.
5. In parallel with a technically viable ARM client path, migrate the shipped
   worker and player-facing operations to Rust, preserving the existing macOS
   27 path until parity and data-preservation checks pass. Replace relevant
   test/release tooling with Rust or shell where practical; leave unrelated
   experiments alone. The current repository does not prove a direct
   Python/Rust version conflict, and the EULA gate still uses Python.

Until these gates pass, no VM adapter is claimed and macOS 28 support remains
unverified. See the
[compatibility evidence and blockers](COMPATIBILITY.md#macos-28-and-windows-11-arm).
