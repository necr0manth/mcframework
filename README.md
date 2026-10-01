# Minecraft Pack Framework

Python 3.13 implementation of the deterministic pack framework described in
`Minecraft Pack Framework — Architectural and Functional Specification.md`.

Commands run from the project root. A project contains branch directories such as `client/`
and `server/`; each branch is an ordinary layer with a `.pack/layer.json` marker. Local state,
instance links, artifact cache, instance parameters, feature overlays, and web process records
are split into purpose-specific files below the root `.mcpack/` directory.

Runtime code uses only the Python standard library. SFTP requires the optional `paramiko`
dependency:

```powershell
python -m pip install -e ".[sftp]"
```

For development tools and tests:

```powershell
python -m pip install -e ".[dev,sftp]"
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
  traversal; glob syntax is interpreted only in fields explicitly named `glob`;
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

## Project and instances

Run commands from the directory containing the branch folders. Instance paths point directly
to the game root (`.minecraft`), not to a PrismLauncher instance wrapper.

```powershell
cd C:\packs\my-pack

mcpack init client
mcpack createinstance dev C:\Minecraft\dev\.minecraft
mcpack link client dev
mcpack sync dev
```

`init <layer>` creates a minimal empty layer at `<layer>/.pack/layer.json`. It accepts an
absent or empty target directory and refuses to overwrite an existing layer or non-empty
directory.

`link` can register and link an instance in one command:

```powershell
mcpack link client dev -p C:\Minecraft\dev\.minecraft
```

An instance has at most one link. Re-linking to another base branch requires `unlink` first.
Instance roots must not intersect the project, `.mcpack`, `build`, or branch directories.
Registry deletion never deletes the game directory:

```powershell
mcpack unlink dev
mcpack renameinstance dev testing
mcpack deleteinstance testing
```

The local state layout is:

```text
.mcpack/
├── instances.json
├── links.json
├── parameters/<instance>.json
├── features/<instance>/.pack/layer.json
├── cache/sha256/...
└── web/
    ├── <instance>.json
    └── logs/<instance>.log
```

The files use strict versioned JSON schemas and atomic replacement. `.mcpack/` is ignored by
Git. Parameter files separate ordinary values from secret references.

## Resolve, build, update, and sync

```powershell
mcpack resolve client
mcpack build client
mcpack build client -o C:\pack-builds\client
mcpack update client example-mod
mcpack sync dev
mcpack diff dev
mcpack explain dev mod-id
mcpack explain dev config/example.properties
mcpack import dev
mcpack import dev <delta-id> --provider-match modrinth
```

`build <branch>` defaults to `build/<branch>`. Custom output is rejected when it intersects
the project, any branch, `.mcpack`, or a registered instance. A build writes only the
materialized files, without an additional manifest. The artifact cache defaults to
`.mcpack/cache`.

`sync <instance>` resolves the linked effective layer and synchronizes its direct game root.
It remains stateless with respect to that game directory: no ownership marker or deployment
manifest is written there. Extra files are deleted only when they match resolved `prune`
selectors. `ignore` selectors hide development-diff noise only; they never suppress writes or
prune deletion.

Layers describe only the managed portion of an instance. `remove` drops an inherited file
from the composition; an existing instance copy remains and appears as an `add` in delta
(unless ignored). To delete that copy during sync, also declare a matching `prune` rule.
Files outside the desired composition and prune rules are preserved.

`import <instance>` captures changed and extra instance files into the instance-local layer;
extra JARs become exact-hash local package declarations. Fabric, Quilt, Forge, and NeoForge
metadata is inspected without executing the JAR. Local SHA-256 capture is the default. An
explicit `--provider-match modrinth` performs the official exact SHA-512 lookup and falls back
to local capture on no match or network failure. Fuzzy candidates are never selected
automatically. Missing desired files are deliberately left for explicit review. Pass a delta ID
shown by `diff` to import only one item.

`explain` accepts a package, group, `source->target` dependency, or file path and reports the
focused operation provenance and dependency traversal.

## Features and development web interface

Features are ordinary project-root branches. Adding the first feature creates an ordinary
instance-local top layer under `.mcpack/features/<instance>` whose ordered parents are the base
branch followed by selected feature branches. Local operations and captured payload in that
layer are preserved when the feature list changes.

```powershell
mcpack feature add dev shaders
mcpack feature list dev
mcpack feature remove dev shaders
```

Start the authenticated loopback web interface in the background:

```powershell
mcpack web dev
mcpack web stop dev
```

The start command prints a one-shot browser capability URL and the exact stop command. The
capability is exchanged once for a different in-memory HttpOnly, SameSite cookie and cannot be
replayed. Health and CLI stop use a separate management capability. Browser mutations require
the exact loopback Origin, a per-process CSRF token, and the quoted bootstrap revision in
`If-Match`; stale revisions return `409` before making changes. Per-instance mutations are
serialized. At most one verified server may run for each instance. Stop is idempotent and
addresses the authenticated server rather than killing a recorded PID blindly.

The interface exposes resolved declarations, packages, groups, sections, desired-versus-instance
semantic file/package delta, writable layers, feature controls, package/group/section activation
edits, bounded delta capture, exact provider or local JAR import, synchronization, and shutdown.
Every edit is fully re-resolved before success is reported. Only explicitly supported operation
types are editable; unsupported JSON metadata is rejected by the loader.

The core transport still supports SFTP programmatically. SFTP configuration should remain local:

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
