# Compatibility

Updated November 18, 2026; local RC4 artifact evidence dates to October 7. **Historical observations and source behavior are not
release-candidate certification.** Local **0.3.0 RC4, build 6** has an inspected macOS 15.0 floor; gameplay
qualification remains pending. See [release readiness](RELEASE.md).

| Area | Evidence | Current-candidate status |
| --- | --- | --- |
| Apple Silicon | Required by launcher source | Target platform; clean-machine verification pending |
| M4 Pro, 24 GB, macOS 15.3.1 | Earlier standalone gameplay recorded September 30 | Historical only; not the new app bundle |
| MacBook Air M1, 16 GB | Maintainer reports it working; exact OS/revision/scenarios unrecorded | Reported working; clean-machine and exact-candidate qualification pending |
| Minimum macOS | 0.3.0 RC4 whole-bundle audit: 15.0 across 95 native files | Clean-M1 / game execution on the minimum OS untested |
| Intel Mac | Rejected by launcher hardware check | Unsupported |
| Rosetta | Required for x86-64 Wine/graphics/game path | Required; native ARM game execution is not shipped |
| macOS 28 and later | Apple says Rosetta will be limited to certain older, unmaintained games relying on Intel frameworks | Unverified: current runtime needs Rosetta; no OSS Windows ARM replacement is implemented or qualified |
| Minecraft edition | Helper checks Microsoft entitlement for Minecraft for Windows | A console/mobile purchase alone is not proof of this entitlement |
| Game version | Fresh replacement-helper output config: 1.26.5203.0; earlier gameplay notes use 1.26.52 | Catalog can change; RC3 in-game version is not yet recorded in the bounded session result |
| First owned-game download | Same-Mac RC2 reached sign-in/catalog; four initial HTTPS GETs returned 404 | Old route failed in RC2 and was retained in RC3; exact replacement helper passed outside app and is now in RC4. RC4 native first-run pending |
| Packaged graphics probes | Actual RC3 GPU presentation/readback/texture/discard passed; resources/signature intact | Probe pass only; game/font behavior and save acceptance pending |
| RC3 functioning / exit | User-confirmed same-Mac functioning with copied owned payload; normal return supported by UI/process cleanup | Bounded pass; save/reopen, soak and clean-M1 remain unverified |
| Local worlds | Earlier development save/reopen observed | Current candidate save/reopen and clean-M1 test pending |
| Xbox / PlayStation friend worlds | Maintainer estimates 10+ cumulative hours across repeated friend sessions (October 20 report); two monitored sessions in [gameplay evidence](history/GAMEPLAY.md#repeated-cross-platform-play) | New packaged-candidate join/disconnect/rejoin still needs recording |
| Minecraft Social parties | October 25 Create party failure traced to duplicate PlayFab HTTP writes; temporary callback-order correction succeeded in game; compiled source fix passed isolated native tests ([evidence](history/PARTY-WINHTTP-2026-10-25.md)) | October 31: installed compiled fix passed all three native write tests; host reported the friend-world retry worked on 26.60. Party creation/invites and broader joining acceptance remain unverified |
| Featured servers | Earlier lobby entry observed | Candidate untested |
| Realms | Earlier joined-Realm list observed | Active Realm entry untested |
| Marketplace / controllers | No complete qualification record | Untested; no blanket support claim |
| Custom skin import | October 19 local native checks passed dialog/cancellation and PNG import/rendering, including a Unicode filename | Picker crash no longer reproduces locally; #4 awaits reporter verification. 26.52 restart reset matches an [upstream bug](history/TESTING.md#october-20-skin-persistence-diagnosis); newer-game validation and world import/export regression pending |
| Offline launch | Account verified on Play | Unsupported |
| Store market | Native Settings saves a two-letter country code (default CA); Play and updates pass it to the helper | Offline propagation fixtures exist; real-account behavior in other markets remains unverified |
| Cancel while waiting for Keychain | Actual RC3 native Cancel stopped helper; no socket/prefix/game; imported payload key hashes unchanged | Passed only this stage; retry/authentication/gameplay remain untested |
| App/game updates | Manual app replacement; **Update Minecraft…** downloads a new game and retains backups | Candidate live upgrade and recovery test pending |
| Signing / Gatekeeper | Local ad-hoc builds; no public notarized app | Developer ID/notarization is an external blocker |

Record the actual Mac, macOS, app hash, game version, and scenario when reporting
a result. A menu, server list, or screenshot cannot prove a completed multiplayer
session. See [historical session details](STABILITY.md) and
[backup/update instructions](USER_GUIDE.md#your-files-backups-and-updates).

## macOS 28 and Windows 11 ARM

Apple's [Rosetta guidance](https://support.apple.com/en-us/102527) says Rosetta
is available on Apple silicon through macOS 27 and, starting with macOS 28, is
limited to certain older, unmaintained games that rely on Intel-based
frameworks. The guidance does not say whether a Windows game running through
Wine qualifies for that exception. Do not treat macOS 28 as supported based on
the current x86-64 capability check alone.

**Current implementation:** the macOS 27 Wine/Rosetta route is unchanged and
remains the default. There is no VM adapter or playable macOS 28 replacement.
Use OSS supporting components where practical. The proprietary Minecraft
client, Microsoft GDK/GameInput files, and any later vendor runtime components
may be used only after the user enables the current acceptance in `EULA.md`
and obtains them under their own vendor licenses. The opt-in is additional to,
not a replacement for, Microsoft's real account ownership check. No VM or
graphics stack has been shown to run the actual game.

**OSS candidate, not a validated path:** [QEMU's accelerator
overview](https://www.qemu.org/docs/master/system/introduction.html) lists its
TCG CPU emulator for macOS on Arm; TCG is software emulation and is the mode
that avoids Apple's proprietary Hypervisor.framework. QEMU also supports
Apple's `hvf` accelerator. The user permits licensed vendor virtualization
APIs other than Parallels; an HVF configuration could therefore be evaluated
under Apple's terms, but it would not be OSS. [UTM's
source](https://github.com/utmapp/UTM) describes an open-source QEMU front end
with ARM64 and TCG support, but also exposes HVF; a test must record which
accelerator is used and the applicable Apple terms.

Apple's [Virtualization framework documentation](https://developer.apple.com/documentation/virtualization)
describes macOS and Linux guests, not Windows. It does not establish a supported
Windows 11 ARM guest path or DirectX-capable graphics on macOS 28. This is a
documentation gap, not proof that the framework cannot boot Windows; a candidate
would still need an actual guest and graphics test.

The [UTM Windows guest graphics guide](https://docs.getutm.app/guest-support/windows/)
documents experimental accelerated DirectX graphics for Windows 11 guests
using its QEMU backend, while warning that game compatibility is limited and
the driver may destabilize the guest. It identifies DXMT for DirectX 11 and
D3DMetal for DirectX 12; on Apple silicon D3DMetal requires Rosetta, so it is
not the candidate graphics route. The docs do not establish that UTM can pair
an ARM64 Windows guest, forced TCG, and DXMT, nor that the native Bedrock client
works with it. UTM's guest graphics also rely on an Apple Metal renderer; if
this proprietary macOS API is used, it must be disclosed and covered by the
user's Apple license/terms. The precise guest-driver source and license in the
distributed UTM guest-tools image also need verification before including it
in a product.

[QEMU's `virtio-gpu` documentation](https://www.qemu.org/docs/master/system/devices/virtio/virtio-gpu.html)
calls 3D acceleration evolving and describes Linux guest-kernel support and
Linux-specific accelerated-host requirements; it does not document a Windows
ARM DirectX 11 gaming route on macOS. This is not a drop-in alternative to
UTM's experimental driver. UTM identifies its front end as Apache-licensed
with LGPL/GPL components; any adopted build still needs an exact dependency
and redistribution review. DXMT's current repository license/provenance is
recorded in [THIRD_PARTY.md](../THIRD_PARTY.md); no QEMU/UTM VM or game launch
has been tested for this repository.

**Blocking client/package facts:** the existing acquisition path explicitly
accepts only an AMD64 PE for `Minecraft.Windows.exe`
(`client/standalone-setup.py`). That proves the repo's downloaded client is
x64; it does not establish that Microsoft distributes a native ARM64 Bedrock
client. Microsoft's [Windows ARM emulation documentation](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation)
says Windows 11 can emulate x86 and x64 user-mode apps, but this migration
specifically targets the native ARM client, not an x64-emulation substitute.
A native ARM package must be verified from an official Microsoft source before
claiming the requested client exists or changing the downloader.

**Feasibility result (November 18, 2026):** no complete implementation is
supported by the evidence above. The current prepared-game check requires the
AMD64 machine type and labels the result `prepared_windows_x64_game`; Windows
ARM's documented x64 emulation does not meet the native-client requirement.
Apple's framework overview does not document Windows guests, and UTM's
DirectX-on-Windows path is explicitly experimental and has not been shown here
to run this game. The official sources reviewed do not verify a native ARM64
Minecraft for Windows package, Windows guest graphics, or vanilla gameplay on
macOS 28. These are unverified prerequisites, not proof that Microsoft has no
such package or that a VM route is impossible. Do not add a VM launcher,
change the AMD64 package acceptance, or claim macOS 28 support until those
prerequisites and real gameplay are validated. The existing macOS 27
Wine/Rosetta path remains unchanged; no speculative VM scaffolding is added.

Before implementation, establish that a user-supplied, licensed Windows 11 ARM
guest can be run with only permitted OSS components, identify a native ARM64
Bedrock package, and verify its ownership/install path, DirectX 11 APIs, and
vanilla gameplay. Document each vendor component in `EULA.md` before enabling
its download/use; keep the setting disabled until the user accepts. No Linux
support, Parallels, x64-client substitution, or visual modifications are part
of this migration.
