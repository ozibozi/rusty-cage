# Proprietary component terms and opt-in

This file is separate from the project's canonical GPL license in `LICENSE`.
It records a local user's acceptance of vendor terms; it does not change the
project's license or grant rights to any third-party software.

EULA_VERSION = 1
PROPRIETARY_COMPONENTS_ACCEPTED = false

## Components and vendor terms

Bedrock for Mac is open source where applicable. Some features require
proprietary software governed by its vendor's terms:

- The Minecraft for Windows (Bedrock) game and its Microsoft Store content,
  under the [Minecraft EULA](https://www.minecraft.net/en-us/eula) and
  applicable Microsoft Store/account terms. A separate qualifying game
  entitlement is still required.
- Microsoft's Game Development Kit/Gaming Services threading DLL and
  GameInput redistributables used by the current Wine setup. Review the
  Microsoft terms shipped with or presented for the exact download; this
  project does not claim that those files may be redistributed.
- A user-provided, licensed Windows 11 ARM guest if a future VM route is
  implemented, under the applicable
  [Windows license terms](https://www.microsoft.com/useterms).
- Apple system software and services, including Rosetta, Metal or
  virtualization services when required by the macOS version and selected
  runtime, under Apple's
  [software license terms](https://www.apple.com/legal/sla/).

The project does not include Minecraft, Windows, or Microsoft runtime payloads
in its source export. Setup obtains the game and Microsoft components from
official Microsoft endpoints. Apple components are supplied/managed by macOS,
not bundled here. Any later VM implementation must identify each vendor
download and applicable license before enabling it. The Windows ARM
virtualization route is not currently implemented or verified; Parallels is
out of scope.

## Your acceptance

Before enabling proprietary component downloads or use, you confirm that you
have the required personal license or entitlement, have reviewed and will
follow the applicable vendor terms, and will not redistribute proprietary
components unless the vendor's terms expressly allow it. You obtain Windows
and other separately licensed vendor software yourself where applicable.

To record acceptance, manually change
`PROPRIETARY_COMPONENTS_ACCEPTED = false` above to
`PROPRIETARY_COMPONENTS_ACCEPTED = true` after reviewing this document and the
vendor terms. It is disabled by default. Keep it false to prevent this
project's setup and launch tools from downloading or using the covered
proprietary components.

This setting is only an additional local acceptance gate and record of your
intent; it is not itself a vendor EULA or a license grant. It does not grant a
Minecraft or Windows license, change Apple's or Microsoft's terms, bypass
Microsoft account sign-in or ownership verification, or authorize downloads
that a vendor does not permit. The existing Microsoft ownership and package
integrity checks remain mandatory. Do not redistribute proprietary payloads
unless the applicable vendor terms expressly permit it.

For source setup, edit this `EULA.md` in the checkout. For a packaged app, edit
`EULA.md` in `~/Library/Application Support/Bedrock for Mac/`; the app copies
the default here on first setup and preserves the user's file across updates.
Do not commit a locally enabled acceptance setting.
