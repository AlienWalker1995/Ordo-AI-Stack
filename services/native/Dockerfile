# dashboard (native SPA) — the localhost control-plane UI. Build context is THIS directory:
#   docker build -t ordo/dashboard:latest services/native
# Static single-file UI + an nginx reverse proxy to the ops-controller (ordo serve).
# nginx:1.27-alpine
FROM nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
EXPOSE 8080
