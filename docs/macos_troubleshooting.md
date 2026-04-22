
## Stack4Things Common problems on MacOS (Apple chip)
**Date:** April 22, 2026  
**Author:** Mohsen Ghalem

---

## 1. Context

The Stack4Things deployment was running on WSL (Windows Subsystem for Linux) on an Apple Silicon Mac (ARM64). The full stack - IoTronic Conductor, Lightning-Rod, Crossbar (WAMP router), WSTUN (WebSocket tunnel), MariaDB, RabbitMQ, and Keystone - was deployed via Docker Compose.

The goal was to register a virtual IoT board and successfully enable a service on it through the IoTronic Horizon dashboard.

---


## 2. Problems and Solutions

### Problem 01 Apple macOS - Freeing Port 5000 (AirPlay Conflict)

On macOS Monterey and later, the **AirPlay Receiver** service occupies port `5000` by default. This directly conflicts with the Keystone authentication container which also needs port `5000`.

**Symptom:** Keystone fails to bind, conductor cannot authenticate, or `docker-compose up` reports port already in use.

**Fix:** Disable AirPlay Receiver in System Settings:

1. Open **System Settings** (or System Preferences on older macOS)

2. Go to **General** > **AirDrop & Handoff**

3. Toggle **AirPlay Receiver** to **Off**

Or via terminal:

```bash

# Confirm AirPlay is holding port 5000

sudo lsof -i :5000

# Disable AirPlay Receiver via defaults

sudo defaults write /Library/Preferences/com.apple.AirPlayReceiver.plist AllowPairing -bool false

```

After disabling, restart Docker Compose:

```bash

docker compose down && docker compose up -d

```

Port `5000` will then be free for Keystone.
### Problem 2 - Missing TLS Certificates

| | |
|---|---|
| **Symptom** | Board status showed `None` for UUID, Session ID, and WAMP Agent. WSTUN showed Offline. |
| **Root Cause** | Crossbar and WSTUN wait for TLS certificates in the shared Docker volume `iotronic_ssl` before starting. The `ca_service` container had not generated them, leaving the volume empty. |
| **Solution** | Manually generated CA and Crossbar certificates on the WSL host using OpenSSL, then copied them into the Docker volume via a temporary Debian container. File permissions were set to `644` so Crossbar (running as non-root) could read the private keys. Restarted: `crossbar`, `iotronic-wstun`, `lightning-rod`. |

```bash
# Commands used to generate certs
mkdir ~/s4t-certs && cd ~/s4t-certs
openssl genrsa -out iotronic_CA.key 2048
openssl req -x509 -new -nodes -key iotronic_CA.key -sha256 -days 18250 \
  -subj "/C=IT/O=iotronic" -out iotronic_CA.pem
openssl genrsa -out crossbar.key 2048
openssl req -new -key crossbar.key -subj "/C=IT/O=iotronic/CN=crossbar" -out crossbar.csr
openssl x509 -req -in crossbar.csr -CA iotronic_CA.pem -CAkey iotronic_CA.key \
  -CAcreateserial -out crossbar.pem -days 18250 -sha256
chmod 644 *.key *.pem

# Copy into Docker volume
docker run --rm \
  -v ~/s4t-certs:/source \
  -v stack4things_dockercompose_deployment_iotronic_ssl:/dest \
  debian:buster \
  bash -c "cp /source/* /dest/ && chmod 644 /dest/*.key /dest/*.pem"
```

---

### Problem 3 - Missing CBOR Serializer (Apple Silicon)

|                |                                                                                                                                                                                                                                                            |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Symptom**    | Lightning-Rod logs: `WAMP Connection Failure: could not create serializer for cbor (available: json, msgpack)`. Docker warning: `The requested image platform (linux/amd64) does not match the detected host platform (linux/arm64/v8)`.                   |
| **Root Cause** | The Lightning-Rod image `lucadagati/lrod:compose` is built for `linux/amd64` only. On Apple Silicon, Docker runs it via Rosetta 2 emulation, but the `cbor`/`cbor2` Python packages required by the autobahn WAMP library were not installed in the image. |
| **Solution**   | Added `platform: linux/amd64` to the `lightning-rod` service in `docker-compose.yml` to force explicit Rosetta 2 emulation. Modified the container startup command to install `cbor2` and `cbor` via pip before launching `startLR`.                       |

```yaml
# docker-compose.yml change
lightning-rod:
  image: lucadagati/lrod:compose
  platform: linux/amd64        # added for Apple Silicon
  command: >
    -c "pip install cbor2 cbor && sed -i \"s|self\\.wstun_ip *= .*|self.wstun_ip = \\\"iotronic-wstun\\\"|\" /usr/local/lib/python3*/site-packages/iotronic_lightningrod/modules/service_manager.py && exec startLR"
```

---

## 3. Final Outcome

After applying all four fixes, the system reached a fully operational state:

- Lightning-Rod registration status: **operative**
- UUID: assigned
- WAMP Session ID: `<session_id>`
- WSTUN: **Online**
- Board visible and manageable in the IoTronic dashboard
- Services can now be enabled on the board

---

## 4. Things to verify

- On Apple Silicon Macs, always set `platform: linux/amd64` for amd64-only images in `docker-compose.yml` to enable proper Rosetta 2 emulation.
- The `iotronic_ssl` Docker volume must be populated with TLS certificates before Crossbar and WSTUN can start. If `ca_service` fails silently, generate certificates manually using OpenSSL.
- File permissions on private keys must be `644` (not `600`) when running in Docker with non-root service users - otherwise Crossbar cannot read the key and fails to start.
