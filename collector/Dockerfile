FROM docker.io/library/python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt
COPY collect.py /app/collect.py
USER 65534:65534
ENTRYPOINT ["python", "/app/collect.py"]
