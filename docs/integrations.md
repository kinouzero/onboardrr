# Integration setup

## OpenBao KV v2

Use an initialized OpenBao deployment over HTTPS with KV v2 mounts named
`hosts` and `services`. Apply the [controller policy](../provisioning/openbao/onboardrr-read.hcl).
Deliver a scoped, short-lived controller token through a private regular file
owned by the controller account with mode 0600 or 0400.

| Split KV v2 path | Legacy path relative to the deployment mount | Fields |
| --- | --- | --- |
| `hosts/goku/smb` | `deployments/goku` | `smb_username`, `smb_password` |
| `hosts/<device>/periphery` | `deployments/periphery/<device>` | `passkey` |
| `hosts/common/wifi/2G` | `deployments/wifi/2G` | `ssid`, `psk` |
| `hosts/common/wifi/5G` | `deployments/wifi/5G` | `ssid`, `psk` |
| `services/beszel/onboardrr` | `deployments/monitoring` | `beszel_identity`, optional `beszel_agent_key` and `beszel_password` |
| `services/komodo/onboardrr` | `integrations/komodo` | `api_key`, `api_secret` |
| `services/pangolin/onboardrr` | `integrations/pangolin` | `api_token` |
| `services/wud/onboardrr` | `integrations/wud` | `api_token` |

Load the values through the authenticated OpenBao CLI or UI. The CLI can
import the key/value pairs from a protected JSON file using
`bao kv put -mount=hosts goku/smb @/private/path/smb.json`.
Store this file outside the repository or use the UI to avoid a local copy.
Set the non-secret connection parameters and the path to the token:

```sh
export ONBOARDRR_BAO_URL=https://bao.example.org
export ONBOARDRR_BAO_LAYOUT=split
export ONBOARDRR_BAO_TOKEN_FILE=/private/path/bao-token
export ONBOARDRR_KOMODO_URL=https://komodo.example.org
export ONBOARDRR_PANGOLIN_URL=https://pangolin-api.example.org
```

Existing controller installations default to the `legacy` layout. In that
layout, `ONBOARDRR_BAO_MOUNT` selects the single deployment mount and defaults
to `secret`. Use the legacy column when configuring ACLs for that layout.
Enable split mode once the records and scoped policies are in place. Onboardrr
uses the selected layout for reads and writes throughout provisioning.
Before retiring the legacy mount, preserve the KV version history you need,
switch every installed controller and Agent to the split layout, and verify
fresh renders with their existing scoped credentials.

For split provisioning, configure the Komodo Core Agent to publish
`<DEVICE>_HOSTNAME` and `<DEVICE>_IP` from `hosts/<device>/HOSTNAME` and
`hosts/<device>/IP`. Grant the Core Agent read access to those fields and
list access to the host metadata used by its template. Onboardrr creates the
host records and waits for their aliases to appear before deploying stacks.

The controller reads API credentials from the integration records above.
For a terminal session without shell exports, save the controller settings in
`~/.config/onboardrr/config.toml` (or under `$XDG_CONFIG_HOME`):

```toml
openbao_url = "https://bao.example.org"
openbao_layout = "split"
openbao_token_file = "~/.local/state/onboardrr/bao-controller-token"
komodo_url = "https://komodo.example.org"
pangolin_ssh_target = "core.example.org"
komodo_openbao_token_file = "~/.local/state/onboardrr/bao-komodo-newt-token"
newt_openbao_mount = "services"
```

Environment variables take precedence over these settings. Use the Komodo
HTTPS origin, without an API or UI path. Keep the token itself in its private
file.

If already set, `ONBOARDRR_KOMODO_API_KEY` and
`ONBOARDRR_KOMODO_API_SECRET` together, or `ONBOARDRR_PANGOLIN_API_TOKEN`,
override their respective records. Use a read-scoped integration key for
inspection or a key authorized to create servers for Komodo registration.

## Read reference configuration and register a test device

```sh
python3 -m onboardrr komodo inspect naruto
python3 -m onboardrr pangolin ORGANIZATION SITE_IDENTIFIER
python3 -m onboardrr komodo register lab-pi
```

The Komodo inspector reports the Core version, reference server settings, and
visible stack templates. Pangolin reads the referenced site through its
integration API. These commands filter responses to connection and resource
metadata. Use the site's identifier in the organization selected for onboarding.

After the SSH checkpoint succeeds, run `komodo register lab-pi --apply
--expected-version CORE_VERSION` with the version reported by inspection.
For new servers, Onboardrr generates the per-device passkey if the OpenBao
record is missing. The split controller policy needs `read` and `create` on
`hosts/data/+/periphery`. The write uses
CAS 0, so concurrent runs and retries use the stored passkey. A server
already registered in Komodo requires its original passkey if the OpenBao
record is missing. The CLI creates a disabled registration for
the TLS-authenticated Periphery endpoint or reuses a matching registration
when it can verify the address and passkey. Agent startup and registration
activation belong to the subsequent Periphery stage. Use
`--periphery-host` and `--periphery-port` for a different endpoint.

Onboardrr assigns the `rpi` tag to a new Raspberry Pi registration and the
`server` tag to a new Linux server registration. It creates the profile tag
in Komodo if needed. Each new baseline stack inherits the tags of its matched
Komodo template. Tagging preserves tags already attached to the resource.
Re-run the provisioning command if tagging is interrupted. The pending
checkpoint resumes tagging recorded resources before moving on.

## Enroll a Pangolin site

After Periphery connects and the run reaches `komodo-stacks`, preview and
apply the site before deploying its network stack.

```sh
python3 -m onboardrr pangolin-enroll zero lab-pi --ssh-target zero.local
python3 -m onboardrr pangolin-enroll zero lab-pi --ssh-target zero.local --apply
python3 -m onboardrr komodo baseline lab-pi --apply
python3 -m onboardrr pangolin-enroll zero lab-pi --ssh-target zero.local --apply
```

Set `pangolin_ssh_target` (or `ONBOARDRR_PANGOLIN_SSH_TARGET`) to use the
private port 3003 of the `pangolin` container over SSH. It requires a known
host and passwordless SSH with Docker access. Set `pangolin_url` (or
`ONBOARDRR_PANGOLIN_URL`) for an Integration API with its own HTTPS origin.
If both defaults are set, SSH takes precedence. An explicit `--url` or
`--ssh-target` overrides the configured defaults.

Apply uses the integration token with site read and create permissions. Set
`openbao_url` and `komodo_openbao_token_file` in the controller configuration
or set `ONBOARDRR_BAO_URL` and `ONBOARDRR_KOMODO_BAO_TOKEN_FILE`. Store the
OpenBao token in a private file and grant `read` and `patch` on
`komodo/data/variables`. Core must already be configured to load that record.
Writing new credentials triggers the existing Core reload on Zero. Once
Komodo's baseline preview sees the new secret names, apply the stacks.

For dedicated storage, set `newt_openbao_mount = "services"` in the controller
configuration (or `ONBOARDRR_NEWT_BAO_MOUNT=services`). Provisioning and
retirement then use the `ID` and `SECRET` fields at
`services/pangolin/newt/<device>`. Both values are written together with a KV
version check. The controller token needs `create`, `read`, and `patch` on
`services/data/pangolin/newt/+`.

Before switching the controller, copy all active pairs and configure Core to
render the dedicated records. Compare the rendered `*_NEWT_ID` and
`*_NEWT_SECRET` aliases with the running configuration. Switch all controllers
together and remove their legacy write grants after the cutover. A retired
pair is removed from the current record. Keep its KV metadata so Core can
still render an empty record and the host can be enrolled again.

The command saves the credential pair before creating the site. A retry uses
the saved pair and checks the site's Newt identity. If a site has a mismatched
identity or its credential pair is incomplete, correct the configuration
before retrying. After the network stack starts, a second apply checks that
the same site is online and reconciles the private host routes before
completing the Pangolin checkpoint. If the site is still offline, rerun apply
after Newt connects.

### Reconcile private host routes

The route uses a private Pangolin Host resource named after the device, with
alias `<device>.pang` and the inventory device's IPv4 address as its
destination. Use `--ipv4` to override the destination, including when an
inventory hostname resolves to multiple addresses.

All Newt sites in the organization, including the device's own site, are
attached to the new resource. The device's site is added to existing private
Host resources named for an organization's Newt sites with matching
`<name>.pang` aliases. Existing site assignments and access grants are
preserved. Pangolin grants the new resource to the organization's admin role.
The integration token needs site listing and private resource listing,
creation, and update permissions.

Use the standalone command to preview or apply the same reconciliation for an
already enrolled device. A retry only applies missing assignments.

```sh
python3 -m onboardrr pangolin-network zero lab-pi --ssh-target zero.local
python3 -m onboardrr pangolin-network zero lab-pi --ssh-target zero.local --apply
```

## Deploy the baseline Komodo stacks

On an existing Linux host with a healthy inbound HTTPS Periphery, Auto
verifies the existing server over trusted SSH before reusing its Komodo
registration. It checks the endpoint IP, the installed agent's public
key, and the existing OpenBao passkey. The workflow keeps the agent's
deployment and data mounts and continues with the selected system
and service operations. For a standalone preview, use
`onboardrr komodo register DEVICE --public-key ~/.ssh/id_ed25519.pub`.

If the existing Pangolin site has a generated identifier, Onboardrr can
reuse it by matching its display name and verifying its Newt identity
against OpenBao. If local DNS returns no IPv4 address, Pangolin and Termix
can use the IPv4 address stored under `hosts/<device>/IP`.

```sh
python3 -m onboardrr komodo baseline lab-pi
python3 -m onboardrr komodo baseline lab-pi --apply
```

The preview checks whether Komodo can reach the registered Periphery service.
It matches `<device>-docker`, `<device>-monitoring`, and `<device>-network`
against the existing stacks and their template names. It lists missing
Komodo variable names and Compose environment keys without exposing values.
Lookups cover database variables and Core's configured secret names.
Apply requires the recorded server and the `komodo-stacks` checkpoint.
It creates missing instances, records their ownership before deployment, and
waits for Komodo to report each stack as running. A retry reuses existing
instances and skips deployments that are already running.

Before creating a stack, Onboardrr reads public Docker Hub tags for each
versioned image with a `wud.tag.include`, `wud.tag.exclude`, or
`wud.tag.transform` label. It applies the label's channel filters, handles
Compose's escaped dollar signs, and selects the newest matching version for
the target architecture. Preview reports changes under `image_updates`.
Apply checks the registry again and writes the selected tags into the
instance Compose before deployment. An unsuccessful registry lookup stops
the command before stack creation. Mutable channel tags such as `latest`
are resolved by Komodo's image pull. Digest pins and services with
`wud.watch=false` keep their image references.

With OpenBao configured, baseline apply creates a missing
`hosts/<device>/HOSTNAME` KV v2 entry with the field `value` set to the inventory
name. It reuses an existing matching entry and creates the corresponding
`DEVICE_HOSTNAME` variable in Komodo for the stack templates. The controller
needs read and create on `hosts/data/+/HOSTNAME` and permission to create
Komodo variables. Preview leaves both services unchanged.

Baseline also reads `hosts/<device>/IP` in OpenBao, including when
`DEVICE_IP` is a Komodo Core secret. If the host entry is missing, apply
creates it from the existing Komodo variable or a single LAN IPv4 address
resolved from the inventory target. OpenBao stores the address as a literal
IPv4 string in the `value` field. Older `HOST_IP` records can supply this
value when the `IP` record is missing. Apply creates the Komodo IP variable
when Core does not already expose it. The controller needs read and create
on `hosts/data/+/IP` and read on the older `hosts/data/+/HOST_IP` path.
Separate conflicting values require review.
Replace `DEVICE` with the uppercase device name and use underscores for
hyphens. The Docker API proxy listens on this LAN
address without authentication. Provide `DEVICE_NEWT_ID` and
`DEVICE_NEWT_SECRET` in Komodo's configured secret store for the network stack.
Pass `--primary-server NAME` if the primary Linux server is named differently
from `zero`. The command skips the primary Linux server.

## Run installation or retirement automatically

Use **Auto** in the terminal UI or pass repeated `--select` options to the CLI:

```sh
onboardrr auto-install lab-pi --public-key ~/.ssh/id_ed25519.pub --apply
onboardrr auto-install lab-server --select ssh --select system --select docker --select nvidia \
  --public-key ~/.ssh/id_ed25519.pub --apply
onboardrr auto-install lab-pi --select wud --select beszel --apply
onboardrr prepare lab-pi --component bluetooth --apply
onboardrr prepare lab-pi --component goku-mounts --cifs-source //goku/compose \
  --nfs-source goku:/onboardrr-lab-pi --apply
onboardrr auto-uninstall lab-pi --public-key ~/.ssh/id_ed25519.pub
onboardrr auto-uninstall lab-pi --select wud --apply
```

The displayed order is also the execution order, including when CLI flags
arrive in a different order. In the terminal UI, **Install Docker**
includes registry authentication when configured, and **Configure Goku**
includes the files, export, and both mounts. The CLI retains individual
`--select` options for those operations. Linux presets enable SSH, system preparation,
Docker, and Bluetooth. Secondary hosts also select Komodo registration,
Goku staging and mounts, Periphery, the Pangolin site, Docker proxy,
monitoring agents, Newt, Pangolin routes, and final verification.
Each is individually selectable. The direct command
`komodo baseline DEVICE --kind docker` handles just the Docker proxy.
The Pangolin site supplies Newt credentials to the baseline network stack.
Sync the routes after Newt connects. Use `reconcile --only goku-files` or
`reconcile --only linux` to verify just the staging or mount checkpoint.

QNAP has separate QTS/Entware and Docker CLI options. Container Station
supplies the existing engine. Initial SSH bootstrap installs Entware Python
if needed when system preparation is selected. Pi Wi-Fi, NVIDIA, and VNC
are opt-in. Docker Hub authentication, OpenBao Agent, and Termix have
separate options. A component-scoped Linux preparation runs only the
matching Ansible tasks and their prerequisites. QNAP also probes only
the selected preparation area. An interrupted Auto run retains its
provisioning checkpoints and skips host preparation components it already applied.

WUD, Dozzle, Prometheus, and Beszel can each be added or removed separately.
The direct command is `onboardrr monitoring DEVICE --component TOOL`.
Only the selected tool and its endpoint are checked. On QNAP, supplying
the enrolled public key allows the selected monitoring action to start its
missing local agent. WUD and Dozzle share the Docker telemetry endpoint.
The older CLI selections `services` and `monitoring` remain available as
group shortcuts.

Retirement stops on the first blocker and follows ownership records.
By default it removes managed Linux stacks, monitoring, host bindings,
Pangolin, and Komodo registrations and their recorded variables and Newt
credentials. The NAS preset removes its central monitoring registration.
Linux stacks and host cleanup actions can each be selected individually.
Goku export and deployment file removal, data purge, Periphery passkey
deletion, and SSH revocation are separate selections. Purge requires the
mounted Goku data root and selection of export removal. SSH revocation
requires a second verified identity. Shared applications and operating-system
packages remain installed. Optional privileged operations may require
`--ask-become-pass`.

## Retire a Linux device

```sh
python3 -m onboardrr retire lab-pi
python3 -m onboardrr retire lab-pi --apply --organization zero --ssh-target zero.local
```

Apply removes the owned Komodo baseline instances after taking their Compose
projects down. It preserves volumes and shared instances. With Pangolin
configured, the command removes the recorded Newt site when it is offline
and has zero resource dependencies. Other actions appear as pending. Retry
after eligible dependencies are cleared or Periphery is reachable again.
The same apply removes the device's recorded WUD watcher, Dozzle remote,
Prometheus scrape job, Beszel system, and unused host binding in the
monitoring Compose. This works with the device offline. It uses the same
hub and Beszel account as enrollment, validates the Prometheus config
before reload, and redeploys WUD or Dozzle only if needed. Shared entries
and pre-existing entries without an ownership record are preserved.
The preview lists the owned monitoring entries. A failed removal can be
retried with the same command.
For a confirmed Onboardrr enrollment made before ownership tracking, use
`retire DEVICE --apply --include-untracked-monitoring`. It verifies the
existing entries against the device's Komodo address and ports before removal.

Use `--detach-private-routes` with `--apply` and the Pangolin organization or
SSH target to detach an offline site from shared private Host routes named
`<name>.pang`. Shared routes keep their other sites.
Add `--delete-owned-private-route` to delete the device's own single-site
route when Onboardrr recorded its creation during enrollment and Pangolin
still reports the same alias and destination. Existing routes without a
creation record, other private resource types, and public dependencies
require review.

After removing the device's Komodo stacks, preview and then apply host
detachment. Use the sources supplied to `prepare` for the mounts selected
for removal. Existing run records identify the mount targets and their
ownership. The source arguments verify the specific fstab entries.

```sh
python3 -m onboardrr retire-host lab-pi --public-key ~/.ssh/id_ed25519.pub \
  --cifs-source //goku.example.org/compose --nfs-source goku.example.org:/onboardrr-lab-pi
python3 -m onboardrr retire-host lab-pi --public-key ~/.ssh/id_ed25519.pub \
  --cifs-source //goku.example.org/compose --nfs-source goku.example.org:/onboardrr-lab-pi \
  --stop-periphery --apply
```

The command verifies the active mounts and fstab, blocks when other
containers are using the selected paths, and performs ordinary unmounts
before replacing fstab. It keeps a private fstab backup on the host.
`--stop-periphery` disables automatic restart and stops the agent whose
Compose labels match the owned deployment file. For outbound recovery, the
command verifies the local Compose path, device identity, and data and key
mounts. Stopping that agent requires a single owned Komodo registration.
Apply uses sudo on
non-root accounts. Add `--ask-become-pass` when sudo requires a password.

After detaching storage, add `--remove-network` to preview or remove the
device's empty Docker bridge. Outbound recovery labels new bridges with the
device ID and records their full Docker ID. Retirement requires both to
match and blocks if another container uses the bridge. Older bridge records
with only a name are preserved and require review.

Use `retire-shell` to clean up the Linux operator's Bash aliases before
revoking SSH access:

```sh
python3 -m onboardrr retire-shell lab-pi --public-key ~/.ssh/id_ed25519.pub
python3 -m onboardrr retire-shell lab-pi --public-key ~/.ssh/id_ed25519.pub --apply
```

The command uses the enrolled SSH account and needs no sudo. It recognizes
the managed block installed by `prepare`, preserves the rest of `.bashrc`,
and keeps a private backup on apply. Edited or ambiguous blocks report
`review` and require manual inspection.

After SMB detachment, run `retire-smb-credentials DEVICE --public-key KEY`
to preview removal of the device's local credentials. The check needs sudo
because the files are private. Add `--ask-become-pass` if needed and
`--apply` to remove the verified credential file and its ownership marker.
The command checks mounted SMB targets and fstab references. Older files
without a marker and files with edited contents require manual review.

After Periphery has disconnected, an administrator can remove the owned
Komodo server registration with
`python3 -m onboardrr retire lab-pi --apply --unregister-server`.
The command leaves the registration pending while the agent is connected
or a stack, deployment, repository, builder, or onboarding key references it.

Use `komodo unregister lab-pi` to preview removing just the owned Komodo
registration. Add `--apply` to remove it and keep the OpenBao passkey.
Add `--delete-passkey` to include the device's OpenBao Periphery record and
all its versions in the same operation. Onboardrr removes that record only
after it verifies the Komodo server is gone. Use a separate scoped token
with the [Periphery retirement policy](../provisioning/openbao/onboardrr-periphery-retirement.hcl).
Set `periphery_retire_token_file` in
the controller configuration or `ONBOARDRR_BAO_PERIPHERY_RETIRE_TOKEN_FILE`.
Both choices are available under **Maintenance** in the TUI. Removing the
registration reopens its checkpoint so the same device can be registered again.

After removing the server registration and the Pangolin site, preview or
retire its variables and Newt credentials.

```sh
python3 -m onboardrr retire-credentials lab-pi
python3 -m onboardrr retire-credentials lab-pi --ssh-target zero.local --apply
```

Apply uses an administrator Komodo API key and the same Pangolin and
OpenBao settings as enrollment. It preserves names referenced by other
Komodo resources and values that changed. Newt credentials installed by
the current enrollment flow carry an ownership fingerprint. Older pairs
without that record require manual review. Removal patches only the two
fields in the current OpenBao record, leaving unrelated secrets and
historical KV versions intact.

After detaching the NFS mount, preview and optionally disable the dedicated
QTS export before purging the backing data.

```sh
python3 -m onboardrr retire-export lab-pi --goku-ssh goku.local
python3 -m onboardrr retire-export lab-pi --goku-ssh goku.local \
  --public-key ~/.ssh/id_ed25519.pub --ask-become-pass --apply
```

Use `--client-ip` if the inventory uses a DNS name. The command requires
the device-owned data directory and mount, verifies the corresponding
share and its single client, and checks detachment over trusted SSH before
apply. Apply uses QTS's native NFS re-export and checks that unrelated
exports persist. It keeps the shared-folder definition and backing data.

Purge is a separate operation after host detachment. The
controller needs access to the Goku parent directory, mounted at the path
provided in `--goku-data-root` or `--goku-compose-root`.

To remove only the deployment files, use `retire-goku-files` with the
mounted Compose share. Preview shows the recorded files and their status.
Apply requires the device's Komodo stacks and its active and persistent
Goku mounts to be detached.

```sh
python3 -m onboardrr retire-goku-files lab-pi --goku-compose-root /mnt/goku-compose
python3 -m onboardrr retire-goku-files lab-pi --apply \
  --goku-compose-root /mnt/goku-compose --public-key ~/.ssh/id_ed25519.pub
```

Apply checks each file against the digest recorded when Onboardrr staged
it. Edited files and older records without a digest require review.
The device directory, its other files, and backing data are kept.

```sh
python3 -m onboardrr retire lab-pi --purge
python3 -m onboardrr retire lab-pi --purge --apply \
  --organization zero --ssh-target zero.local \
  --goku-data-root /mnt/goku-komodo-data \
  --public-key ~/.ssh/id_ed25519.pub
```

Apply verifies that the device's Komodo stacks and Pangolin site are gone
and checks active and persistent mounts over trusted SSH. It purges only
recorded device-owned directories within the selected root. It preserves
reused paths and blocks targets containing active filesystem mounts.

Revoke the enrolled SSH key after the other retirement actions. Both
public key paths identify keys for the same target account. Load the
distinct fallback identity into the controller's SSH agent first.

```sh
python3 -m onboardrr retire-ssh lab-pi --public-key ~/.ssh/id_ed25519.pub \
  --fallback-public-key ~/.ssh/lab-fallback.pub
python3 -m onboardrr retire-ssh lab-pi --public-key ~/.ssh/id_ed25519.pub \
  --fallback-public-key ~/.ssh/lab-fallback.pub --apply
```

Apply uses the fallback connection to remove the exact owned entry, then
opens a new fallback session. If the new session fails, the controller
attempts to restore the entry through the original fallback connection.
Entries with changed options or duplicates require review.

## Prepare the Periphery configuration

The terminal UI shows `~/.docker/compose/<name>` and
`/share/docker/komodo/data/<name>` on the selected device. It defaults to
the dedicated NFS export `/onboardrr-<name>`. The controller reads the
Naruto reference and stages the files on Goku over trusted SSH, with no
controller-side mount required. Set `goku_host` in the controller settings
to select a different NAS SSH endpoint.
Set `goku_mount_host` if Linux devices use a different address for mounting
Goku, such as its LAN IPv4 address when mDNS is unavailable on those devices.

Pass `--nfs-export /onboardrr-DEVICE` when the NAS exposes a dedicated
Periphery data share. Use the same export path in the Linux
`prepare --nfs-source` value. The path inside the container stays the same.

At the `goku-files` checkpoint, render and stage the files using Goku's
`/share/compose/naruto/docker-compose.komodo.yml` and the bundled environment
template:

```sh
python3 -m onboardrr periphery lab-pi
```

This updates the host identity and monitoring address, preserves the network
settings, and retains the unexpanded secret marker. It refuses to overwrite
a different file. For the default export `/onboardrr-<device>`, staging also
creates the backing directory and activates the QTS NFSv4 export for the
device's LAN IPv4 address. It reuses a matching export and rejects conflicting
paths and clients. The staging SSH account needs QTS administrative access.
If sudo prompts, Onboardrr uses the Goku credential from
`hosts/goku/smb` on the configured Goku endpoint when its username
matches the SSH account. Set up
nonstandard exports on the NAS. Staging leaves validation pending.

Set the host's `KOMODO_PASSKEY`
to the same value from `hosts/<device>/periphery` that was used to
register it. The rendered Compose uses that value for `PERIPHERY_PASSKEYS`.
For an agent using the cache-based Compose, populate the device's OpenBao
Agent `periphery.env` cache with `PERIPHERY_PASSKEYS` from the same record.
New files are staged in the device directory on Goku with ownership digests.
New Linux preparations install the managed `dcu` alias for local Docker Compose
and OpenBao Agent. Re-running preparation updates the older managed alias that
called the Goku `docker-env-generate-and-up.sh` script.

## Resume the Linux onboarding checkpoints

After Komodo registration, stage the rendered Compose file and unexpanded
template in the device's Goku Compose directory. Prepare the Linux mounts
and start Periphery using the agreed deployment configuration. Verify the
staged files, persistent mounts, Docker network and recorded Komodo server
before applying the baseline.

```sh
python3 -m onboardrr reconcile lab-pi --apply \
  --cifs-source //goku.example.org/compose --nfs-source goku.example.org:/onboardrr-lab-pi \
  --public-key ~/.ssh/id_ed25519.pub
```

Without `--apply`, the command checks the next pending preparation stage.
Apply records completed stages and stops at the first failed check. On retry
it resumes there. Resources found through reconciliation are recorded as
reused when ownership is unknown, so retirement preserves them. Previously
recorded ownership continues to apply. At `komodo-stacks`, continue with
Pangolin enrollment, baseline deployment, and the final verification.
Linux preparation creates the device's local Docker bridge when missing.
Reconciliation records ownership only when the bridge carries the device
identifier label.

### Start Periphery on a new device

At the Periphery checkpoint, choose **Start and verify Periphery** in the terminal
interface and apply the action. The equivalent CLI command is:

```sh
onboardrr periphery-recover lab-pi --bootstrap --apply --public-key ~/.ssh/id_ed25519.pub \
  --cifs-source //goku.example.org/compose --nfs-source goku.example.org:/onboardrr-lab-pi
```

Bootstrap requires the Komodo server created by the current run and the Goku
mounts. It reads the staged `template.env` to verify device identity, detects
the Komodo Core version, and pairs the host-local agent using its X25519 key.
The private key stays on the device and the agent connects to Core over TLS.
Retry the same action if startup is interrupted before the checkpoint advances.

### Recover an outbound Periphery agent

After reinstalling Linux, verify the new SSH host key, enroll the operator
key, run the SSH preflight, and prepare Docker and the persistent Goku
mounts. Then preview and apply outbound recovery:

```sh
python3 -m onboardrr periphery-recover lab-pi --expected-version 2.3.4 \
  --known-hosts ~/.ssh/onboardrr-known-hosts --public-key ~/.ssh/id_ed25519.pub \
  --cifs-source //goku.example.org/compose --nfs-source goku.example.org:/onboardrr-lab-pi
```

Use the Komodo API environment supplied for registration. The command
reuses the outbound server identified in run history. Provide its verified
`--server-id` if history cannot identify a single server. It validates the
two mounted Goku references, including the device identity in
`.env` or `template.env`, and
pins the Core public key obtained from the configured HTTPS API.

Use `--apply` to start an absent agent or verify and reuse a running one.
If the original private key was lost, add `--replace-agent-key` to authorize
updating only that offline server's public key. The new private key stays
on the device. Re-run the same command after an interruption to reuse the
staged key and finish starting the agent.

Recovery uses a private local Compose file and an outbound-only agent with
no published port. On success it records the verified registration,
Goku files, Linux mounts and Periphery connection and resumes at
`komodo-stacks`. Continue with the existing baseline and Pangolin steps.

## Enroll devices in central monitoring

Set `ONBOARDRR_MONITORING_HOST` to the SSH host running the central tools,
and `ONBOARDRR_BESZEL_URL` to the Beszel HTTPS origin. The SSH alias must
already have a trusted host key and allow the operator to access Docker.
The hub also needs Python 3 with PyYAML. Set
`ONBOARDRR_MONITORING_STACK` if the existing inline Komodo stack is named
something other than `zero-monitoring`. These settings also accept
`monitoring_host`, `beszel_url`, and `monitoring_stack` in the controller's
`config.toml`.

Use the controller's OpenBao deployment token to read
`services/beszel/onboardrr`. This record contains
`beszel_identity` and `beszel_password` for a Beszel user allowed to add
systems. When the password already lives in `services/beszel/PASSWORD`,
omit `beszel_password` and grant the controller read access to that field.
To bootstrap monitoring containers on a fresh QNAP NAS, supply the hub's
OpenSSH public key as `beszel_agent_key` or grant the controller read access
to the existing key at `services/beszel/AGENT_KEY`.

After Linux baseline stacks and Pangolin are ready, `verify --apply`
enrolls the device before closing the run. NAS `prepare --apply` enrolls
the device after Container Station preparation. On a fresh NAS it starts
Beszel Agent, Node Exporter and a read-only LAN Docker proxy. A retry
reuses its managed Compose project. Existing reachable agents are reused,
and partial deployments without a managed project require review.

```sh
onboardrr monitoring lab-pi
onboardrr monitoring lab-pi --apply
```

Set the device's `<DEVICE>_IP` Komodo variable using its uppercase name
with underscores for hyphens. Monitoring reads that address and the existing
`DOCKER_API_PORT`, `NODE_EXPORTER_PORT`, and `BESZEL_AGENT_PORT` variables
from Komodo. When Komodo publishes the host address or `DOCKER_API_PORT` as
a secret, grant the controller token read access to `hosts/<device>/IP` or
`services/docker/API_PORT`, respectively. The monitoring stack keeps its
Komodo aliases for Compose bindings. Use `--ipv4` to require that the supplied
address matches the registered address.

Enrollment checks all three host endpoints from the hub. It adds a Beszel
system, reconciles the WUD watcher and Dozzle endpoint using Compose variable
references bound to Komodo variables, adds a six-hour WUD schedule when the
watcher has no schedule, and validates the Prometheus target
configuration before reloading it with
SIGHUP. Komodo redeploys only WUD or Dozzle when its live configuration
needs to change. Beszel and Prometheus receive the endpoint values resolved
from Komodo. Prometheus uses its existing writable config mount and keeps a
private backup on the hub. Re-run enrollment after a partial failure to reuse
entries already applied.

## Register SSH connections in Termix

Set `termix_url` to the Termix HTTPS origin in the controller's
`~/.config/onboardrr/config.toml`. Store a Termix API key and an existing
SSH credential's numeric ID as `api_token` and `credential_id` in
`services/termix/onboardrr`. The key needs permission to list, create, update,
and delete its account's SSH hosts and edit their plugin settings. The
OpenBao controller needs read access to this one record.

Auto installation selects Termix when the origin is configured. For an
existing device, run `onboardrr termix DEVICE` to preview and add
`--apply` to register or update it. The entry appears at the root as
`SSH_USER@DEVICE` with the inventory's SSH port and profile tag `rpi`,
`server`, or `nas`. Onboardrr applies the host settings used by Naruto
for Docker, the file manager, host metrics, session recording, Mosh, and
tunnels. Reapplying moves an older managed entry to the root and fixes
any missing settings. Termix stores an IP address. The controller resolves
hostnames and prefers a unique IPv4 result when one is available.
Use `--address` to specify the IP reachable from Termix if DNS is ambiguous.

An existing matching SSH entry is reused. Auto uninstallation and
`onboardrr termix DEVICE --remove --apply` remove only the entry marked
with the device's Onboardrr identity. Termix retains the shared credential.

## Update existing Compose projects

In **Maintenance**, choose **Update Compose files via WUD**.
Select the SSH key, load the host's files, and select one or more entries.
Onboardrr searches `~/.docker/compose/<device>`, the older
`~/.config/docker/compose/<device>` layout, or the default Compose directory
when no host subdirectory exists.

```sh
onboardrr compose-update lab-pi --list-files --public-key ~/.ssh/id_ed25519.pub
onboardrr compose-update lab-pi --file compose.yaml --file media/docker-compose.yml \
  --public-key ~/.ssh/id_ed25519.pub
```

The preview lists the services in each selected file and the tags offered
by WUD for the same image, current tag, platform, and update filters.
`--apply` replaces matching literal `image` values directly in the selected
files while preserving comments and formatting. In the terminal, **Apply and
deploy** also pulls the changed images, recreates the affected running
services, and waits for them to start. The equivalent CLI adds `--apply
--deploy`. The deployment uses the existing project's Compose files and
environment, including the OpenBao Agent cache when configured.
It requires the changed services to match a running local Compose project
and leaves unrelated services alone. If a deployment fails after the files
are written, the output lists the saved files and the failed project.
Reapplying can deploy an already-saved tag when WUD still advertises the
update for the running container.

### Update by deployed project name

Onboardrr discovers running Compose projects through Docker on the selected
host. Its SSH operator needs access to Docker, the recorded Compose files,
and their environment files. Projects stored exclusively in Komodo are
skipped and reported with the reason.

```sh
onboardrr compose-update lab-pi --all --public-key ~/.ssh/id_ed25519.pub
onboardrr compose-update lab-pi monitoring --public-key ~/.ssh/id_ed25519.pub --files-only --apply
```

Set the WUD HTTPS origin in `services/wud/onboardrr` as `url`, in the
controller settings as `wud_url`, or with `--wud-url`. Use an `api_token`
in the WUD record, or configure its `username` for Basic authentication
with the existing `services/wud/PASSWORD` record. The
`ONBOARDRR_WUD_TOKEN` environment override takes precedence.

WUD suggestions apply when the watched container and tag still match.
`--files-only --apply` validates and writes the image overrides beside the
recorded Compose files and reports their paths. The original Compose and
environment files are preserved. For projects started from independent
sets of Compose files, each set gets its own override. To deploy the
selected versions, use `--apply` without `--files-only`. That mode pulls
images for the selected services, keeps their volumes, and waits for
them to start. Use `--refresh` for a single-project refresh of configured tags
without consulting WUD.

When a host's recorded environment paths changed, the operator can add a
private `~/.config/onboardrr/compose-sources.json`. Its optional
`replace_env` object maps missing environment paths to ordered lists of
local environment files. Its optional `append_env` object maps Compose
paths to additional environment files. Use absolute paths on the target.

### Use OpenBao Agent with Docker Compose and Komodo Core

For the Raspberry Pi and Linux server profiles, `prepare --apply`
provisions OpenBao Agent when selected in **Auto** or when run with
`--ensure-openbao-agent --apply`. Onboardrr uses the existing controller
connection to create a missing host AppRole and bootstrap record. At
initial controller setup, enable the dedicated `onboardrr-compose` AppRole
mount, install the [host policy](../provisioning/openbao/onboardrr-compose-host.hcl)
with its mount accessor, and add the
[managed bootstrap rules](../provisioning/openbao/onboardrr-compose-managed.hcl)
to the controller policy. Agents can read their own project records and their own Periphery
passkey, which renders directly into the cache. If no project or Periphery
record exists yet, the agent renders the device's hostname as a local
defaults file.

Existing deployments can still use `ONBOARDRR_BAO_AGENT_TOKEN_FILE`. Provide
a private token file with the
[Compose bootstrap read policy](../provisioning/openbao/onboardrr-compose-bootstrap.hcl),
and configure `ONBOARDRR_BAO_URL` and `ONBOARDRR_BAO_LAYOUT`.
Use `ONBOARDRR_BAO_AGENT_AUTH_MOUNT` for an AppRole mount other than
`approle`. The controller token stays on the controller.

For manually managed agents or custom project secrets, configure these records:

| Split KV v2 path | Legacy path relative to the deployment mount | Content |
| --- | --- | --- |
| `hosts/<device>/compose-agent` | `deployments/compose-agent/<device>` | `role_id`, `secret_id`, a comma-separated `projects` list, and optional `host_ip` and `docker_api_port` overrides |
| `hosts/<device>/compose/<project>` | `deployments/compose/<device>/<project>` | A `dotenv` field containing a Compose-compatible environment file, a `variables` JSON object with string values, or both |

For an existing Agent that renders from several OpenBao paths, the bootstrap
record can include `auth_mount` and a `legacy_templates` object keyed by project
name. Store the template source using the active KV paths. Update any absolute
paths in the custom templates and the existing agents' configuration when
migrating to the split layout. The Agent keeps rendering
from the referenced secrets through the existing AppRole. Linux preparation
reuses an existing operator-owned `~/.config/docker/compose-openbao/cache`
for those records.

Add host-wide variables to the `variables` object in
`hosts/<device>/compose/config`. The agent renders them to `config.env`
and checks for changes every 30 seconds. Variables in configured project
records are rendered into their matching environment file as well.
For migrated agents with a legacy `config` template, the bootstrap record
uses `compose_config_variables: true` to append these variables to the
existing host configuration. Give that AppRole read access to the exact
host's config record. If the same key occurs in both sources, the value
from `variables` takes precedence.

Assign each device a separate [OpenBao AppRole](https://openbao.org/docs/agent-and-proxy/autoauth/methods/approle/)
with read permission on its own project records, for example:

```hcl
path "hosts/data/lab-pi/compose/*" {
  capabilities = ["read"]
}
```

Configure unlimited uses for the AppRole token and SecretID so the agent can
renew and authenticate again after restarting. Choose the SecretID lifetime
and renew it in the bootstrap record before it expires. Re-run preparation
to install replacement credentials. Use the OpenBao UI or a protected
input file for secret values. Host credentials stay in the operator's private
`~/.config/docker/openbao/compose/agent` directory.

Preparation generates `~/.config/docker/openbao/compose/compose.yaml` for
`openbao-agent-compose`, using the enrolled account's UID, GID, and home.
The shared `~/.config/docker/openbao/compose.env` supplies `URL_BAO`,
`DOCKER_HOSTNAME`, `HOST_IP`, and `DOCKER_API_PORT` on every host. The playbook
uses the primary IPv4 address detected by Ansible, falling back to an IPv4
inventory address, and defaults to Docker API port `2380`. Override the IP
or port in the bootstrap record, or set `ONBOARDRR_DOCKER_API_PORT` on the
controller to override the default port. Kuma uses `/var/run/docker.sock`
on `zero` and the generated TCP address on the other hosts. Both agent
variants belong to the Docker Compose project `secrets` and use the
`docker.stack: secrets` label. Preparation starts only the requested agent
service within that project.

On Zero, both agents join the existing `zero` Docker network and connect to
OpenBao at `http://openbao:8200`. Hosts without an internal endpoint use
`ONBOARDRR_BAO_URL`. The controller and the agents' Homepage links also use
that URL. If OpenBao moves, set the new host, Docker network name, and
internal address in the Linux role's `onboardrr_openbao_internal_endpoints`
mapping. Use a trusted Docker network local to that host with OpenBao already
reachable at the configured address.

The agent renews authentication and renders owner-only `0600` files in
`~/.config/docker/openbao/compose/cache`. Managed Compose agents render `USER`
from `hosts/common/USER`. The managed AppRole needs read access to that exact
path. On re-preparation, the legacy managed
agent is stopped after the new cache is verified. Installations still using
the legacy cache continue to work with `dcu`.
`prepare` also installs the Bash aliases, including `cdcompose` and `dcu`
on both Linux profiles and `cdconfig` on the server profile. From the stack's
directory, run:

```sh
dcu
dcu --project-name my-project --pull always
```

`dcu` runs `docker compose up --detach` on the local Docker host. For a
manual Compose command, use `env -u HOSTNAME docker compose` so the
host value in the selected environment file takes precedence.

For a project declared by the agent, it preserves the project's environment files
and adds the rendered file as the last `--env-file`. It stops if that file is
missing or has unsafe permissions. Use `--project-name` if the project name
depends on interpolation, or `--file` to select a Compose file explicitly.
The launcher also recognizes `--project-directory`, `--profile`, and
`--env-file`. Additional options go to `docker compose up`.

For existing projects, `onboardrr compose-update` also uses the managed
environment file automatically. Secret changes refresh the rendered files.
Apply or recreate the affected services to load the new values.

For a host running Komodo Core, add `komodo_role_id` and `komodo_secret_id`
to its bootstrap record and use a separate AppRole. For a deployment using
dedicated records, store the Core template in `komodo_template` and grant the
AppRole access to the paths that template reads. Zero's template reads from
`hosts` and `services`, including `hosts/data/common/USER`. Deployments still
using the generated legacy template need read access to `komodo/data/variables`
and `hosts/data/common/USER`.

For dedicated Newt storage, set `komodo_newt_mount` to `services` in the
Komodo bootstrap record. Grant the Core Agent `list` on
`services/metadata/pangolin/newt` and `read` on `services/data/pangolin/newt/+`.
The generated template reads the dedicated pairs for the existing Komodo
aliases and excludes legacy Newt fields from the aggregate.

For an existing Core agent, set `komodo_auth_mount` to its AppRole mount and
keep the deployed Core template in `komodo_template`. Linux preparation
reuses an existing operator-owned `~/.docker/komodo-openbao/rendered` so the
active Core watcher can keep reading its configuration.

On a fresh host, preparation starts `openbao-agent-komodo` using
`~/.config/docker/openbao/komodo/compose.yaml` and writes the configuration
to `komodo/rendered/core.config.toml`. To adopt the generated override,
append `~/.config/docker/openbao/komodo/core.override.yaml` to the Komodo
Core Compose files, replacing any previous OpenBao-specific override.
The Core container must be able to read the operator-owned `0600` file and
use a restart policy such as `unless-stopped` so that the watcher can reload
Core when secrets change. Both agents use the pinned official OpenBao image.

Use `--skip-openbao-agent` for a Linux host that exclusively uses the
existing Komodo secret integration or does not need either agent.
This setup currently runs for the Linux profiles.

## Docker Hub authentication

During an apply, Linux and QNAP preparation sign the SSH operator in to
Docker Hub using `services/dockerhub/USERNAME` and
`services/dockerhub/TOKEN` in OpenBao. Both records use the field `value`.
Give the controller read access to these two paths using the
[controller policy](../provisioning/openbao/onboardrr-read.hcl).
Use a Docker Hub access token with pull permissions. If neither record
exists, preparation skips the login.

The token is sent to Docker through standard input, and Docker updates the
operator's credential store or private `~/.docker/config.json`. This runs
before agent or monitoring image pulls and also runs in Auto when system
preparation is selected.

## Prepare Linux on a test Raspberry Pi

Ansible Core 2.20 uses Python 3.12 or later on the controller. Put
`ansible-playbook` on the controller's PATH. Confirm the target's registered
SSH endpoint and trusted host key first. Full preparation also requires the
OpenBao Goku credentials.

Use `onboardrr prepare DEVICE --system-only` to preview the local package,
Docker, and operator-directory preparation before Goku is configured. Add
`--apply` to perform that work. Use both mount sources below for full
preparation, including SMB credentials, persistent mounts, and deployment aliases.

```sh
python3 -m pip install -r provisioning/requirements.txt
ansible-galaxy collection install -r provisioning/collections.yml
python3 -m onboardrr prepare lab-pi \
  --cifs-source //goku.example.org/compose \
  --nfs-source goku.example.org:/docker/komodo/data/lab-pi
```

The command defaults to Ansible check mode. Add `--apply` to apply the role,
`--ask-become-pass` if sudo needs a password, and `--allow-reboot` to allow a
required reboot. Use `--known-hosts` for a separate SSH trust file. Ansible
logs go to stderr and the CLI result to stdout. The controller sends SMB
credentials to the playbook in memory. The applied role keeps a root-only
file on the target for persistent CIFS mounts.

The standalone playbook can also use a pre-existing root-owned credentials
file, with its path set in `onboardrr_smb_credentials_file` and mode 0600 or
0400. The role uses Debian Trixie's Docker and Compose packages on a fresh
system, preserves existing Docker packages, and adds the sudo-capable
operator to the Docker group. A new login picks up group membership. The
role refuses to mount over local data or modify conflicting mounts, and
backs up `fstab` and `.bashrc` before edits. When moving from system-only
preparation to Goku mounts, it can remove the operator's empty per-device
Compose workspace directory if there are no other entries.

Raspberry Pi and Linux server preparation install a passwordless sudo rule
for the enrolled operator. Use `sudo -n systemctl reboot` to reboot or
`sudo -n shutdown -h now` to power off. The rule also allows plain
`reboot`, `poweroff`, `shutdown now`, and `systemctl poweroff` through
sudo with those exact arguments.

Both Linux profiles install BlueZ, enable `bluetooth.service`, clear software
blocks for Bluetooth, and power the detected adapters. A missing or
hardware-blocked adapter is reported without stopping Linux preparation.
The role leaves Raspberry Pi UART and firmware overlays intact.

## Prepare a Linux server

Register the device with `--profile server`. The CLI also accepts `serveur` at
registration and stores the canonical `server` value. This profile supports
Debian 13, Ubuntu 24.04, and Linux Mint 22.x on ARM64 or x86-64. It installs
OpenSSH, curl, Git, jq, rsync, and the CIFS and NFS clients. With no existing
Docker CLI, it installs Docker CE, Buildx, and the Compose plugin from Docker's
signed APT repository for Debian Trixie or Ubuntu Noble. Mint 22.x uses its
Noble base. An existing Docker installation retains its package source.

Use `prepare DEVICE --system-only` for local preparation. For full preparation
provide the same Compose SMB and Periphery NFS sources as the Raspberry Pi.
Additional shares and kernel settings are opt-in and can be selected per host.

```sh
python3 -m onboardrr prepare lab-server \
  --cifs-source //goku.local/compose \
  --nfs-source goku.local:/docker/komodo/data/lab-server \
  --dev-source //goku.local/dev \
  --scripts-source //goku.local/scripts \
  --downloads-source //goku.local/downloads \
  --inotify-instances 1024 \
  --swappiness 1
```

The extra mounts use the same OpenBao SMB credentials and conflict checks.
Their destinations are `~/dev`, `~/scripts`, and `~/downloads`.
`--plex-source` is also available for `/mnt/goku_plex`. Requested kernel values
are stored in dedicated files under `/etc/sysctl.d` and applied immediately.

- **NVIDIA**. Add `--nvidia` on a server with an NVIDIA PCI graphics device.
  The playbook reuses a working driver and installs NVIDIA Container Toolkit
  if needed. When selecting a new driver, pass
  `--nvidia-driver-package PACKAGE` using a package available in the target
  distribution's repositories. The playbook preserves the default Docker
  runtime and restarts Docker only when loading a missing NVIDIA runtime.
  A newly installed driver can require a reboot. Use `--allow-reboot` to
  authorize an OS reboot when the system reports one is pending.
- **VNC**. Add `--vnc` for an XFCE desktop with on-demand TigerVNC. As the
  operator, run `tigervncpasswd`, then `~/.config/onboardrr-vnc`. The launcher
  binds to loopback and requires VNC authentication. Tunnel the port shown
  by the launcher over SSH, such as
  `ssh -L 5902:127.0.0.1:5902 operator@lab-server.local` for display `:2`.

Zero's existing container handles UPS monitoring. The server profile leaves
UPS workloads, Plex jobs, and disk layout to their own deployment workflows.

## Prepare a QNAP NAS

Register the NAS with `--profile nas`. Install or enable Container Station in
QNAP App Center. The profile uses the alternative Entware QPKG from the
[NAS Entware guide](https://wiki.kinou.xyz/doc/installation-entware-xCnjanMRaD).

With the account's key already authorized on the NAS and loaded in the SSH
agent, run the preflight and preview:

```sh
python3 -m onboardrr preflight lab-nas --use-key --public-key ~/.ssh/operator.pub --apply-key
python3 -m onboardrr prepare lab-nas --public-key ~/.ssh/operator.pub
```

The preflight records an already authorized key when run this way.
`bootstrap-key --prepare-nas` installs missing Entware and Python before
enrolling an operator key.
The native preflight and preparation work without target Python. Use
`--ask-become-pass` if installing Entware or querying Docker requires sudo.
The preview checks firmware, the local Container Station engine, Compose V2,
and Entware. With `--apply`, the profile installs missing Entware through
QTS, verifies that `/opt/bin/opkg` works, and installs Python through opkg
if needed. It prepares the account's `~/.docker/compose` workspace and
adds `opkg`, `cdcompose`, and `dcu` to its login shell. It reuses a working Entware
installation and stops for a conflicting package or occupied `/opt`.
The QPKG download uses HTTPS and a pinned SHA-256. The upstream QPKG
installer fetches its bootstrap files over HTTP.

In a new SSH login shell, `docker` and `docker compose` use the binaries
from Container Station and its local Docker socket. The launcher supports
the Compose plugin or Container Station's bundled standalone Compose V2.
`dcu` starts the current project in detached mode. It uses a private
per-project OpenBao environment when one is already rendered in
`~/.config/docker/openbao/compose/cache` and supports explicit `--env-file`
options. `compose-update` uses the same Container Station installation.

The guided and automatic NAS installation continue through OpenBao,
Komodo registration, Goku storage, Periphery, Pangolin site creation,
the Docker proxy, monitoring agents, Newt, private Pangolin routes, and
final verification. Wi-Fi and Bluetooth are outside the NAS profile.
OpenBao runs as the `secrets` Compose project through Container Station.
Its rendered environment is available to `dcu` and the Compose update
action. When a NAS already runs the OpenBao Compose agent from the previous
`~/.config/docker/compose-openbao` directory, Onboardrr recognizes the
existing `secrets` deployment and migrates it to
`~/.config/docker/openbao/compose`. It verifies that the new agent has
rendered its private files and restores the previous deployment if the
migration fails. Use **OpenBao agent** or the following CLI command to set
it up independently.

```sh
onboardrr prepare lab-nas --public-key ~/.ssh/operator.pub \
  --component openbao-agent --apply --ensure-openbao-agent
```

Onboardrr stages the deployment files in `/share/compose/<nas>` on the
primary storage NAS over SSH. The primary NAS links `~/.docker/compose`
to its local share and binds its local Periphery directory. A secondary
NAS mounts the primary Compose share over SMB at `~/.docker/compose`
and its dedicated Periphery export over NFS at
`/share/docker/komodo/data/<nas>`, as on a Raspberry Pi. Container
Station also uses that export directly for the agent volume, so an agent
start before the host mount cannot use an empty local directory.
Onboardrr stores the mount configuration privately and uses QTS's
persistent crontab to retry the mounts after reboot. It uses QTS's native
`/usr/bin/crontab` and verifies the recovery job after reloading cron. The secondary setup
requires QTS sudo and the primary NAS SMB credential in OpenBao.
QTS reports changes in mount restoration state to the system log with
the `onboardrr-mounts` tag. Restarts verify the permissions on the
private configuration before loading it.
Onboardrr checks final container
state, the Komodo connection, Goku storage, and the Pangolin site. An
existing NAS run that already completed QTS and Docker preparation
resumes at Komodo registration.

### Choose the primary storage NAS

Choose **Maintenance → Select the primary storage NAS**, then select
a QNAP NAS from the inventory list. The preview resolves its endpoint to a LAN IPv4
address. Supply **NAS IPv4 address** when the storage address differs
or hostname resolution is ambiguous.

```sh
onboardrr set-storage-nas new-nas --ipv4 192.168.1.50
onboardrr set-storage-nas new-nas --ipv4 192.168.1.50 --apply
```

On apply, Onboardrr updates the `value` field of `services/storage/IP`,
which Komodo's OpenBao Agent renders as `STORAGE_IP`. Apply the updated
controller policy in `provisioning/openbao/onboardrr-read.hcl` to grant
access to this key. A generated Komodo Agent uses that same path in the
split layout and needs read access. The selected inventory host supplies
the SSH endpoint, username, and port. On Linux profiles, SMB and NFS
defaults use the stored IPv4.
In the split layout, put the replacement NAS credentials in
`hosts/<device>/smb` as `smb_username` and `smb_password`.

Prepare the replacement `/share/compose` and
`/share/docker/komodo/data` shares before switching. The selection changes
the defaults for upcoming operations. Migrating existing data and mounts
requires a separate operation. If the OpenBao update is interrupted
after publication starts, apply the same selection again to resume it.

## Wi-Fi after Ethernet bootstrap

The Wi-Fi step uses NetworkManager on Raspberry Pi OS and can run independently
of Goku, Komodo, and Pangolin. Connect the Pi over Ethernet using DHCP and make
sure SSH is enabled. Run `sudo raspi-config` to set the local WLAN country if
needed. Use a supported 2.4 GHz network with a Raspberry Pi 3 Model B. A 3B+ also
supports 5 GHz.

`onboardrr wifi DEVICE` runs a read-only check, including when the connection
already uses Wi-Fi. Apply from a wired SSH session with `--apply` and the
adapter enabled. Use `--band 2G` or `--band 5G` to select the shared OpenBao
entry and lock the profile to that band. Without a band, OpenBao uses the
legacy per-device entry if present. Without OpenBao, the command prompts for
credentials in the terminal. Add `--interface` for an adapter named
something other than `wlan0`. The first profile supports visible WPA2 Personal
networks and DHCP. The passphrase stays out of the controller inventory and
command arguments. NetworkManager stores the resulting key in a root-only
profile on the Pi. Wi-Fi uses a higher route metric than the wired connection.

After the playbook reports an IPv4 address on Wi-Fi, verify a separate SSH
connection through that address with the known host key while keeping Ethernet
connected. The device's inventory endpoint and Ethernet settings stay as they
were. An active profile created outside Onboardrr is preserved. Switching
between managed bands retains the earlier profile and attempts to reconnect
it if the new activation fails.

## References

- [OpenBao HTTP authentication](https://openbao.org/api-docs/)
- [OpenBao KV v2](https://openbao.org/docs/secrets/kv/kv-v2/)
- [Komodo API and clients](https://komo.do/docs/ecosystem/api)
- [Pangolin integration API routes](https://docs.pangolin.net/manage/common-api-routes)
- [Debian Trixie Compose package](https://packages.debian.org/stable/docker-compose)
- [Ansible mount module](https://docs.ansible.com/projects/ansible/latest/collections/ansible/posix/mount_module.html)
- [Raspberry Pi OS networking configuration](https://www.raspberrypi.com/documentation/computers/configuration.html)
- [NetworkManager keyfile settings](https://networkmanager.dev/docs/api/latest/nm-settings-keyfile.html)
- [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)
- [WUD REST API and version tracking](https://getwud.app/)
