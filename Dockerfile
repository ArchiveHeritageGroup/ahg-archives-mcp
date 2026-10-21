FROM python:3.12-slim

# One process, no data inside: settings and the PSIS key come in as environment
# variables at run time, never in the image.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY glam_mcp ./glam_mcp

RUN useradd --system --uid 10001 glam
USER glam

ENV GLAM_HOST=0.0.0.0 GLAM_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "glam_mcp.server"]
