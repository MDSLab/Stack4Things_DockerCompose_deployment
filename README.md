# Stack4Things IoTronic: Docker Compose Deployment Guide

This document describes how to deploy **Stack4Things IoTronic** services using **Docker Compose**. It provides an overview of each container defined in the `docker-compose.yml` file, outlines installation requirements, and offers step-by-step instructions for initializing and running the entire environment.

---

## Table of Contents

1. [Introduction](#introduction)  
2. [Requirements](#requirements)  
3. [Docker Compose Overview](#docker-compose-overview)  
4. [Services Description](#services-description)  
   - [CA Service](#ca-service)  
   - [Crossbar](#crossbar)  
   - [IoT WSTUN](#iot-wstun)  
   - [IoT Database (MariaDB)](#iot-database-mariadb)  
   - [RabbitMQ](#rabbitmq)  
   - [RabbitMQ Setup](#rabbitmq-setup)  
   - [Keystone](#keystone)  
   - [IoTronic Conductor](#iotronic-conductor)  
   - [IoTronic WAgent](#iotronic-wagent)  
   - [IoTronic UI](#iotronic-ui)  
   - [Lightning-Rod](#lightning-rod)  
5. [Digital Twin Layer (Eclipse Ditto)](#digital-twin-layer-eclipse-ditto)  
   - [Ditto Services](#ditto-services)  
   - [Integration Components](#integration-components)  
   - [Running the Twin Layer](#running-the-twin-layer)  
   - [Verifying the Twin Layer](#verifying-the-twin-layer)  
6. [Configuration and Customization](#configuration-and-customization)  
7. [Deployment Steps](#deployment-steps)  
8. [Verifying the Deployment](#verifying-the-deployment)  
9. [Troubleshooting](#troubleshooting)  
10. [Further Reading](#further-reading)  

---

## Introduction

**Stack4Things** is an IoT framework that integrates with **OpenStack** to provide a robust, cloud-oriented platform for managing IoT devices at scale. The framework uses **IoTronic** as the OpenStack-based service and provides optional components (e.g., database, message queue) that can be either hosted in Docker containers or integrated with existing OpenStack services.

This guide focuses on a **Docker Compose**-based deployment, detailing how each container fits into the larger ecosystem. Once properly configured, this environment enables end-to-end IoTronic functionality, secure communications, device management, logging, and a user interface (UI) all running within Docker.

---

## Requirements

- **Operating System**: Ubuntu 18.04 (Bionic) or newer, or equivalent Linux distribution.  
- **Docker Engine**: Ensure Docker CE or EE is installed and running.  
- **Docker Compose**: Version 3+ is recommended.  
- **Sufficient Permissions**: The ability to run `docker-compose` and bind to ports (e.g., 80, 3306, etc.).  

If you already have:  
- A separate **MariaDB**, **RabbitMQ**, or **Keystone** from an existing OpenStack setup, you may skip the corresponding container in `docker-compose.yml` or adapt environment variables to point to external services.

---

## Docker Compose Overview

Docker Compose allows defining and running multi-container Docker applications. You specify all your services (containers), networks, and volumes in a single YAML file (`docker-compose.yml`), then run:
```bash
    docker-compose up -d
```
to bring the entire stack up in the background.

---

## Key Benefits

- **Single Configuration File**: Centralizes all container definitions, environment variables, and volumes.  
- **Dependency Management**: Allows specifying `depends_on` to ensure that containers start in a specific order.  
- **Reproducibility**: The same `docker-compose.yml` can be used across environments, ensuring consistent setups.

---

# Connecting a Virtualized IoT Board to Stack4Things

This guide explains how to connect a **virtualized Lightning-Rod board**, running as a Docker container, to the **Stack4Things (S4T)** platform using the provided `docker-compose.yml` environment.

---

##  Overview

- The **Lightning-Rod** container simulates an IoT board.
- The **Crossbar** container acts as the WAMP router.
- The **Stack4Things UI** (`iotronic-ui`) allows registering and managing boards.
- All containers are part of the same Docker network (`s4t`).

---

##  Step-by-Step Setup

### 1. Register the Virtual Board in Stack4Things UI

1. Open the **Stack4Things dashboard** in your browser:

```bash
http://0.0.0.0/horizon
```
(Replace `0.0.0.0` with iotronic-ui Docker host IP if needed)

2. Log in with the default credentials:
- **Username**: `admin`
- **Password**: `s4t`

3. Navigate to the **IoT** section in the Horizon sidebar.

4. Click on **“Create Board”** and provide the required board information.

5. After registration, **copy the board code**. You’ll need it to complete the Lightning-Rod configuration.

![](images/1.png)

---

### 2. Access the Lightning-Rod Web Interface

Open your browser and navigate to:

```bash
http://0.0.0.0:1474
```
(Replace 0.0.0.0 with lightning-rod Docker host IP if needed)

Log in with the default credentials:
- **Username**: `me`
- **Password**: `arancino`

This opens the Lightning-Rod container’s internal configuration interface.

>  Ensure the Lightning-Rod container exposes port `1474` in your `docker-compose.yml`.

---

### 3. Configure the Crossbar Endpoint

In the Lightning-Rod interface:

- Set the **Crossbar URL** to:
 
```bash
wss://crossbar:8181
```
This address works because both containers share the same Docker network (`s4t`), allowing hostname resolution by container name.

---


### 4. Finalize the Lightning-Rod Configuration

Back in the Lightning-Rod browser interface:

- Paste the **board code** from the dashboard registration.
- Submit the form to finalize the connection.

Once submitted, the board will connect to the Crossbar WAMP router and register with the IoTronic Conductor.

![](images/2.png)
---

## ✅ Board Onboarding Complete

The virtualized board is now fully integrated with Stack4Things:

- It is visible in the IoTronic dashboard.
- You can interact with it via the UI.
- Plugins and services can be deployed remotely.

![](images/3.png)
![](images/4.png)
---


## Services Description

### CA Service

- **Role**: Acts as a local Certificate Authority (CA).  
- **Purpose**: Generates SSL/TLS certificates used by other components.  
- **Key Directory**: `/etc/ssl/iotronic/`  
- **Note**: Useful if you do not have a pre-existing CA or certificates.

### Crossbar

- **Role**: WAMP router for real-time event-based communication.  
- **Ports**: Typically runs on port 8181.  
- **Note**: Receives certificates from the CA container.

### IoT WSTUN

- **Role**: Manages WebSocket tunnels allowing IoT devices behind NATs to reach IoTronic.  
- **Ports**: Default is 8080 plus custom tunnels.  
- **Note**: Works together with Crossbar and uses SSL.

### IoT Database (MariaDB)

- **Role**: Stores IoTronic data.  
- **Environment Variables**:  
  - `MYSQL_ROOT_PASSWORD`  
  - `MYSQL_DATABASE`  
  - `MYSQL_USER`  
  - `MYSQL_PASSWORD`  
- **Note**: Creates/migrates schemas on first startup.

### RabbitMQ

- **Role**: Message queue system (optional).  
- **Ports**: 5672 (AMQP), 15672 (web UI).

### RabbitMQ Setup

- **Role**: Initializes users, permissions, tags.  
- **Depends On**: Waits for RabbitMQ to be healthy.

### Keystone

- **Role**: OpenStack identity and authentication service.  
- **Ports**: Usually 5000.  
- **Note**: Optional if already available externally.

### IoTronic Conductor

- **Role**: Core IoTronic component.  
- **Environment Variables**:  
  - `DB_CONNECTION_STRING`  
  - `OS_AUTH_URL`  
  - `OS_USERNAME`  
  - `OS_PASSWORD`

### IoTronic WAgent

- **Role**: Device-side agent that bridges boards to the conductor.  
- **Usage**: For local testing or production devices.

### IoTronic UI

- **Role**: Web-based admin interface.  
- **Port**: 80  
- **Note**: Connects to the conductor API.

### Lightning-Rod

- **Role**: Acts as the runtime agent that runs **on the IoT device itself**.  
- **Function**: Communicates with the IoTronic Conductor and executes plugin logic, controls board status, and manages device-level interactions.  
- **Deployment**: Typically deployed directly on physical devices (e.g., Raspberry Pi, embedded boards).  
- **Note**: In containerized test setups, it can be run inside Docker for simulation, but in production it is usually installed natively on the device OS.

---

# Digital Twin Layer (Eclipse Ditto)

An optional layer that gives every registered board a **digital twin**: a queryable object holding the state the device last reported and the state an operator wants it to reach. Applications read and write the twin instead of addressing the device, so they work whether or not the device is currently reachable.

It is delivered as a **second Compose file**, `docker-compose.ditto.yml`, which is layered on top of `docker-compose.yml`. No IoTronic image is rebuilt, no existing service definition is edited, and no existing volume is touched. If you do not want the twin layer, use `docker-compose.yml` alone exactly as before.

**What it adds once running:**

- A board registered in Horizon automatically gets a twin within about 30 seconds.
- Telemetry published by a device on the WAMP bus lands in that twin.
- A desired value written on the twin is delivered to the device over WAMP.
- Twins are searchable across the whole fleet, with per-path access control and a full history of every change.

Full design and rationale: `docs/Digital_Twin_Layer_Complete_Report.md`.

## Ditto Services

Eclipse Ditto is five microservices in one Apache Pekko cluster, plus MongoDB and an nginx reverse proxy. All are added unmodified from the upstream images.

| Service | Role |
|---|---|
| `ditto-policies` | Persists policies and makes authorization decisions |
| `ditto-things` | Persists things and features, enforces authorization on them |
| `ditto-things-search` | Maintains the search index and executes RQL queries |
| `ditto-connectivity` | Runs the AMQP connection to RabbitMQ and the payload mappings |
| `ditto-gateway` | Terminates the HTTP and WebSocket APIs |
| `ditto-mongodb` | Storage for all of the above |
| `ditto-nginx` | Reverse proxy and HTTP basic authentication, the only published port |
| `ditto-ui` | Explorer UI for browsing twins, policies and connections |

- **Port**: only `ditto-nginx` is published, on **8090**. Port 8080 is already taken by `iotronic-wstun` on this host.
- **Note**: every Java service carries the `ditto-cluster` network alias. Pekko uses it for seed node discovery, so removing it prevents the cluster from forming.
- **Requirements**: Ditto documents a minimum of **2 CPU cores and 4 GB of RAM available to Docker**, in addition to the base stack.

## Integration Components

Three small containers connect Ditto to Stack4Things. Their source is in `scripts/`.

### s4t-ditto-bridge

- **Role**: The only component that speaks both protocols. Stack4Things uses WAMP over websockets, Ditto uses AMQP 0.9.1.
- **Function**: Subscribes to `s4t.telemetry.<uuid>.report` on Crossbar and publishes to the `ditto.inbound` queue. In the other direction it consumes desired-state changes and publishes only the *difference* against the reported state on `s4t.command.<uuid>.apply`.
- **Note**: sending the difference rather than the whole desired state makes command traffic self-terminating. Once the device complies and reports the new value, the difference is empty and nothing further is sent.

### s4t-ditto-provisioner

- **Role**: Keeps the set of twins matching the set of boards.
- **Function**: Reads the `boards` table, lists existing twins, and creates whatever is missing. Runs every 30 seconds by default.
- **Note**: it compares whole sets rather than reacting to events, because IoTronic emits no lifecycle notifications. A board registered while the twin layer was offline still receives its twin when the layer returns.

### s4t-ditto-bootstrap

- **Role**: One-shot setup, run once at startup and then exits.
- **Function**: Declares the AMQP queues and exchanges, creates the Ditto access policy and the RabbitMQ connection, and verifies both.
- **Note**: the bridge and the provisioner depend on it via `service_completed_successfully`. If bootstrap fails, neither starts. The stack refuses to run against an unconfigured Ditto rather than running and appearing healthy.

### s4t_mock_board.py

- **Role**: A simulated device for testing and demonstration, in `scripts/`.
- **Function**: Publishes the same WAMP topics a real board would and applies any command it receives, so nothing downstream can tell the difference.

## Running the Twin Layer

Both files must be passed on every command. Defining a shell alias saves a lot of typing:

```bash
    alias dcs='docker compose -f docker-compose.yml -f docker-compose.ditto.yml'
```

Before the first run, append `.env.ditto.example` to your `.env` and fill in the two `CHANGE_ME` values:

```bash
    openssl rand -base64 24     # -> DITTO_DEVOPS_PASSWORD
    openssl rand -base64 24     # -> DITTO_API_PASSWORD
```

The API password must also be written into `conf_ditto/nginx.htpasswd`, because nginx reads a file rather than an environment variable:

```bash
    openssl passwd -apr1        # paste DITTO_API_PASSWORD, then write the
                                # result as  s4t:$apr1$...  into that file
```

Then bring everything up:

```bash
    dcs up -d
    sleep 180
    dcs ps
```

The JVMs have a 120 second health check start period and genuinely need most of it. Do not conclude anything before two minutes.

**Three separate credentials** are in play, which is a common source of confusion:

| Path | Credential |
|---|---|
| `/status`, `/health` | none |
| `/api/2/things`, `/api/2/policies`, `/ws/2`, Explorer UI | `s4t` with `DITTO_API_PASSWORD` |
| `/api/2/connections`, `/devops` | `devops` with `DITTO_DEVOPS_PASSWORD` |

Ditto sees the basic-auth caller as the subject **`nginx:s4t`**. That is the string policies must grant; getting it wrong produces a 403 on twins with a cause several steps removed from the symptom.

The Explorer UI is at `http://localhost:8090/ui/`. On first use, open the **Environments** tab and set the API URI to `http://localhost:8090`. It defaults to port 8080, which on this host is `iotronic-wstun`, not Ditto.

## Verifying the Twin Layer

```bash
    ./scripts/verify_stage_c.sh
```

Thirteen checks, exiting non-zero if any fail. Two of them are the ones that matter: they write a message in at one end and look for it at the other, rather than reading a status field. A component reporting itself as healthy is not evidence that a message was delivered.

For a narrated demonstration across two terminals:

```bash
    ./scripts/demo.sh board      # terminal 1, the simulated device
    ./scripts/demo.sh            # terminal 2, the operator
```

If a twin is not updating, ask Ditto what it did with the message rather than guessing:

```bash
    curl -s -u devops:$DITTO_DEVOPS_PASSWORD \
      http://localhost:8090/api/2/connections/s4t-rabbitmq/logs
```

---

## Configuration and Customization

### Environment Variables

Can be set:
- Inline in `docker-compose.yml`  
- In an external `.env` file  
- As Docker secrets

The Ditto overlay reads its own set from the same `.env`. Copy them from `.env.ditto.example`, which documents each one:

| Variable | Purpose |
|---|---|
| `DITTO_VERSION` | Pinned image tag. Do not use `latest`, since a re-pull silently changes what you measured |
| `DITTO_EXTERNAL_PORT` | Published port, 8090 by default |
| `DITTO_API_PASSWORD` | Basic auth for twins and the Explorer UI, user `s4t` |
| `DITTO_DEVOPS_PASSWORD` | Guards `/devops` and `/api/2/connections` |
| `DITTO_MEM_*` | Per-service memory limits, roughly 3.0 GB in total by default |
| `DITTO_JAVA_OPTS` | JVM options. `ditto-connectivity` has its own variant, deliberately without `ExitOnOutOfMemoryError`, because the AMQP client can throw it spuriously |
| `S4T_AMQP_VHOST` | RabbitMQ virtual host, `/` by default |

### Volumes

Used for:
- Database data (`unime_iotronic_db_data`)  
- SSL certs (`iotronic_ssl`)  
- Logs (`iotronic_logs`, `iotronic-ui_logs`)  
- Twin storage (`ditto_mongodb_data`), only when the Ditto overlay is used

> **Note**: `unime_iotronic_db_data` and `rabbitmq_data` hold production state. They are ordinary named volumes, so Docker will happily recreate them empty if they are missing, and `docker compose up` reports that as `Created` without warning. See the cleanup note under Troubleshooting.

### Networking

Services are placed on a user-defined Docker network (e.g., `s4t`) and can reach each other by hostname.

### Ports

Check `ports:` entries and adapt to your host as needed (e.g., `8080:8080`, `5000:5000`).

With the Ditto overlay, **8090** is published for `ditto-nginx`. Ditto's own MongoDB (27017) and the internal gateway port (8081) are deliberately **not** published.

---

## Deployment Steps

### 1. Review the `docker-compose.yml` File

- Adjust environment variables and services.  
- Remove/comment containers you don't need.

### 2. (Optional) Build or Pull the Images
```bash
    docker-compose build
```
### 3. Launch the Deployment
```bash
    docker-compose up -d
```
To include the digital twin layer, pass both files:
```bash
    docker compose -f docker-compose.yml -f docker-compose.ditto.yml up -d
```
- Creates network  
- Starts containers in correct order  
- Logs handled via Docker engine

### 4. Check Logs
```bash
    docker-compose logs -f
```
Press `Ctrl+C` to stop watching logs (containers stay running).

### 5. Validate Service Health
```bash
    docker ps
```
---

## Verifying the Deployment

- **CA Service**: Check if certificates are in `/etc/ssl/iotronic/`.  
- **Crossbar**: Should listen on port 8181. Check logs with:
```bash
      docker-compose logs crossbar
```
- **MariaDB**: Test access with:
```bash
      docker exec -it iotronic-db mysql -uroot -punime
```
- **Conductor**: Should respond on port 8812.  
- **UI**: Open your browser to:  
  `http://<your_docker_host_or_ip>/`
- **Ditto** (if the overlay is running): `curl -s http://localhost:8090/status` should report overall `UP`, and `./scripts/verify_stage_c.sh` should report 13 passes.

## Stopping and Removing the Deployment

Upon completion of testing, or if it becomes necessary to dismantle the environment, all containers, networks, and associated resources can be stopped and removed as follows:

### Stop and Remove Containers
```bash
      docker-compose down
```
This command halts and removes all containers defined in the `docker-compose.yml` configuration file.

### (Optional) Remove Persistent Volumes
```bash
      docker-compose down -v
```
Appending the `-v` flag will also delete any named volumes (e.g., for the database, logs, or certificates), resulting in the loss of all persisted data.

⚠️ **Warning**: This operation is irreversible. Ensure that all critical data has been properly backed up before proceeding with the `-v` option.

`-v` is not the only way to lose these volumes. See the cleanup note under
Troubleshooting: prune commands remove them too, with no prompt and no `-v`.
---

## Troubleshooting

### Containers Restarting

- Verify environment variables  
- Check for port conflicts  
- Ensure dependencies are available

### Database Connectivity Issues

- Confirm that `iotronic-db` is healthy  
- Check `DB_CONNECTION_STRING`  
- Ensure external DB (if any) is reachable

### SSL Certificate Problems

- Confirm CA container generated certificates  
- Check volume sharing and file permissions

### Keystone Authentication Fails

- Check `OS_AUTH_URL`, `OS_USERNAME`, `OS_PASSWORD`, etc.  
- Make sure port 5000 is reachable inside the Docker network

### Logs
```bash
    docker-compose logs -f <service_name>
```
### Ditto twin layer

- **A container is `unhealthy`**: the health checks here mean *connected and working*, not merely running. Read the logs, do not restart blindly: `dcs logs --tail 40 <service>`.
- **`ditto-policies` or `ditto-things` restarting**: usually the Pekko cluster failing to form. Confirm every Java service still carries the `ditto-cluster` network alias.
- **401 on `/api` with a password you believe is right**: `conf_ditto/nginx.htpasswd` and `DITTO_API_PASSWORD` have drifted apart. Regenerate both together with `openssl passwd -apr1`.
- **Explorer UI shows "unauthorized" with no detail**: the API URI in its Environments tab is still the default 8080, which is `iotronic-wstun`. Set it to 8090.
- **Twin not updating**: do not guess. Ask Ditto what it did with the message: `curl -s -u devops:$DITTO_DEVOPS_PASSWORD http://localhost:8090/api/2/connections/s4t-rabbitmq/logs`.
- **`s4t-ditto-bridge` did not start**: the bootstrap did not exit 0. Nothing downstream runs until it does.

### Cleanup (use with caution)

> ### ⚠️ Read this before running any prune
>
> **`docker system prune` and `docker volume prune` can delete the production
> volumes**, and on this deployment they have. A prune skips any volume attached
> to a container, including a stopped one, but once containers have been removed
> (for example by `docker compose down`) the volumes are unreferenced and become
> eligible for deletion.
>
> The failure is silent. The next `docker compose up` recreates the missing
> volumes empty and reports it as `Created`, so the stack comes up looking
> perfectly healthy with an empty database.
>
> Take a dump **before** any cleanup, and prefer removing specific objects over
> pruning:
>
> ```bash
>     docker exec iotronic-db mariadb-dump -uroot -p"$MYSQL_ROOT_PASSWORD" \
>       --databases iotronic keystone > backup_$(date +%Y%m%d_%H%M%S).sql
> ```
>
> A post-mortem of one such incident is in `docs/Volume_Loss_Recovery_23_Aug_2026.md`.

If you have read the above and still want to reclaim space, prune images only
and leave volumes alone:

```bash
    docker image prune -f
```

---

## Further Reading

- [Iotronic (S4T Cloud-Side) Official Repo](https://opendev.org/x/iotronic)
- [Lightning-Rod (S4T Device-Side) Official Repo](https://opendev.org/x/iotronic-lightning-rod)
- [Iotronic-UI (S4T Dashboard) Official Repo](https://opendev.org/x/iotronic-ui)
- [Iotronic-Client (S4T CLI) Official Repo](https://opendev.org/x/python-iotronicclient)
- [Docker Documentation](https://docs.docker.com)
- [Docker Compose Docs](https://docs.docker.com/compose)
- [OpenStack Documentation](https://docs.openstack.org)

### Digital twin layer

- [Eclipse Ditto Documentation](https://eclipse.dev/ditto/)
- [Ditto AMQP 0.9.1 binding](https://eclipse.dev/ditto/connectivity-protocol-bindings-amqp091.html)
- [RFC 7396, JSON Merge Patch](https://tools.ietf.org/html/rfc7396)
- `docs/Digital_Twin_Layer_Complete_Report.md`, the consolidated evaluation, design, implementation and verification report

- `scripts/verify_stage_c.sh`, the executable definition of done

---

## Disclaimer

This guide assumes standard Docker and Compose usage.  
Always secure your `.env` files and manage certificates properly.  
For production, consider:

- Volume and data backups  
- TLS everywhere  
- Service monitoring  
- Least-privilege credentials

---

© 2025 MDSLab, University of Messina  
*Feel free to modify or extend this guide for your organization’s needs.*
