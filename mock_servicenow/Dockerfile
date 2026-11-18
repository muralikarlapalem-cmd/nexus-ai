# Mock ServiceNow for local NEXUS AI testing (small image, no database server needed)
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN useradd -r -m mock && mkdir -p /data && chown mock /data
USER mock
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/mock/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
