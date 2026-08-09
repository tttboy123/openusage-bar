# Observer native runner prerequisites

Date: 2026-08-10

This note defines the environment needed to run
`observer-source-native-evidence/v1` on real GitHub-hosted Windows and Linux
runners. Runner-image inventory, local containers, package installation, and a
started credential service are prerequisites only. None of them is source
verification or permission to change the Provider catalog.

## Static runner inventory is not runtime evidence

The official runner-images table currently maps the project matrix to
`ubuntu-24.04`, `ubuntu-24.04-arm`, `windows-2025`, and `windows-11-arm`.
([available images](https://github.com/actions/runner-images/blob/main/README.md#available-images))

The mutable image inventories fetched on 2026-08-10 reported:

| Label | Inventory snapshot | Relevant observation |
| --- | --- | --- |
| `ubuntu-24.04` | [`20260720.247.2`](https://raw.githubusercontent.com/actions/runner-images/main/images/ubuntu/Ubuntu2404-Readme.md) | lists `dbus` `1.14.10-4ubuntu4.1`; does not list `gnome-keyring` |
| `ubuntu-24.04-arm` | [`20260719.67.1`](https://raw.githubusercontent.com/actions/runner-images/main/images/ubuntu/Ubuntu2404-Arm64-Readme.md) | lists `dbus` `1.14.10-4ubuntu4.1`; does not list `gnome-keyring` |
| `windows-2025` | [`Windows2025-Readme`](https://raw.githubusercontent.com/actions/runner-images/main/images/windows/Windows2025-Readme.md) | inventory does not prove a Credential Manager round trip |
| `windows-11-arm` | [`Windows11-Arm64-Readme`](https://raw.githubusercontent.com/actions/runner-images/main/images/windows/Windows11-Arm64-Readme.md) | inventory does not prove a Credential Manager round trip |

These are design inputs, not retained evidence. The workflow continues to
require `GITHUB_ACTIONS=true`, which GitHub defines for Actions workflows, and
`runner.environment=github-hosted`, whose documented alternatives are
`github-hosted` and `self-hosted`.
([default variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables),
[runner context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#runner-context))
Those values identify the runner class; they do not prove a credential seam.

## Windows: use the native credential set directly

Windows requires no compatibility service. `openusage_bar/keychain.py` calls
`CredReadW`, `CredWriteW`, and `CredDeleteW` through `advapi32`. Microsoft
defines `CredWriteW` as writing a credential into the current token's
credential set.
([CredWriteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew))

Both Windows rows therefore run the source probe directly. Only the probe's
synthetic write/read/delete round trip followed by the production Moonshot
parser can mark the source verified. If an ARM runner rejects the selected
persistence mode, the fix belongs at the Win32 boundary; DBus, GNOME, fallback
storage, and self-reported status are not acceptable substitutes.

## Linux: provide an ephemeral Secret Service session

The Linux backend uses pinned `SecretStorage==3.5.0`, calls
`secretstorage.dbus_init()`, and opens the default Secret Service collection.
SecretStorage implements the FreeDesktop Secret Service protocol over D-Bus.
([SecretStorage documentation](https://secretstorage.readthedocs.io/en/latest/))
Ubuntu documents that `dbus-run-session` starts a session bus for one program,
while `gnome-keyring-daemon` provides the `secrets` component and accepts an
unlock password on standard input.
([dbus-run-session](https://manpages.ubuntu.com/manpages/noble/man1/dbus-run-session.1.html),
[gnome-keyring-daemon](https://manpages.ubuntu.com/manpages/noble/man1/gnome-keyring-daemon.1.html))

The Linux-only workflow step installs and then checks these exact Noble package
versions:

- [`dbus-user-session=1.14.10-4ubuntu4.1`](https://packages.ubuntu.com/noble-updates/dbus-user-session)
- [`gnome-keyring=46.1-2build1`](https://packages.ubuntu.com/noble/gnome-keyring)

The source probe then runs inside `dbus-run-session` with private, mode-0700
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR` directories below
`RUNNER_TEMP`. The keyring is unlocked with an empty password because it is a
disposable store containing only the probe's random synthetic credential. The
existing probe still refuses pre-existing state, deletes only a value that
matches its own secret, and independently validates the Moonshot parser.

The workflow suppresses daemon output, never prints the session-bus address,
shuts the daemon down, and deletes only the validated `mktemp` directory after
the probe. Windows does not enter this branch. The temporary keyring is not
uploaded and does not become a native-evidence check.

## Local execution evidence and its limit

Before the workflow change, an ephemeral Ubuntu 24.04 ARM64 container
(`ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea`)
was used to exercise the same package pins and `SecretStorage==3.5.0`. Within
`dbus-run-session`, a private XDG root and `gnome-keyring-daemon --unlock
--components=secrets` first completed a synthetic create/read/delete round
trip. The complete project probe then passed canonical offline verification
with `verifiedSourceCount=2`: `moonshot_official_api` and `codex_local_log`
were both `verified`, followed by clean container exit. This validates the
bootstrap and implementation mechanics only. It is ARM64 container evidence,
not GitHub-hosted source evidence, and it cannot promote Windows or Linux
support.

## Promotion boundary

Package installation, service startup, static workflow checks, local mocks,
containers, and N-1 native evidence never produce `supportedSourceCount` or
`promotionEligible`. New Windows/Linux native evidence embeds the canonical
`observerSourceEvidence`; macOS forbids that field, and the release handoff
remains exactly five files. Catalog support may change only after the matching
real hosted row is retained and the embedded per-source state passes the
combined native-evidence verifier. Until then the public platform truth remains
macOS `49/49`, Windows/Linux `0/49`, and unknown runtime `null/49`.
