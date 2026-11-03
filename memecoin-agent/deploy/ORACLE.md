# Run the agent 24/7 on Oracle Cloud's free tier

Paper trading only. The server holds no wallet key. It serves the full dashboard over HTTPS with a password. Allow about 45 minutes the first time.

## 1. Create the account
1. Go to https://signup.cloud.oracle.com and sign up for **Oracle Cloud Free Tier**.
2. A credit card is needed for identity checks only. Pick the home region carefully: it can't be changed, and
   free servers are scarce in busy regions. A quieter region is more likely to have capacity.
3. Stay on the **Always Free** resources. Don't click "Upgrade" and you won't be billed.

## 2. Create the server
1. Console menu -> **Compute -> Instances -> Create instance**.
2. Image: **Canonical Ubuntu 24.04** (Change image -> Ubuntu).
3. Shape: **Change shape -> Ampere -> VM.Standard.A1.Flex**, 1 OCPU and 6 GB memory (Always Free allows up to 4 and 24).
   If it says "Out of capacity", try another availability domain, or retry later; it can take a few days.
   Fallback: **VM.Standard.E2.1.Micro** (AMD, 1 GB). It works but is tight; add the swap step below.
4. Networking: keep the defaults, with a **public IPv4 address**.
5. Open the web ports: after creating, click the instance's **subnet -> Default Security List -> Add Ingress Rules**:
   source CIDR `0.0.0.0/0`, protocol TCP, destination ports `80,443`.
6. SSH keys: choose **Generate a key pair** and **download the private key**. Keep it safe: it is the only way in.
7. Create. When it shows **Running**, copy the **Public IP address**.

## 3. Log in
On Windows (PowerShell), from the folder holding the key:
```
ssh -i .\ssh-key-XXXX.key ubuntu@PUBLIC_IP
```
(If it complains about key permissions, right-click the key -> Properties -> Security, and leave only your user.)

## 4. Install Docker and get the code
```
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT && sudo apt-get install -y iptables-persistent
sudo usermod -aG docker ubuntu && exit
```
Log in again, then:
```
git clone https://github.com/felixabou/Cyber-projects.git
cd Cyber-projects/memecoin-agent
```
Only on the 1 GB AMD shape, add swap first:
```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 5. Add your keys
```
cp .env.example .env
nano .env
```
Also set `MEMECOIN_UI_PASSWORD` (a long password for the dashboard login) and `DOMAIN`
(your public IP with dashes, plus `.sslip.io`, e.g. IP 129.80.1.2 gives `129-80-1-2.sslip.io`; it's free and needs no purchase).
Fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `LARPCHECK_API_KEY` and `GMGN_API_KEY`. Save with Ctrl+O, Enter, Ctrl+X.
Type the keys in the server's editor yourself. Never paste them into chat and never commit `.env`.
If GMGN ties your key to an IP, add the server's public IP in your GMGN API settings.

## 6. Move your state over (keeps your cash, positions and trade history)
**First stop the agent on your PC**, so the two copies never run at once.
In PowerShell on your PC:
```
cd $HOME\Cyber-projects\memecoin-agent
scp -i C:\path\to\ssh-key-XXXX.key -r state ubuntu@PUBLIC_IP:~/Cyber-projects/memecoin-agent/data-in
```
Then on the server:
```
mkdir -p data && mv data-in data/state && ls data/state
```
You should see `state.json` and `trades.csv`. Skip this step to start fresh from a new paper wallet.
The server uses `deploy/server.yaml`; it already has the $150k market-cap floor, the $500 daily loss limit
and your three watchlist tokens. Edit that file and rebuild if you want other settings.

## 7. Start it
```
docker compose up -d --build
docker compose logs -f
```
Look for "Agent started in PAPER mode" and "Copy lane on". Then open `https://YOUR-DOMAIN` (the `.sslip.io` address)
and log in with any username and your `MEMECOIN_UI_PASSWORD`. The certificate can take a minute the first time. Ctrl+C leaves the log view; the agent keeps running.
`restart: unless-stopped` brings it back after a crash or a server reboot.

## Everyday use
- Logs: `docker compose logs --tail 100`
- Stop: `docker compose down`   Update: `git pull && docker compose up -d --build`
- Trades: `data/state/trades.csv` (download with `scp`), or use the Telegram `/csv` command.
- The dashboard is the same one you use on your PC (Trade tab, positions, downloads).
- Telegram controls (`/buy /sell /agent /csv`) work from your phone.
- **Never run the PC agent and the server agent at the same time.**

## Troubleshooting
- Oracle may reclaim idle free servers. An agent running all day isn't idle, but check the console weekly.
- Many `RATE_LIMIT_BANNED` lines: the server's IP is limited harder than your home one. Stop and wait out the ban.
- Out of memory: use the swap step above, or the Ampere shape.
