FROM node:20-slim AS frontend-build
WORKDIR /app/client

COPY client/package*.json ./
COPY client/postcss.config.js ./postcss.config.js
COPY client/tailwind.config.js ./tailwind.config.js
COPY client/vite.config.js ./vite.config.js
COPY client/index.html ./index.html
COPY client/src ./src
RUN npm ci
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
COPY --from=frontend-build /app/client/dist ./static

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8080"]
