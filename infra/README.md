# infra — Terraform for the hosted MCP service

Declares everything the Cloud Run MCP server needs: the four APIs, the Artifact
Registry repository, the two Secret Manager secrets, the service account and its
accessor bindings, the service itself, and a public invoker binding. Design:
`docs/superpowers/specs/2026-09-02-hosted-mcp-design.md` §5. The step-by-step
deploy — roles SQL, image build, the two applies, secret values, smoke test — is
`docs/runbooks/2026-09-02-deploy-mcp.md`.

Only the MCP service lives here. The fetcher runs on GitHub Actions against R2
and Neon, both created by hand, and is not modelled in Terraform.

## What is deliberately absent

- **Secret values.** No `google_secret_manager_secret_version`. A version holds
  its value in plain text in the state file, and this repository is public; the
  runbook adds both versions with `gcloud secrets versions add --data-file=-`.
  This is also why the deploy takes two applies: the Cloud Run revision resolves
  both secrets before it may serve, so the secrets are created (and filled)
  before the service is created.
- **Remote state.** State is the local `infra/terraform.tfstate`, gitignored
  along with `.terraform/` and any `*.tfvars`. One operator, one machine; a GCS
  backend would be ceremony, and losing the file costs a `terraform import` per
  resource — a dozen of them, none holding anything precious.
- **A closed invoker.** `allUsers` holds `roles/run.invoker` on purpose: an MCP
  client sends a static bearer, not a Google identity token. The token check in
  `jobhunter/mcp.py` is the authentication boundary.

## Running it

Sean runs these; per the repo's standing rules the assistant never applies, and
terraform is not installed on the machine the assistant works from — this config
is validated on Sean's.

```bash
terraform -chdir=infra init
terraform -chdir=infra plan -var project_id=<project> -var image=<full-image-ref>
```

A `infra/terraform.tfvars` holding `project_id` and `image` saves repeating the
flags; it is gitignored because the image ref names the project.
