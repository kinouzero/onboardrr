import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.style import Style
from textual.widgets import Button, DataTable, Input, Log, OptionList, ProgressBar, Select, SelectionList, Static

from onboardrr.inventory import NAS_PROFILE, PROFILE, SERVER_PROFILE, Inventory
from onboardrr.primary_storage import write_selection
from onboardrr.runs import RunStore
from onboardrr.storage import connect
from onboardrr.tui import ConfirmScreen, ExecutionPrompt, OnboardrrApp, clean_output, command_args
from onboardrr.tui_theme import system_theme


class TerminalCommandTest(unittest.TestCase):
    device = {"name": "lab", "profile": PROFILE}

    def test_reads_the_macos_appearance_and_handles_unavailable_settings(self):
        with patch("onboardrr.tui_theme.sys.platform", "darwin"), patch("onboardrr.tui_theme.subprocess.run") as read:
            for result, expected in (
                (subprocess.CompletedProcess([], 0, "Dark\n", ""), "graphite"),
                (subprocess.CompletedProcess([], 1, "", "Could not find key 'AppleInterfaceStyle'"), "ivory"),
                (subprocess.CompletedProcess([], 1, "", "The domain/default pair does not exist"), "ivory"),
                (subprocess.CompletedProcess([], 1, "", "Access denied"), None),
            ):
                with self.subTest(expected=expected):
                    read.return_value = result
                    self.assertEqual(system_theme(), expected)
            read.side_effect = subprocess.TimeoutExpired("defaults", 1)
            self.assertIsNone(system_theme())
        with patch("onboardrr.tui_theme.sys.platform", "linux"), patch("onboardrr.tui_theme.subprocess.run") as read:
            self.assertIsNone(system_theme())
            read.assert_not_called()

    def test_nas_goku_action_selects_staging_and_storage_preparation(self):
        self.assertEqual(
            command_args("goku", {"name": "luffy", "profile": NAS_PROFILE}, "", apply=True,
                         public_key="/fixture.pub"),
            ["auto-install", "luffy", "--select", "goku-files", "--select", "goku-mounts",
             "--public-key", "/fixture.pub", "--apply"],
        )

    def test_komodo_retirement_actions_keep_passkey_deletion_explicit(self):
        base = ["komodo", "unregister", "lab"]
        self.assertEqual(command_args("unregister", self.device, "", apply=True), [*base, "--apply"])
        self.assertEqual(command_args("unregister-passkey", self.device, ""), [*base, "--delete-passkey"])
        self.assertEqual(command_args("unregister-passkey", self.device, "", apply=True),
                         [*base, "--delete-passkey", "--apply"])

    def test_compose_arguments_keep_paths_and_add_apply_deliberately(self):
        kwargs = {"public_key": "/tmp/key with spaces.pub", "known_hosts": "/tmp/trusted"}
        base = ["compose-update", "lab", "--public-key", kwargs["public_key"],
                "--known-hosts", kwargs["known_hosts"]]
        self.assertEqual(command_args("compose", self.device, "--list-files", **kwargs), [*base, "--list-files"])
        options = "--file=compose.yaml '--file=Media Room/docker-compose.yml'"
        self.assertEqual(command_args("compose", self.device, options, apply=True, **kwargs),
                         [*base, "--apply", "--deploy", "--file=compose.yaml", "--file=Media Room/docker-compose.yml"])

    def test_preview_rejects_apply_aliases_and_terminal_password_echo(self):
        for option in ("--apply", "--app", "--apply-key", "--password-stdin", "--pass", "--sudo-password-fd"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                command_args("preflight", self.device, option, public_key="/tmp/example.pub")
        self.assertEqual(command_args("free", None, "run show 'a b'"), ["run", "show", "a b"])
        with self.assertRaises(ValueError):
            command_args("free", None, "--dat /tmp/other.sqlite tui")

    def test_pangolin_uses_the_organization_before_the_device(self):
        self.assertEqual(command_args("routes", self.device, "--ssh-target zero.local",
                                      argument="zero", apply=True),
                         ["pangolin-network", "zero", "lab", "--checkpoint", "--apply", "--ssh-target", "zero.local"])

    def test_stage_defaults_to_remote_goku_staging(self):
        self.assertEqual(command_args("stage", self.device, ""), ["periphery", "lab"])
        with self.assertRaisesRegex(ValueError, "Goku share over SSH"):
            command_args("stage", self.device, "--goku-compose-root /mnt/goku")

    def test_periphery_action_bootstraps_with_the_selected_mounts_and_ssh_key(self):
        options = "--cifs-source //goku/compose --nfs-source goku:/onboardrr-lab"
        self.assertEqual(
            command_args("periphery-start", self.device, options, public_key="/tmp/key.pub", apply=True),
            ["periphery-recover", "lab", "--bootstrap", "--public-key", "/tmp/key.pub", "--apply",
             "--cifs-source", "//goku/compose", "--nfs-source", "goku:/onboardrr-lab"],
        )

    def test_monitoring_previews_and_applies_on_linux_and_nas(self):
        for profile in (PROFILE, NAS_PROFILE):
            with self.subTest(profile=profile):
                device = {**self.device, "profile": profile}
                self.assertEqual(command_args("monitoring", device, ""), ["monitoring", "lab"])
                self.assertEqual(
                    command_args("monitoring", device, "--ipv4 192.168.1.42", apply=True),
                    ["monitoring", "lab", "--apply", "--ipv4", "192.168.1.42"],
                )
                self.assertEqual(command_args("retire", device, ""), ["retire", "lab"])
                self.assertEqual(command_args("retire", device, "", apply=True), ["retire", "lab", "--apply"])

    def test_normalizes_terminal_output_for_the_session_log(self):
        self.assertEqual(clean_output("\x1b[32mready\x1b[0m\r\n\x00ok"), "ready\nok")


class TerminalAppTest(unittest.IsolatedAsyncioTestCase):
    async def test_router_dns_maintenance_previews_and_confirms_without_an_inventory_host(self):
        with tempfile.TemporaryDirectory() as directory:
            app = OnboardrrApp(Path(directory) / "inventory.sqlite3")
            async with app.run_test(size=(100, 32)) as pilot:
                self.assertFalse(app.query_one("#section-maintenance", Button).disabled)
                app.navigate("maintenance", "router-dns")
                await pilot.pause()
                with patch.object(app, "launch") as launch:
                    self.assertTrue(await pilot.click("#run"))
                    launch.assert_called_once_with(["router-dns"], False)
                    launch.reset_mock()
                    app.query_one("#argument", Input).value = "192.168.1.17,192.168.1.4"
                    self.assertTrue(await pilot.click("#apply"))
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    launch.assert_not_called()
                    self.assertTrue(await pilot.click("#confirm"))
                    launch.assert_called_once_with(
                        ["router-dns", "--servers", "192.168.1.17,192.168.1.4", "--apply"], False)

    async def test_router_dns_prefills_detected_addresses_and_retains_manual_edits(self):
        servers = [{"host": "fly", "address": "192.168.1.4"}, {"host": "naruto", "address": "192.168.1.17"}]
        with tempfile.TemporaryDirectory() as directory:
            app = OnboardrrApp(Path(directory) / "inventory.sqlite3")
            async with app.run_test(size=(100, 36)) as pilot:
                with patch("onboardrr.tui.discover_dns", return_value={"servers": servers}) as discover:
                    app.navigate("maintenance", "router-dns")
                    await app.workers.wait_for_complete()
                    discover.assert_called_once_with(app.database)
                argument = app.query_one("#argument", Input)
                self.assertEqual(argument.value, "192.168.1.4,192.168.1.17")
                argument.value = "192.168.1.17"
                with patch("onboardrr.tui.discover_dns", return_value={"servers": []}) as discover:
                    self.assertTrue(await pilot.click("#detect-dns"))
                    await app.workers.wait_for_complete()
                    discover.assert_called_once_with(app.database)
                self.assertEqual(argument.value, "192.168.1.17")

    async def test_usb_format_confirms_the_selected_volume_without_typing_its_path(self):
        alias = "/dev/disk/by-id/usb-fixture-part2"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("fly", None, "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(80, 28)) as pilot:
                app.navigate("maintenance", "format-usb")
                app.usb_source_host = "fly"
                picker = app.query_one("#usb-device", Select)
                picker.set_options([("Fixture USB volume", alias)])
                picker.value = alias
                app.update_controls("format-usb")
                await pilot.pause()
                self.assertFalse(app.query("#usb-confirm-device"))

                with (patch.object(app, "selected_public_key", return_value="zero.pub"),
                      patch.object(app, "launch") as launch):
                    for decision in ("escape", "confirm"):
                        app.query_one("#apply", Button).scroll_visible(animate=False, immediate=True)
                        await pilot.pause()
                        self.assertTrue(await pilot.click("#apply"))
                        self.assertIsInstance(app.screen, ConfirmScreen)
                        message = str(app.screen.query_one(".command-preview", Static).content)
                        self.assertIn("erase all data", message)
                        self.assertIn("fly", message)
                        self.assertIn(alias, message)
                        self.assertTrue(app.screen.query_one("#cancel", Button).has_focus)
                        launch.assert_not_called()
                        if decision == "escape":
                            await pilot.press("escape")
                            launch.assert_not_called()
                        else:
                            self.assertTrue(await pilot.click("#confirm"))
                    launch.assert_called_once()
                    args, interactive = launch.call_args.args
                    self.assertFalse(interactive)
                    self.assertEqual(args[args.index("--usb-device") + 1], alias)
                    self.assertEqual(args[args.index("--confirm-device") + 1], alias)
                    self.assertIn("--format", args)
                    self.assertIn("--apply", args)

    async def test_usb_picker_lists_volumes_and_previews_the_selection(self):
        alias = "/dev/disk/by-id/usb-fixture-part2"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("fly", None, "operator", 22, PROFILE)
            app = OnboardrrApp(database)

            async def preview(job, argv):
                self.assertNotIn("--apply", argv)
                if "--usb-device" in argv:
                    self.assertEqual(argv[argv.index("--usb-device") + 1], alias)
                    app.append_output(job, json.dumps({"status": "preview", "usb_device": alias}))
                else:
                    app.append_output(job, json.dumps({"usb_devices": [
                        {"path": alias, "size": "1T", "label": "Data", "filesystem": "exfat", "mountpoints": []},
                    ]}))
                return 0

            async with app.run_test(size=(110, 36)) as pilot:
                app.navigate("maintenance", "usb-storage")
                await pilot.pause()
                with (patch.object(app, "selected_public_key", return_value="zero.pub"),
                      patch.object(app, "run_captured", side_effect=preview) as runner):
                    self.assertTrue(await pilot.click("#load-usb-devices"))
                    await app.workers.wait_for_complete()
                    picker = app.query_one("#usb-device", Select)
                    self.assertTrue(picker.display)
                    picker.value = alias
                    app.query_one("#run", Button).scroll_visible(animate=False, immediate=True)
                    await pilot.pause()
                    self.assertTrue(await pilot.click("#run"))
                    await app.workers.wait_for_complete()
                    self.assertEqual(runner.await_count, 2)

    async def test_home_button_returns_from_subpages_and_compact_results(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", None, "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(60, 24)) as pilot:
                home = app.query_one("#home", Button)
                self.assertFalse(home.display)
                for button in ("#dashboard-guide", "#dashboard-wizard", "#dashboard-compose", "#dashboard-history"):
                    with self.subTest(button=button):
                        self.assertTrue(await pilot.click(button))
                        self.assertTrue(home.display)
                        self.assertTrue(await pilot.click("#home"))
                        self.assertTrue(app.query_one("#dashboard").display)
                        self.assertFalse(home.display)
                        self.assertTrue(app.query_one("#hosts", DataTable).has_focus)
                    await pilot.pause(.22)

                await pilot.press("f6")
                self.assertTrue(app.query_one("#results").display)
                self.assertTrue(home.display)
                self.assertLessEqual(home.region.bottom, app.query_one("#topbar").region.bottom)
                self.assertTrue(await pilot.click("#home"))
                self.assertTrue(app.query_one("#dashboard").display)
                self.assertFalse(app.query_one("#results").display)

    async def test_prompts_logs_and_both_progress_bars_stay_in_the_tui(self):
        program = """
import json
import sys
from onboardrr.progress import emit, request_input, terminal_events

with terminal_events():
    emit("overall", completed=1, total=3)
    emit("step", label="Prepare fixture", done=False)
    print("Live fixture log", file=sys.stderr, flush=True)
    assert request_input("ssh-password") == "fixture-secret"
    emit("step", label="Prepare fixture", done=True)
    emit("overall", completed=3, total=3)
    print(json.dumps({"status": "applied"}))
"""
        with tempfile.TemporaryDirectory() as directory:
            app = OnboardrrApp(Path(directory) / "inventory.sqlite3")
            capture = app.run_captured

            async def fixture(job, _argv):
                return await capture(job, [sys.executable, "-c", program])

            async with app.run_test(size=(102, 30)) as pilot:
                with patch.object(app, "run_captured", side_effect=fixture):
                    app.launch(["list"], True)
                    for _ in range(80):
                        if (isinstance(app.screen, ExecutionPrompt)
                                and len(app.screen.query("#execution-input"))
                                and "Live fixture log" in app.executions[0].output):
                            break
                        await pilot.pause(.02)
                    self.assertIsInstance(app.screen, ExecutionPrompt)
                    self.assertIn("Live fixture log", app.executions[0].output)
                    screen = app.screen_stack[0]
                    overall = screen.query_one("#overall-progress", ProgressBar)
                    step = screen.query_one("#step-progress", ProgressBar)
                    self.assertEqual((overall.progress, overall.total), (1, 3))
                    self.assertIsNone(step.total)
                    field = app.screen.query_one("#execution-input", Input)
                    self.assertTrue(field.password)
                    field.value = "fixture-secret"
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                self.assertTrue(app.screen.has_class("output-focus"))
                self.assertEqual((overall.progress, overall.total), (3, 3))
                self.assertEqual((step.progress, step.total), (1, 1))
                self.assertNotIn("fixture-secret", app.executions[0].output)
                self.assertIn("Changes applied", app.executions[0].status)

    async def test_button_focus_uses_a_fill_without_underlining_the_label(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", None, "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(108, 31)) as pilot:
                for theme in ("graphite", "ivory"):
                    app.theme = theme
                    for selector in ("#dashboard-compose", "#dashboard-guide"):
                        app.query_one("#hosts", DataTable).focus()
                        await pilot.hover(selector)
                        button = app.query_one(selector, Button)
                        resting_tint = button.styles.background_tint
                        button.focus()
                        await pilot.pause()
                        self.assertTrue(button.has_focus)
                        self.assertNotEqual(button.styles.background_tint, resting_tint)
                        self.assertFalse(button.styles.text_style.underline)
                        self.assertFalse(any(edge for edge, _ in button.styles.border))

    async def advance_wizard(self, pilot):
        await pilot.click("#wizard-next")
        await pilot.pause(.22)

    async def test_dashboard_and_palette_route_to_the_selected_hosts_real_actions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ONBOARDRR_TUI_PUBLIC_KEY": "/fixture.pub"}):
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            inventory.add("fly", "fly.local", "operator", 22, PROFILE)
            inventory.add("goku", "goku.local", "operator", 22, NAS_PROFILE)
            RunStore(database).open("goku")
            app = OnboardrrApp(database)

            async def preview(job, argv):
                self.assertIn("auto-install", argv)
                self.assertIn("goku", argv)
                self.assertNotIn("--apply", argv)
                app.append_output(job, '{"status":"blocked","blockers":["Test preview"]}')
                return 1

            async with app.run_test(size=(110, 36)) as pilot:
                self.assertTrue(app.query_one("#dashboard").display)
                self.assertFalse(app.query_one("#controls").display)
                self.assertFalse(app.query_one("#sections").display)
                await pilot.press("down")
                self.assertEqual(app.device_name, "goku")
                self.assertIn("Step 1/9", str(app.query_one("#dashboard-checkpoint", Static).content))
                await pilot.press("ctrl+p", "c", "o", "m", "p", "o", "s", "e", "enter")
                self.assertEqual(app.selected_action, "compose")
                self.assertTrue(app.query_one("#load-compose-files", Button).display)
                await pilot.press("escape")
                self.assertTrue(app.query_one("#dashboard").display)
                await pilot.click("#dashboard-wizard")
                self.assertFalse(app.query_one("#sidebar").display)
                self.assertFalse(app.query_one("#sections").display)
                components = app.query_one("#auto-components", SelectionList)
                self.assertNotIn("wifi", components.selected)
                self.assertNotIn("bluetooth", components.selected)
                await self.advance_wizard(pilot)
                await self.advance_wizard(pilot)
                self.assertIn("goku · NAS", str(app.query_one("#wizard-position", Static).content))
                with patch.object(app, "run_captured", side_effect=preview) as runner:
                    await pilot.click("#run")
                    await app.workers.wait_for_complete()
                    runner.assert_awaited_once()
                self.assertTrue(app.screen.has_class("output-focus"))
                self.assertEqual(app.viewing.detail, "Test preview")
                await pilot.press("escape")
                self.assertTrue(app.query_one("#wizard-review").display)
                await pilot.press("ctrl+p", "u", "n", "i", "n", "s", "t", "a", "l", "l", "enter")
                self.assertEqual(app.selected_action, "auto-uninstall")
                self.assertTrue(app.screen.has_class("wizard-components"))
                await pilot.resize_terminal(60, 24)
                await pilot.press("f3")
                self.assertGreaterEqual(app.query_one("#hosts", DataTable).region.height, 2)
                self.assertFalse(app.query_one("#edit-host-profile").display)
                await pilot.press("ctrl+p", "p", "r", "o", "f", "i", "l", "enter")
                self.assertEqual(app.screen.query_one("#profile", Select).value, NAS_PROFILE)

    async def test_theme_changes_keep_the_form_and_related_buttons_aligned(self):
        with tempfile.TemporaryDirectory() as directory, patch("onboardrr.tui.system_theme", return_value="ivory") as appearance:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(108, 35)) as pilot:
                self.assertEqual(app.theme, "ivory")
                await pilot.click("#dashboard-wizard")
                components = app.query_one("#auto-components", SelectionList)
                components.deselect("ssh")
                selected = list(components.selected)
                appearance.return_value = "graphite"
                await app.sync_system_theme()
                self.assertEqual(app.theme, "graphite")
                self.assertEqual(components.selected, selected)
                await pilot.press("f7", "f7")
                self.assertEqual(app.theme, "ivory")
                await app.sync_system_theme()
                self.assertEqual(app.theme, "ivory")
                await pilot.press("f7")
                self.assertEqual(app.theme, "graphite")

                await pilot.resize_terminal(64, 27)
                await pilot.pause()
                for selector in ("#wizard-nav Button", "#dashboard-actions Button"):
                    if selector == "#dashboard-actions Button":
                        await pilot.press("escape")
                    buttons = [button for button in app.query(selector) if button.display]
                    self.assertGreater(len(buttons), 1)
                    self.assertEqual({button.region.height for button in buttons}, {3})
                    widths = [button.region.width for button in buttons]
                    self.assertLessEqual(max(widths) - min(widths), 1)
                await pilot.press("f2")
                buttons = list(app.screen.query(".dialog-buttons Button"))
                self.assertEqual({button.region.height for button in buttons}, {3})
                self.assertLessEqual(abs(buttons[0].region.width - buttons[1].region.width), 1)

    async def test_primary_nas_badge_follows_the_active_storage_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            goku = inventory.add("goku", None, "operator", 22, NAS_PROFILE)
            luffy = inventory.add("luffy", None, "operator", 22, NAS_PROFILE)
            with connect(database, write=True) as connection:
                write_selection(connection, "active", goku, "192.0.2.1")
            app = OnboardrrApp(database)
            async with app.run_test(size=(110, 36)) as pilot:
                hosts = app.query_one("#hosts", DataTable)
                profile = hosts.get_row("goku")[1]
                self.assertEqual(profile.plain, "NAS [Primary]")
                self.assertEqual([(span.start, span.end) for span in profile.spans], [(4, 13)])
                self.assertEqual(hosts.get_row("luffy")[1].plain, "NAS")
                self.assertIn("NAS [Primary]", str(app.query_one("#dashboard-address", Static).content))
                for theme in ("graphite", "ivory"):
                    app.theme = theme
                    await pilot.pause()
                    selected = Style.parse(hosts.get_row("goku")[1].spans[0].style)
                    self.assertIsNotNone(selected.color)
                    self.assertIsNone(selected.bgcolor)
                    self.assertIsNone(selected.bold)
                    await pilot.press("down")
                    unselected = Style.parse(hosts.get_row("goku")[1].spans[0].style)
                    self.assertNotEqual(selected.color, unselected.color)
                    self.assertIsNone(unselected.bgcolor)
                    await pilot.press("up")

                with connect(database, write=True) as connection:
                    write_selection(connection, "active", luffy, "192.0.2.2")
                app.refresh_hosts("luffy")
                self.assertEqual(hosts.get_row("goku")[1].plain, "NAS")
                self.assertEqual(hosts.get_row("luffy")[1].plain, "NAS [Primary]")

    async def test_primary_storage_picker_targets_a_registered_nas_independently_of_the_sidebar(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            inventory.add("a-server", "server.local", "operator", 22, SERVER_PROFILE)
            inventory.add("goku", "goku.local", "operator", 22, NAS_PROFILE)
            inventory.add("luffy", "luffy.local", "operator", 22, NAS_PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(110, 38)) as pilot:
                self.assertEqual(app.device_name, "a-server")
                await pilot.click("#dashboard-compose")
                app.query_one("#command", Select).value = "storage-nas"
                await pilot.pause()
                picker = app.query_one("#storage-nas", Select)
                self.assertTrue(picker.display)
                self.assertIs(picker.value, Select.NULL)
                with patch.object(app, "launch") as launch:
                    await pilot.click("#run")
                    launch.assert_not_called()
                    self.assertTrue(picker.has_focus)
                    await pilot.click("#storage-nas")
                    await pilot.pause()
                    items = picker.query_one(OptionList)
                    prompts = [str(items.get_option_at_index(index).prompt) for index in range(items.option_count)]
                    self.assertEqual(prompts[1:], ["goku · goku.local", "luffy · luffy.local"])
                    await pilot.press("end", "enter")
                    self.assertEqual(picker.value, "luffy")
                    app.query_one("#storage-ipv4", Input).value = "192.168.1.50"
                    await pilot.click("#apply")
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    await pilot.click("#confirm")
                    launch.assert_called_once_with(["set-storage-nas", "luffy", "--apply", "--ipv4", "192.168.1.50"], False)

    async def test_auto_sudo_field_passes_the_password_outside_command_arguments(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", None, "operator", 22, PROFILE)
            runs = RunStore(database)
            runs.complete_step(runs.open("lab")["id"], "ssh")
            app = OnboardrrApp(database)
            with patch.dict(os.environ, {"ONBOARDRR_TUI_PUBLIC_KEY": "/fixture.pub"}):
                async with app.run_test(size=(110, 38)) as pilot:
                    await pilot.click("#dashboard-wizard")
                    await self.advance_wizard(pilot)
                    field = app.query_one("#sudo-password", Input)
                    self.assertTrue(field.display and field.password)
                    field.value = "fixture sudo"
                    await self.advance_wizard(pilot)
                    with patch.object(app, "launch") as launch:
                        await pilot.click("#apply", offset=(2, 1))
                    args, interactive = launch.call_args.args
                    self.assertFalse(interactive)
                    self.assertIn("--ask-become-pass", args)
                    self.assertNotIn("fixture sudo", args)
                    self.assertEqual(launch.call_args.kwargs, {"sudo_secret": "fixture sudo"})

    async def test_edits_the_selected_profile_and_refreshes_the_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            initial = inventory.add("naruto", "naruto.local", "operator", 22, SERVER_PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(110, 38)) as pilot:
                await pilot.click("#edit-host-profile")
                self.assertEqual(app.screen.query_one("#profile", Select).value, SERVER_PROFILE)
                app.screen.query_one("#profile", Select).value = PROFILE
                await pilot.click("#save-profile")
                await pilot.pause()
                self.assertEqual(inventory.get("naruto"), {**initial, "profile": PROFILE})
                self.assertEqual(app.device_name, "naruto")
                self.assertIn("Raspberry Pi", str(app.query_one("#device-summary", Static).content))

    async def test_compose_maintenance_loads_and_submits_multiple_host_files(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            paths = ["compose.yaml", "Media Room/docker-compose.yml"]
            capture = app.run_captured
            response = json.dumps({"workspace": "/srv/compose", "files": paths})

            async def listing(job, argv):
                self.assertIn("--list-files", argv)
                program = 'import sys; print("Reading Compose files", file=sys.stderr); print(sys.argv[1])'
                return await capture(job, [sys.executable, "-c", program, response])

            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.click("#dashboard-compose")
                with patch.object(app, "selected_public_key", return_value="/tmp/fixture.pub"), patch.object(
                    app, "run_captured", side_effect=listing
                ):
                    await pilot.click("#load-compose-files")
                    await app.workers.wait_for_complete()
                    selector = app.query_one("#compose-files", SelectionList)
                    self.assertTrue(selector.display)
                    self.assertEqual(selector.option_count, 2)
                    self.assertEqual(app.compose_workspace, "/srv/compose")
                    self.assertIn("Reading Compose files", app.executions[0].output)
                    for path in paths:
                        selector.select(path)
                    with patch.object(app, "launch") as launch:
                        await pilot.click("#run")
                        launch.assert_called_once_with([
                            "compose-update", "lab", "--public-key", "/tmp/fixture.pub",
                            "--workspace", "/srv/compose", *(f"--file={path}" for path in paths),
                        ], False)
                        launch.reset_mock()
                        await pilot.click("#apply")
                        self.assertIsInstance(app.screen, ConfirmScreen)
                        await pilot.click("#confirm")
                        launch.assert_called_once_with([
                            "compose-update", "lab", "--public-key", "/tmp/fixture.pub", "--apply", "--deploy",
                            "--workspace", "/srv/compose", *(f"--file={path}" for path in paths),
                        ], False)

    async def test_automatic_modes_offer_profile_defaults_and_submit_selected_components(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", None, "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            with patch.dict(os.environ, {"ONBOARDRR_TUI_PUBLIC_KEY": "/fixture.pub"}):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.click("#dashboard-wizard")
                    components = app.query_one("#auto-components", SelectionList)
                    self.assertEqual(components.selected, [
                        "ssh", "system", "docker", "bluetooth", "komodo", "goku",
                        "periphery", "pangolin", "docker-proxy", "monitoring-agents", "newt", "pangolin-routes", "verify",
                    ])
                    components.deselect("pangolin")
                    await self.advance_wizard(pilot)
                    await self.advance_wizard(pilot)
                    with patch.object(app, "launch") as launch:
                        await pilot.click("#apply", offset=(2, 1))
                        args, interactive = launch.call_args.args
                        self.assertEqual(args[:2], ["auto-install", "lab"])
                        self.assertNotIn("pangolin", args)
                        self.assertNotIn("goku", args)
                        self.assertTrue({"docker", "docker-auth", "goku-files", "goku-mounts"} <= set(args))
                        self.assertIn("--apply", args)
                        self.assertTrue(interactive)
                    await pilot.press("escape", "escape")
                    app.query_one("#command", Select).value = "auto-uninstall"
                    await pilot.pause()
                    self.assertIn("docker-proxy", components.selected)
                    self.assertNotIn("purge", components.selected)
                    self.assertNotIn("ssh", components.selected)

    async def test_ssh_picker_uses_public_files_and_forwards_the_selected_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ssh = home / ".ssh"
            ssh.mkdir()
            for name in ("first.pub", "zero.pub", "zero", "known_hosts"):
                (ssh / name).touch()
            (ssh / "directory.pub").mkdir()
            database = home / "inventory.sqlite3"
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            RunStore(database).open("lab")
            app = OnboardrrApp(database)
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"ONBOARDRR_TUI_PUBLIC_KEY": ""}):
                async with app.run_test(size=(60, 24)) as pilot:
                    await pilot.press("enter")
                    picker = app.query_one("#ssh-key", Select)
                    self.assertTrue(picker.has_focus)
                    await pilot.press("enter")
                    options = picker.query_one(OptionList)
                    labels = [str(options.get_option_at_index(index).prompt) for index in range(options.option_count)]
                    self.assertEqual(labels[1:], ["first.pub", "zero.pub", "Other file…"])
                    await pilot.press("z", "enter")
                    self.assertEqual(picker.value, str(ssh / "zero.pub"))
                    self.assertFalse(app.query_one("#public-key", Input).display)
                    with patch.object(app, "launch") as launch:
                        await pilot.click("#run", offset=(2, 1))
                        launch.assert_called_once_with(
                            ["preflight", "lab", "--public-key", str(ssh / "zero.pub"), "--use-key", "--ssh-only"], False,
                        )

    async def test_ssh_picker_preserves_a_configured_path_outside_ssh_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            database = home / "inventory.sqlite3"
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            RunStore(database).open("lab")
            app = OnboardrrApp(database)
            with (
                patch.object(Path, "home", return_value=home),
                patch.dict(os.environ, {"ONBOARDRR_TUI_PUBLIC_KEY": "~/keys/custom.pub"}),
            ):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("enter")
                    custom = app.query_one("#public-key", Input)
                    self.assertTrue(custom.display)
                    self.assertEqual(custom.value, "~/keys/custom.pub")
                    with patch.object(app, "launch") as launch:
                        await pilot.click("#run", offset=(2, 1))
                        launch.assert_called_once_with(
                            ["preflight", "lab", "--public-key", "~/keys/custom.pub", "--use-key", "--ssh-only"], False,
                        )

    async def test_inventory_real_cli_output_history_and_compact_results(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            inventory.add("lab", "lab.local", "operator", 22, PROFILE)
            inventory.add("nas", "nas.local", "operator", 22, NAS_PROFILE)
            run = RunStore(database).open("lab")
            app = OnboardrrApp(database)
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.pause()
                self.assertEqual(app.device_name, "lab")
                await pilot.click("#dashboard-history")
                app.query_one("#command", Select).value = "current"
                await pilot.pause()
                await pilot.click("#run")
                await app.workers.wait_for_complete()
                self.assertEqual(json.loads(app.executions[0].output)["id"], run["id"])
                self.assertIn("Done", app.executions[0].status)
                self.assertFalse(app.query_one("#buttons").display)
                await pilot.click("#back-to-action", offset=(2, 1))
                self.assertFalse(app.screen.has_class("output-focus"))
                self.assertTrue(app.query_one("#buttons").display)

                app.query_one("#command", Select).value = "history"
                await pilot.pause()
                await pilot.click("#run")
                await app.workers.wait_for_complete()
                self.assertEqual(json.loads(app.executions[0].output)[0]["id"], run["id"])
                app.query_one("#execution", Select).value = 1
                await pilot.pause()
                self.assertEqual(app.viewing.number, 1)

                await pilot.resize_terminal(76, 24)
                await pilot.pause()
                output = app.query_one("#output", Log)
                self.assertTrue(output.has_focus)
                self.assertGreater(output.region.height, 6)
                self.assertLessEqual(output.region.bottom, 24)

    async def test_apply_confirmation_and_registration_form(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            app = OnboardrrApp(database)
            async with app.run_test(size=(108, 35)) as pilot:
                await pilot.press("f2")
                for selector, value in (("#name", "lab"), ("#host", "lab.local"), ("#user", "operator")):
                    app.screen.query_one(selector, Input).value = value
                await pilot.click("#save")
                await pilot.pause()
                self.assertEqual(app.device_name, "lab")
                self.assertFalse(app.query_one("#command", Select).display)
                await pilot.click("#other-steps", offset=(2, 1))
                app.query_one("#command", Select).value = "docker-proxy"
                await pilot.pause()
                self.assertTrue(app.query_one("#apply", Button).display)

                with patch.object(app, "launch") as launch:
                    await pilot.click("#apply")
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    await pilot.press("escape")
                    launch.assert_not_called()
                    await pilot.click("#apply")
                    await pilot.click("#confirm")
                    launch.assert_called_once_with(["komodo", "baseline", "lab", "--kind", "docker", "--apply"], False)

    async def test_wifi_band_selection_is_passed_to_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.click("#dashboard-compose")
                app.query_one("#command", Select).value = "wifi"
                await pilot.pause()
                selector = app.query_one("#wifi-band", Select)
                self.assertTrue(selector.display)
                selector.value = "5G"
                with patch.object(app, "launch") as launch:
                    await pilot.click("#run")
                    launch.assert_called_once_with(["wifi", "lab", "--band", "5G"], False)

    async def test_empty_inventory_guides_first_host_and_resumes_from_the_real_run(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            app = OnboardrrApp(database)
            async with app.run_test(size=(60, 24)) as pilot:
                self.assertTrue(app.query_one("#start-add", Button).has_focus)
                self.assertFalse(app.query_one("#command", Select).display)
                await pilot.press("enter")
                for selector, value in (("#name", "lab"), ("#host", ""), ("#user", "operator"), ("#port", "22")):
                    field = app.screen.query_one(selector, Input)
                    self.assertTrue(field.has_focus)
                    field.value = value
                    await pilot.press("enter")
                self.assertTrue(app.screen.query_one("#profile", Select).has_focus)
                await pilot.press("tab", "enter")
                await pilot.pause()
                self.assertEqual(Inventory(database).get("lab")["host"], "lab.local")
                self.assertEqual(app.query_one("#command", Select).value, "open")
                self.assertTrue(app.query_one("#run", Button).has_focus)
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertTrue(app.screen.has_class("output-focus"))
                await pilot.press("escape")
                self.assertEqual(app.query_one("#command", Select).value, "preflight")
                self.assertIn("Step 1/8", str(app.query_one("#checkpoint", Static).content))
                self.assertFalse(app.query_one("#options", Input).display)
                self.assertTrue(app.query_one("#start-ssh", Button).display)
                self.assertFalse(app.query_one("#command", Select).display)

    async def test_komodo_preview_prefills_the_version_and_nas_guide_stays_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            inventory.add("lab", "lab.local", "operator", 22, PROFILE)
            inventory.add("nas", "nas.local", "operator", 22, NAS_PROFILE)
            runs = RunStore(database)
            for host in ("lab", "nas"):
                runs.complete_step(runs.open(host)["id"], "ssh")
            app = OnboardrrApp(database)

            async def preview(job, argv):
                app.append_output(job, '{"core_version":"2.3.4","action":"create"}')
                return 0

            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press("enter")
                self.assertEqual(app.query_one("#command", Select).value, "register")
                with patch.object(app, "run_captured", side_effect=preview):
                    await pilot.click("#run")
                    await app.workers.wait_for_complete()
                await pilot.press("escape")
                self.assertEqual(app.query_one("#version", Input).value, "2.3.4")
                with patch.object(app, "launch") as launch:
                    await pilot.click("#apply")
                    await pilot.click("#confirm")
                    launch.assert_called_once_with(
                        ["komodo", "register", "lab", "--apply", "--expected-version", "2.3.4"], False,
                    )
                app.query_one("#hosts", DataTable).move_cursor(row=1)
                await pilot.pause()
                self.assertEqual(app.query_one("#command", Select).value, "prepare")
                self.assertIn("Step 2/9", str(app.query_one("#checkpoint", Static).content))
                self.assertFalse(app.query_one("#version", Input).display)
                await pilot.click("#section-maintenance")
                await pilot.pause()
                self.assertEqual(app.query_one("#command", Select).value, "wud")
                await pilot.click("#section-guide")
                await pilot.pause()
                self.assertEqual(app.query_one("#command", Select).value, "prepare")

    async def test_linux_prepare_has_a_working_default_and_validates_mount_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            app = OnboardrrApp(database)
            async with app.run_test(size=(60, 24)) as pilot:
                await pilot.press("enter")
                self.assertTrue(app.query_one("#run", Button).has_focus)
                await pilot.click("#other-steps", offset=(2, 1))
                app.query_one("#command", Select).value = "prepare"
                await pilot.pause()
                with patch.object(app, "launch") as launch:
                    await pilot.click("#run")
                    launch.assert_called_once_with(["prepare", "lab", "--component", "system"], False)
                    launch.reset_mock()
                    app.query_one("#command", Select).value = "goku"
                    app.query_one("#nfs-source", Input).value = ""
                    await pilot.pause()
                    await pilot.click("#run")
                    self.assertIn("SMB and NFS", str(app.query_one("#form-error", Static).content))
                    launch.assert_not_called()
                    app.query_one("#cifs-source", Input).value = "//goku/compose"
                    app.query_one("#nfs-source", Input).value = "goku:/export"
                    with patch.object(app, "selected_public_key", return_value="/fixture.pub"):
                        await pilot.click("#run")
                    launch.assert_called_once_with([
                        "auto-install", "lab", "--select", "goku-files", "--select", "goku-mounts",
                        "--public-key", "/fixture.pub", "--cifs-source", "//goku/compose", "--nfs-source", "goku:/export",
                    ], False)

    async def test_periphery_paths_follow_the_device_and_skip_controller_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            inventory = Inventory(database)
            runs = RunStore(database)
            for name in ("fly", "lab"):
                inventory.add(name, None, "operator", 22, PROFILE)
                run = runs.open(name)
                runs.complete_step(run["id"], "ssh")
                runs.complete_step(run["id"], "komodo-registration")
            app = OnboardrrApp(database)
            settings = {"ONBOARDRR_GOKU_HOST": "nas.local"}
            with patch.dict(os.environ, settings):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("enter")
                    self.assertEqual(app.recommended, "goku")
                    self.assertEqual(app.query_one("#nfs-source", Input).value, "nas.local:/onboardrr-fly")
                    self.assertIn("~/.docker/compose/fly", str(app.query_one("#periphery-paths", Static).content))
                    self.assertIn("/share/docker/komodo/data/fly", str(app.query_one("#periphery-paths", Static).content))
                    await pilot.click("#other-steps", offset=(2, 1))
                    app.query_one("#command", Select).value = "goku"
                    await pilot.pause()
                    with (patch.object(app, "launch") as launch,
                          patch.object(app, "selected_public_key", return_value="/fixture.pub")):
                        await pilot.click("#run", offset=(2, 1))
                        launch.assert_called_once_with([
                            "auto-install", "fly", "--select", "goku-files", "--select", "goku-mounts",
                            "--public-key", "/fixture.pub", "--cifs-source", "//nas.local/compose",
                            "--nfs-source", "nas.local:/onboardrr-fly",
                        ], False)
                    app.query_one("#hosts", DataTable).move_cursor(row=1)
                    await pilot.pause()
                    self.assertIn("~/.docker/compose/lab", str(app.query_one("#periphery-paths", Static).content))
                    self.assertEqual(app.query_one("#nfs-source", Input).value, "nas.local:/onboardrr-lab")

    async def test_failed_linux_check_suggests_preparation_and_prefills_mount_server(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            Inventory(database).add("lab", None, "operator", 22, PROFILE)
            runs = RunStore(database)
            run = runs.open("lab")["id"]
            for step in ("ssh", "komodo-registration", "goku-files"):
                runs.complete_step(run, step)
            runs.mark_failed(run, "linux")
            app = OnboardrrApp(database)
            with patch.dict(os.environ, {"ONBOARDRR_GOKU_MOUNT_HOST": "192.0.2.10"}):
                async with app.run_test(size=(100, 32)):
                    self.assertEqual(app.recommended, "goku")
                    self.assertEqual(app.query_one("#cifs-source", Input).value, "//192.0.2.10/compose")
                    self.assertEqual(app.query_one("#nfs-source", Input).value, "192.0.2.10:/onboardrr-lab")
