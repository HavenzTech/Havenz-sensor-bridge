FROM python:3.13-alpine
# openssl: the sink generates its own private certificate authority at start.
RUN apk add --no-cache openssl
COPY sim/sink.py /opt/sink.py
EXPOSE 443 1026 8026
CMD ["python3", "-u", "/opt/sink.py"]
