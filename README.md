# Minecraft Pack Framework

An initial Python 3.13 implementation of the deterministic core described in
`Minecraft Pack Framework — Architectural and Functional Specification.md`.

The current slice provides ordered layer composition, immutable same-layer evaluation,
exact package artifacts, local builds, stateless local/SFTP synchronization, explicit
provider-driven updates, and a command-line interface. It intentionally does not include
the development web interface, import/explain commands, remote Git parents, provider-side
automatic dependency discovery, Minecraft/loader version-range ranking, or an instance-local
virtual-layer editor.

Runtime code uses only the Python standard library. SFTP requires the optional `paramiko`
dependency:

```powershell
python -m pip install -e ".[sftp]"
```

## Layer format

The initial JSON frontend reserves `.pack/`. A layer marker is `.pack/layer.json`; other
`.pack/*.json` files contain operations. Files outside `.pack/` are direct payload and are
never interpreted as metadata.

```text
client/
├── .pack/
│   ├── layer.json
│   └── packages.json
├── config/
│   └── example.properties
└── options.txt
```

`.pack/layer.json`:

```json
{
  "schema": 1,
  "id": "client",
  "parents": [
    {"path": "../base", "parameters": {"side": "client"}}
  ]
}
```

`.pack/packages.json`:

```json
{
  "operations": [
    {"type": "patch_properties", "path": "config/example.properties", "set": {"enabled": true}},
    {"type": "remove_paths", "glob": "server-only/**"},
    {"type": "prune", "glob": "mods/*.jar"},
    {"type": "ignore", "glob": "logs/**"},
    {
      "type": "package",
      "id": "example-mod",
      "logical_id": "example",
      "kind": "mod",
      "version": "1.2.3",
      "activation": "enabled",
      "update_policy": "fixed",
      "destination": "mods/example-1.2.3.jar",
      "source": {
        "provider": "direct_url",
        "location": "https://example.invalid/example-1.2.3.jar",
        "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      }
    }
  ]
}
```

Supported operation types are `put`, `remove`, `remove_paths`, `patch_properties`,
`package`, `activation`, `dependency`, `group`, `section`, `prune`, and `ignore`.
`put` accepts UTF-8 `content` or `content_base64`.
An `activation` operation targets a package inherited from the immutable parent state. A new
package in the same layer must set its `activation` directly on its `package` declaration;
separate declaration and activation producers in one layer are rejected regardless of file order.

Implementation choices left open by the specification are explicit here:

- concrete paths are portable POSIX-style, relative, case-sensitive, and cannot contain
  traversal or glob characters;
- glob matching is case-sensitive and supports `**`;
- payload symlinks are rejected;
- package update policy defaults to `fixed`;
- automatic dependency activation is enabled unless `--no-auto-dependencies` is passed;
- automatic candidates are ranked only by layer precedence in this slice; equal candidates
  fail rather than being selected arbitrarily;
- persistent `remove_paths` selectors also suppress writes from later layers;
- SFTP requires an absolute remote root and rejects unknown host keys; the config may name a
  `known_hosts` file;
- SFTP rejects symlinks in managed remote paths. Atomic replacement uses the OpenSSH
  `posix-rename` extension when available; the standard-SFTP fallback removes an existing
  destination before `rename` and therefore has weaker atomicity;
- `ignore` selectors are recorded for instance-diff tooling and do not suppress sync writes or
  prune deletion.

## Commands

```powershell
mcpack resolve C:\packs client
mcpack build C:\packs client C:\build\client
mcpack sync C:\packs client --local C:\Minecraft\instance
mcpack sync C:\packs server --sftp-config C:\private\server-sftp.json
mcpack update C:\packs client example-mod
```

Common options include `--cache`, `--parameters '{"key":"value"}'`, and
`--no-auto-dependencies`. A build writes `.mcpack-manifest.json` alongside the materialized
files. It includes exact paths, SHA-256 hashes, artifacts, redacted parameters, and operation
trace. The manifest currently reports no external parent commits because remote Git parents
are outside this slice.

SFTP configuration is local JSON and should not be committed:

```json
{
  "host": "server.example.com",
  "port": 22,
  "username": "minecraft",
  "key_filename": "C:/Users/me/.ssh/id_ed25519",
  "known_hosts": "C:/Users/me/.ssh/known_hosts",
  "root": "/srv/minecraft"
}
```

## Development

```powershell
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy src
python -m build
```
