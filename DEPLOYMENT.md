# ResourceTuner — Complete AWS EC2 Deployment Guide

A step-by-step production runbook documenting all commands, configurations, and procedures used to deploy **ResourceTuner** onto an Amazon EC2 instance (Ubuntu 24.04 LTS).

---

## 1. Cloud Infrastructure & EC2 Provisioning

| Setting | Value | Rationale |
|---|---|---|
| **AMI / OS** | **Ubuntu Server 24.04 LTS (HVM)**, 64-bit (x86_64) | Ships with Linux Kernel 6.8+, unified **cgroup v2** active by default, and native **Pressure Stall Information (PSI)**. |
| **Instance Type** | **`t3.small`** (or `t3.micro` / `c6i.2xlarge`) | 2 vCPUs to support concurrent build and telemetry classification. |
| **Storage (EBS)** | **30 GiB gp3** | Covers Docker base images, zstd C compilation tree, Python virtualenvs, and frontend node_modules within AWS Free Tier. |
| **Credit Specification** | **Standard** (under Advanced Details) | Prevents surplus CPU burst charges. |

### Security Group Inbound Firewall Rules
* **SSH (Port 22)**: Source `My IP` (for terminal access)
* **Custom TCP (Port 8000)**: Source `Anywhere-IPv4` (`0.0.0.0/0`) or `My IP` (for Dashboard UI & SSE Stream)

---

## 2. Server Initialization (OS, Swap & Kernel Tuning)

SSH into the EC2 instance and run:

```bash
# 1. Update system packages
sudo apt update && sudo apt upgrade -y

# 2. Setup a 4GB Swap file (safeguard against OOM crashes on low RAM instances)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 3. Enable process I/O delay accounting (task_delayacct) for accurate disk wait telemetry
sudo sysctl kernel.task_delayacct=1
echo "kernel.task_delayacct=1" | sudo tee /etc/sysctl.d/99-resourcetuner.conf

# 4. Install essential development packages & git
sudo apt install -y git curl build-essential sysstat
```

---

## 3. Runtime & Tooling Installation

### A. Docker Engine (Official CE Repository)
```bash
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

# Grant docker group permissions to ubuntu user
sudo usermod -aG docker $USER
newgrp docker

# Verify Docker
docker ps
```

### B. Python 3.13 via `uv`
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### C. Node.js 22 LTS
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

---

## 4. Application Setup & Compilation

```bash
# 1. Clone the repository
git clone https://github.com/Krishnamundra11/ResourceTuner.git
cd ResourceTuner

# 2. Setup Python 3.13 Virtual Environment
uv python install 3.13
uv venv venv --python 3.13
source venv/bin/activate

# 3. Install pinned Python dependencies
uv pip install -r requirements.txt -c constraints.txt

# 4. Build the Frontend Single-Page Application (with memory bounds)
cd frontend
npm install
NODE_OPTIONS="--max-old-space-size=1536" npm run build
cd ..

# 5. Build the Synthetic Simulation Docker Images
./venv/bin/python scripts/run_scenario.py build
```

---

## 5. API Keys Configuration (`.env`)

Create the environment file:
```bash
cp .env.example .env
nano .env
```

Populate the required cloud API keys:
```ini
GOOGLE_API_KEY=your_gemini_api_key       # For ADK Supervisor & Decision agents (Google AI Studio)
GROQ_API_KEY=your_groq_api_key           # For CrewAI Diagnostic specialists (Groq Console)
OPENROUTER_API_KEY=your_openrouter_key   # Optional fallback
```

---

## 6. Pre-flight Verification Checks

Run verification tests before starting the daemon:
```bash
# 1. Probe API keys and test structured JSON Schema output
./venv/bin/python scripts/check_llm_providers.py

# 2. Verify Host Telemetry and Linux PSI readers
./venv/bin/python -m resourcetuner.telemetry.host

# 3. Verify cgroup v2 direct container telemetry
./venv/bin/python -m resourcetuner.telemetry.containers
```

---

## 7. 24/7 Production Daemon Setup (`systemd`)

To run the application continuously in the background with auto-restart on boot or crash:

```bash
sudo bash -c 'cat > /etc/systemd/system/resourcetuner.service << "EOF"
[Unit]
Description=ResourceTuner Dashboard Service
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ResourceTuner/frontend/ResourceTuner
ExecStart=/home/ubuntu/ResourceTuner/frontend/ResourceTuner/venv/bin/python -m resourcetuner.dashboard --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF'

# Reload systemd and enable service
sudo systemctl daemon-reload
sudo systemctl enable resourcetuner
sudo systemctl restart resourcetuner
sudo systemctl status resourcetuner
```

---

## 8. Managing and Updating Code Changes

### Updating Backend (Python)
```bash
cd /home/ubuntu/ResourceTuner/frontend/ResourceTuner
git pull
sudo systemctl restart resourcetuner
```

### Updating Frontend (React / TypeScript / UI)
```bash
cd /home/ubuntu/ResourceTuner/frontend/ResourceTuner/frontend
NODE_OPTIONS="--max-old-space-size=1536" npm run build
cd ..
sudo systemctl restart resourcetuner
```
*Note: In your browser, perform a hard refresh (`Ctrl + Shift + R` or `Cmd + Shift + R`) to bypass cached static bundles.*

### Helpful Service Commands
```bash
# View live logs
journalctl -u resourcetuner -f

# Restart service
sudo systemctl restart resourcetuner

# Check status
sudo systemctl status resourcetuner
```

