import contextlib
import io
import json
import unittest

from services_fakes import Harness

from tools.services.nextdns import matches

URL = "http://svc.example.com"
PROFILE = {
    "data": {
        "security": {"threatIntelligenceFeeds": True},
        "privacy": {"blocklists": [{"id": "oisd", "name": "OISD"}]},
        "denylist": [{"id": "bad.example.com", "active": True}],
        "rewrites": [{"name": "home.@DOMAIN@", "content": "192.0.2.1"}],
        "settings": {"logs": {"enabled": False}},
    }
}
FILES = {
    "/cfg/profile.json": json.dumps(PROFILE),
    "/s/key": "api-key",
    "/s/domain": "secret-domain.example.org\n",
    "/s/id": "abc123",
}


def cfg(**profile):
    base = {
        "profileFile": "/cfg/profile.json",
        "apiKeyFile": "/s/key",
        "profileIdFile": "/s/id",
        "varsFiles": {"DOMAIN": "/s/domain"},
    }
    return {"nextdns": {"baseUrl": URL, "profiles": {"home": {**base, **profile}}}}


def remote(**overrides):
    data = {
        "security": {"threatIntelligenceFeeds": True, "cryptojacking": True},
        "privacy": {"blocklists": [{"id": "oisd", "name": "OISD", "entries": 5}]},
        "denylist": [{"id": "bad.example.com", "active": True}],
        "rewrites": [
            {"id": "r1", "name": "home.secret-domain.example.org", "content": "192.0.2.1"}
        ],
        "settings": {"logs": {"enabled": False, "retention": 1}},
    }
    data.update(overrides)
    return {"data": data}


class MatchesTest(unittest.TestCase):
    def test_subset_dicts_and_id_lists(self):
        self.assertTrue(matches({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(matches({"a": 1}, {"a": 2}))
        self.assertTrue(matches([{"id": "x"}], [{"id": "x", "name": "X"}]))
        self.assertFalse(matches([{"id": "x"}], [{"id": "x"}, {"id": "y"}]))
        self.assertFalse(matches([], [{"id": "x"}]))


class NextDnsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(files=FILES)
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()

    def tearDown(self):
        self.out.__exit__(None, None, None)

    def test_matching_profile_is_a_no_op(self):
        self.h.server.on("GET", "/profiles/abc123", remote())
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok)
        self.assertEqual(rows, [])
        self.assertEqual(self.h.server.writes(), [])

    def test_only_differing_sections_are_written(self):
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(denylist=[{"id": "other.example.com"}], settings={"logs": {"enabled": True}}),
        )
        for m, p in [
            ("POST", "/profiles/abc123/denylist"),
            ("DELETE", "/profiles/abc123/denylist/other.example.com"),
            ("PATCH", "/profiles/abc123/settings"),
        ]:
            self.h.server.on(m, p, None)
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok)
        self.assertEqual(
            self.h.server.writes(),
            [
                ("POST", "/profiles/abc123/denylist"),
                ("DELETE", "/profiles/abc123/denylist/other.example.com"),
                ("PATCH", "/profiles/abc123/settings"),
            ],
        )

    def test_inactive_entries_are_not_wanted(self):
        data = json.loads(FILES["/cfg/profile.json"])
        data["data"]["denylist"] = [
            {"id": "off.example.com", "active": False},
            {"id": "on.example.com", "active": True},
            {"id": "new.example.com", "active": False},
        ]
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(
                denylist=[
                    {"id": "off.example.com", "active": True},
                    {"id": "on.example.com", "active": False},
                ]
            ),
        )
        self.h.server.on("DELETE", "/profiles/abc123/denylist/off.example.com", None)
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok, rows)
        self.assertEqual(
            self.h.server.writes(), [("DELETE", "/profiles/abc123/denylist/off.example.com")]
        )

    def test_rewrite_changes_redact_secret_vars(self):
        self.h.server.on("GET", "/profiles/abc123", remote(rewrites=[]))
        rows, ok, _ = self.h.run(cfg(), apply=False)
        self.assertIn("  + home rewrite home.*** -> 192.0.2.1", rows)
        self.assertFalse(any("secret-domain" in r for r in rows))

    def test_profile_resolved_by_name_or_created(self):
        self.h.server.on("GET", "/profiles", {"data": []})
        self.h.server.on("POST", "/profiles", {"data": {"id": "new1"}})
        for m, p in [
            ("PATCH", "/profiles/new1/security"),
            ("PATCH", "/profiles/new1/privacy"),
            ("POST", "/profiles/new1/denylist"),
            ("POST", "/profiles/new1/rewrites"),
            ("PATCH", "/profiles/new1/settings"),
        ]:
            self.h.server.on(m, p, None)
        rows, ok, _ = self.h.run(cfg(profileIdFile=None))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("POST", "/profiles")[0][3], {"name": "home"})

    def test_profiles_followed_across_cursor_pages(self):
        def pages(body, query):
            if "cursor" not in query:
                return {"data": [], "meta": {"pagination": {"cursor": "c2"}}}
            return {"data": [{"id": "abc123", "name": "home"}], "meta": {"pagination": {}}}

        self.h.server.on("GET", "/profiles", pages)
        self.h.server.on("GET", "/profiles/abc123", remote())
        rows, ok, _ = self.h.run(cfg(profileIdFile=None))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("POST", "/profiles"), [])

    def test_malformed_profile_read_aborts(self):
        self.h.server.on("GET", "/profiles/abc123", {"data": {"denylist": "nope"}})
        rows, ok, _ = self.h.run(cfg())
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_empty_lists_refuse_to_delete_everything(self):
        for kind, current in (
            ("denylist", [{"id": "bad.example.com"}]),
            ("allowlist", [{"id": "ok.example.com"}]),
            ("rewrites", [{"id": "r1", "name": "a.example.com", "content": "192.0.2.1"}]),
        ):
            data = {"data": {kind: []}}
            self.h.files["/cfg/profile.json"] = json.dumps(data)
            self.h.server.on("GET", "/profiles/abc123", remote(**{kind: current}))
            rows, ok, _ = self.h.run(cfg())
            self.assertFalse(ok, kind)
            self.assertTrue(any("prune: true" in r for r in rows), rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_invalid_entry_in_any_profile_fails_before_any_request(self):
        bad = json.loads(FILES["/cfg/profile.json"])
        bad["data"]["denylist"] = [{"active": True}]
        self.h.files["/cfg/bad.json"] = json.dumps(bad)
        self.h.server.on("GET", "/profiles/abc123", remote())
        config = cfg()
        good = config["nextdns"]["profiles"]["home"]
        config["nextdns"]["profiles"] = {
            "bad": {**good, "profileFile": "/cfg/bad.json"},
            "home": good,
        }
        rows, ok, _ = self.h.run(config)
        self.assertFalse(ok)
        self.assertTrue(any("denylist[0] needs a string id" in r for r in rows))
        self.assertEqual(self.h.server.calls, [])

    def test_null_profile_is_skipped(self):
        self.h.server.on("GET", "/profiles/abc123", remote())
        config = cfg()
        config["nextdns"]["profiles"]["other"] = None
        rows, ok, _ = self.h.run(config)
        self.assertTrue(ok, rows)

    def test_ambiguous_profile_name_fails(self):
        self.h.server.on(
            "GET",
            "/profiles",
            {"data": [{"id": "p1", "name": "home"}, {"id": "p2", "name": "home"}]},
        )
        rows, ok, _ = self.h.run(cfg(profileIdFile=None))
        self.assertFalse(ok)
        self.assertTrue(any("set profileIdFile" in r for r in rows))
        self.assertEqual(self.h.server.writes(), [])

    def test_duplicate_denylist_ids_rejected(self):
        data = {"data": {"denylist": [{"id": "x.example.com"}, {"id": "x.example.com"}]}}
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        rows, ok, _ = self.h.run(cfg())
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_dry_run_never_creates_profile(self):
        self.h.server.on("GET", "/profiles", {"data": []})
        rows, ok, _ = self.h.run(cfg(profileIdFile=None), apply=False)
        self.assertIn("  + profile home", rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_bad_profile_json_fails_before_api(self):
        self.h.files["/cfg/profile.json"] = "{not json"
        rows, ok, _ = self.h.run(cfg())
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_relative_profile_file_resolves_against_base_dir(self):
        from tools.services import nextdns

        spec = nextdns.parse(
            {"profiles": {"p": {"profileFile": "nd/p.json", "apiKeyFile": "/k"}}}, "/cfg"
        )
        self.assertEqual(spec.profiles[0].profile_file, "/cfg/nd/p.json")

    def test_vars_cannot_inject_json(self):
        evil = 'a"b\\c","x":"y'
        self.h.server.on("GET", "/profiles/abc123", remote(rewrites=[]))
        self.h.server.on("POST", "/profiles/abc123/rewrites", None)
        rows, ok, _ = self.h.run(cfg(varsFiles=None, vars={"DOMAIN": evil}))
        self.assertTrue(ok, rows)
        (post,) = self.h.server.made("POST", "/profiles/abc123/rewrites")
        self.assertEqual(post[3], {"name": "home." + evil, "content": "192.0.2.1"})

    def test_vars_files_cannot_inject_json(self):
        evil = 'q"\\","x":"'
        self.h.files["/s/domain"] = evil + "\n"
        self.h.server.on("GET", "/profiles/abc123", remote(rewrites=[]))
        self.h.server.on("POST", "/profiles/abc123/rewrites", None)
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok, rows)
        (post,) = self.h.server.made("POST", "/profiles/abc123/rewrites")
        self.assertEqual(post[3]["name"], "home." + evil)

    def test_int_and_str_profile_names_collide(self):
        config = cfg()
        home = config["nextdns"]["profiles"]["home"]
        config["nextdns"]["profiles"] = {1: home, "1": home}
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertFalse(ok)
        self.assertEqual(self.h.server.calls, [])

    def test_remote_duplicate_denylist_ids_abort(self):
        dup = [{"id": "bad.example.com", "active": True}] * 2
        self.h.server.on("GET", "/profiles/abc123", remote(denylist=dup, settings={}))
        rows, ok, _ = self.h.run(cfg())
        self.assertFalse(ok)
        self.assertEqual(self.h.server.writes(), [])

    def test_entries_match_case_insensitively(self):
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(
                denylist=[{"id": "Bad.Example.com", "active": True}],
                rewrites=[
                    {"id": "r1", "name": "HOME.secret-domain.example.org", "content": "192.0.2.1"}
                ],
            ),
        )
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_create_sends_config_spelling_and_delete_uses_remote(self):
        data = {"data": {"denylist": [{"id": "New.example.com"}]}}
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        self.h.server.on("GET", "/profiles/abc123", remote(denylist=[{"id": "Old.example.com"}]))
        self.h.server.on("POST", "/profiles/abc123/denylist", None)
        self.h.server.on("DELETE", "/profiles/abc123/denylist/Old.example.com", None)
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok, rows)
        (post,) = self.h.server.made("POST", "/profiles/abc123/denylist")
        self.assertEqual(post[3], {"id": "New.example.com", "active": True})
        self.assertEqual(
            len(self.h.server.made("DELETE", "/profiles/abc123/denylist/Old.example.com")), 1
        )

    def test_rejected_add_keeps_stale_entries(self):
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(
                denylist=[{"id": "old.example.com"}],
                rewrites=[{"id": "r9", "name": "old.example.org", "content": "192.0.2.9"}],
            ),
        )
        self.h.server.on("POST", "/profiles/abc123/denylist", {"errors": []}, status=400)
        self.h.server.on("POST", "/profiles/abc123/rewrites", {"errors": []}, status=400)
        rows, ok, _ = self.h.run(cfg())
        self.assertFalse(ok)
        deletes = [w for w in self.h.server.writes() if w[0] == "DELETE"]
        self.assertEqual(deletes, [])

    def test_null_remote_lists_are_empty(self):
        self.h.server.on("GET", "/profiles/abc123", remote(denylist=None, rewrites=None))
        self.h.server.on("POST", "/profiles/abc123/denylist", None)
        self.h.server.on("POST", "/profiles/abc123/rewrites", None)
        rows, ok, _ = self.h.run(cfg())
        self.assertTrue(ok, rows)

    def test_unnamed_remote_profiles_do_not_block_lookup(self):
        self.h.server.on(
            "GET",
            "/profiles",
            {"data": [{"id": "p0", "name": ""}, {"id": "p1"}, {"id": "p2", "name": "home"}]},
        )
        self.h.server.on("GET", "/profiles/p2", remote())
        rows, ok, _ = self.h.run(cfg(profileIdFile=None))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.writes(), [])

    def test_profile_id_dir_is_read_before_name_lookup(self):
        self.h.files["/ids/nextdns-profile-home"] = "abc123\n"
        self.h.server.on("GET", "/profiles/abc123", remote())
        rows, ok, _ = self.h.run(cfg(profileIdFile=None, profileIdDir="/ids"))
        self.assertTrue(ok, rows)
        self.assertEqual(self.h.server.made("GET", "/profiles"), [])

    def test_missing_id_in_profile_id_dir_falls_back_to_name(self):
        self.h.server.on("GET", "/profiles", {"data": [{"id": "abc123", "name": "home"}]})
        self.h.server.on("GET", "/profiles/abc123", remote())
        rows, ok, _ = self.h.run(cfg(profileIdFile=None, profileIdDir="/ids"))
        self.assertTrue(ok, rows)
        self.assertEqual(len(self.h.server.made("GET", "/profiles")), 1)

    def test_profile_id_dir_defaults_to_sops_nix_path(self):
        import os

        from tools.services import nextdns

        spec = nextdns.parse({"profiles": {"p": {"profileFile": "/p.json", "apiKeyFile": "/k"}}})
        self.assertEqual(
            spec.profiles[0].profile_id_dir, os.path.expanduser("~/.config/sops-nix/secrets")
        )

    def test_2xx_with_errors_is_a_failed_add_and_keeps_stale(self):
        self.h.server.on("GET", "/profiles/abc123", remote(denylist=[{"id": "old.example.com"}]))
        self.h.server.on(
            "POST", "/profiles/abc123/denylist", {"errors": [{"detail": "invalid domain"}]}
        )
        rows, ok, _ = self.h.run(cfg())
        self.assertFalse(ok)
        self.assertTrue(any("invalid domain" in r for r in rows), rows)
        self.assertEqual(
            self.h.server.made("DELETE", "/profiles/abc123/denylist/old.example.com"), []
        )

    def two_profiles(self, later):
        config = cfg()
        good = config["nextdns"]["profiles"]["home"]
        config["nextdns"]["profiles"] = {"home": good, "later": {**good, **later}}
        return config

    def test_bad_later_profile_input_aborts_before_any_write(self):
        self.h.files["/cfg/broken.json"] = "{not json"
        self.h.server.on("GET", "/profiles/abc123", remote(settings={"logs": {"enabled": True}}))
        self.h.server.on("PATCH", "/profiles/abc123/settings", None)
        for later in (
            {"profileFile": "/cfg/broken.json"},
            {"apiKeyFile": "/s/missing"},
            {"profileIdFile": "/s/missing"},
            {"varsFiles": {"DOMAIN": "/s/missing"}},
        ):
            self.h.server.calls.clear()
            rows, ok, _ = self.h.run(self.two_profiles(later))
            self.assertFalse(ok, later)
            self.assertTrue(any("later" in r or "missing" in r for r in rows), rows)
            self.assertEqual(self.h.server.calls, [], later)

    def test_valid_profiles_still_sync_one_after_another(self):
        self.h.server.on("GET", "/profiles/abc123", remote(settings={"logs": {"enabled": True}}))
        self.h.server.on("PATCH", "/profiles/abc123/settings", None)
        rows, ok, _ = self.h.run(self.two_profiles({}))
        self.assertTrue(ok, rows)
        self.assertEqual(len(self.h.server.made("PATCH", "/profiles/abc123/settings")), 2)

    def test_profile_dropped_from_config_is_shown_as_kept_once(self):
        self.h.server.on("GET", "/profiles/abc123", remote())
        state = {"nextdns": {"managed": {"profiles": self.h.managed("profiles", "home", "old")}}}
        rows, ok, _ = self.h.run(cfg(), state=state, apply=False)
        self.assertTrue(ok, rows)
        self.assertIn(
            "  = profile old removed from config, kept (profiles are never deleted)", rows
        )
        self.assertEqual(self.h.server.writes(), [])
        rows, ok, state = self.h.run(cfg(), state=state)
        self.assertTrue(any("profile old" in r for r in rows), rows)
        self.assertEqual(list(state["nextdns"]["managed"]["profiles"].values()), ["home"])
        rows, ok, _ = self.h.run(cfg(), state=state)
        self.assertEqual(rows, [])

    def test_null_profile_is_shown_as_no_longer_managed(self):
        self.h.server.on("GET", "/profiles/abc123", remote())
        config = cfg()
        config["nextdns"]["profiles"]["other"] = None
        state = {"nextdns": {"managed": {"profiles": self.h.managed("profiles", "home", "other")}}}
        rows, ok, _ = self.h.run(config, state=state, apply=False)
        self.assertTrue(any("= profile other" in r for r in rows), rows)

    def test_dry_run_of_new_profile_lists_every_entry(self):
        self.h.server.on("GET", "/profiles", {"data": []})
        rows, ok, _ = self.h.run(cfg(profileIdFile=None), apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  + profile home", rows)
        self.assertIn("  + home denylist bad.example.com", rows)
        self.assertIn("  + home rewrite home.*** -> 192.0.2.1", rows)
        self.assertIn("  ~ home settings", rows)

    def test_removed_blocklist_and_categories_are_listed(self):
        data = json.loads(FILES["/cfg/profile.json"])
        data["data"]["parentalControl"] = {"categories": [{"id": "gambling"}]}
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(
                privacy={"blocklists": [{"id": "oisd"}, {"id": "stale"}]},
                parentalControl={"categories": [{"id": "porn"}]},
            ),
        )
        rows, ok, _ = self.h.run(cfg(), apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  ~ home privacy", rows)
        self.assertIn("  - home privacy blocklist stale", rows)
        self.assertIn("  ~ home parentalControl/categories", rows)
        self.assertIn("  + home parentalControl/categories gambling", rows)
        self.assertIn("  - home parentalControl/categories porn", rows)

    def test_every_list_a_patch_replaces_lists_its_ids(self):
        data = json.loads(FILES["/cfg/profile.json"])
        data["data"]["security"] = {"tlds": [{"id": "ru"}]}
        data["data"]["privacy"] = {"blocklists": [{"id": "oisd"}], "natives": [{"id": "apple"}]}
        data["data"]["settings"] = {"logs": {"enabled": False}, "nested": {"ids": [{"id": "a"}]}}
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(
                security={"tlds": [{"id": "ru"}, {"id": "cn"}]},
                privacy={
                    "blocklists": [{"id": "oisd"}],
                    "natives": [{"id": "apple"}, {"id": "alexa"}],
                },
                settings={"logs": {"enabled": False}, "nested": {"ids": [{"id": "b"}]}},
            ),
        )
        rows, ok, _ = self.h.run(cfg(), apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  ~ home security", rows)
        self.assertIn("  - home security tld cn", rows)
        self.assertIn("  ~ home privacy", rows)
        self.assertIn("  - home privacy native alexa", rows)
        self.assertFalse(any("blocklist" in r for r in rows), rows)
        self.assertIn("  ~ home settings", rows)
        self.assertIn("  + home settings nested.ids a", rows)
        self.assertIn("  - home settings nested.ids b", rows)

    def test_list_rows_redact_secrets_whatever_their_escaping(self):
        for secret in ("bücher.example", 'q"uote.example', "back\\slash.example"):
            self.h.files["/s/domain"] = secret + "\n"
            data = json.loads(FILES["/cfg/profile.json"])
            data["data"]["settings"] = {
                "logs": {
                    "excludedDomains": ["@DOMAIN@", "x.@DOMAIN@"],
                    "objects": [{"name": "@DOMAIN@"}],
                },
                "ids": [{"id": {"host": "@DOMAIN@"}}],
            }
            self.h.files["/cfg/profile.json"] = json.dumps(data)
            self.h.server.on("GET", "/profiles/abc123", remote())
            h = Harness(files=self.h.files)
            h.server = self.h.server
            rows, ok, _ = h.run(cfg(), apply=False)
            self.assertTrue(ok, rows)
            self.assertIn("  + home settings logs.excludedDomains ***", rows)
            self.assertIn("  + home settings logs.excludedDomains x.***", rows)
            self.assertIn('  + home settings logs.objects {"name": "***"}', rows)
            self.assertIn('  + home settings ids {"host": "***"}', rows)
            for r in rows:
                for part in ("bücher", "uote.example", "slash.example"):
                    self.assertNotIn(part, r, (secret, rows))

    def test_vars_file_with_inner_newline_is_redacted_as_substituted(self):
        self.h.files["/s/domain"] = "top\nsecret\n"
        data = json.loads(FILES["/cfg/profile.json"])
        data["data"]["privacy"] = {"blocklists": [{"id": "x-@DOMAIN@"}]}
        data["data"]["denylist"] = [{"id": "d-@DOMAIN@.com"}]
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        self.h.server.on("GET", "/profiles/abc123", remote(rewrites=[]))
        rows, ok, _ = self.h.run(cfg(), apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  + home privacy blocklist x-***", rows)
        self.assertIn("  + home denylist d-***.com", rows)
        self.assertFalse(any("topsecret" in r for r in rows), rows)

    def test_list_rows_compare_raw_values_not_masked_ones(self):
        self.h.files["/s/domain"] = "new.example\n"
        self.h.files["/s/old"] = "old.example\n"
        data = json.loads(FILES["/cfg/profile.json"])
        data["data"]["settings"] = {"logs": {"excluded": ["@DOMAIN@"], "other": "@OLD@"}}
        self.h.files["/cfg/profile.json"] = json.dumps(data)
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(settings={"logs": {"excluded": ["old.example"], "other": "old.example"}}),
        )
        config = cfg()
        config["nextdns"]["profiles"]["home"]["varsFiles"]["OLD"] = "/s/old"
        rows, ok, _ = self.h.run(config, apply=False)
        self.assertTrue(ok, rows)
        self.assertIn("  + home settings logs.excluded ***", rows)
        self.assertIn("  - home settings logs.excluded ***", rows)
        self.assertFalse(any("example" in r for r in rows if "settings" in r), rows)

    def test_remote_rewrite_without_id_is_shown_as_unremovable(self):
        self.h.server.on(
            "GET",
            "/profiles/abc123",
            remote(
                rewrites=[
                    {"id": "r1", "name": "home.secret-domain.example.org", "content": "192.0.2.1"},
                    {"id": "", "name": "stray.example.com", "content": "192.0.2.9"},
                ]
            ),
        )
        rows, ok, _ = self.h.run(cfg(), apply=False)
        self.assertIn(
            "  - home rewrite stray.example.com -> 192.0.2.9 (no id, cannot be removed)", rows
        )


if __name__ == "__main__":
    unittest.main()
