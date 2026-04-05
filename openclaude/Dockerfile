FROM node:22-bookworm-slim

ARG OPENCLAUDE_NPM_PACKAGE=@gitlawb/openclaude
ARG OPENCLAUDE_NPM_VERSION=latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        git \
        less \
        openssh-client \
        procps \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "${OPENCLAUDE_NPM_PACKAGE}@${OPENCLAUDE_NPM_VERSION}"

USER node
WORKDIR /workspace

ENV HOME=/home/node \
    TERM=xterm-256color \
    NODE_ENV=production \
    BROWSER=echo

ENTRYPOINT ["openclaude"]
