FROM python:3.12-slim
RUN useradd --create-home --uid 10001 waspid
WORKDIR /app
COPY app.py .
USER waspid
ARG WASPID_VERSION=1.4.2
ENV WASPID_VERSION=${WASPID_VERSION} PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"
CMD ["python", "app.py"]
