FROM python:3.11-slim

WORKDIR /app

# System deps for curl-cffi and chromium fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Install without undetected-chromedriver (not needed on Cloud Run — Kite is used)
RUN pip install --no-cache-dir \
    streamlit>=1.28.0 \
    yfinance>=0.2.28 \
    pandas>=2.0.0 \
    feedparser>=6.0.10 \
    requests>=2.31.0 \
    plotly>=5.17.0 \
    numpy>=1.24.0 \
    curl-cffi>=0.7.0 \
    streamlit-autorefresh>=1.0.0 \
    google-cloud-firestore>=2.16.0 \
    google-cloud-secret-manager>=2.20.0

COPY . .

EXPOSE 8080

HEALTHCHECK CMD curl -f http://localhost:8080/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py", \
     "--server.port=8080", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.enableCORS=false", \
     "--server.enableXsrfProtection=false"]
