"""`${...}` placeholder resolution, independent of any config format.

Load-time syntax, inside string leaves only (mapping keys are never touched):

    ${name}            `vars.name`; anything else is left literal for the
                       consumer (a shell, claude's header expansion, ...)
    ${env:NAME}        an environment variable (unset is an error)
    ${sopsPath:NAME}   the on-disk path of a declared sops secret
    ${sops:NAME}       a secret's value; only via an injected ``secret`` callable
    $${                a literal ``${``

``name`` is ``[A-Za-z_][A-Za-z0-9_]*``. A string that is exactly one
``${name}`` takes the variable's value with its type (an int stays an
int); anywhere else the value is spliced in as text. Anything that is not
one of those forms (``${1}``, ``${FOO:-x}``, ``${FOO-x}``, ``${#arr}``,
``${a.b}``, an unknown ``ns:`` prefix, an unterminated ``${``) is left
exactly as written.

The environment is read only through ``${env:NAME}``. Variables may
reference other variables (and ``env``/``sopsPath``), with cycle detection.
They may never reference ``${sops:...}``.

Render grammar (``render=True``, the second pass over template content):
``$$`` is a literal ``$`` and ``${sops:NAME}`` a secret; any other
placeholder is an error, and other text is literal. ``keep_secrets`` at
load time produces exactly that encoding (see ``encode_literal``), so
text that was resolved once can never be parsed again.
"""

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_PLACEHOLDER = r"\$\{(?:(env|sops|sopsPath):([^{}]*)|(" + _IDENT + r"))\}"
_TOKEN = re.compile(r"\$\$\{|" + _PLACEHOLDER)
_RENDER_TOKEN = re.compile(r"\$\$|" + _PLACEHOLDER)
_FULL = re.compile(_PLACEHOLDER)
_VAR_NAME = re.compile(_IDENT)

SOPS_HINT = (
    "secret values are only resolved inside a `templates` entry; elsewhere reference the "
    "decrypted file by path with ${sopsPath:NAME} (e.g. in a *File: option)"
)


class PlaceholderError(ValueError):
    """A placeholder could not be resolved."""


class _Keep:
    pass


_KEEP = _Keep()


def encode_literal(text: str) -> str:
    """Make text inert for the render grammar."""
    return text.replace("$", "$$")


def _where(where: str) -> str:
    return f" at {where}" if where else ""


def _text(value: Any, token: str, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise PlaceholderError(
        f"${{{token}}} is a {type(value).__name__} and cannot be embedded in text{_where(where)}"
    )


class Resolver:
    """Resolve placeholders against one `vars` mapping.

    ``vars`` may itself hold placeholders; they are resolved lazily and
    cached. ``env`` defaults to no environment at all (pass ``os.environ``
    explicitly). ``sops_path`` and ``secret`` map a secret name to its
    path or value and raise ``KeyError`` when it is unknown; leaving one
    out makes that prefix an error.
    """

    def __init__(
        self,
        vars: Optional[Mapping[str, Any]] = None,
        *,
        env: Optional[Mapping[str, str]] = None,
        sops_path: Optional[Callable[[str], str]] = None,
        secret: Optional[Callable[[str], str]] = None,
        render: bool = False,
    ):
        if vars is not None and not isinstance(vars, Mapping):
            raise PlaceholderError(f"vars must be a mapping, got {type(vars).__name__}")
        self._raw = dict(vars or {})
        bad = [k for k in self._raw if not isinstance(k, str) or not _VAR_NAME.fullmatch(k)]
        if bad:
            raise PlaceholderError(f"invalid variable names {bad!r} (use [A-Za-z_][A-Za-z0-9_]*)")
        self._env = env
        self._sops_path = sops_path
        self._secret = secret
        self._render = render
        self._cache: Dict[str, Any] = {}
        self._stack: List[str] = []
        self._keep_secrets = False
        self.used_secrets: Set[str] = set()

    def vars(self) -> Dict[str, Any]:
        """Every variable, fully resolved."""
        return {name: self._var(name, "vars") for name in self._raw}

    def resolve(self, data: Any, where: str = "", *, keep_secrets: bool = False) -> Any:
        """Return a copy of ``data`` with every string leaf resolved.

        ``keep_secrets`` leaves ``${sops:...}`` in place and encodes every
        other ``$`` for the render grammar.
        """
        if keep_secrets and self._render:
            raise ValueError("keep_secrets is a load-time mode")
        previous = self._keep_secrets
        self._keep_secrets = keep_secrets
        try:
            return self._tree(data, where)
        finally:
            self._keep_secrets = previous

    def _tree(self, data: Any, where: str) -> Any:
        if isinstance(data, Mapping):
            return {k: self._tree(v, f"{where}.{k}" if where else str(k)) for k, v in data.items()}
        if isinstance(data, (list, tuple)):
            return [self._tree(v, f"{where}[{i}]") for i, v in enumerate(data)]
        if isinstance(data, str):
            return self._string(data, where)
        return data

    @property
    def _keeping(self) -> bool:
        # Variables always resolve to their literal value, whatever the
        # caller's mode; encoding applies only where text lands in the caller.
        return self._keep_secrets and not self._stack

    def _emit(self, text: str) -> str:
        return encode_literal(text) if self._keeping else text

    def _emit_tree(self, value: Any) -> Any:
        if not self._keeping:
            return value
        if isinstance(value, str):
            return self._emit(value)
        if isinstance(value, Mapping):
            return {k: self._emit_tree(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._emit_tree(v) for v in value]
        return value

    def _string(self, s: str, where: str) -> Any:
        full = _FULL.fullmatch(s)
        if full:
            value = self._lookup(full, where)
            if value is _KEEP:
                return s if self._is_sops(full) else self._emit(s)
            return self._emit_tree(value)

        out: List[str] = []
        pos = 0
        for m in (_RENDER_TOKEN if self._render else _TOKEN).finditer(s):
            out.append(self._emit(s[pos : m.start()]))
            pos = m.end()
            token = m.group(0)
            if token == "$$":  # render grammar
                out.append("$")
                continue
            if token == "$${":
                out.append(self._emit("${"))
                continue
            value = self._lookup(m, where)
            if value is _KEEP:
                out.append(token if self._is_sops(m) else self._emit(token))
                continue
            out.append(self._emit(_text(value, token[2:-1], where)))
        out.append(self._emit(s[pos:]))
        return "".join(out)

    @staticmethod
    def _is_sops(m: "re.Match") -> bool:
        return m.group(1) == "sops"

    def _lookup(self, m: "re.Match", where: str) -> Any:
        prefix, name, ident = m.group(1), m.group(2), m.group(3)
        token = m.group(0)[2:-1]
        if ident is not None:
            if ident in self._raw:
                return self._var(ident, where)
            if self._render:
                raise PlaceholderError(f"undefined variable {ident!r}{_where(where)}")
            return _KEEP
        if not name:
            raise PlaceholderError(f"${{{token}}} has an empty name{_where(where)}")

        if prefix == "env":
            if self._env is None or name not in self._env:
                raise PlaceholderError(
                    f"environment variable {name!r} is not set (${{{token}}}){_where(where)}"
                )
            return self._env[name]

        if prefix == "sopsPath":
            if self._sops_path is None:
                raise PlaceholderError(f"${{{token}}} is not available here{_where(where)}")
            try:
                return self._sops_path(name)
            except KeyError:
                raise PlaceholderError(
                    f"${{{token}}}: no sops secret named {name!r}{_where(where)}"
                ) from None

        # prefix == "sops"
        if self._stack:
            raise PlaceholderError(f"${{{token}}} in a variable{_where(where)}: {SOPS_HINT}")
        if self._secret is not None:
            try:
                value = self._secret(name)
            except KeyError:
                raise PlaceholderError(
                    f"${{{token}}}: no sops secret named {name!r}{_where(where)}"
                ) from None
            self.used_secrets.add(name)
            return value
        if self._keeping:
            return _KEEP
        raise PlaceholderError(f"${{{token}}}{_where(where)}: {SOPS_HINT}")

    def _var(self, name: str, where: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        if name in self._stack:
            chain = " -> ".join(self._stack[self._stack.index(name) :] + [name])
            raise PlaceholderError(f"variable cycle: {chain}")
        self._stack.append(name)
        try:
            value = self._tree(self._raw[name], f"vars.{name}")
        finally:
            self._stack.pop()
        self._cache[name] = value
        return value


def resolve(data: Any, vars: Optional[Mapping[str, Any]] = None, **kwargs) -> Any:
    """Functional shorthand for ``Resolver(vars, **kwargs).resolve(data)``."""
    return Resolver(vars, **kwargs).resolve(data)
