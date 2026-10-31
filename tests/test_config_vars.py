import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tools import cli
from tools.config import ConfigError, load_config_dir, resolve_config
from tools.user import templates


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


class ConfigVarsTest(unittest.TestCase):
    def test_host_value_overrides_shared_include(self):
        with tempfile.TemporaryDirectory() as d:
            _write(
                os.path.join(d, "shared", "base.yaml"),
                "vars:\n  root: /shared\n  name: base\n"
                "files:\n  - source: a\n    target: ${root}/${name}\n",
            )
            _write(
                os.path.join(d, "machines", "h.yaml"),
                "include: [../shared/base.yaml]\nvars:\n  root: /host\n",
            )
            with mock.patch("tools.config._get_hostname", return_value="h"):
                merged = load_config_dir(d)

            out = resolve_config(merged, d, env={})

        self.assertEqual(out["files"][0]["target"], "/host/base")
        self.assertEqual(out["vars"], {"root": "/host", "name": "base"})

    def test_later_config_path_overrides_vars(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.yaml"), os.path.join(d, "b.yaml")
            _write(a, "vars: {v: one}\nstateFile: /s/${v}.json\n")
            _write(b, "vars: {v: two}\n")
            merged = cli._load_merged_config([a, b])
        self.assertEqual(merged["stateFile"], "/s/two.json")

    def test_nested_refs_and_env(self):
        config = {
            "vars": {"home": "${env:HOME}", "bin": "${home}/bin"},
            "paths": {"uvBin": "${bin}"},
        }
        out = resolve_config(config, env={"HOME": "/u"})
        self.assertEqual(out["paths"]["uvBin"], "/u/bin")

    def test_cycle_is_config_error(self):
        with self.assertRaisesRegex(ConfigError, "cycle"):
            resolve_config({"vars": {"a": "${b}", "b": "${a}"}}, env={})

    def test_typo_var_is_literal(self):
        config = {"vars": {"dataDir": "/d"}, "brew": {"brews": ["a", "${dataDri}/x"]}}
        out = resolve_config(config, env={"dataDri": "/env"})
        self.assertEqual(out["brew"]["brews"][1], "${dataDri}/x")

    def test_runtime_placeholders_pass_through_unresolved(self):
        env = {"API_KEY": "real-key", "HOME": "/u", "TOKEN": "t"}
        config = {
            "mcp": {"servers": {"x": {"headers": ["Authorization: Bearer ${API_KEY}"]}}},
            "npm": {"packages": {"p": {"postInstall": "cd ${HOME} && login ${TOKEN}"}}},
            "curlShell": {"https://example.test/i.sh": "sh -s -- --dir ${HOME}"},
            "launchd": {"services": {"s": {"command": "run --home ${HOME}"}}},
        }
        out = resolve_config(config, env=env)
        self.assertEqual(out, config)
        self.assertNotIn("real-key", repr(out))

    def test_namespaced_errors_still_fatal(self):
        for value in ("${env:UNSET_X}", "${sopsPath:nope}"):
            with self.assertRaises(ConfigError):
                resolve_config({"npm": {"x": value}}, env={})

    def test_explicit_env_resolves_without_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = resolve_config(
                {"paths": {"x": "${env:SOPS_AGE_KEY_FILE}"}}, env={"SOPS_AGE_KEY_FILE": "/k"}
            )
        self.assertEqual(got["paths"]["x"], "/k")
        self.assertEqual(out.getvalue(), "")

    def test_escape_gives_literal(self):
        out = resolve_config({"npm": {"packages": {"x": {"postInstall": "echo $${HOME}"}}}}, env={})
        self.assertEqual(out["npm"]["packages"]["x"]["postInstall"], "echo ${HOME}")

    def test_non_string_leaf_untouched(self):
        config = {"vars": {"x": 1}, "ollamaModels": {"n": 3, "flag": True, "none": None}}
        self.assertEqual(resolve_config(config, env={})["ollamaModels"], config["ollamaModels"])

    def test_keys_never_substituted(self):
        out = resolve_config({"vars": {"k": "v"}, "gitRepos": {"${k}": "url"}}, env={})
        self.assertEqual(out["gitRepos"], {"${k}": "url"})

    def test_sops_value_outside_templates_is_rejected(self):
        for config in (
            {"mcp": {"servers": {"x": {"headers": {"A": "${sops:token}"}}}}},
            {"vars": {"t": "${sops:token}"}, "files": ["${t}"]},
            {"sops": {"secrets": {"a": {"path": "${sops:token}"}}}},
        ):
            with self.assertRaisesRegex(ConfigError, "templates"):
                resolve_config(config, env={})

    def test_sops_path_resolves_everywhere(self):
        config = {
            "vars": {"dir": "/sec"},
            "sops": {"secretsDir": "${dir}", "secrets": {"tok": {"key": "a", "sopsFile": "/f"}}},
            "launchd": {"waitForPaths": ["${sopsPath:tok}"]},
        }
        out = resolve_config(config, env={})
        self.assertEqual(out["launchd"]["waitForPaths"], ["/sec/tok"])
        self.assertEqual(out["sops"]["secretsDir"], "/sec")

    def test_sops_path_inside_sops_section_rejected(self):
        config = {"sops": {"secrets": {"a": {"path": "${sopsPath:a}", "sopsFile": "/f"}}}}
        with self.assertRaisesRegex(ConfigError, "not available"):
            resolve_config(config, env={})

    def test_relative_sops_files_resolve_against_config_dir(self):
        config = {
            "sops": {
                "defaultSopsFile": "secrets/default.yaml",
                "secrets": {"a": {"sopsFile": "secrets/other.yaml"}, "b": {}},
            }
        }
        out = resolve_config(config, "/cfg", env={})
        self.assertEqual(out["sops"]["defaultSopsFile"], "/cfg/secrets/default.yaml")
        self.assertEqual(out["sops"]["secrets"]["a"]["sopsFile"], "/cfg/secrets/other.yaml")

    def test_templates_keep_sops_and_escapes_for_render(self):
        config = {
            "vars": {"domain": "example.test", "lit": "$${raw}"},
            "templates": {
                "t": {
                    "path": "${env:HOME}/out",
                    "format": "json",
                    "content": {"d": "${domain}", "s": "${sops:tok}", "e": "$${x}", "l": "${lit}"},
                }
            },
        }
        out = resolve_config(config, env={"HOME": "/u"})["templates"]["t"]
        self.assertEqual(out["path"], "/u/out")
        self.assertEqual(
            out["content"], {"d": "example.test", "s": "${sops:tok}", "e": "$${x}", "l": "$${raw}"}
        )

    def test_template_sops_outside_content_rejected(self):
        config = {"templates": {"t": {"path": "${sops:x}", "content": ""}}}
        with self.assertRaisesRegex(ConfigError, "templates"):
            resolve_config(config, env={})

    def test_template_file_read_relative_to_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "tpl", "Caddyfile"), "${host} {\n  tls ${sops:email}\n}\n")
            config = {
                "vars": {"host": "app.example.test"},
                "templates": {"caddy": {"path": "/x/Caddyfile", "file": "tpl/Caddyfile"}},
            }
            out = resolve_config(config, d, env={})["templates"]["caddy"]
        self.assertEqual(out["format"], "raw")
        self.assertEqual(out["content"], "app.example.test {\n  tls ${sops:email}\n}\n")
        self.assertNotIn("file", out)

    def test_template_file_type_checked(self):
        for bad in (None, 0, "", ["a"]):
            config = {"templates": {"t": {"path": "/x", "file": bad}}}
            with self.assertRaisesRegex(ConfigError, "file must be a non-empty path string"):
                resolve_config(config, "/cfg", env={})

    def test_template_file_placeholders_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "tpl", "a.conf"), "body\n")
            config = {
                "vars": {"tplDir": "tpl"},
                "templates": {"t": {"path": "/x", "file": "${tplDir}/a.conf"}},
            }
            out = resolve_config(config, d, env={})
        self.assertEqual(out["templates"]["t"]["content"], "body\n")

    def test_empty_sops_file_not_turned_into_config_dir(self):
        out = resolve_config({"sops": {"defaultSopsFile": ""}}, "/cfg", env={})
        self.assertEqual(out["sops"]["defaultSopsFile"], "")

    def test_template_non_utf8_file_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bin.tpl"), "wb") as f:
                f.write(b"\xff\xfe\x00bad")
            config = {"templates": {"t": {"path": "/x", "file": "bin.tpl"}}}
            with self.assertRaisesRegex(ConfigError, "is not UTF-8 text"):
                resolve_config(config, d, env={})

    def test_template_missing_file_is_error(self):
        with self.assertRaisesRegex(ConfigError, "cannot read"):
            resolve_config({"templates": {"t": {"path": "/x", "file": "nope"}}}, "/nonexist")

    def test_links_keep_env_var_paths_and_shell_stays_literal(self):
        config = {
            "links": [{"source": "${SRC}/x", "target": "$HOME/y"}],
            "curlShell": {"https://example.test/i.sh": "sh -s -- ${1} ${FOO:-d} ${#a}"},
        }
        out = resolve_config(config, env={"SRC": "/src"})
        self.assertEqual(out["links"][0], {"source": "${SRC}/x", "target": "$HOME/y"})
        self.assertEqual(
            out["curlShell"], {"https://example.test/i.sh": "sh -s -- ${1} ${FOO:-d} ${#a}"}
        )

    def test_vars_must_be_mapping(self):
        for bad in ([], "x", 0):
            with self.assertRaisesRegex(ConfigError, "vars must be a mapping"):
                resolve_config({"vars": bad}, env={})

    def test_non_mapping_sections_do_not_crash_loader(self):
        out = resolve_config({"sops": ["x"], "templates": "y"}, env={})
        self.assertEqual(out, {"sops": ["x"], "templates": "y"})

    def test_env_never_makes_template_secret(self):
        config = {
            "vars": {"home": "${env:HOME}"},
            "templates": {
                "direct": {"path": "/a", "content": "h=${env:HOME}"},
                "via-var": {"path": "/b", "content": {"h": "${home}"}, "format": "json"},
                "shell": {"path": "/c", "content": "cd ${HOME}"},
                "flagged": {"path": "/d", "content": "x", "secrets": True},
            },
        }
        out = resolve_config(config, env={"HOME": "/u"})["templates"]
        self.assertEqual(out["direct"], {"path": "/a", "content": "h=/u"})
        self.assertEqual(out["via-var"]["content"], {"h": "/u"})
        self.assertEqual(out["shell"]["content"], "cd $${HOME}")
        self.assertNotIn("secrets", out["shell"])
        self.assertTrue(out["flagged"]["secrets"])
        t = templates.parse_templates({"a": out["direct"]})[0][0]
        self.assertEqual(templates.render(t, None), (b"h=/u", False))

    def test_invalid_sops_config_reported_for_sops_path(self):
        config = {
            "sops": {"age": "nope", "secrets": {"tok": {"key": "a", "sopsFile": "/f"}}},
            "launchd": {"waitForPaths": ["${sopsPath:tok}"]},
        }
        with self.assertRaises(ConfigError) as cm:
            resolve_config(config, env={})
        self.assertIn("sops config is invalid", str(cm.exception))
        self.assertIn("age must be a mapping", str(cm.exception))
        self.assertNotIn("no sops secret named", str(cm.exception))

    def test_invalid_secret_entry_reported_for_sops_path(self):
        config = {
            "sops": {"secrets": {"tok": {"key": "a"}}},
            "launchd": {"waitForPaths": ["${sopsPath:tok}"]},
        }
        with self.assertRaisesRegex(ConfigError, "tok: no sopsFile and no defaultSopsFile"):
            resolve_config(config, env={})

    def test_no_vars_no_placeholders_is_identity(self):
        config = {"npm": {"packages": {"a": {"version": "1"}}}, "links": []}
        self.assertEqual(resolve_config(config, env={}), config)


if __name__ == "__main__":
    unittest.main()
