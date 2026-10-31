import contextlib
import io
import unittest

from services_fakes import Harness

from tools.services import arr

URL = "http://svc.example.com"
FILES = {
    "/s/radarr": "radarr-key",
    "/s/prowlarr": "prowlarr-key",
    "/s/tr-user": "truser",
    "/s/tr-pass": "trpass-secret",
    "/s/idx-pass": "idxpass-secret",
}

DC = {
    "name": "Transmission",
    "host": "127.0.0.1",
    "usernameFile": "/s/tr-user",
    "passwordFile": "/s/tr-pass",
    "category": "radarr",
}


def radarr(**extra):
    return {"arr": {"radarr": {"baseUrl": URL, "apiKeyFile": "/s/radarr", **extra}}}


def prowlarr(**extra):
    return {"arr": {"prowlarr": {"baseUrl": URL, "apiKeyFile": "/s/prowlarr", **extra}}}


def current_dc(password="********"):
    fields = arr._transmission_fields(
        arr._parse_download_client(DC, "t", None), "truser", password, "movieCategory"
    )
    return {
        "id": 4,
        "name": "Transmission",
        "fields": fields,
        "enable": True,
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
    }


class ArrTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(files=FILES)
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()

    def tearDown(self):
        self.out.__exit__(None, None, None)

    def test_root_folders_listed_are_authoritative(self):
        self.h.server.on("GET", "/api/v3/rootfolder", [{"id": 1, "path": "/old"}])
        self.h.server.on("DELETE", "/api/v3/rootfolder/1", None)
        self.h.server.on("POST", "/api/v3/rootfolder", {})
        rows, ok, _ = self.h.run(radarr(rootFolders=["/media/movies"]))
        self.assertTrue(ok)
        self.assertEqual(
            self.h.server.writes(),
            [("POST", "/api/v3/rootfolder"), ("DELETE", "/api/v3/rootfolder/1")],
        )

    def test_failed_root_folder_create_keeps_old_one(self):
        self.h.server.on("GET", "/api/v3/rootfolder", [{"id": 1, "path": "/old"}])
        self.h.server.on("POST", "/api/v3/rootfolder", {"message": "x"}, status=500)
        self.h.server.on("DELETE", "/api/v3/rootfolder/1", None)
        rows, ok, _ = self.h.run(radarr(rootFolders=["/media/movies"]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", "/api/v3/rootfolder/1"), [])

    def test_root_folder_trailing_slash_is_not_drift(self):
        self.h.server.on("GET", "/api/v3/rootfolder", [{"id": 1, "path": "/media/movies/"}])
        rows, ok, _ = self.h.run(radarr(rootFolders=["/media/movies"]))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_duplicate_identities_rejected(self):
        for config in (
            radarr(rootFolders=["/m", "/m/"]),
            radarr(downloadClients=[DC, DC]),
            prowlarr(indexers=[{"name": "A", "definitionName": "a"}] * 2),
        ):
            rows, ok, _ = self.h.run(config, apply=False)
            self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_duplicate_remote_names_abort(self):
        self.h.server.on(
            "GET", "/api/v3/downloadclient", [{"id": 1, "name": "T"}, {"id": 2, "name": "T"}]
        )
        rows, ok, _ = self.h.run(radarr(downloadClients=[{**DC, "name": "T"}]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_absent_root_folders_key_leaves_them_alone(self):
        self.h.run(radarr())
        self.assertEqual(self.h.server.calls, [])

    def test_masked_password_converges_via_fingerprint(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [current_dc()])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        rows, ok, state = self.h.run(radarr(downloadClients=[DC]))
        self.assertTrue(ok)
        self.assertIn("  ~ radarr download client Transmission (password)", rows)
        (put,) = self.h.server.made("PUT", "/api/v3/downloadclient/4")
        sent = {f["name"]: f["value"] for f in put[3]["fields"]}
        self.assertEqual(sent["password"], "trpass-secret")
        self.assertNotIn("trpass-secret", str(state))
        self.assertFalse(any("trpass-secret" in r for r in rows))

        rows, ok, _ = self.h.run(radarr(downloadClients=[DC]), state=state)
        self.assertEqual(rows, [])
        self.assertEqual(len(self.h.server.made("PUT", "/api/v3/downloadclient/4")), 1)

    def test_rotated_password_is_pushed(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [current_dc()])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        _, _, state = self.h.run(radarr(downloadClients=[DC]))
        self.h.files["/s/tr-pass"] = "rotated"
        rows, _, _ = self.h.run(radarr(downloadClients=[DC]), state=state)
        self.assertIn("  ~ radarr download client Transmission (password)", rows)

    def test_failed_update_does_not_record_fingerprint(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [current_dc()])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {"message": "x"}, status=500)
        _, ok, state = self.h.run(radarr(downloadClients=[DC]))
        self.assertFalse(ok)
        self.assertNotIn("fingerprints", state.get("arr", {}))

    def test_download_client_update_keeps_unmanaged_fields(self):
        cur = current_dc()
        cur["fields"].append({"name": "tvDirectory", "value": "/downloads/tv"})
        cur["tags"] = [3]
        self.h.server.on("GET", "/api/v3/downloadclient", [cur])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        self.h.run(radarr(downloadClients=[DC]))
        (put,) = self.h.server.made("PUT", "/api/v3/downloadclient/4")
        sent = {f["name"]: f["value"] for f in put[3]["fields"]}
        self.assertEqual(sent["tvDirectory"], "/downloads/tv")
        self.assertEqual(sent["password"], "trpass-secret")
        self.assertEqual(put[3]["tags"], [3])

    def test_omitted_category_defaults_to_empty(self):
        dc = {k: v for k, v in DC.items() if k != "category"}
        self.h.server.on("GET", "/api/v3/downloadclient", [current_dc()])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        rows, ok, _ = self.h.run(radarr(downloadClients=[dc]))
        self.assertTrue(ok, rows)
        (put,) = self.h.server.made("PUT", "/api/v3/downloadclient/4")
        sent = {f["name"]: f["value"] for f in put[3]["fields"]}
        self.assertEqual(sent["movieCategory"], "")

    def test_null_lists_are_unmanaged(self):
        self.h.run(
            {"arr": {"radarr": {"baseUrl": URL, "apiKeyFile": "/s/radarr", "rootFolders": None}}}
        )
        self.h.run(prowlarr(indexers=None, applications=None))
        self.assertEqual(self.h.server.calls, [])

    def test_empty_lists_refuse_to_delete_everything(self):
        self.h.server.on("GET", "/api/v3/rootfolder", [{"id": 1, "path": "/m"}])
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "X"}])
        self.h.server.on("GET", "/api/v1/applications", [{"id": 2, "name": "Radarr"}])
        for config in (
            radarr(rootFolders=[]),
            prowlarr(indexers=[]),
            prowlarr(applications=[]),
        ):
            rows, ok, _ = self.h.run(config)
            self.assertFalse(ok)
            self.assertTrue(any("prune: true" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_prune_allows_deleting_everything(self):
        self.h.server.on("GET", "/api/v3/rootfolder", [{"id": 1, "path": "/m"}])
        self.h.server.on("DELETE", "/api/v3/rootfolder/1", None)
        rows, ok, _ = self.h.run(radarr(rootFolders=[], prune=True))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [("DELETE", "/api/v3/rootfolder/1")])

    def test_download_client_properties_are_set_only_on_create(self):
        cur = {**current_dc(), "enable": False, "priority": 5}
        self.h.server.on("GET", "/api/v3/downloadclient", [cur])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        _, _, state = self.h.run(radarr(downloadClients=[DC]))
        (put,) = self.h.server.made("PUT", "/api/v3/downloadclient/4")
        self.assertEqual((put[3]["enable"], put[3]["priority"]), (False, 5))
        rows, ok, _ = self.h.run(radarr(downloadClients=[DC]), state=state)
        self.assertEqual(rows, [])

    def test_application_update_manages_categories_not_enable(self):
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/radarr"}
        fields = [
            {"name": "prowlarrUrl", "value": "http://localhost:9696"},
            {"name": "baseUrl", "value": "http://127.0.0.1:7878"},
            {"name": "apiKey", "value": "********"},
            {"name": "syncCategories", "value": [2000]},
            {"name": "apiPath", "value": "/api"},
        ]
        cur = {
            "id": 2,
            "name": "Radarr",
            "enable": False,
            "syncLevel": "fullSync",
            "fields": fields,
        }
        self.h.server.on("GET", "/api/v1/applications", [cur])
        self.h.server.on("PUT", "/api/v1/applications/2", {})
        rows, ok, _ = self.h.run(prowlarr(applications=[app]))
        self.assertTrue(ok, rows)
        self.assertFalse(any("enable" in r for r in rows), rows)
        (put,) = self.h.server.made("PUT", "/api/v1/applications/2")
        sent = {f["name"]: f["value"] for f in put[3]["fields"]}
        self.assertEqual(sent["syncCategories"], [])
        self.assertEqual(sent["apiPath"], "/api")
        self.assertFalse(put[3]["enable"])

    def test_non_list_read_aborts_before_writes(self):
        self.h.server.on("GET", "/api/v3/rootfolder", {"oops": True})
        rows, ok, _ = self.h.run(radarr(rootFolders=["/media/movies"]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])
        self.assertTrue(any("expected a JSON list" in r for r in rows))

    def test_empty_body_is_not_an_empty_list(self):
        self.h.server.on("GET", "/api/v3/rootfolder", None)
        rows, ok, _ = self.h.run(radarr(rootFolders=["/media/movies"]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_download_client_created_when_missing(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [])
        self.h.server.on("POST", "/api/v3/downloadclient", {})
        rows, ok, _ = self.h.run(radarr(downloadClients=[DC]))
        self.assertTrue(ok)
        (post,) = self.h.server.made("POST", "/api/v3/downloadclient")
        self.assertEqual(post[3]["implementation"], "Transmission")

    def test_host_config_put_preserves_fields(self):
        self.h.server.on("GET", "/api/v3/config/host", {"bindAddress": "10.0.0.1", "port": 7878})
        self.h.server.on("PUT", "/api/v3/config/host", {})
        self.h.run(radarr(bindAddress="*"))
        (put,) = self.h.server.made("PUT", "/api/v3/config/host")
        self.assertEqual(put[3], {"bindAddress": "*", "port": 7878})

    def test_prowlarr_unlisted_indexer_deleted_listed_created(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "Old"}])
        self.h.server.on("DELETE", "/api/v1/indexer/7", None)
        self.h.server.on(
            "GET",
            "/api/v1/indexer/schema",
            [{"definitionName": "eztv", "fields": [{"name": "baseUrl", "value": "x"}]}],
        )
        self.h.server.on("POST", "/api/v1/indexer", {})
        rows, ok, _ = self.h.run(prowlarr(indexers=[{"name": "EZTV", "definitionName": "eztv"}]))
        self.assertTrue(ok)
        (post,) = self.h.server.made("POST", "/api/v1/indexer")
        self.assertEqual(post[3]["name"], "EZTV")
        self.assertEqual(self.h.server.made("DELETE", "/api/v1/indexer/7")[0][0], "DELETE")

    def test_indexer_without_definition_fails_only_that_indexer(self):
        self.h.server.on("GET", "/api/v1/indexer", [])
        rows, ok, _ = self.h.run(prowlarr(indexers=[{"name": "X"}]))
        self.assertFalse(ok)
        self.assertTrue(any("definitionName is required" in r for r in rows))

    def test_indexer_credentials_need_both_fields(self):
        h = Harness(files=FILES)
        rows, ok, _ = h.run(
            prowlarr(indexers=[{"name": "T", "definitionName": "t", "username": "u"}]),
            apply=False,
        )
        self.assertFalse(ok)
        self.assertEqual(h.server.calls, [])

    def test_application_created_like_arr_mgmt(self):
        self.h.server.on("GET", "/api/v1/applications", [])
        self.h.server.on("POST", "/api/v1/applications", {})
        app = {
            "name": "Radarr",
            "baseUrl": "http://127.0.0.1:7878",
            "apiKeyFile": "/s/radarr",
            "syncCategories": [2000],
        }
        rows, ok, _ = self.h.run(prowlarr(applications=[app]))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("GET", "/api/v1/applications/schema"), [])
        (post,) = self.h.server.made("POST", "/api/v1/applications")
        self.assertEqual(post[3]["configContract"], "RadarrSettings")
        sent = {f["name"]: f["value"] for f in post[3]["fields"]}
        self.assertEqual(
            sent,
            {
                "prowlarrUrl": "http://localhost:9696",
                "baseUrl": "http://127.0.0.1:7878",
                "apiKey": "radarr-key",
                "syncCategories": [2000],
            },
        )

    def test_application_created_with_implementation(self):
        self.h.server.on("GET", "/api/v1/applications", [])
        self.h.server.on(
            "GET", "/api/v1/applications/schema", [{"implementation": "Radarr", "fields": []}]
        )
        self.h.server.on("POST", "/api/v1/applications", {})
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/radarr"}
        rows, ok, _ = self.h.run(prowlarr(applications=[app]))
        self.assertTrue(ok)
        (post,) = self.h.server.made("POST", "/api/v1/applications")
        self.assertEqual(post[3]["implementation"], "Radarr")

    def test_one_app_down_other_app_continues(self):
        def down(body, q):
            return 503, None

        self.h.server.on("GET", "/api/v3/rootfolder", down)
        self.h.server.on("GET", "/api/v1/indexer", [])
        config = {
            "arr": {
                "radarr": {
                    "baseUrl": URL,
                    "apiKeyFile": "/s/radarr",
                    "rootFolders": [],
                    "retries": 0,
                },
                "prowlarr": {"baseUrl": URL, "apiKeyFile": "/s/prowlarr", "indexers": []},
            }
        }
        rows, ok, _ = self.h.run(config)
        self.assertFalse(ok)
        self.assertTrue(any(c[1] == "/api/v1/indexer" for c in self.h.server.calls))

    def test_indexer_replacement_created_before_stale_deleted(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "Old"}])
        self.h.server.on("DELETE", "/api/v1/indexer/7", None)
        self.h.server.on("GET", "/api/v1/indexer/schema", [{"definitionName": "eztv"}])
        self.h.server.on("POST", "/api/v1/indexer", {})
        rows, ok, _ = self.h.run(prowlarr(indexers=[{"name": "New", "definitionName": "eztv"}]))
        self.assertTrue(ok, rows)
        self.assertEqual(
            self.h.server.writes(), [("POST", "/api/v1/indexer"), ("DELETE", "/api/v1/indexer/7")]
        )

    def test_failed_indexer_create_keeps_stale(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "Old"}])
        self.h.server.on("GET", "/api/v1/indexer/schema", [{"definitionName": "eztv"}])
        self.h.server.on("POST", "/api/v1/indexer", {"message": "x"}, status=400)
        rows, ok, _ = self.h.run(prowlarr(indexers=[{"name": "New", "definitionName": "eztv"}]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", "/api/v1/indexer/7"), [])
        self.assertTrue(any("kept indexer Old" in r for r in rows), rows)

    def test_application_replacement_created_before_stale_deleted(self):
        self.h.server.on("GET", "/api/v1/applications", [{"id": 3, "name": "OldRadarr"}])
        self.h.server.on("DELETE", "/api/v1/applications/3", None)
        self.h.server.on(
            "GET", "/api/v1/applications/schema", [{"implementation": "Radarr", "fields": []}]
        )
        self.h.server.on("POST", "/api/v1/applications", {})
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/radarr"}
        rows, ok, _ = self.h.run(prowlarr(applications=[app]))
        self.assertTrue(ok, rows)
        self.assertEqual(
            self.h.server.writes(),
            [("POST", "/api/v1/applications"), ("DELETE", "/api/v1/applications/3")],
        )

    def test_failed_application_create_keeps_stale(self):
        self.h.server.on("GET", "/api/v1/applications", [{"id": 3, "name": "OldRadarr"}])
        self.h.server.on("POST", "/api/v1/applications", {"message": "x"}, status=400)
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/radarr"}
        rows, ok, _ = self.h.run(prowlarr(applications=[app]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", "/api/v1/applications/3"), [])

    def test_dry_run_shows_stale_delete_without_failure(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "Old"}])
        rows, ok, _ = self.h.run(
            prowlarr(indexers=[{"name": "New", "definitionName": "eztv"}]), apply=False
        )
        self.assertTrue(ok, rows)
        self.assertIn("  - prowlarr indexer Old", rows)

    def test_remote_int_and_str_names_collide(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 1, "name": 1}, {"id": 2, "name": "1"}])
        rows, ok, _ = self.h.run(prowlarr(indexers=[{"name": "1", "definitionName": "a"}]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_null_fields_are_empty(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [{**current_dc(), "fields": None}])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        rows, ok, _ = self.h.run(radarr(downloadClients=[DC]))
        self.assertTrue(ok, rows)
        (put,) = self.h.server.made("PUT", "/api/v3/downloadclient/4")
        self.assertTrue({f["name"] for f in put[3]["fields"]} >= {"host", "password"})

    def test_non_list_fields_abort_before_writes(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [{**current_dc(), "fields": "x"}])
        rows, ok, _ = self.h.run(radarr(downloadClients=[DC]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_null_application_and_indexer_fields_do_not_crash(self):
        self.h.server.on(
            "GET",
            "/api/v1/applications",
            [{"id": 3, "name": "Radarr", "fields": None, "syncLevel": "x", "enable": True}],
        )
        self.h.server.on("PUT", "/api/v1/applications/3", {})
        self.h.server.on(
            "GET", "/api/v1/indexer", [{"id": 7, "name": "T", "fields": None, "enable": False}]
        )
        self.h.server.on("PUT", "/api/v1/indexer/7", {})
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/radarr"}
        idx = {
            "name": "T",
            "definitionName": "t",
            "username": "u",
            "passwordFile": "/s/idx-pass",
        }
        rows, ok, _ = self.h.run(prowlarr(applications=[app], indexers=[idx]))
        self.assertTrue(ok, rows)
        (put,) = self.h.server.made("PUT", "/api/v1/indexer/7")
        self.assertEqual({f["name"] for f in put[3]["fields"]}, {"username", "password"})

    def test_bad_indexer_fields_abort_before_writes(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "T", "fields": [1]}])
        rows, ok, _ = self.h.run(prowlarr(indexers=[{"name": "T", "enable": False}]))
        self.assertFalse(ok)
        self.assertTrue(any("fields" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_schema_read_failure_fails_item_and_continues(self):
        self.h.server.on(
            "GET",
            "/api/v1/indexer",
            [{"id": 7, "name": "Old"}, {"id": 8, "name": "Kept", "enable": False, "priority": 25}],
        )
        self.h.server.on("GET", "/api/v1/indexer/schema", {"message": "x"}, status=500)
        self.h.server.on("PUT", "/api/v1/indexer/8", {})
        indexers = [
            {"name": "A", "definitionName": "a"},
            {"name": "B", "definitionName": "b"},
            {"name": "Kept"},
        ]
        rows, ok, _ = self.h.run(prowlarr(indexers=indexers))
        self.assertFalse(ok)
        self.assertTrue(any("indexer A: schema unavailable" in r for r in rows), rows)
        self.assertTrue(any("indexer B: schema unavailable" in r for r in rows), rows)
        self.assertEqual(len(self.h.server.made("PUT", "/api/v1/indexer/8")), 1)
        self.assertEqual(self.h.server.made("DELETE", "/api/v1/indexer/7"), [])

    def test_bad_applications_read_aborts_before_indexer_writes(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "Old"}])
        self.h.server.on("GET", "/api/v1/indexer/schema", [{"definitionName": "eztv"}])
        self.h.server.on("POST", "/api/v1/indexer", {})
        self.h.server.on("DELETE", "/api/v1/indexer/7", None)
        self.h.server.on(
            "GET", "/api/v1/applications", [{"id": 1, "name": "A"}, {"id": 2, "name": "A"}]
        )
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/radarr"}
        rows, ok, _ = self.h.run(
            prowlarr(indexers=[{"name": "New", "definitionName": "eztv"}], applications=[app])
        )
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_password_pushed_again_on_another_endpoint(self):
        self.h.server.on("GET", "/api/v3/downloadclient", [current_dc()])
        self.h.server.on("PUT", "/api/v3/downloadclient/4", {})
        _, _, state = self.h.run(radarr(downloadClients=[DC]))
        rows, _, _ = self.h.run(radarr(downloadClients=[DC]), state=state)
        self.assertEqual(rows, [])
        other = radarr(downloadClients=[DC])
        other["arr"]["radarr"]["baseUrl"] = "http://other.example.com"
        rows, _, _ = self.h.run(other, state=state)
        self.assertIn("  ~ radarr download client Transmission (password)", rows)

    def test_unreadable_secret_aborts_before_any_write(self):
        self.h.server.on("GET", "/api/v1/indexer", [{"id": 7, "name": "Old"}])
        self.h.server.on("GET", "/api/v1/indexer/schema", [{"definitionName": "eztv"}])
        self.h.server.on("POST", "/api/v1/indexer", {})
        self.h.server.on("DELETE", "/api/v1/indexer/7", None)
        self.h.server.on("GET", "/api/v1/applications", [])
        app = {"name": "Radarr", "baseUrl": "http://127.0.0.1:7878", "apiKeyFile": "/s/missing"}
        rows, ok, _ = self.h.run(
            prowlarr(indexers=[{"name": "New", "definitionName": "eztv"}], applications=[app])
        )
        self.assertFalse(ok)
        self.assertTrue(any("cannot read Radarr apiKeyFile" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])


if __name__ == "__main__":
    unittest.main()
