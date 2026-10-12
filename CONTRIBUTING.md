# Contributing

Tracaus is MIT licensed. In each pull request, describe the behavior change, the
validation you ran and any remaining live limits. Report suspected vulnerabilities
privately ([SECURITY.md](SECURITY.md)), never in public issues.

## Project layout

| Path | What it holds |
| --- | --- |
| `app.py`, `.streamlit/`, `.env.example` | Streamlit web UI and its settings |
| `assets/` | Tracaus logos: square mark (favicon, collapsed sidebar), white wordmark for the dark UI, black wordmark for light pages |
| `tracaus/` | Shared Python runtime: orchestration, tools transport, redaction, team mode, model providers |
| `lambda/` | Lambda handlers: read-only tools, incident handlers, observers |
| `tracaus_agentcore.py` | AgentCore entry point (Bedrock only) |
| `schemas/`, `config/`, `agent-instruction.txt` | Tool contracts, default metric catalog and the model prompt. They ship inside the Lambda packages at these paths |
| `infra/` | Template generators, deployment automation and operator commands (`python -m infra.automation`) |
| `examples/` | Synthetic starting configs that `init` copies; cloud commands reject them until you replace them |
| `scripts/` | Operator tools: run the UI, build packages, replay, collector heartbeat |
| `scripts/dev/` | Maintainer checks used by CI: validators, build and render verification, secret and publication checks, diagnostics evaluation |
| `tests/`, `evaluations/` | Offline tests and synthetic diagnostic cases |
| `requirements/` | Hash-locked dependencies for the UI, development and Lambda packages |
| `docs/` | Guides: prerequisites, deploy, servers, operate, acceptance, architecture |

## Set up and run the checks

Use Python 3.12. Dependencies are pinned with hashes in `requirements/*.lock`; each
lock's header shows the `pip-compile` command that produced it. None of the checks
needs AWS access.

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # run again in each new terminal; the prompt shows (.venv)
python -m pip install --require-hashes -r requirements/dev.lock
python -m pytest -q
ruff check .
ruff format --check .
python scripts/dev/validate_schemas.py
python scripts/dev/validate_infrastructure.py
python scripts/dev/validate_durable.py          # also lints the model API renders
python scripts/dev/validate_observations.py
python scripts/dev/evaluate_diagnostics.py --output .build/diagnostics-evaluation.json
python scripts/dev/check_public_repository.py
python scripts/dev/check_secrets.py
```

The build check needs hash-verified Lambda wheels. `scripts/dev/verify_build.py` builds
each package set twice, requires identical manifests, then imports each package with
only its bundled dependencies. Optional targets: `tools`, `pipeline`, `agentcore`,
`observation` (default: all four).

```bash
python -m pip download --require-hashes --only-binary=:all: --dest .build/wheels -r requirements/lambda.lock
python scripts/dev/verify_build.py [tools] [pipeline] [agentcore] [observation]
```

CI (`.github/workflows/ci.yml`) runs the same checks plus a `pip-audit` dependency
scan, the build verification, a build of the synthetic inventory release and renders
of complete synthetic releases for both runtime targets. A local pass does not
replace live AWS acceptance.

## Tests and evaluations

Use synthetic fixtures and injected SDK clients. `tests/conftest.py` fails any test
that opens a network connection or creates an unmocked boto3 client, so tests never
touch your AWS account, paid models or notification channels. When you change
runtime behavior, keep the denial, fencing, budget, redaction and scope regressions,
and test failure paths as well as successful requests. Diagnostic evaluations run
offline in the checks above; the optional paid model run is described in
[evaluations/diagnostics/README.md](evaluations/diagnostics/README.md).

Model API tests (`tests/test_model_api.py`) follow the same rule. Pass a fake HTTP
opener and a fake Secrets Manager client to `ModelAPI`, use `api.example.com` URLs and an
obviously fake credential, and never contact a real provider. Cover each failure kind,
the no-redirect and no-retry rules, and the token-accounting paths that must stop the run.
Team mode is optional, so test both the default local mode and team mode when you
change sign-in or chat.

## Documentation and private data

Update the relevant guide in `docs/` when commands, settings, IAM boundaries or
manual responsibilities change. `check_public_repository.py` rejects links to files
that are not tracked, absolute local paths and internal planning references. New
source or configuration needs a new reviewed release, because deployment plans bind
clean committed source.

Credentials, real inventory, logs, cloud responses and private work records belong
in ignored storage (`.local/`, `.env`), never in issues, pull requests, screenshots
or fixtures. Check `git diff --cached` before committing. `.gitignore` only stops
future additions; removing a tracked file does not remove it from earlier commits.

## Prepare a clean public repository

Review the README, guides, MIT license, dependency licenses and CI first. If your
working repository has private history, export a fresh snapshot instead of pushing it:

```bash
python scripts/dev/prepare_public_repo.py --output .local/publication
```

The script needs committed source with no uncommitted tracked changes. It runs the
secret and public-file checks, then copies only the pinned commit's regular files into
a new repository with one initial commit on `main`, no history and no remote. Ignored
and untracked files are not copied, and an existing destination is never overwritten.
Review the snapshot and run its checks, then connect an empty GitHub repository
(replace the example owner and name):

```bash
cd .local/publication
git remote add origin git@github.com:YOUR_ACCOUNT/YOUR_REPOSITORY.git
ssh -T git@github.com    # exits nonzero even on success; the greeting must name the intended account
git push -u origin main
```

Do not force-push over an existing repository; review any remote history separately.
Turn on branch protection, required CI and GitHub private vulnerability reporting,
and keep public claims consistent with the live-acceptance status.
