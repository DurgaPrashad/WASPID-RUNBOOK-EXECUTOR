FROM python:3.12-slim
RUN useradd --create-home --uid 10001 waspid
WORKDIR /app
COPY worker.py .
USER waspid
ENV WASPID_VERSION=1.4.2 HEARTBEAT_FILE=/tmp/heartbeat
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=10s \
  CMD python -c "import os,time,sys; st=os.stat(os.environ.get('HEARTBEAT_FILE','/tmp/heartbeat')); sys.exit(0 if time.time()-st.st_mtime < 20 else 1)"
CMD ["python", "worker.py"]
