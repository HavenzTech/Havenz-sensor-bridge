ARG BUILD_FROM
FROM ${BUILD_FROM}

# Pure Python standard library — just need python3. No pip, no wheels, nothing to audit and
# nothing that can fail to build on an aarch64 Pi.
RUN apk add --no-cache python3

COPY agent.py entry.py /opt/

CMD [ "python3", "/opt/entry.py" ]
