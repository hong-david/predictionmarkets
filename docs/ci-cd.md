# CI/CD

This project uses GitHub Actions for a small production workflow:

- pull requests run Python tests, database smoke tests, and the frontend build
- pushes to `main` deploy to the EC2 host after tests pass
- the EC2 host keeps the live `.env`, Postgres volume, Redis, Caddy, and Docker state

## Why This Shape

The production server already has the right network access, Docker volumes, and
private `.env` file. A self-hosted GitHub Actions runner on that server can
deploy without opening SSH to GitHub-hosted runners or storing the EC2 SSH key in
GitHub secrets.

Only use this with a private repo or a repo where you fully control pull
requests. A self-hosted runner on the production box is powerful and should not
run untrusted code.

## One-Time EC2 Setup

1. In GitHub, open the repository.
2. Go to **Settings -> Actions -> Runners -> New self-hosted runner**.
3. Choose Linux x64.
4. On EC2, run the download and `config.sh` commands GitHub shows.
5. When GitHub asks for runner labels, include:

```text
production
```

Install it as a service:

```bash
cd ~/actions-runner
sudo ./svc.sh install ubuntu
sudo ./svc.sh start
sudo ./svc.sh status
```

The `ubuntu` user must be able to run Docker without `sudo`. If `docker ps`
works from your normal SSH session, the runner should be fine.

## Deployment Flow

Local development:

```bash
git checkout -b my-change
git add -A
git commit -m "Describe the change"
git push origin my-change
```

Open a pull request. GitHub Actions runs the test/build job.

After the PR is merged to `main`, the deploy job runs on EC2:

```bash
cd ~/predictionmarkets
git pull --ff-only origin main
docker compose -f docker-compose.budget.yml build app pipeline
docker compose -f docker-compose.budget.yml run --rm app python -m alembic upgrade head
docker compose -f docker-compose.budget.yml up -d app pipeline
```

## Check A Deploy

On EC2:

```bash
cd ~/predictionmarkets
docker compose -f docker-compose.budget.yml ps
curl -s http://127.0.0.1:8000/api/health
```

The site should keep using the same database. Deploys replace app code, not the
Postgres volume.
