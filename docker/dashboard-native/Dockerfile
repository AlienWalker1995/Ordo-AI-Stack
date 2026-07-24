# dashboard (native SPA) — the localhost control-plane UI. Build context is THIS directory:
#   docker build -t ordo/dashboard:latest docker/dashboard-native
# Static single-file UI + an nginx reverse proxy to the ops-controller (ordo serve).
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
EXPOSE 8080
