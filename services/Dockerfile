# Una sola imagen para los 5 servicios. Cuál corre lo decide SERVICE_NAME
# (lo pone docker-compose.yml en cada servicio).

# Etapa 1: compilar TypeScript
FROM node:24-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY tsconfig.json ./
COPY shared ./shared
COPY students ./students
COPY courses ./courses
COPY enrollment ./enrollment
COPY billing ./billing
COPY gateway ./gateway
RUN npm run build

# Etapa 2: imagen final liviana, solo lo necesario para correr
FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package*.json ./
RUN npm ci --omit=dev
COPY --from=build /app/dist ./dist
EXPOSE 3000
USER node
CMD ["sh", "-c", "node dist/$SERVICE_NAME/index.js"]
