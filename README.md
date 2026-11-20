# Onboardrr

Onboardrr is a self-hosted controller for preparing Linux devices and enrolling
them in Komodo and Pangolin. The CLI manages device inventory and run records,
previews a target over SSH, enrolls its operator key, and prepares its Komodo
registration. The Linux preparation playbook is available for test devices.

## Install and launch

Install with [pipx](https://pipx.pypa.io/) on macOS or Linux:

```sh
pipx install onboardrr
pipx ensurepath
onboardrr
```

From any directory, `onboardrr` opens the TUI and `onboardrr --help` lists
CLI commands. Run `pipx install .` to install from a source checkout. The
checkout also supports `python3 -m onboardrr`
with Python 3.11 or later and the dependencies in `requirements.txt`.
Linux provisioning and Wi-Fi require an external `ansible-playbook` and
the collections in [provisioning/collections.yml](provisioning/collections.yml).

### Download GitHub releases

Download a wheel or a native archive from [GitHub Releases](https://github.com/kinouzero/onboardrr/releases).
The [installer workflow](.github/workflows/build-installers.yml) builds Linux
x86-64 and ARM64 on Ubuntu 22.04 and macOS Intel and Apple Silicon on macOS
15. Version tags publish the matching archives to GitHub Releases and the
Python distributions to PyPI. A manual run on `master` builds preview
artifacts. Each binary archive and wheel includes a SHA-256 checksum.

Install the wheel with `pipx install ./onboardrr-0.1.0-py3-none-any.whl`.
For a native archive, extract it and run `./onboardrr` or put the executable
in a directory on your `PATH`.

### Build distributions

Run these from the checkout with Python 3.11 or later:

```sh
python3 -m pip install -r requirements.txt build pyinstaller==6.22.3
python3 -m build
python3 -m PyInstaller --noconfirm distribution/onboardrr.spec
```

The wheel in `dist/` can be installed with pipx. The standalone
`dist/onboardrr` executable includes Python and runs on its build platform.
Build it on macOS or Linux for the desired operating system and CPU
architecture. For Linux, build on the oldest glibc distribution you intend
to support. Place the executable in a directory on your `PATH` to use it
from any terminal.

## Local usage

```sh
onboardrr add lab-pi --user operator
onboardrr add lab-nas lab-nas.local --user operator --profile nas
onboardrr add lab-server lab-server.local --user operator --profile server
onboardrr set-profile lab-server raspberry-pi
onboardrr list
```

### Terminal interface

```sh
onboardrr
```

On macOS, the terminal interface follows the system appearance while open.
Graphite is the dark theme and the fallback when system appearance is
unavailable. Ivory is the light theme. Press **F7** to cycle through
Graphite, Ivory, and automatic mode for the current session.

The app opens on **Home**, where the selected host's checkpoint and
shortcuts to the wizard, Compose updates, and history are visible.
Select **Home** in the top bar to return from any subpage, including the
wizard and results.
Use **F2** to add a host, or **F3** to return to the inventory and **Enter** to
open the selected host's guided step. Use **Ctrl+P** to search for a screen or
action for the selected host. Use **Edit profile** from the host list or the
palette to correct the profile. Profile changes keep the host's identity and history.
Switching between Linux and QNAP starts a new workflow when the host already
has a provisioning run. **Continue** shows the next action for its profile
and saved checkpoint. Choose **Steps** for a different provisioning action.
Use **Preview** to check readiness and **Apply** to confirm changes.
**Steps** lists actions in execution order. **Install Docker**
includes Docker Hub authentication. **Configure Goku** prepares the
deployment files, the NFS export, and the SMB and NFS mounts.

For a Raspberry Pi with USB storage, choose **Mount USB storage**, load its
USB devices, and select the data volume. The same step is available as an
opt-in in **Auto**. Apply mounts it at `~/ssd` in the SSH user's home on the
Raspberry Pi and adds a UUID entry for systemd automount. Boot can continue
if the drive is disconnected.
Unmount a volume already mounted at another location before applying.

For the JMicron USB-to-SATA bridge `152d:1561`, Mount and Format first
configure the Raspberry Pi to use `usb-storage` instead of UAS. Onboardrr
backs up the boot command line and preserves quirks for other adapters. When
the fallback is pending, reboot the Pi and apply the same step again. An
automatic installation stops at the USB step until the fallback is active.

Use **Maintenance → Unmount USB storage** to detach a selected volume and
stop its active Onboardrr automount for the current boot. Its UUID entry in
`/etc/fstab` stays in place. The CLI option is `--usb-device VOLUME --unmount
--apply`. Close files on the volume before applying.

In **Maintenance → Format USB storage**, select a volume and confirm the
dialog showing its host and device path. The action unmounts the volume,
erases its data, and mounts the new ext4 filesystem at `~/ssd` by default.
USB formatting uses ordinary writes in place of discard and block zeroing requests.
Close any files on the volume so that it can be unmounted. Use `--mount-point`
to choose another directory directly under that user's home or `/mnt`. The CLI equivalents are
`onboardrr usb-storage DEVICE --public-key PATH`, with `--usb-device VOLUME
--apply` to mount or `--usb-device VOLUME --format --confirm-device VOLUME
--apply` to format.

Commands stream their logs into **Results**. The overall bar counts selected
components processed by an automatic run. The current-step bar animates until
the command reports that the step has finished. SSH, sudo, and Wi-Fi prompts
open inside the interface. Results also show the first blocker and the last
20 executions of the session. Use **Esc** to return to the form and **F6**
to reopen results. **Maintenance** groups routine operations and
**History** shows saved provisioning runs.

Use **F1** for command help, **F5** to refresh the inventory, and **F10**
to exit. Open **Options** for extra arguments or **CLI** to run another
Onboardrr subcommand. Enter the target's sudo password once in the masked
**Host sudo password** field to use it throughout a run.
Leave the field blank for passwordless sudo. The CLI also supports
`--ask-become-pass` for a masked prompt. To open a different inventory,
pass `--database PATH` before `tui`.

Open **Wizard** from the dashboard or **Auto** from the action screens to
install or uninstall several components in one run. The wizard starts with
the profile's component defaults, then presents the connection settings and
the review. **Preview** checks the selection and opens the results.
**Install selection** starts installation. **Uninstall selection**
opens a removal confirmation. Return from the results to adjust the selection
or settings. Installation resumes from the recorded checkpoint.

The **SSH public key** picker lists files in `~/.ssh/`. Choose **Other file…**
to enter a different path. Set
`ONBOARDRR_TUI_PUBLIC_KEY` and `ONBOARDRR_TUI_KNOWN_HOSTS` to prefill the
SSH paths. The interface inherits the existing CLI environment, including
OpenBao and service settings.

The device name identifies the record independently of its SSH endpoint.
Omit the address in `add` or leave the address field blank in the terminal
form to use `<name>.local`. Update an existing address with
`onboardrr set-host lab-pi lab-pi.local` to keep the same device and run history.
Conflicting names and SSH endpoints return an error. The inventory does not
resolve DNS or contact a host.

The CLI prints JSON and stores its database at
`$XDG_STATE_HOME/onboardrr/inventory.sqlite3`, using
`~/.local/state/onboardrr/inventory.sqlite3` when the environment variable is
unset or relative. Use `--database PATH` before the command to select a different
file.

### SSH preflight and key enrollment

Install the SSH dependency in the controller environment with
`python3 -m pip install -r requirements.txt`. Add the target's verified host
key to an OpenSSH `known_hosts` file and load the operator identity in the local
SSH agent. If the inventory hostname cannot resolve, Onboardrr tries its local
OpenSSH `HostName` and checks the key against the inventory hostname and port.
The same fallback supplies IP addresses to integrations such as Pangolin and
Termix when they require an IP. A hostname with multiple matching addresses
still requires an explicit address.
Bootstrap the public key with the target's initial login and password.

```sh
python3 -m onboardrr bootstrap-key lab-pi --public-key ~/.ssh/id_ed25519.pub
```

This installs the key through the password-authenticated session and verifies it
through a second, key-only session. It can run before the central services are
configured. The password is prompted for in a terminal or can be piped in from
a trusted credential source with `--password-stdin`. In the terminal
interface, **Verify SSH** checks the enrolled key and records the SSH
checkpoint. The CLI equivalent is:

```sh
python3 -m onboardrr preflight lab-pi --ssh-only --use-key \
  --public-key ~/.ssh/id_ed25519.pub --apply-key
```

Run the full preflight to check sudo and the central services from the target:

```sh
python3 -m onboardrr preflight lab-pi \
  --public-key ~/.ssh/id_ed25519.pub \
  --goku-host goku.local --komodo-host komodo.example.org \
  --pangolin-host pangolin.example.org
```

The preflight queries SMB and NFS on Goku and HTTPS on Komodo and Pangolin from
the target and prints the planned steps. Re-run it with `--apply-key` to
complete the SSH checkpoint after those checks pass. Both commands use the SSH
port in the inventory and support `--known-hosts PATH` for a separate trust file.
After bootstrapping, pass `--use-key` to use the selected agent identity instead
of the initial SSH password. Add `--ask-become-pass` if sudo needs a password.

### Start on Ethernet and add Wi-Fi later

For a first boot without Wi-Fi, enable SSH and configure the account when
writing the OS image. Plug the Pi into Ethernet, find its DHCP address, and
register that address or a name that resolves to it. Run `bootstrap-key` over
Ethernet. Once the adapter and WLAN country are configured, keep the cable
connected while checking and adding Wi-Fi:

```sh
python3 -m onboardrr wifi lab-pi
python3 -m onboardrr wifi lab-pi --band 2G --apply --ask-become-pass
```

The first command reads the current SSH route, adapter, and Wi-Fi status. The
second configures a persistent WPA2 Personal profile for the selected band
from a wired SSH session. Choose `2G` or `5G` in **Maintenance** → **Enable
Wi-Fi** in the TUI. The command preserves unrelated profiles and reports
the Wi-Fi address after activation. Verify SSH to that address before unplugging
Ethernet. The application leaves the registered endpoint unchanged.
See [Wi-Fi deployment settings](docs/integrations.md#wifi-after-ethernet-bootstrap).

### Central services and Linux preparation

See [integration setup](docs/integrations.md) for OpenBao, the read-only
Komodo and Pangolin commands, the Komodo registration preview, the Periphery
configuration renderer, and the Ansible playbook for Docker and Goku mounts.
Use [outbound Periphery recovery](docs/integrations.md#recover-an-outbound-periphery-agent)
to restore an existing Komodo registration after reinstalling Linux.
See [NAS](docs/integrations.md#prepare-a-qnap-nas) and
[server](docs/integrations.md#prepare-a-linux-server) for their preparation commands.
See [primary NAS selection](docs/integrations.md#choose-the-primary-storage-nas)
to switch the Compose and Komodo storage endpoint.

The TUI actions **Deploy baseline stacks** and **Add to monitoring tools**
preview changes before applying them. Baseline deployment
selects compatible Docker Hub image tags using WUD labels. Monitoring uses
Komodo variables to enroll hosts in Beszel, WUD, Prometheus, and Dozzle.
See [baseline stacks](docs/integrations.md#deploy-the-baseline-komodo-stacks)
and [monitoring setup](docs/integrations.md#enroll-devices-in-central-monitoring).

### Update selected Compose files

```sh
python3 -m onboardrr compose-update lab-pi --list-files --public-key ~/.ssh/id_ed25519.pub
python3 -m onboardrr compose-update lab-pi --file compose.yaml --file media/docker-compose.yml \
  --public-key ~/.ssh/id_ed25519.pub --apply
```

The terminal's Maintenance menu lets you select multiple files from the
host's default Compose directory. Its **Apply and deploy** button
edits the files directly and redeploys the affected running services.
Add `--deploy` to the CLI command for the same behavior.
See [Compose maintenance](docs/integrations.md#update-existing-compose-projects).

### Run history

The terminal shows operation progress alongside the result. To enable
the same timestamped progress in the CLI, add `--verbose` before the
subcommand. Progress goes to stderr and the JSON result goes to stdout.

```sh
python3 -m onboardrr run open lab-pi
python3 -m onboardrr run list lab-pi
python3 -m onboardrr run show RUN_ID
```

Use the ID printed by `run open` in `run show`. Opening a run creates a local
record or returns the existing unfinished run and its next step. These commands
operate only on SQLite. Existing inventory databases migrate automatically.

`python3 -m onboardrr verify lab-pi --public-key ~/.ssh/id_ed25519.pub` checks
the mounts, local Docker and Compose, Periphery, Komodo stacks, and Pangolin.
Add `--apply` to record successful verification or a failed checkpoint.
Use the integration API environment or add `--ssh-target` for private Pangolin.
Once Newt connects, enrollment reconciles the device's private `<device>.pang`
host route and its peer site assignments. See
[private host routes](docs/integrations.md#reconcile-private-host-routes)
for preview and apply commands for existing sites.

`python3 -m onboardrr retire lab-pi` previews recorded resources in retirement
order. Add `--purge` to preview deletion of tracked Goku data directories.
`retire lab-pi --apply` removes recorded monitoring entries and owned Komodo
baseline stacks when eligible, preserving volumes. See [retirement and Goku purge](docs/integrations.md#retire-a-linux-device)
for Pangolin site removal and the separate opt-in purge.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

To measure statement and branch coverage, including supported Python
subprocesses, install the optional test dependency and run:

```sh
python3 -m pip install -e '.[test]'
python3 -m coverage run -m unittest discover -s tests -q
python3 -m coverage report -m
```

See the [MVP architecture](docs/architecture.md) for the first target and
integration milestones.
