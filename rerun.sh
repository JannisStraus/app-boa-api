source .envrc
docker compose down
docker image rm app-boa-api-api:latest
docker image rm app-boa-api-worker:latest
docker compose build
docker compose up -d
docker logs --follow app-boa-api-worker-1
