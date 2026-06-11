FROM nginx:1.27-alpine

COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx/10-render-web-access.sh /docker-entrypoint.d/10-render-web-access.sh

RUN chmod +x /docker-entrypoint.d/10-render-web-access.sh
