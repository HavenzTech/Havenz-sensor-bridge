# Build context is tools/, so the stand-in reader the repo already ships is used as it is.
FROM python:3.13-alpine
RUN apk add --no-cache iproute2
COPY fake_reader.py /opt/fake_reader.py
COPY plant-rehearsal/sim/readers_sim.py /opt/readers_sim.py
EXPOSE 80 9100
CMD ["python3", "-u", "/opt/readers_sim.py"]
