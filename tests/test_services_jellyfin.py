import contextlib
import io
import unittest

from services_fakes import Harness

URL = "http://svc.example.com"
KEY = "/secrets/jf"


def cfg(**extra):
    return {"jellyfin": {"baseUrl": URL, "apiKeyFile": KEY, **extra}}


MOVIES = {"name": "Movies", "type": "movies", "paths": ["/media/movies"]}


class JellyfinTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(files={KEY: "jf-key"})
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()

    def tearDown(self):
        self.out.__exit__(None, None, None)

    def test_creates_missing_library_and_sends_key(self):
        self.h.server.on("GET", "/Library/VirtualFolders", [])
        self.h.server.on("POST", "/Library/VirtualFolders", None)
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertTrue(ok)
        (call,) = self.h.server.made("POST", "/Library/VirtualFolders")
        self.assertEqual(call[2]["paths"], ["/media/movies"])
        self.assertEqual(call[2]["refreshLibrary"], ["true"])
        self.assertEqual(call[4]["X-emby-token"], "jf-key")
        self.assertFalse(any("jf-key" in r for r in rows))

    def test_matching_library_makes_no_writes(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/media/movies"], "ItemId": "1"}],
        )
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertTrue(ok)
        self.assertEqual(rows, [])
        self.assertEqual(self.h.server.writes(), [])

    def test_paths_in_listed_library_are_authoritative(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/old"], "ItemId": "1"}],
        )
        self.h.server.on("DELETE", "/Library/VirtualFolders/Paths", None)
        self.h.server.on("POST", "/Library/VirtualFolders/Paths", None)
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertTrue(ok)
        self.assertIn("  - library Movies path /old", rows)
        self.assertIn("  + library Movies path /media/movies", rows)

    def test_failed_path_add_keeps_stale_paths(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/old"], "ItemId": "1"}],
        )
        self.h.server.on("POST", "/Library/VirtualFolders/Paths", {"message": "x"}, status=500)
        self.h.server.on("DELETE", "/Library/VirtualFolders/Paths", None)
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.made("DELETE", "/Library/VirtualFolders/Paths"), [])

    def test_paths_added_before_stale_removed(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/old"], "ItemId": "1"}],
        )
        self.h.server.on("POST", "/Library/VirtualFolders/Paths", None)
        self.h.server.on("DELETE", "/Library/VirtualFolders/Paths", None)
        self.h.run(cfg(libraries=[MOVIES]))
        self.assertEqual(
            self.h.server.writes(),
            [
                ("POST", "/Library/VirtualFolders/Paths"),
                ("DELETE", "/Library/VirtualFolders/Paths"),
            ],
        )

    def test_duplicate_library_names_rejected(self):
        rows, ok, _ = self.h.run(
            cfg(libraries=[MOVIES, {**MOVIES, "paths": ["/other"]}]), apply=False
        )
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_trailing_slash_paths_are_equal(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/media/movies/"], "ItemId": "1"}],
        )
        rows, ok, _ = self.h.run(cfg(libraries=[{**MOVIES, "paths": ["/media/movies"]}]))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_unlisted_library_is_never_deleted(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Hand made", "Locations": ["/x"], "ItemId": "9"}],
        )
        self.h.run(cfg(libraries=[]))
        self.assertEqual(self.h.server.writes(), [])

    def test_options_updated_only_when_different(self):
        lib = {**MOVIES, "enableRealtimeMonitor": True, "automaticRefreshIntervalDays": 7}
        current = {
            "Name": "Movies",
            "Locations": ["/media/movies"],
            "ItemId": "1",
            "LibraryOptions": {"EnableRealtimeMonitor": True, "AutomaticRefreshIntervalDays": 0},
        }
        self.h.server.on("GET", "/Library/VirtualFolders", [current])
        self.h.server.on("POST", "/Library/VirtualFolders/LibraryOptions", None)
        rows, ok, _ = self.h.run(cfg(libraries=[lib]))
        self.assertTrue(ok)
        (call,) = self.h.server.made("POST", "/Library/VirtualFolders/LibraryOptions")
        self.assertEqual(call[3]["LibraryOptions"]["AutomaticRefreshIntervalDays"], 7)

    def test_dry_run_plans_same_rows_without_writes(self):
        self.h.server.on("GET", "/Library/VirtualFolders", [])
        self.h.server.on("GET", "/System/Configuration/network", {"LocalNetworkAddresses": []})
        planned, _, _ = self.h.run(cfg(libraries=[MOVIES], bindAddress="0.0.0.0"), apply=False)
        self.assertEqual(self.h.server.writes(), [])
        self.h.server.on("POST", "/Library/VirtualFolders", None)
        self.h.server.on("POST", "/System/Configuration/network", None)
        applied, ok, _ = self.h.run(cfg(libraries=[MOVIES], bindAddress="0.0.0.0"))
        self.assertTrue(ok)
        self.assertEqual(planned, applied)

    def test_network_post_preserves_other_fields(self):
        self.h.server.on(
            "GET",
            "/System/Configuration/network",
            {"LocalNetworkAddresses": ["10.0.0.1"], "InternalHttpPort": 8096},
        )
        self.h.server.on("POST", "/System/Configuration/network", None)
        self.h.run(cfg(bindAddress="0.0.0.0"))
        (call,) = self.h.server.made("POST", "/System/Configuration/network")
        self.assertEqual(call[3], {"LocalNetworkAddresses": ["0.0.0.0"], "InternalHttpPort": 8096})

    def test_non_list_libraries_abort_before_writes(self):
        self.h.server.on("GET", "/Library/VirtualFolders", {"Items": []})
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_failed_create_skips_its_options(self):
        lib = {**MOVIES, "automaticRefreshIntervalDays": 7}
        self.h.server.on("GET", "/Library/VirtualFolders", [])
        self.h.server.on("POST", "/Library/VirtualFolders", {"message": "x"}, status=500)
        rows, ok, _ = self.h.run(cfg(libraries=[lib]))
        self.assertFalse(ok)
        self.assertEqual(len(self.h.server.made("GET", "/Library/VirtualFolders")), 1)
        self.assertFalse(any("not found after create" in r for r in rows))

    def test_failed_write_reports_failure(self):
        self.h.server.on("GET", "/Library/VirtualFolders", [])
        self.h.server.on("POST", "/Library/VirtualFolders", {"message": "boom"}, status=500)
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertFalse(ok)
        self.assertTrue(any(r.startswith("  ! create library Movies") for r in rows))

    def test_null_network_addresses_do_not_crash(self):
        current = {"LocalNetworkAddresses": None, "KnownProxies": None, "InternalHttpPort": 8096}
        self.h.server.on("GET", "/System/Configuration/network", current)
        self.h.server.on("POST", "/System/Configuration/network", None)
        rows, ok, _ = self.h.run(cfg(bindAddress="0.0.0.0"))
        self.assertTrue(ok, rows)
        (call,) = self.h.server.made("POST", "/System/Configuration/network")
        self.assertEqual(call[3], {**current, "LocalNetworkAddresses": ["0.0.0.0"]})

    def test_non_list_network_addresses_abort(self):
        self.h.server.on(
            "GET", "/System/Configuration/network", {"LocalNetworkAddresses": "10.0.0.1"}
        )
        rows, ok, _ = self.h.run(cfg(bindAddress="0.0.0.0"))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_null_locations_are_empty(self):
        self.h.server.on(
            "GET", "/Library/VirtualFolders", [{"Name": "Movies", "Locations": None, "ItemId": "1"}]
        )
        self.h.server.on("POST", "/Library/VirtualFolders/Paths", None)
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [("POST", "/Library/VirtualFolders/Paths")])

    def test_remote_duplicate_library_names_abort(self):
        lib = {"Name": "Movies", "Locations": ["/media/movies"]}
        self.h.server.on("GET", "/Library/VirtualFolders", [lib, lib])
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_null_library_options_abort_with_zero_writes(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/old"], "ItemId": "1", "LibraryOptions": None}],
        )
        rows, ok, _ = self.h.run(cfg(libraries=[{**MOVIES, "automaticRefreshIntervalDays": 7}]))
        self.assertFalse(ok)
        self.assertTrue(any("LibraryOptions" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_options_posted_from_state_after_path_changes(self):
        def lib(path):
            return {
                "Name": "Movies",
                "Locations": [path],
                "ItemId": "1",
                "LibraryOptions": {"PathInfos": [{"Path": path}], "Other": "keep"},
            }

        reads = [lib("/old"), lib("/media/movies")]
        self.h.server.on("GET", "/Library/VirtualFolders", lambda b, q: [reads.pop(0)])
        self.h.server.on("POST", "/Library/VirtualFolders/Paths", None)
        self.h.server.on("DELETE", "/Library/VirtualFolders/Paths", None)
        self.h.server.on("POST", "/Library/VirtualFolders/LibraryOptions", None)
        rows, ok, _ = self.h.run(cfg(libraries=[{**MOVIES, "automaticRefreshIntervalDays": 7}]))
        self.assertTrue(ok, rows)
        (call,) = self.h.server.made("POST", "/Library/VirtualFolders/LibraryOptions")
        options = call[3]["LibraryOptions"]
        self.assertEqual(options["PathInfos"], [{"Path": "/media/movies"}])
        self.assertEqual(options["Other"], "keep")
        self.assertEqual(options["AutomaticRefreshIntervalDays"], 7)

    def test_bad_library_read_aborts_before_network_write(self):
        self.h.server.on("GET", "/System/Configuration/network", {"LocalNetworkAddresses": []})
        self.h.server.on("POST", "/System/Configuration/network", None)
        self.h.server.on("GET", "/Library/VirtualFolders", {"Items": []})
        rows, ok, _ = self.h.run(cfg(bindAddress="0.0.0.0", libraries=[MOVIES]))
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_path_with_comma_rejected(self):
        lib = {**MOVIES, "paths": ["/media/a,b"]}
        rows, ok, _ = self.h.run(cfg(libraries=[lib]), apply=False)
        self.assertFalse(ok)
        self.assertTrue(any("comma" in r for r in rows), rows)
        self.assertEqual(self.h.server.calls, [])

    def test_library_dropped_from_config_is_shown_as_kept_once(self):
        self.h.server.on(
            "GET",
            "/Library/VirtualFolders",
            [{"Name": "Movies", "Locations": ["/media/movies"], "ItemId": "1"}],
        )
        state = {
            "jellyfin": {"managed": {"libraries": self.h.managed("libraries", "Movies", "Shows")}}
        }
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]), state=state, apply=False)
        self.assertTrue(ok, rows)
        self.assertEqual(
            rows, ["  = library Shows removed from config, kept (libraries are never deleted)"]
        )
        rows, ok, state = self.h.run(cfg(libraries=[MOVIES]), state=state)
        self.assertEqual(self.h.server.writes(), [])
        rows, ok, _ = self.h.run(cfg(libraries=[MOVIES]), state=state)
        self.assertEqual(rows, [])

    def test_absent_libraries_key_releases_tracked_libraries(self):
        state = {"jellyfin": {"managed": {"libraries": self.h.managed("libraries", "Movies")}}}
        rows, ok, state = self.h.run(cfg(), state=state)
        self.assertTrue(any("= library Movies" in r for r in rows), rows)
        self.assertEqual(self.h.server.calls, [])


if __name__ == "__main__":
    unittest.main()
