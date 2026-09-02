variable "project_id" {
  description = "Google Cloud project that owns the service, the image repository and the secrets."
  type        = string
}

# us-east4 is the closest region to Neon's us-east; latency is not load-bearing
# for an hourly routine, but a cross-continent hop would be gratuitous.
variable "region" {
  description = "Region for the Cloud Run service and the Artifact Registry repository."
  type        = string
  default     = "us-east4"
}

# Rolling out a new build is: push a tag, change this, apply.
variable "image" {
  description = "Image to serve, e.g. us-east4-docker.pkg.dev/<project>/job-hunter/mcp:v1."
  type        = string
}

variable "service_name" {
  description = "Cloud Run service name; also the label Google puts in the generated URL."
  type        = string
  default     = "job-hunter-mcp"
}

# Required by config.Settings, unused by the serving path (spec §4): documents'
# markdown is in the store, so no request opens the archive and no AWS
# credentials are provisioned to this service.
variable "archive_url" {
  description = "JOB_HUNTER_ARCHIVE_URL for the container."
  type        = string
  default     = "s3://job-hunter/corpus"
}
