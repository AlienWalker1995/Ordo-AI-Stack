# dashboard — the localhost control-plane SPA. Build context is v2/.
# Static single-file UI + an nginx reverse proxy to the ops-controller (ordo serve).
FROM nginx:1.27-alpine
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY dashboard/index.html /usr/share/nginx/html/index.html
EXPOSE 8080
