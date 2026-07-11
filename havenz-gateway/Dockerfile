ARG BUILD_FROM
FROM ${BUILD_FROM}

# The bridge is pure Python standard library — just need python3.
RUN apk add --no-cache python3

COPY bridge.py entry.py /opt/

CMD [ "python3", "/opt/entry.py" ]
