<img width="1672" height="941" alt="captain-bridge" src="https://github.com/user-attachments/assets/8077ff7a-dbbd-4fe2-96d5-346f17eb4e2c" />


# Captain Bridge

Captain Bridge orchestrates Git, Herdr, and OMP for one local macOS user. The human is the captain; the Officer manages Herdr workers and is the only agent the human talks to.

## Prerequisites

- macOS with Python 3.11+ and `pipx`
- A Git worktree for the repository you want to orchestrate
- Pi (OMP-Pi) and its local agent directories
- Herdr when launching/inspecting agent work

## Install and configure OMP

OMP uses `~/.omp/agent` by default, not Pi's `~/.pi/agent`. Links under `~/.pi/agent` will not be discovered by OMP. The commands below target OMP's default profile; if you use a named profile or override its configuration directory, use that profile's agent directory instead.

From this checkout, run each command on a single line:

```sh
pipx install --editable .
mkdir -p ~/.omp/agent/extensions ~/.omp/agent/skills
ln -sfn "$PWD/extensions/captain-bridge.ts" ~/.omp/agent/extensions/captain-bridge.ts
ln -sfn "$PWD/skills/officer" ~/.omp/agent/skills/officer
```

Restart OMP after updating the extension. For an existing installation, remove the old `~/.omp/agent/skills/captain` symlink if it points to this checkout. The optional manual skill is `/skill:officer`; normal startup activates the Officer automatically.

Terminal worker turns automatically notify the Officer through the extension. Final assistant text is stored in assignment events and exposed by `assignment inspect`; workers no longer write `result.md`. Notifications do not require report headings or nonempty text. Delivery errors are shown in the worker session; there is no polling or automatic recovery after a failed nudge.

An editable installation must keep pointing at an existing checkout. If that checkout moves, rebind with `pipx install --force --editable /path/to/current/captain-bridge`.

## First ship

From your repository inside Herdr:

```sh
captain start
```

The wrapper resolves the current checkout root, reuses its registered ship or creates one, binds the Officer, and starts OMP with the Officer policy. Existing live Officers are attached through Herdr when available. OMP arguments, including model selection and custom appended instructions, can follow `start`.

If multiple ships match the checkout, explicitly select one with `CAPTAIN_BRIDGE_SHIP`; the wrapper does not guess. Startup requires a Git checkout. `captain ship open` remains a state-reconciliation command, not an interactive launcher.

Writable workers use `<main-checkout>/.worktrees/<assignment-id>`, including when the originating checkout is linked. The directory is ignored through Git's local exclude file. Existing external worktrees are not automatically moved or deleted. Nesting does not copy uncommitted files, load the main `.env`, or inject main-checkout skills; the Officer supplies task context.

## Normal workflow

1. Open or wake and reconcile the ship.
2. Capture only durable context and findings.
3. Create assignments, then launch and inspect them through the Officer.
4. Apply the authority policy: autonomous reversible work, reviewable rationale, and approval for side effects or costly-to-reverse decisions.
5. Resolve approval gates and integrate accepted work; leave the next Officer state explicit.

## Minimal CLI examples

With `CAPTAIN_BRIDGE_SHIP` set (or while your current directory is the ship directory or one of its descendants), examples include:

```sh
captain ship reconcile
captain assignment create --role builder --prompt "Add input validation"
captain assignment launch <assignment-id>
captain assignment inspect <assignment-id>
captain decision request --mode reviewable --confidence medium --question "Should this change ship?"
```

Run `captain --help` and `captain <group> --help` for the complete command surface. `captain-bridge` is an equivalent script name.

## State and customization

Durable state defaults to `~/.captain-bridge/ships/`. Set `CAPTAIN_BRIDGE_HOME` to relocate it and `CAPTAIN_BRIDGE_SHIP` to select a ship. On first use, default authority and role files are copied into `$CAPTAIN_BRIDGE_HOME` (default `~/.captain-bridge/`); customize the Officer policy in `$CAPTAIN_BRIDGE_HOME/authority.md` (default `~/.captain-bridge/authority.md`) and role prompts in `$CAPTAIN_BRIDGE_HOME/roles/`. `CAPTAIN_BRIDGE_OFFICER_NAME` and `CAPTAIN_BRIDGE_OFFICER_ID` customize the Officer identity recorded for new or opened ships.

Existing installed authority and role files are preserved. On upgrade, compare the packaged `src/captain_bridge/resources/authority.md` and `resources/roles/` with your installed copies and merge your customizations. In particular, remove old worker instructions requiring `result.md`; final assistant responses are now the report. The startup Officer policy is loaded directly from the installed package.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
bun tests/test_extension.ts
```
