import unittest

from tools.placeholders import SOPS_HINT, PlaceholderError, Resolver, resolve


class ResolveTest(unittest.TestCase):
    def test_vars_and_env_in_string_leaves(self):
        data = {"a": "x-${name}-y", "b": ["${env:HOME}/bin"], "c": 5}
        out = resolve(data, {"name": "n"}, env={"HOME": "/h"})
        self.assertEqual(out, {"a": "x-n-y", "b": ["/h/bin"], "c": 5})

    def test_non_string_leaves_and_keys_untouched(self):
        data = {"${name}": True, "n": None, "f": 1.5, "i": 3}
        self.assertEqual(resolve(data, {"name": "k"}), data)

    def test_whole_string_keeps_var_type(self):
        out = resolve(
            {"port": "${port}", "s": "p=${port}", "b": "${on}x"}, {"port": 8080, "on": True}
        )
        self.assertEqual(out, {"port": 8080, "s": "p=8080", "b": "truex"})

    def test_list_var_cannot_be_embedded(self):
        with self.assertRaisesRegex(PlaceholderError, "cannot be embedded"):
            resolve("a ${xs}", {"xs": [1]})
        self.assertEqual(resolve("${xs}", {"xs": [1]}), [1])

    def test_nested_var_references(self):
        out = Resolver({"a": "${b}/a", "b": "${c}/b", "c": "${env:ROOT}"}, env={"ROOT": "/r"})
        self.assertEqual(out.vars(), {"a": "/r/b/a", "b": "/r/b", "c": "/r"})

    def test_cycle_detected(self):
        with self.assertRaisesRegex(PlaceholderError, "cycle: a -> b -> a"):
            Resolver({"a": "${b}", "b": "${a}"}).vars()
        with self.assertRaisesRegex(PlaceholderError, "cycle: s -> s"):
            Resolver({"s": "x${s}"}).vars()

    def test_unresolved_identifier_stays_literal(self):
        r = Resolver({"dataDir": "/d"}, env={"LOCAL": "from-env"})
        out = r.resolve([{}, {"t": "${dataDri}/x", "cmd": "echo ${LOCAL}"}], "files")
        self.assertEqual(out, [{}, {"t": "${dataDri}/x", "cmd": "echo ${LOCAL}"}])

    def test_render_mode_rejects_unresolved_identifier(self):
        with self.assertRaisesRegex(PlaceholderError, "undefined variable 'nope'"):
            Resolver(render=True).resolve("${nope}")

    def test_unset_env_is_error(self):
        with self.assertRaisesRegex(PlaceholderError, "MISSING"):
            resolve("${env:MISSING}", env={})

    def test_escape_yields_literal(self):
        self.assertEqual(resolve("echo $${HOME} ${x}", {"x": 1}), "echo ${HOME} 1")
        self.assertEqual(resolve("$$", {}), "$$")

    def test_escaped_text_from_var_is_not_rescanned(self):
        self.assertEqual(resolve("${a}", {"a": "$${b}"}), "${b}")

    def test_spliced_dollars_are_inert_at_render(self):
        # Property-style: any value, any neighbour, renders back exactly.
        values = ["$", "$$", "${", "$${", "cost$", "a$b", "${x}", "$${sops:t}", "{x}", "}", ""]
        neighbours = ["", "{x}", "{sops:t}", "$", "${sops:t}", " $$ ", "${1}", "$${sops:t}"]
        secret = {"t": "S"}
        for value in values:
            for after in neighbours:
                for before in ("", "$", "x"):
                    text = before + "${v}" + after
                    loader = Resolver({"v": value}, env={})
                    first = loader.resolve(text, keep_secrets=True)
                    render = Resolver(secret=secret.__getitem__, render=True)
                    got = render.resolve(first)
                    # Reference: resolve the same text with the secret available up front.
                    want = Resolver({"v": value}, env={}, secret=secret.__getitem__).resolve(text)
                    self.assertEqual(got, want, (value, before, after))

    def test_split_dollar_and_brace_across_vars(self):
        loader = Resolver({"a": "$", "b": "{sops:tok}"})
        first = loader.resolve("${a}${b}", keep_secrets=True)
        got = Resolver(secret=lambda n: self.fail("resolved"), render=True).resolve(first)
        self.assertEqual(got, "${sops:tok}")
        first = Resolver({"v": "cost$"}).resolve("${v}${sops:pw}", keep_secrets=True)
        self.assertEqual(
            Resolver(secret={"pw": "P"}.__getitem__, render=True).resolve(first), "cost$P"
        )

    def test_shell_default_forms_stay_literal(self):
        for text in ("${FOO-bar}", "${FOO+x}", "${FOO=x}", "${FOO?x}", "${a.b}", "${my-var}"):
            r = Resolver({"FOO": "v", "a": "x"}, env={"FOO": "e"})
            self.assertEqual(r.resolve(text), text)

    def test_var_names_must_be_identifiers(self):
        with self.assertRaisesRegex(PlaceholderError, "invalid variable names"):
            Resolver({"my-var": 1})

    def test_non_placeholder_forms_stay_literal(self):
        for text in (
            "a ${b",
            "${vault:x}",
            "${1}",
            "${FOO:-x}",
            "${#arr}",
            "${FOO/a/b}",
            "${@}",
            'for f in "${files[@]}"; do',
        ):
            self.assertEqual(resolve(text, {}, env={"FOO": "v"}), text)

    def test_identifier_never_reads_env(self):
        self.assertEqual(resolve("${HOME}/x", {}, env={"HOME": "/h"}), "${HOME}/x")
        self.assertEqual(resolve("${HOME}", {"HOME": "/v"}, env={"HOME": "/h"}), "/v")
        self.assertEqual(resolve("${env:HOME}/x", {}, env={"HOME": "/h"}), "/h/x")
        self.assertEqual(
            resolve("Bearer ${API_KEY}", {}, env={"API_KEY": "k"}), "Bearer ${API_KEY}"
        )

    def test_unresolved_identifier_in_content_passes_through_render(self):
        first = Resolver({}, env={"HOME": "/h"}).resolve(
            "cd ${HOME} && echo ${sops:t}", keep_secrets=True
        )
        self.assertEqual(first, "cd $${HOME} && echo ${sops:t}")
        got = Resolver(secret={"t": "S"}.__getitem__, render=True).resolve(first)
        self.assertEqual(got, "cd ${HOME} && echo S")

    def test_splice_boundary_before_sops(self):
        secret = {"t": "S"}.__getitem__
        cases = {
            "$${sops:t}": "${sops:t}",
            "$$${sops:t}": "$${sops:t}",
            "a$": "a$",
            "${v}${sops:t}": "x$S",
            "$${v}${sops:t}": "${v}S",
            "${v}$${sops:t}": "x$${sops:t}",
            "$ ${sops:t}": "$ S",
            "${dollar}{sops:t}": "${sops:t}",
            "${dollar}${sops:t}": "$S",
        }
        for text, want in cases.items():
            first = Resolver({"v": "x$", "dollar": "$"}).resolve(text, keep_secrets=True)
            got = Resolver(secret=secret, render=True).resolve(first)
            self.assertEqual(got, want, text)

    def test_whole_string_mapping_reescaped_in_keep_mode(self):
        r = Resolver({"m": {"a": "$${sops:x}", "l": ["$${y}"]}})
        first = r.resolve("${m}", keep_secrets=True)
        self.assertEqual(first, {"a": "$${sops:x}", "l": ["$${y}"]})  # $ doubled
        second = Resolver(secret=lambda n: self.fail("re-resolved"), render=True).resolve(first)
        self.assertEqual(second, {"a": "${sops:x}", "l": ["${y}"]})

    def test_sops_without_lookup_points_at_templates(self):
        with self.assertRaises(PlaceholderError) as cm:
            Resolver({}).resolve({"k": "${sops:token}"}, "brew")
        self.assertIn(SOPS_HINT, str(cm.exception))
        self.assertIn("brew.k", str(cm.exception))

    def test_sops_in_vars_always_rejected(self):
        r = Resolver({"v": "${sops:x}"}, secret=lambda n: "s")
        with self.assertRaisesRegex(PlaceholderError, "in a variable"):
            r.resolve("${v}")

    def test_sops_lookup_records_usage(self):
        r = Resolver(secret={"tok": "S"}.__getitem__)
        self.assertEqual(r.resolve("a=${sops:tok}"), "a=S")
        self.assertEqual(r.used_secrets, {"tok"})
        with self.assertRaisesRegex(PlaceholderError, "no sops secret named 'x'"):
            r.resolve("${sops:x}")

    def test_sops_path(self):
        r = Resolver(sops_path={"k": "/s/k"}.__getitem__)
        self.assertEqual(r.resolve("file:${sopsPath:k}"), "file:/s/k")
        with self.assertRaisesRegex(PlaceholderError, "no sops secret named 'z'"):
            r.resolve("${sopsPath:z}")
        with self.assertRaisesRegex(PlaceholderError, "not available"):
            Resolver().resolve("${sopsPath:k}")

    def test_keep_secrets_mode_round_trips_through_second_pass(self):
        r = Resolver({"a": "$${lit}", "d": "dom"})
        first = r.resolve("${d} ${sops:t} $${x} ${a}", keep_secrets=True)
        self.assertEqual(first, "dom ${sops:t} $${x} $${lit}")
        self.assertEqual(first, "dom ${sops:t} $${x} $${lit}")
        second = Resolver(secret={"t": "S"}.__getitem__, render=True).resolve(first)
        self.assertEqual(second, "dom S ${x} ${lit}")


if __name__ == "__main__":
    unittest.main()
