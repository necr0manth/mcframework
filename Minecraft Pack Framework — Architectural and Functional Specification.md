# Minecraft Pack Framework

**Status:** consolidated project specification  
**Purpose:** define the semantic model, required behavior, architecture, and initial scope closely enough to begin implementation. Exact DSL syntax and UI appearance are intentionally left implementation-defined.

## 1. Project purpose

The framework is a universal source-controlled system for:

- creating Minecraft client and server packs;
- composing reusable pack variations;
- managing exact mod and artifact versions;
- building materialized `.minecraft` or server directories;
- comparing a desired pack state with a real instance;
- capturing instance changes back into source layers;
- synchronizing local and remote instances.

The initial system manages files only. A remote server is accessed through SFTP; process lifecycle, console access, backups, Pterodactyl APIs, and restart management are outside the initial scope.

The framework consists of four principal modules:

1. **Layer resolver** — composes layers into a logical desired state.
2. **Artifact resolver** — resolves and downloads declared mods and other packages.
3. **Instance synchronizer** — materializes a resolution locally or through SFTP.
4. **Development web interface** — displays logical pack contents and differences between a resolution and an instance, then performs supported source edits.

---

# 2. Core terminology

## Layer

A **layer** is the only fundamental compositional object.

A layer contains:

```text
LayerDefinition {
    identity
    ordered parent references
    local payload files
    metadata sources
    parameter-dependent operation producers
}
```

The words **feature** and **profile** are conventions, not different runtime types:

- A **feature** is usually a parentless reusable layer.
- A **profile** is usually a layer with several parents and little or no local content.
- An ordinary derived pack is usually a layer with one parent and a local delta.

A layer may therefore represent:

- a base pack;
- a client pack;
- a server pack;
- a development variation;
- an optional feature;
- a production configuration;
- a developer-local overlay;
- a composition of several other layers.

## Layer application

A **layer application** is one concrete occurrence of a layer with resolved parameter values:

```text
LayerApplication {
    layer: LayerDefinition
    parameters: Map<String, Value>
}
```

The same layer may appear more than once and may receive different parameters.

## Pack state

`PackState` is the logical desired state transformed by layers. It contains more than a filesystem:

```text
PackState {
    environment metadata
    virtual filesystem
    package declarations and activation states
    dependency relationships
    groups
    sections and display order
    synchronization rules
    instance-diff ignore rules
}
```

Minecraft version, loader, side, and similar properties may be represented in the environment portion.

## Layer delta

A **layer delta** is the partial logical state produced by the local contents of one layer relative to the state produced by its parents.

## Resolution

A **resolution** is the completely evaluated desired state for one top-level layer application:

```text
Resolution {
    logical PackState
    concrete filesystem manifest
    exact selected artifacts and hashes
    synchronization and ignore rules
    resolution trace
    resolved external parent commits
}
```

The resolution contains no framework metadata files.

## Instance

An **instance** is a real target filesystem:

- a local `.minecraft`;
- a local server root;
- a remote server root accessed through SFTP.

## Instance delta

An **instance delta** is the difference between the current resolution and the current instance. It is distinct from a layer delta.

---

# 3. Repository and layer layout

A workspace may contain multiple layer directories, framework configuration, scripts, and unrelated project files.

A layer directory uses a direct payload layout:

```text
client/
├── <reserved metadata directory>/
│   ├── layer marker and parent declarations
│   ├── package declarations
│   ├── groups and sections
│   └── synchronization rules
├── config/
├── kubejs/
├── defaultconfigs/
├── options.txt
└── other payload files
```

Requirements:

- A fixed marker inside the reserved metadata directory identifies a directory as a layer.
- Everything in the reserved metadata directory is excluded from the materialized instance.
- Everything outside it is ordinary payload unless explicitly removed or ignored.
- Payload paths correspond directly to paths in the resulting Minecraft directory.
- Metadata may be divided into arbitrarily many files.
- The web interface groups declarations by metadata file, section, and declaration order.
- Metadata-file execution order must not affect semantics.
- The exact reserved directory name, marker filename, DSL extension, and file syntax are implementation-defined.

A parent reference may point to:

- another layer in the current workspace;
- a layer in another local repository;
- a repository URL, Git ref, and layer path.

Remote parents may be treated as read-only by the editor. A Git ref may be mutable during development, but every resolution records the exact commit that was used.

---

# 4. Layer composition semantics

A layer may have any number of ordered parents.

Parents are recursively applied in their declared order. Their ancestry is **not deduplicated**.

```text
ResolveLayer(application, inputState):
    state = inputState

    for parentReference in application.layer.parents:
        parentApplication = bindParent(parentReference, application.parameters)
        state = ResolveLayer(parentApplication, state)

    parentState = state
    localDelta = EvaluateLocalLayer(application, parentState)

    return ApplyDelta(parentState, localDelta)
```

Consequences:

- A later parent has higher precedence than an earlier parent.
- The current layer has higher precedence than every parent.
- The same ancestor reached through two parents is applied twice.
- Combining several heavily inherited layers may therefore replay common ancestors.
- Avoiding inappropriate combinations is the pack author’s responsibility.
- The framework does not provide a stylistic linter for questionable inheritance.
- Parent cycles are resolution errors.

Ordinary overlap between different layers is not a conflict:

```text
earlier layer writes file X
later layer writes file X
```

The later layer wins.

A conflict occurs when an operation cannot be evaluated, when a local layer has incompatible simultaneous writes, or when another explicit invariant is violated.

---

# 5. Local-layer evaluation and merging

The local contents of one layer are order-independent.

All local producers and direct payload contributions evaluate relative to the same immutable `parentState`.

```text
EvaluateLocalLayer(application, parentState):
    partialDeltas = []

    for producer in all local producers:
        operations = producer.produce(application.parameters)
        partialDeltas += evaluateOperations(operations, parentState)

    partialDeltas += payloadFilesAsStaticDelta()

    return merge(partialDeltas)
```

A local operation may read the state produced by the parents, but it must not depend on the result of another independent local producer.

Therefore:

- Metadata-file ordering has no semantic meaning.
- One metadata file cannot rely on another metadata file executing first.
- Creating a file in one producer and patching it in another producer in the same layer is invalid.
- Such behavior must be represented by one composite producer or separated into two layers.
- Identical writes may merge.
- Different writes to the same logical resource cause a merge conflict.
- Structured operations may merge at a finer granularity; for example, two property patches changing different keys may coexist.
- Direct payload files participate in the same local merge rules as metadata-generated files.

The framework must support at least:

- adding or overriding an exact path;
- removing an exact inherited path;
- persistently excluding inherited paths through a glob;
- declaring logical packages;
- enabling and disabling package declarations;
- declaring dependencies, groups, sections, prune rules, and ignore rules.

Structured file patches are extensible operation types. Java properties, JSON, TOML, YAML, and similar formats may receive dedicated patch implementations.

---

# 6. Parameters

A layer application receives a key-value parameter map.

Values must be deterministic serializable DSL values, such as:

- strings;
- numbers;
- booleans;
- lists;
- maps;
- structured DSL values;
- secret references.

A parent receives the common parameter context unless the parent reference overrides particular values:

```text
parentParameters = commonParameters + parentOverrides
```

Parameters may technically influence:

- file contents;
- file paths;
- package versions;
- package activation;
- generated package declarations;
- synchronization selectors;
- other producer behavior.

In ordinary use, parameters are expected to be used mainly for secrets and instance-specific values.

The parent graph itself remains statically declared. Parameters may change bindings passed to parents, but producers do not dynamically create parent relationships.

Secrets:

- are supplied from local configuration, CI secrets, or another explicit input;
- are never committed into tracked metadata;
- must be redacted from logs, traces, diagnostics, and the web interface;
- may be inserted into generated or patched files during materialization.

A producer receives parameters only. It does not receive `PackState`; generated operations receive the immutable parent state when evaluated.

---

# 7. Operation producers and operations

## Operation producer

An `OperationProducer` converts parameters into inert typed operations:

```text
OperationProducer {
    produce(parameters) -> List<Operation>
    mayWrite: ResourceDomain
    mayRead: ResourceDomain or Unknown
}
```

Common categories include:

- `StaticOperationProducer` — independent of parameters;
- fixed-target parametric producer — target is known, values depend on parameters;
- bounded generative producer — may create several operations within a declared domain.

Producers are implemented as known classes. A producer constructor may accept a lambda, but the lambda operates inside restrictions enforced by the producer class. Users do not receive unrestricted access to the filesystem or mutable `PackState`.

## Operation

An operation is a typed transformation evaluated against the parent state.

Examples:

```text
PutFile
RemoveFile
RemovePaths
PatchProperties
DeclarePackage
SetPackageActivation
AddDependency
DeclareGroup
DeclareSection
AddPruneSelector
AddIgnoreSelector
```

The base `Operation` abstraction must remain minimal. Operation-specific data belongs to concrete operation types. The web interface uses type-specific handlers rather than expecting one universal metadata structure.

## Effect domains

Each producer must expose a conservative `mayWrite` domain that is valid for all parameter values.

A resource domain may address:

```text
File(path or glob)
Property(path, key)
PackageDeclaration(id)
PackageIdentity(id)
DependencyEdge(source, target)
Group(id)
Section(id)
SynchronizationRule(id)
```

Dynamic paths must still be bounded, for example:

```text
config/generated/**
```

A producer may have a broad domain, but broad or unbounded domains prevent proof-based web editing around that producer.

`mayRead` should also be bounded where practical. Unknown or universal reading may theoretically be supported, but it is expected to be exceptional and disables automatic editing of affected earlier-layer resources.

Before applying generated operations:

1. The producer generates inert operations.
2. Their concrete reads and writes are calculated.
3. The framework verifies that concrete writes are within `mayWrite`.
4. Concrete reads are checked when a bounded `mayRead` contract exists.
5. Only validated operations are evaluated against the parent state.

A producer should ideally reject an escaping lambda itself; the generic runtime check remains a second safety boundary.

---

# 8. Resolution trace

The core trace records actual execution without forcing every operation into one universal provenance model.

```text
ResolutionTrace:
    ordered LayerApplicationTrace entries

LayerApplicationTrace:
    LayerApplication
    ordered map:
        OperationProducer -> produced List<Operation>
```

Concrete producer implementations may additionally retain:

- source metadata file;
- syntax-node handle;
- declaration identity;
- editable arguments;
- formatting information;
- source ranges;
- section membership.

Read/write graphs, package derivation graphs, and shadowing relationships may be derived from the trace when needed. They are not mandatory fields of the base trace.

---

# 9. Package and artifact model

A package declaration represents one exact concrete artifact.

```text
PackageDeclaration {
    declaration identity
    optional user alias
    logical package identity
    artifact kind
    provider/source
    exact version or provider file
    destination path
    update policy
    source activation state
}
```

Supported or extensible artifact kinds include:

- mod;
- plugin;
- resource pack;
- shader pack;
- datapack;
- server executable;
- library;
- local artifact.

Supported or extensible sources include:

- Modrinth;
- CurseForge;
- direct URL;
- local file;
- Maven;
- GitHub release;
- workspace build output.

A direct URL must include an expected cryptographic hash.

## Declaration identity versus logical identity

Each declaration is a separate object with its own version and enabled state.

Several declarations may have the same logical mod identity:

```text
mod-version-a
mod-version-b
```

Both may be enabled simultaneously.

They:

- appear separately in the web interface;
- are enabled and disabled separately;
- may have different destinations and providers.

Logical identity exists mainly for automatic dependency matching. It is normally inferred from provider project IDs or internal mod IDs, with explicit overrides for cross-provider equivalents.

If two active declarations materialize to the same destination path, resolution fails with a merge conflict.

If two JARs have different filenames but the same internal mod ID, the framework permits both. Minecraft or its loader may reject them later.

## Activation states

A declaration may be:

- declared and available for references;
- explicitly enabled as a root;
- explicitly disabled;
- activated transitively as a dependency.

Aliases are optional. An unnamed declaration is valid but cannot be conveniently referenced by source-level explicit relationships unless it has another stable identity.

---

# 10. Dependencies

## Explicit dependencies

An explicit dependency references a concrete declaration or group:

```text
requires(sourceDeclaration, targetDeclaration)
```

It is a hard constraint.

If the target is missing, incompatible, or explicitly disabled, resolution fails.

## Automatically discovered dependencies

Required dependencies may be discovered from provider or JAR metadata.

Automatic dependencies are best-effort and refer to a logical package identity.

Resolution rules:

1. If no matching package declaration exists, the dependency is ignored.
2. The web interface shows that the mod reports an unavailable dependency.
3. If exactly one acceptable declaration exists, it is activated transitively.
4. If several declarations exist, the resolver chooses the most suitable candidate.
5. Automatic activation does not disable any other explicitly enabled version.
6. If matching declarations exist but every usable candidate is explicitly disabled, resolution fails.
7. If equally suitable candidates remain, resolution fails instead of selecting arbitrarily.

Candidate suitability is evaluated in this order:

1. satisfies the dependency version range, when available;
2. matches the Minecraft version;
3. matches the loader;
4. comes from the higher-precedence layer;
5. otherwise remains ambiguous.

Automatic dependency discovery must be disableable.

## Groups

A group is a virtual package-like node:

- it has no artifact;
- it may have a name, icon, and description;
- it may require packages or other groups;
- it may be enabled or disabled;
- it appears in the package browser.

Groups are independent from reusable feature layers.

## Sections

A section is a structural and visual grouping of declarations.

A section has no enabled state in the resolved `PackState`. Enabling or disabling a section in the web interface performs a batch operation on each contained package declaration.

Sections are explicit semantic objects rather than special comments. Ordinary comments remain documentation.

---

# 11. Versions and updates

Every package declaration identifies an exact version or provider artifact.

Examples include:

- exact Modrinth version ID or version URL;
- exact CurseForge file ID;
- direct URL plus hash;
- exact local artifact.

A separate version lock file is not required because automatic dependencies select only among already declared exact candidates.

Ordinary `build`, `resolve`, and `sync` operations never search for or apply newer versions.

Updates happen only through an explicit update command.

Each declaration has an update policy. At minimum:

```text
FIXED
UPDATABLE / LATEST_COMPATIBLE
```

Rules:

- `FIXED` declarations never change automatically.
- Update policies also apply to dependency-only declarations.
- Updating a root package may cause eligible old dependency declarations to be updated.
- An explicit update rewrites the corresponding source declaration.
- Build operations never silently modify source metadata.

A resolution manifest records exact downloaded hashes and exact commits of external Git parents used for that resolution.

---

# 12. Resolution pipeline

The complete pipeline is:

```text
1. Load workspace and layer definitions.
2. Bind top-level parameters.
3. Recursively apply ordered parents without ancestor deduplication.
4. Evaluate every layer’s local producers against its immutable parent state.
5. Merge local partial deltas; fail on incompatible same-layer writes.
6. Produce the logical PackState.
7. Resolve explicit and automatic package dependencies.
8. Select exact declared artifact candidates.
9. Download or locate artifacts.
10. Materialize packages and generated files into a concrete filesystem manifest.
11. Produce Resolution and ResolutionTrace.
```

The same tracked source, resolved parent commits, deterministic parameters, and package artifacts must produce the same resolution. Secret values may intentionally make materialized instance output different between instances.

Artifacts should be cached by cryptographic hash to avoid repeated downloads.

---

# 13. Instances

Local and remote instances implement one filesystem transport abstraction:

```text
InstanceTransport {
    list
    stat
    read
    write
    createDirectories
    delete
    moveOrReplace
}
```

Initial implementations:

```text
LocalFilesystemInstance
SftpInstance
```

Instance configuration is local and is not committed to the shared repository.

It contains:

- local path or SFTP connection data;
- authentication data;
- selected base layer;
- ordered local feature layers;
- parameter values and secret references;
- a persistent instance-local virtual layer.

## Instance-local virtual layer

Every instance may have a virtual top layer persisted in framework-local state outside Git.

It may contain:

- additional parent/feature references;
- local package activation changes;
- local metadata operations;
- captured payload files;
- local parameter bindings.

Adding a feature through the web interface changes the virtual layer or its parent list; it does not modify the original tracked base layer.

Several instances may reference the same tracked layer while having different virtual layers and parameters.

---

# 14. Synchronization

Synchronization is stateless.

The framework compares:

```text
current resolution
against
current instance filesystem
```

There is no:

- previous deployment state;
- three-way merge;
- managed-file ownership;
- hidden deployment manifest inside the instance;
- conflict classification based on history.

Synchronization performs:

1. Create every desired file missing from the instance.
2. Overwrite every desired file whose contents differ.
3. Delete extra instance files matching configured prune selectors.
4. Leave every other extra file untouched.

Running synchronization already means that the user wants desired files overwritten. Additional overwrite warnings or destructive confirmation modes are not required.

## Prune selectors

Prune selectors make only selected paths authoritative:

```text
mods/*.jar
plugins/*.jar
resourcepacks/*.zip
```

For example, `mods/*.jar` removes obsolete JARs but leaves:

```text
mods/.index
mods/generated-directory/
mods/non-jar-files
```

Path globs are preferred over regular expressions for normal configuration.

`remove` and `prune` are distinct:

- `remove` modifies inherited desired state;
- `prune` controls deletion of extra files from a real instance.

## Ignore selectors

Ignore selectors hide runtime-generated or irrelevant paths from the development comparison:

```text
world/**
saves/**
logs/**
cache/**
crash-reports/**
```

## Rename presentation

A rename is not a fundamental stateless operation. Internally it is an addition plus a deletion.

The UI may infer and display a rename heuristically, using content equality or similarity in the same manner as Git or IntelliJ IDEA.

---

# 15. Development web interface

The developer starts a local web server connected to an instance and a selected resolution.

The interface has two principal views.

## Logical pack browser

It displays:

- enabled and disabled package declarations;
- transitively activated packages;
- groups;
- sections;
- declaration order;
- declaration source file;
- provider;
- exact version;
- icon, display name, and description loaded from the provider where possible;
- dependency reasons;
- unavailable automatically reported dependencies.

Declarations are grouped by:

```text
metadata file
→ section
→ declaration order
```

Packages and groups have enable/disable controls. A section toggle batch-updates its package declarations.

## Instance-delta browser

It displays semantic differences such as:

- file added;
- file removed;
- file modified;
- heuristic rename;
- package added;
- package removed;
- package updated.

A package update may internally consist of removal of one JAR, addition of another, and dependency changes, but may be displayed as one semantic item.

The user chooses:

- a default target layer;
- optionally a different target layer for a particular change.

The target may be any writable layer participating in the current instance composition, including the instance-local virtual layer.

The intended postcondition of capturing one item is:

```text
new instance delta
=
old instance delta without the selected semantic item
```

All unrelated items must remain unchanged.

---

# 16. Web editing and proof requirements

The web interface edits only producer and operation types for which it has explicit handlers.

A handler may know how to:

- replace a static payload file;
- add or remove a file;
- edit an exact package declaration;
- change an update policy;
- enable or disable a declaration;
- add a dependency edge;
- modify a known structured patch;
- change a parameter binding.

Unknown or opaque producers are displayed but are not automatically edited. The interface tells the user to edit source manually. There is no generic fallback that silently inserts an unrelated override.

A supported automatic edit requires:

1. A type-specific source-edit strategy.
2. A type-specific semantic argument showing that the intended resource changes correctly.
3. Effect-domain reasoning showing, for all valid parameter values, that unrelated resources cannot be affected.
4. Analysis of later producers that may read or overwrite the affected resource.
5. Complete re-resolution with current parameters as a final implementation check.

Re-resolution is verification, not the sole justification for correctness.

If one source decision necessarily controls several outputs, they form one semantic edit unit. The interface must not pretend that only one output can be captured independently.

## Structured patch example

A typical server composition may be:

```text
base layer:
    server.properties with non-secret values or placeholders

later instance/secret layer:
    structured patch replacing secret keys
```

A properties patch exposes key-level semantics:

```text
overwritten keys
pass-through keys
required input file
parse precondition
```

When the real server changes a non-secret pass-through key, the web interface may safely edit the earlier base file.

When the changed key is owned by the secret patch, the interface must edit the patch through a supported handler or refuse automatic editing.

Unknown or unbounded readers prevent proof-based edits to earlier layers.

---

# 17. Adding mods from an instance

When an unknown JAR appears in an instance, the framework should:

1. parse its embedded mod metadata;
2. calculate provider-compatible hashes;
3. attempt exact Modrinth or CurseForge identification;
4. display the detected project, version, name, and icon;
5. ask for confirmation when only a fuzzy match exists;
6. allow the user to choose the destination metadata file and section;
7. create the appropriate exact declaration when the source frontend supports editing;
8. otherwise provide the exact declaration text for manual insertion;
9. support storing the JAR as a local artifact when no provider match exists.

An uncertain provider match must never be accepted silently.

---

# 18. Required resolution errors

Resolution must fail on actual invalid state, including:

- layer-parent cycle;
- incompatible same-layer writes;
- two active artifacts with the same destination path;
- failed patch precondition;
- malformed structured file required by a patch;
- producer write outside its declared domain;
- explicit hard dependency that is missing or disabled;
- ambiguous automatic dependency candidate selection;
- invalid parameter value;
- missing exact artifact;
- failed artifact hash verification.

The following are explicitly not errors:

- overlapping writes in successive parent layers;
- repeated application of a shared ancestor;
- an automatically reported dependency with no declared candidate;
- several enabled JARs with the same internal mod ID but different paths.

The framework may expose diagnostics explaining actual failures, but no stylistic inheritance linter is required.

---

# 19. Initial commands and modules

The implementation should expose equivalent operations to:

```text
resolve
    Produce logical PackState and Resolution.

build
    Materialize a resolution into a directory or distributable artifact.

sync
    Synchronize a resolution with a local or SFTP instance.

update
    Update eligible exact package declarations according to policy.

dev
    Start the local development web interface.

import
    Scan an existing instance and create preliminary declarations and payload.

explain
    Explain why a file, package, dependency, or group exists in a resolution.
```

`import` and `explain` may follow the first usable implementation but should be supported by the architecture.

A machine-readable build manifest should expose:

- paths and hashes;
- exact artifacts;
- package identities;
- resolved parent commits;
- parameter fingerprints with secrets redacted;
- declaration and producer provenance.

---

# 20. Explicit non-goals of the initial implementation

The initial framework does not include:

- Minecraft server process management;
- console access;
- Pterodactyl integration;
- automatic backups or rollback;
- three-way instance reconciliation;
- managed-file ownership tracking;
- unrestricted arbitrary scripts with uncontrolled effects;
- silent installation of undeclared automatic dependencies;
- forced uniqueness of logical mod identity;
- a separate runtime type for feature or profile;
- a required lock file separate from exact source declarations;
- stylistic lint warnings about unusual layer composition.

---

# 21. Implementation-defined details

The following may be selected during implementation without changing the architecture:

- exact DSL syntax;
- reserved metadata directory and marker names;
- metadata frontend/parser technology;
- source-node representation used for editing;
- exact provider plugin interface;
- concrete UI layout;
- exact update-policy enum beyond fixed and compatible update;
- exact glob syntax;
- local framework-state directory;
- serialization format for instance configuration and virtual layers.

Any selected design must preserve:

- compact one-line common package declarations;
- exact source versions;
- order-independent same-layer metadata merging;
- ordered parent composition;
- bounded producer effects;
- type-specific web editing;
- direct payload layout;
- stateless predictable synchronization.