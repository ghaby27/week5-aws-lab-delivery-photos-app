# Onward delivery photos (application)

Python (Flask) full stack app for the week 5 lab *Storing and Serving Delivery Photos for
Onward Logistics*. Infrastructure: [week5-aws-lab-delivery-photos](https://github.com/ghaby27/week5-aws-lab-delivery-photos).

| Page | What it does |
|---|---|
| `/` | Gallery: each photo with its title, description, merchant, order, rider and city |
| `/upload` | Upload a photo (JPEG, PNG or WebP, up to 10 MB) with its details |
| `/deleted` | Recently deleted photos, with Restore |
| `/about` | Owner's full name and the lab name |
| `/health` | Load balancer health check |

- Photos go to a private S3 bucket under `photos/YYYY/MM/DD/<uuid>.<ext>`; details go to
  PostgreSQL with the photo's `s3_key`. Title and description are also stored as S3 object
  metadata so the replica in the second Region is evidence on its own.
- The gallery shows photos through CloudFront with signed URLs that expire after 5 minutes.
- Delete marks the record deleted and adds an S3 delete marker; Restore copies the last
  version back. The app's role cannot remove object versions.
- No credentials in code: ECS injects `DB_PASSWORD` and `SIGNING_PRIVATE_KEY` from Secrets Manager.

## Release

`.github/workflows/release.yml` runs on every push to `main`: it signs in to AWS with OIDC,
builds the image, pushes it to ECR as `<sha>` and `latest`, and starts a new ECS deployment.

## Run locally

```sh
docker run -d --name pg -e POSTGRES_USER=onward -e POSTGRES_PASSWORD=local -e POSTGRES_DB=photos -p 5432:5432 postgres:17-alpine
pip install -r requirements.txt
DB_HOST=localhost DB_PASSWORD=local DB_SSLMODE=disable python app.py   # http://localhost:8080
```

Uploads need AWS credentials and `PHOTOS_BUCKET`; signed links need `CDN_DOMAIN`,
`SIGNING_KEY_ID` and `SIGNING_PRIVATE_KEY`.
