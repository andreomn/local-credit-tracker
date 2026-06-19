# Backend Jobs Deployment

## Architecture

`local-credit-tracker` is the source of truth for the backend Cloud Run Jobs. The deployable job sources are:

| Source directory | Container image | Cloud Run Job | Region |
| --- | --- | --- | --- |
| `jobs/anbima-to-gcs` | `gcr.io/debenture-tracker/anbima-to-gcs` | `anbima-download-job` | `southamerica-east1` |
| `jobs/b3-predi-to-gcs` | `gcr.io/debenture-tracker/b3-predi-job` | `b3-predi-job` | `southamerica-east1` |
| `jobs/b3-trades-to-gcs` | `gcr.io/debenture-tracker/b3-trades-job` | `b3-trades-job` | `southamerica-east1` |

Google Cloud executes builds and deployments only. Cloud Run Jobs and Cloud Scheduler jobs already exist and are not recreated by this repository's CI/CD flow.

## Deployment flow

1. A pull request is reviewed and merged into `main`.
2. A GitHub-connected Cloud Build trigger that matches only `^main$` starts a build with `cloudbuild.jobs.yaml`.
3. Cloud Build builds all three backend job images.
4. Each image is pushed to the existing GCR repository twice:
   - an immutable commit tag: `gcr.io/debenture-tracker/<image>:<SHORT_SHA>`
   - the convenience tag: `gcr.io/debenture-tracker/<image>:latest`
5. Cloud Build updates each existing Cloud Run Job in place with `gcloud run jobs update --image ...`.

The deploy step intentionally updates only the container image. Existing Cloud Run Job settings are preserved, including memory, CPU, retry count, timeout, service account, and environment variables.

## Cloud Build Trigger

Create the trigger after connecting the GitHub repository to Cloud Build:

```bash
gcloud builds triggers import \
  --source=infra/cloudbuild/backend-jobs-trigger.yaml \
  --project=debenture-tracker
```

Before importing, replace `GITHUB_OWNER` in `infra/cloudbuild/backend-jobs-trigger.yaml` with the GitHub organization or user that owns `local-credit-tracker`.

The trigger uses:

- `filename: cloudbuild.jobs.yaml`
- `github.name: local-credit-tracker`
- `github.push.branch: ^main$`
- `includedFiles` scoped to backend job sources and the Cloud Build file
- a dedicated Cloud Build service account: `cloud-build-backend-deployer@debenture-tracker.iam.gserviceaccount.com`

Recommended IAM for the deployer service account:

- permission to read source and write build logs
- permission to push to `gcr.io/debenture-tracker/*`
- permission to update Cloud Run Jobs in `southamerica-east1`
- `iam.serviceAccounts.actAs` on the runtime service accounts already attached to the jobs, if required by the Cloud Run update operation

Prefer the least-privilege predefined or custom roles that satisfy those permissions in your project.

## Rollback procedure

Use the immutable commit tag from a previous successful Cloud Build run:

```bash
gcloud run jobs update anbima-download-job \
  --image=gcr.io/debenture-tracker/anbima-to-gcs:<SHORT_SHA> \
  --region=southamerica-east1 \
  --project=debenture-tracker

gcloud run jobs update b3-predi-job \
  --image=gcr.io/debenture-tracker/b3-predi-job:<SHORT_SHA> \
  --region=southamerica-east1 \
  --project=debenture-tracker

gcloud run jobs update b3-trades-job \
  --image=gcr.io/debenture-tracker/b3-trades-job:<SHORT_SHA> \
  --region=southamerica-east1 \
  --project=debenture-tracker
```

This rollback also updates only the image and preserves the rest of each job's configuration.

## Manual deployment

Manual deployment should be exceptional. To run the same pipeline from a local checkout:

```bash
gcloud builds submit \
  --config=cloudbuild.jobs.yaml \
  --project=debenture-tracker \
  --substitutions=SHORT_SHA=$(git rev-parse --short HEAD)
```

To manually deploy one already-built image without rebuilding:

```bash
gcloud run jobs update b3-trades-job \
  --image=gcr.io/debenture-tracker/b3-trades-job:<SHORT_SHA> \
  --region=southamerica-east1 \
  --project=debenture-tracker
```

## Production safeguards and best practices

- Deployments happen only from merges or direct pushes to `main`; feature branch commits do not deploy.
- Images are tagged immutably with the Git commit SHA for auditability and rollback.
- Existing Cloud Run Jobs are updated in place and are never recreated by the pipeline.
- Cloud Scheduler jobs are not managed by this pipeline.
- Frontend deployment is not changed.
- Trigger path filters keep documentation-only and frontend-only changes from starting backend deployments.
- A dedicated Cloud Build service account is used so deployment permissions can be kept narrow.
