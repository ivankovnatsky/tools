import contextlib
import io
import json
import unittest

from services_fakes import Harness

URL = "http://svc.example.com"
LOCAL = "-".join(["AAAAAAA"] * 8)
BETA = "-".join(["BBBBBBB"] * 8)
OLD = "-".join(["CCCCCCC"] * 8)
STRANGER = "-".join(["DDDDDDD"] * 8)
SHARED_N = {
    "id": "n",
    "label": "n",
    "path": "/n",
    "devices": [{"deviceID": LOCAL}, {"deviceID": BETA}],
}
FILES = {
    "/s/key": "st-key",
    "/s/devices": json.dumps({"alpha": LOCAL, "beta": BETA}),
    "/s/gui-user": "admin",
    "/s/gui-pass": "plain-secret",
    "/st/config.xml": "<configuration><gui><apikey>xml-key</apikey></gui></configuration>",
}


def cfg(**extra):
    return {"syncthing": {"baseUrl": URL, "apiKeyFile": "/s/key", **extra}}


class SyncthingTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(files=FILES)
        self.h.server.on("GET", "/rest/system/status", {"myID": LOCAL})
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()

    def tearDown(self):
        self.out.__exit__(None, None, None)

    def devices(self, items):
        self.h.server.on("GET", "/rest/config/devices", items)

    def test_api_key_from_config_xml(self):
        self.h.run({"syncthing": {"baseUrl": URL, "configDir": "/st"}})
        self.assertEqual(self.h.server.calls[0][4]["X-api-key"], "xml-key")

    def test_devices_authoritative_but_local_device_kept(self):
        self.devices(
            [
                {"deviceID": LOCAL, "name": "alpha"},
                {"deviceID": OLD, "name": "old"},
                {"deviceID": STRANGER, "name": "x"},
            ]
        )
        for m, p in [
            ("PUT", f"/rest/config/devices/{BETA}"),
            ("DELETE", f"/rest/config/devices/{OLD}"),
            ("DELETE", f"/rest/config/devices/{STRANGER}"),
        ]:
            self.h.server.on(m, p, None)
        # Registry has "alpha" too but it is not requested: still never removed.
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertTrue(ok)
        self.assertNotIn(("DELETE", f"/rest/config/devices/{LOCAL}"), self.h.server.writes())
        self.assertIn(("PUT", f"/rest/config/devices/{BETA}"), self.h.server.writes())
        self.assertIn(("DELETE", f"/rest/config/devices/{OLD}"), self.h.server.writes())

    def test_devices_untouched_without_registry(self):
        self.devices([{"deviceID": OLD, "name": "old"}])
        self.h.run(cfg(folders={}))
        self.assertFalse(any(m == "DELETE" and "devices" in p for m, p in self.h.server.writes()))

    def test_unknown_device_name_fails_before_any_write(self):
        self.devices([{"deviceID": OLD, "name": "old"}])
        rows, ok, _ = self.h.run(cfg(devices=["typo"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_folder_added_with_resolved_devices_and_ignores(self):
        self.devices([])
        self.h.server.on("GET", "/rest/config/folders", [{"id": "gone", "label": "Gone"}])
        for m, p in [
            ("PUT", "/rest/config/folders/notes"),
            ("POST", "/rest/db/ignores"),
            ("DELETE", "/rest/config/folders/gone"),
            ("PUT", f"/rest/config/devices/{BETA}"),
            ("PUT", f"/rest/config/devices/{LOCAL}"),
        ]:
            self.h.server.on(m, p, None)
        folders = {
            "notes": {"path": "/n", "devices": ["alpha", "beta"], "ignorePatterns": [".git"]}
        }
        rows, ok, _ = self.h.run(
            cfg(devices=[], deviceDefinitionsFile="/s/devices", folders=folders)
        )
        self.assertTrue(ok, rows)
        (put,) = self.h.server.made("PUT", "/rest/config/folders/notes")
        self.assertEqual({d["deviceID"] for d in put[3]["devices"]}, {LOCAL, BETA})
        (ign,) = self.h.server.made("POST", "/rest/db/ignores")
        self.assertEqual(ign[2], {"folder": ["notes"]})
        self.assertIn(("DELETE", "/rest/config/folders/gone"), self.h.server.writes())

    def test_plaintext_gui_password_converges(self):
        gui = {"user": "admin", "password": "$2a$10$remotehash"}
        self.h.server.on("GET", "/rest/config/gui", gui)
        self.h.server.on("PATCH", "/rest/config/gui", None)
        config = cfg(gui={"usernameFile": "/s/gui-user", "passwordFile": "/s/gui-pass"})
        rows, ok, state = self.h.run(config)
        self.assertTrue(ok)
        self.assertEqual(len(self.h.server.made("PATCH", "/rest/config/gui")), 1)
        self.assertNotIn("plain-secret", json.dumps(state))
        rows, ok, _ = self.h.run(config, state=state)
        self.assertEqual(rows, [])
        self.assertEqual(len(self.h.server.made("PATCH", "/rest/config/gui")), 1)

        gui["password"] = "$2a$10$changedbyhand"
        rows, _, _ = self.h.run(config, state=state)
        self.assertIn("  ~ gui credentials (password)", rows)

    def test_restart_only_after_changes(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}])
        self.h.server.on("POST", "/rest/system/restart", None)
        self.h.run(cfg(localDeviceName="alpha", restart=True))
        self.assertEqual(self.h.server.made("POST", "/rest/system/restart"), [])

    def test_unknown_folder_device_name_aborts_before_writes(self):
        self.devices([{"deviceID": BETA, "name": "beta"}])
        self.h.server.on("GET", "/rest/config/folders", [])
        folders = {"n": {"path": "/n", "devices": ["beta", "btea"]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_folder_name_without_registry_aborts(self):
        self.h.server.on("GET", "/rest/config/folders", [])
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "devices": ["beta"]}}))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_raw_device_id_in_folder_is_not_removed(self):
        raw = "-".join(["ABCDEFG"] * 8)
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": raw, "name": "r"}])
        self.h.server.on("GET", "/rest/config/folders", [])
        self.h.server.on("PUT", "/rest/config/folders/n", None)
        folders = {"n": {"path": "/n", "devices": ["alpha", raw]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertTrue(ok, rows)
        self.assertNotIn(("DELETE", f"/rest/config/devices/{raw}"), self.h.server.writes())

    def test_missing_local_id_fails_before_any_delete(self):
        self.h.server.on("GET", "/rest/system/status", {})
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_implicit_local_device_on_folder_converges(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": BETA, "name": "beta"}])
        remote = {
            "id": "n",
            "label": "n",
            "path": "/n",
            "devices": [{"deviceID": LOCAL}, {"deviceID": BETA}],
        }
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("GET", "/rest/db/ignores", {"ignore": []})
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_registry_alone_never_manages_devices(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices"))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_empty_devices_refuses_to_remove_every_peer(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        rows, ok, _ = self.h.run(cfg(devices=[], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])
        self.assertTrue(any("prune: true" in r for r in rows))

    def test_empty_devices_with_prune_removes_peers(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        self.h.server.on("DELETE", f"/rest/config/devices/{OLD}", None)
        rows, ok, _ = self.h.run(cfg(devices=[], deviceDefinitionsFile="/s/devices", prune=True))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [("DELETE", f"/rest/config/devices/{OLD}")])

    def test_bad_folder_read_aborts_before_writes(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}])
        self.h.server.on("GET", "/rest/config/folders", None)
        rows, ok, _ = self.h.run(cfg(localDeviceName="renamed", folders={"n": {"path": "/n"}}))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_bad_device_entry_aborts(self):
        self.devices([{"deviceID": LOCAL}, "junk"])
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_absent_ignore_patterns_and_label_are_unmanaged(self):
        remote = {"id": "n", "label": "Custom", "path": "/n", "devices": [{"deviceID": LOCAL}]}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n"}}))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])
        self.assertEqual(self.h.server.made("GET", "/rest/db/ignores"), [])

    def test_folder_registry_devices_not_added_without_devices_key(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        self.h.server.on("GET", "/rest/config/folders", [])
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertFalse(ok)
        self.assertTrue(any("not configured" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_folder_named_devices_are_managed_with_devices_key(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        self.h.server.on("GET", "/rest/config/folders", [])
        self.h.server.on("PUT", f"/rest/config/devices/{BETA}", None)
        self.h.server.on("DELETE", f"/rest/config/devices/{OLD}", None)
        self.h.server.on("PUT", "/rest/config/folders/n", None)
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(
            cfg(devices=[], deviceDefinitionsFile="/s/devices", folders=folders)
        )
        self.assertTrue(ok, rows)
        self.assertEqual(
            self.h.server.writes()[:2],
            [("PUT", f"/rest/config/devices/{BETA}"), ("DELETE", f"/rest/config/devices/{OLD}")],
        )

    def test_local_device_renamed_from_registry_without_local_name(self):
        self.devices([{"deviceID": LOCAL, "name": "old-name"}])
        self.h.server.on("PATCH", f"/rest/config/devices/{LOCAL}", None)
        rows, ok, _ = self.h.run(cfg(devices=["alpha"], deviceDefinitionsFile="/s/devices"))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [("PATCH", f"/rest/config/devices/{LOCAL}")])
        self.assertEqual(
            self.h.server.made("PATCH", f"/rest/config/devices/{LOCAL}")[0][3], {"name": "alpha"}
        )

    def test_raw_id_devices_not_in_devices_are_removed(self):
        raw = "-".join(["EEEEEEE"] * 8)
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": raw, "name": "r"}])
        self.h.server.on("PUT", f"/rest/config/devices/{BETA}", None)
        self.h.server.on("DELETE", f"/rest/config/devices/{raw}", None)
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertTrue(ok, rows)
        self.assertIn(("DELETE", f"/rest/config/devices/{raw}"), self.h.server.writes())

    def test_failed_folder_add_skips_its_ignores(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}])
        self.h.server.on("GET", "/rest/config/folders", [])
        self.h.server.on("PUT", "/rest/config/folders/n", {"error": "x"}, status=500)
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "ignorePatterns": [".git"]}}))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("POST", "/rest/db/ignores"), [])

    def test_failed_device_add_skips_dependent_folder(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}])
        self.h.server.on("GET", "/rest/config/folders", [])
        self.h.server.on("PUT", f"/rest/config/devices/{BETA}", {"error": "x"}, status=500)
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("PUT", "/rest/config/folders/n"), [])

    def test_local_device_name_wins_over_devices_alias(self):
        registry = json.dumps({"self-alias": LOCAL, "beta": BETA})
        self.h.files["/s/devices"] = registry
        self.devices([{"deviceID": LOCAL, "name": "old"}, {"deviceID": BETA, "name": "beta"}])
        self.h.server.on("PATCH", f"/rest/config/devices/{LOCAL}", None)
        config = cfg(
            localDeviceName="alpha",
            devices=["self-alias", "beta"],
            deviceDefinitionsFile="/s/devices",
        )
        rows, ok, _ = self.h.run(config)
        self.assertTrue(ok, rows)
        patches = self.h.server.made("PATCH", f"/rest/config/devices/{LOCAL}")
        self.assertEqual([p[3] for p in patches], [{"name": "alpha"}])

    def test_null_folder_entry_is_a_config_error(self):
        rows, ok, _ = self.h.run(cfg(folders={"n": None}), apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("GET", "/rest/config/folders"), [])

    def test_folder_patch_keeps_per_device_settings(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": BETA, "name": "beta"}])
        remote = {
            "id": "n",
            "label": "old",
            "path": "/n/",
            "devices": [
                {"deviceID": LOCAL},
                {"deviceID": BETA, "encryptionPassword": "keep-me", "introducedBy": ""},
                {"deviceID": OLD},
            ],
        }
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("PATCH", "/rest/config/folders/n", None)
        folders = {"n": {"path": "/n", "label": "new", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/rest/config/folders/n")
        sent = {d["deviceID"]: d for d in patch[3]["devices"]}
        self.assertEqual(set(sent), {LOCAL, BETA})
        self.assertEqual(sent[BETA]["encryptionPassword"], "keep-me")
        self.assertIn("  ~ folder n (label, devices)", rows)

    def test_label_drift_does_not_send_devices(self):
        remote = {"id": "n", "label": "old", "path": "/n", "devices": [{"deviceID": LOCAL}]}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("PATCH", "/rest/config/folders/n", None)
        self.h.run(cfg(folders={"n": {"path": "/n", "label": "new", "devices": []}}))
        (patch,) = self.h.server.made("PATCH", "/rest/config/folders/n")
        self.assertNotIn("devices", patch[3])

    def test_duplicate_device_names_rejected(self):
        rows, ok, _ = self.h.run(
            cfg(devices=["beta", "beta"], deviceDefinitionsFile="/s/devices"), apply=False
        )
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_null_folders_is_unmanaged(self):
        rows, ok, _ = self.h.run(cfg(folders=None))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("GET", "/rest/config/folders"), [])

    def test_empty_folders_refuses_to_delete_everything(self):
        self.h.server.on("GET", "/rest/config/folders", [{"id": "n", "label": "N"}])
        rows, ok, _ = self.h.run(cfg(folders={}))
        self.assertFalse(ok)
        self.assertTrue(any("prune: true" in r for r in rows))
        self.assertEqual(self.h.server.writes(), [])

    def test_empty_folders_file_refuses_too(self):
        self.h.files["/s/folders.json"] = "{}"
        self.h.server.on("GET", "/rest/config/folders", [{"id": "n", "label": "N"}])
        rows, ok, _ = self.h.run(cfg(foldersFile="/s/folders.json"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_folders_and_folders_file_are_exclusive(self):
        rows, ok, _ = self.h.run(
            cfg(folders={"n": {"path": "/n"}}, foldersFile="/s/folders.json"), apply=False
        )
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_apikey_and_config_dir_are_exclusive(self):
        rows, ok, _ = self.h.run(cfg(configDir="/st"), apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_registry_id_in_any_spelling_matches_remote(self):
        loose = BETA.replace("-", "").lower()
        self.h.files["/s/devices"] = json.dumps({"alpha": LOCAL, "beta": loose})
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": BETA, "name": "beta"}])
        self.h.server.on("GET", "/rest/config/folders", [SHARED_N])
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(
            cfg(devices=["beta"], deviceDefinitionsFile="/s/devices", folders=folders)
        )
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_registry_duplicate_ids_are_an_error_before_writes(self):
        self.h.files["/s/devices"] = json.dumps(
            {"alpha": LOCAL, "beta": BETA, "beta2": BETA.lower()}
        )
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        rows, ok, _ = self.h.run(
            cfg(devices=["beta"], deviceDefinitionsFile="/s/devices", localDeviceName="x")
        )
        self.assertFalse(ok)
        self.assertTrue(any("duplicate" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_registry_duplicate_names_are_an_error(self):
        self.h.files["/s/devices"] = f'{{"beta": "{BETA}", "beta": "{OLD}"}}'
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_registry_invalid_id_is_an_error(self):
        self.h.files["/s/devices"] = json.dumps({"beta": "not-an-id"})
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_remote_duplicate_device_ids_abort(self):
        self.devices(
            [
                {"deviceID": LOCAL, "name": "alpha"},
                {"deviceID": OLD, "name": "a"},
                {"deviceID": OLD.lower(), "name": "b"},
            ]
        )
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_remote_duplicate_folder_ids_abort(self):
        self.h.server.on(
            "GET", "/rest/config/folders", [{"id": "n", "path": "/n"}, {"id": "n", "path": "/m"}]
        )
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "label": "x"}}))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_int_and_str_folder_ids_collide(self):
        rows, ok, _ = self.h.run(cfg(folders={1: {"path": "/a"}, "1": {"path": "/b"}}), apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls[1:], [])

    def test_raw_folder_device_id_is_canonicalized(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": BETA, "name": "beta"}])
        self.h.server.on("GET", "/rest/config/folders", [SHARED_N])
        folders = {"n": {"path": "/n", "devices": [BETA.lower()]}}
        rows, ok, _ = self.h.run(
            cfg(devices=["beta"], deviceDefinitionsFile="/s/devices", folders=folders)
        )
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_same_device_twice_in_folder_rejected(self):
        self.h.server.on("GET", "/rest/config/folders", [])
        folders = {"n": {"path": "/n", "devices": ["beta", BETA.lower()]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_failed_device_add_keeps_stale_devices(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": OLD, "name": "old"}])
        self.h.server.on("PUT", f"/rest/config/devices/{BETA}", {"error": "x"}, status=500)
        rows, ok, _ = self.h.run(cfg(devices=["beta"], deviceDefinitionsFile="/s/devices"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", f"/rest/config/devices/{OLD}"), [])

    def test_failed_folder_add_keeps_stale_folders(self):
        self.h.server.on("GET", "/rest/config/folders", [{"id": "old", "path": "/o"}])
        self.h.server.on("PUT", "/rest/config/folders/n", {"error": "x"}, status=500)
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n"}}))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", "/rest/config/folders/old"), [])

    def test_null_folder_devices_are_empty(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": BETA, "name": "beta"}])
        remote = {"id": "n", "label": "n", "path": "/n", "devices": None}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("PATCH", "/rest/config/folders/n", None)
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(cfg(deviceDefinitionsFile="/s/devices", folders=folders))
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/rest/config/folders/n")
        self.assertEqual({d["deviceID"] for d in patch[3]["devices"]}, {LOCAL, BETA})

    def test_unconfigured_raw_folder_device_holds_folder(self):
        stray = "-".join(["EEEEEEE"] * 8)
        self.devices([{"deviceID": LOCAL, "name": "alpha"}])
        remote = {"id": "n", "label": "n", "path": "/n", "devices": [{"deviceID": LOCAL}]}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("PATCH", "/rest/config/folders/n", None)
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "devices": [stray]}}))
        self.assertFalse(ok)
        self.assertTrue(any("not configured" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_raw_folder_device_already_on_server_is_kept(self):
        known = "-".join(["EEEEEEE"] * 8)
        self.devices([{"deviceID": LOCAL, "name": "alpha"}, {"deviceID": known, "name": "k"}])
        remote = {"id": "n", "label": "n", "path": "/n", "devices": [{"deviceID": LOCAL}]}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("PATCH", "/rest/config/folders/n", None)
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "devices": [known]}}))
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/rest/config/folders/n")
        self.assertEqual({d["deviceID"] for d in patch[3]["devices"]}, {LOCAL, known})

    def test_empty_label_is_kept_and_converges(self):
        remote = {"id": "n", "label": "", "path": "/n", "devices": [{"deviceID": LOCAL}]}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "label": ""}}))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_label_can_be_cleared(self):
        remote = {"id": "n", "label": "Old", "path": "/n", "devices": [{"deviceID": LOCAL}]}
        self.h.server.on("GET", "/rest/config/folders", [remote])
        self.h.server.on("PATCH", "/rest/config/folders/n", None)
        rows, ok, _ = self.h.run(cfg(folders={"n": {"path": "/n", "label": ""}}))
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/rest/config/folders/n")
        self.assertEqual(patch[3]["label"], "")

    def test_bcrypt_hash_requires_full_pattern(self):
        real = "$2a$10$" + "a" * 53
        self.h.server.on("GET", "/rest/config/gui", {"user": "admin", "password": real})
        self.h.server.on("PATCH", "/rest/config/gui", None)
        config = cfg(gui={"usernameFile": "/s/gui-user", "passwordFile": "/s/gui-pass"})
        self.h.files["/s/gui-pass"] = real
        rows, ok, _ = self.h.run(config)
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("PATCH", "/rest/config/gui"), [])

        self.h.files["/s/gui-pass"] = "$2-not-a-hash"
        rows, ok, state = self.h.run(config)
        self.assertTrue(ok, rows)
        (patch,) = self.h.server.made("PATCH", "/rest/config/gui")
        self.assertEqual(patch[3]["password"], "$2-not-a-hash")
        self.assertTrue(state.get("syncthing", {}).get("fingerprints"))

    def test_unreadable_gui_password_aborts_before_local_rename(self):
        self.devices([{"deviceID": LOCAL, "name": "alpha"}])
        self.h.server.on("GET", "/rest/config/gui", {"user": "admin", "password": ""})
        self.h.server.on("PATCH", f"/rest/config/devices/{LOCAL}", None)
        config = cfg(
            localDeviceName="renamed",
            gui={"usernameFile": "/s/gui-user", "passwordFile": "/s/missing"},
        )
        rows, ok, _ = self.h.run(config)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_folder_device_changes_are_listed(self):
        remote = {
            "id": "n",
            "label": "n",
            "path": "/n",
            "devices": [{"deviceID": LOCAL}, {"deviceID": OLD}],
        }
        self.devices([{"deviceID": LOCAL}, {"deviceID": BETA}, {"deviceID": OLD}])
        self.h.server.on("GET", "/rest/config/folders", [remote])
        folders = {"n": {"path": "/n", "devices": ["beta"]}}
        rows, ok, _ = self.h.run(
            cfg(deviceDefinitionsFile="/s/devices", folders=folders), apply=False
        )
        self.assertTrue(ok, rows)
        self.assertIn("  ~ folder n (devices)", rows)
        self.assertIn("  + folder n device BBBBBBB...", rows)
        self.assertIn("  - folder n device CCCCCCC...", rows)


if __name__ == "__main__":
    unittest.main()
