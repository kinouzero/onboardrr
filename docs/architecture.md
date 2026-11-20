# Onboardrr MVP architecture

## Controller and first target

The first release uses a Python CLI on a single self-hosted controller.
SQLite stores device identities, run checkpoints, and resource ownership.
Ansible handles Linux, Docker, mounts, and host-specific configuration over
SSH. Dedicated connectors handle Goku, Komodo, and Pangolin. Komodo will manage
deployed stacks after enrollment, including Pangolin and Newt secrets.
OpenBao KV v2 supplies deployment credentials for Goku and Periphery bootstrap.
A web interface can follow once the CLI workflow is validated.

| Profile | MVP platform | Default modules |
| --- | --- | --- |
| `raspberry-pi` | Raspberry Pi OS Lite 64-bit, Debian 13 (Trixie), ARM64. Hardware acceptance pending. | SSH checks and key setup, Docker Compose, Goku mounts and aliases, Komodo Periphery, baseline stacks, and Pangolin Newt. |
| `server` | Debian 13, Ubuntu 24.04 LTS, or Linux Mint 22.x, ARM64 or x86-64. | Server tools and Docker CE on fresh installs, Goku mounts, and opt-in SMB shares, kernel tuning, NVIDIA support, and VNC. |
| `nas` | QNAP with QTS or QuTS hero and Container Station. QTS 5.2.11 preview verified. | Native Container Station checks, Entware installation and opkg verification, and operator workspace. |

The first validation assumes an installed and SSH-accessible OS. The MVP
excludes SD flashing, physical disk configuration, and Plex deployment or
scheduled backups. A server can opt in to mount its Plex share. Legacy
Raspberry Pi OS and 32-bit builds are outside the profiles.

## SSH identity and settings

Inventory records contain a device name, hostname or IP, account name, and SSH
port (22 by default). Enrollment will require a trusted SSH host key, initial
credentials, and working sudo for host changes. Passwords will be entered
interactively and kept out of command-line arguments, logs, and SQLite.

The configured operator public key and the controller's execution identity
are separate. Enrollment will preserve existing authorized keys and maintain
the bootstrap session until it verifies a fresh login. Proof of access for
the operator key requires access to the corresponding SSH agent. The
controller can use a separate dedicated identity for subsequent execution.
Private keys stay on the controller or in the agent, and password
authentication stays available unless changed by an explicit operation.

The bootstrap command can enroll and verify the operator key before central
service checks are possible. This uses the initial password once and records
key ownership while leaving the full SSH checkpoint pending.

## Deployment sequence

1. Resolve the host, verify SSH and privileges, discover existing resources,
   and preview planned changes. A Raspberry Pi can bootstrap over Ethernet
   and configure the optional Wi-Fi profile after SSH key enrollment. The
   wired route remains available during wireless validation.
2. Register or reuse the Komodo server, then establish Periphery authentication.
3. Stage the host's Compose and environment template on verified Goku mounts.
   Prepare the Linux host, start Periphery using its Compose file and runtime
   environment, and verify Komodo connectivity.
4. For each newly enrolled host except the primary Linux server, instantiate
   `docker`, `monitoring`, and `network` from Komodo's `stack-docker`,
   `stack-monitoring`, and `stack-network` templates. This includes secondary
   Linux servers, Raspberry Pis, and NAS devices. Create the Pangolin site
   and reference the device's Newt secret before deploying `network`.
   Verify the Newt connection, then reconcile the private host alias and
   the reciprocal Newt site assignments. The primary Linux server follows its
   dedicated Compose bootstrap from the `stacks` repository and its
   existing Komodo stack definitions.
5. Report final state and write checkpoints for safe resumption. Record the
   ownership and restoration information needed to preview device removal.

The Hole and Zero procedures provide conventions for the Compose share,
local directories, and aliases. They also include optional setup with different
mount and package choices. Resolve permissions from the device user. The
Onboardrr scope records Naruto as the Periphery, stack, and Pangolin
reference. Verify the actual file resolution, templates, API versions, and
credential lifecycle before implementing those integrations.

## Local run state

SQLite keeps one unfinished provisioning run per device and snapshots its ordered
steps at creation. Reopening a run exposes the first incomplete step. The store
accepts observations only for that step, and keeps its most recent failure
timestamp and a non-secret failure code even after it succeeds. A finished run
stays in history when a new one is opened.

The executor can record resource observations before a step completes, so a
failed step can still expose what it changed. Each observation stores a resource
identifier, its first disposition in the run, and an opaque backup ID for
modified resources. Connectors must pass non-secret resource identifiers, use
previous observations on retry, and reconcile remote state if interruption
occurs between a remote write and a local observation. Removal will use the
history across runs to distinguish resources created by Onboardrr from resources
it reused. The run store has no fields for credentials or raw command output.

## References

- [Onboardrr scope](https://wiki.kinou.xyz/doc/onboardrr-deploiement-et-configuration-linux-tXxtDtvknG)
- [Hole installation](https://wiki.kinou.xyz/doc/hole-fresh-install-Klr613xCXb)
- [Zero installation](https://wiki.kinou.xyz/doc/fresh-install-MMUMEVO0Bv)
- [NAS Entware installation](https://wiki.kinou.xyz/doc/installation-entware-xCnjanMRaD)
- [Raspberry Pi OS downloads](https://www.raspberrypi.com/software/operating-systems/)
- [Ansible installation and control nodes](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html)
