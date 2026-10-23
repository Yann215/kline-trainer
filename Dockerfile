FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /opt/trainer
COPY backend/ ./backend/
RUN pip install --no-cache-dir -c ./backend/constraints.txt ./backend
COPY --from=web /web/dist ./frontend/dist
ENV TRAINER_DATA_DIR=/data TRAINER_WEB_DIR=/opt/trainer/frontend/dist POLARS_MAX_THREADS=2
EXPOSE 8765
VOLUME ["/data"]
CMD ["kline-trainer", "serve", "--host", "0.0.0.0", "--port", "8765"]
