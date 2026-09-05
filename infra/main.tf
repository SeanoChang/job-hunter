# The hosted MCP server's infrastructure (spec 2026-09-02 §5): a Cloud Run
# service that runs the same image the fetcher runs, with the command pointed at
# `job-hunter-mcp` instead.
#
# Everything the service needs exists here except the two secret *values*. A
# secret version carries its value, and a value in Terraform is a value in the
# state file — which in this workflow is a local file beside a public git tree.
# So the secrets are declared empty and filled out-of-band; the runbook
# (docs/runbooks/2026-09-02-deploy-mcp.md) sequences the whole deploy, including
# the two applies that ordering forces.
#
# Sean runs terraform. Nothing in this repo applies it, and CI never sees it.

terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# APIs first: on a project where one was never enabled, its resources fail the
# apply with a 403 that names the API. `disable_on_destroy = false` because
# tearing this service down must not switch off an API something else uses.
resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com", # `gcloud builds submit` builds the image
    "iam.googleapis.com",        # the service account below is an IAM resource
  ])

  service            = each.value
  disable_on_destroy = false
}

# Where the image lives. The runbook pushes to
# <region>-docker.pkg.dev/<project>/job-hunter/mcp:<tag>, and var.image names
# one tag in this repository.
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "job-hunter"
  format        = "DOCKER"
  description   = "job-hunter images — one image, two console scripts (fetcher CLI, MCP server)"

  depends_on = [google_project_service.required]
}

# The Neon DSN of the `jobhunter_mcp` role, and the bearer every client sends.
# Created empty on purpose (see the header): no google_secret_manager_secret_version
# appears anywhere in this config.
resource "google_secret_manager_secret" "database_url" {
  secret_id = "job-hunter-mcp-database-url"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "mcp_token" {
  secret_id = "job-hunter-mcp-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

# Its own identity rather than the default compute service account, which is
# project-wide: this process reads two secrets and talks to Neon, and should be
# able to prove nothing more than that.
resource "google_service_account" "mcp" {
  account_id   = "job-hunter-mcp"
  display_name = "job-hunter hosted MCP server"
  description  = "Runs the Cloud Run MCP service; may read its own two secrets, nothing else"
  depends_on   = [google_project_service.required] # iam.googleapis.com must be on first
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each = {
    database_url = google_secret_manager_secret.database_url.secret_id
    mcp_token    = google_secret_manager_secret.mcp_token.secret_id
  }

  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mcp.email}"
}

resource "google_cloud_run_v2_service" "mcp" {
  name     = var.service_name
  location = var.region

  # Public URL; the bearer check in `jobhunter/mcp.py` is the gate (see the
  # invoker binding below).
  ingress = "INGRESS_TRAFFIC_ALL"

  # The service holds nothing — the corpus is in Neon, the archive in R2, and
  # this config rebuilds it in a minute. Guarding it from `terraform destroy`
  # would only make teardown a two-step.
  deletion_protection = false

  template {
    service_account = google_service_account.mcp.email

    # One instance, scaled to zero between calls: the traffic is an hourly
    # routine, and a second instance would buy nothing but another cold start.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      # The image pins no ENTRYPOINT precisely so this line can choose: the
      # fetcher and the server are two console scripts in one build.
      command = ["job-hunter-mcp"]

      # Serving is a psycopg connection and a JSON encode; the floor of the
      # billable shapes is already more than enough. cpu_idle keeps CPU
      # allocated only during requests — always-allocated would bill the idle
      # instance and refuses memory under 512Mi.
      resources {
        cpu_idle = true
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
      }

      # Cloud Run injects PORT with this number, which `main()` reads through
      # config.py — the same default the console script has locally.
      ports {
        container_port = 8080
      }

      # Settings requires an archive URL, but the serving path never opens the
      # archive (spec §4) — hence a plain variable here and no AWS credentials
      # anywhere in this service.
      env {
        name  = "JOB_HUNTER_ARCHIVE_URL"
        value = var.archive_url
      }

      env {
        name = "JOB_HUNTER_DATABASE_URL"

        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "JOB_HUNTER_MCP_TOKEN"

        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mcp_token.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # The revision resolves both secrets before it is allowed to serve, so the
  # accessor bindings must exist first — and a version of each secret must too,
  # which Terraform cannot express and the runbook orders instead.
  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.accessor,
  ]
}

# Google IAM stays open by design. An MCP client sends a static bearer, not a
# Google identity token, so a closed invoker binding would reject every real
# call before the app ever saw it. `BearerAuth` is the authentication boundary;
# `/healthz` is the one route it lets past, and it returns no corpus data.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.mcp.name
  location = google_cloud_run_v2_service.mcp.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
