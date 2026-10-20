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
FROM deps AS dev
COPY . .
RUN npx prisma generate
ENV PORT=3000
EXPOSE 3000
# Applies Prisma migrations, then serves the app with Vite HMR.
# Use `shopify app dev` on your machine for the tunnel + Partner login (see README).
CMD ["sh", "-c", "npx prisma migrate deploy && npx react-router dev --host 0.0.0.0 --port 3000"]

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
