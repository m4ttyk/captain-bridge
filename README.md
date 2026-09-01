<img width="1672" height="941" alt="captain-bridge" src="https://github.com/user-attachments/assets/8077ff7a-dbbd-4fe2-96d5-346f17eb4e2c" />


# Captain Bridge

Captain Bridge is a local macOS orchestration MVP for Git, Herdr, and OMP-Pi. It gives one Officer durable authority over ships, assignments, decisions, and reusable memory. Runtime currently targets one local macOS user; it is not a multi-user service.

## Prerequisites

- macOS with Python 3.11+ and `pipx`
- A Git worktree for the repository you want to orchestrate
- Pi (OMP-Pi) and its local agent directories
- Herdr when launching/inspecting agent work

## Install and configure Pi

From this checkout:

```sh
pipx install --editable .
mkdir -p ~/.pi/agent/extensions ~/.pi/agent/skills
ln -sf "$PWD/extensions/captain-bridge.ts" ~/.pi/agent/extensions/captain-bridge.ts
ln -sfn "$PWD/skills/captain" ~/.pi/agent/skills/captain
```

Restart Pi after changing extension or skill links. The extension is `extensions/captain-bridge.ts`; the user skill is `skills/captain/SKILL.md`. The registered invocation is `/skill:captain`; the `/captain` alias is not yet registered.

## First ship

Create a ship for an existing Git worktree (the command prints JSON, including the durable ship path):

```sh
cd /path/to/your/repo
captain ship create . my-project
```

Keep the returned ship path, or discover it from the default state directory. To select it explicitly:

```sh
export CAPTAIN_BRIDGE_SHIP="$HOME/.captain-bridge/ships/$(basename "$PWD")-my-project"
captain ship open
```

Then invoke the Officer in Pi with `/skill:captain`. The Officer reconciles the ship, creates assignments before launching them, observes results, records decisions, and curates durable memory.

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

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
