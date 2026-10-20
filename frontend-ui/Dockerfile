# syntax=docker/dockerfile:1.7
# frontend-ui: Shopify embedded app (React Router 7 + Polaris), Prisma on Postgres.
# Targets: `dev` (used by docker-compose) and `runtime` (production / Railway).

ARG NODE_VERSION=22

# ---------------------------------------------------------------- deps
FROM node:${NODE_VERSION}-alpine AS deps
RUN apk add --no-cache openssl
WORKDIR /app
COPY package.json package-lock.json ./
COPY prisma ./prisma
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund

# ---------------------------------------------------------------- dev
# Used by docker-compose. Includes the Shopify CLI, so `shopify app dev` (Partner login,
# tunnel, install on the dev store) runs inside the container: no Node needed on the host.
FROM deps AS dev
RUN --mount=type=cache,target=/root/.npm npm install -g --no-audit --no-fund @shopify/cli@4.9.4
COPY . .
RUN npx prisma generate
# Starts the CLI's dev session, which runs the app with the command in shopify.web.toml.
CMD ["shopify", "app", "dev"]

# ---------------------------------------------------------------- build
FROM deps AS build
COPY . .
RUN npx prisma generate && npm run build && npm prune --omit=dev

# ---------------------------------------------------------------- runtime
FROM node:${NODE_VERSION}-alpine AS runtime
RUN apk add --no-cache openssl && addgroup -S app && adduser -S app -G app
WORKDIR /app
ENV NODE_ENV=production PORT=3000
COPY --from=build --chown=app:app /app/package.json /app/package-lock.json ./
COPY --from=build --chown=app:app /app/node_modules ./node_modules
COPY --from=build --chown=app:app /app/build ./build
COPY --from=build --chown=app:app /app/prisma ./prisma
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1
CMD ["npm", "run", "start"]
