# Deploying to Oracle Cloud Always Free

Oracle's Always Free tier is the only free option that gives this app what it
actually needs: a process that stays running (for the 07:00/09:00/11:00/13:00
campaigns and the 5-minute reply watcher) and a disk that persists (the
SQLite database holding your leads). Netlify, Vercel and free Render/Railway
tiers cannot do either — see the README for why.

## 1. Create the account and instance

1. Go to **[oracle.com/cloud/free](https://www.oracle.com/cloud/free/)** and
   sign up. A card is required for identity verification; the Always Free
   tier is never charged.
2. In the Console, **Compute → Instances → Create Instance**.
3. **Image**: Canonical Ubuntu (22.04 or newer).
4. **Shape**: click *Change shape* → **Ampere (ARM)** → `VM.Standard.A1.Flex`.
   Always Free includes up to 4 OCPUs / 24 GB RAM on this shape — far more
   than this app needs, but it's what's free.
5. Leave networking on the default VCN. **Add SSH keys**: either paste a
   public key you already have, or let the Console generate a key pair and
   **download the private key** — you cannot get it again later.
6. Create. It takes a minute or two to reach "Running".

If ARM capacity is unavailable in your region (an "Out of host capacity"
error), that's a known Oracle constraint on Always Free demand — retry in a
few minutes, try a different Availability Domain, or switch region.

## 2. Open the firewall — two separate layers

Oracle instances are blocked by **two independent firewalls**. Both must
allow port 8000, or nothing gets through even if the other one is open.

**A. The Console's Security List** (you do this manually):
`Networking → Virtual Cloud Networks → (your VCN) → Security Lists →
Default Security List → Add Ingress Rules`
- Source CIDR: `0.0.0.0/0`
- IP Protocol: TCP
- Destination Port Range: `8000`

**B. The instance's own iptables** — `deploy/setup.sh` does this for you.

## 3. Get the app onto the server

From your own machine:

```bash
# find the instance's public IP in the Console first
scp -i /path/to/your-private-key.pem -r \
    /home/kartik/Desktop/projects/moneymaker \
    ubuntu@<INSTANCE_PUBLIC_IP>:~/moneymaker
```

(`venv/` and `__pycache__/` inside it don't need to travel — they'll be
rebuilt — but copying them does no harm either, just takes longer.)

Then SSH in:

```bash
ssh -i /path/to/your-private-key.pem ubuntu@<INSTANCE_PUBLIC_IP>
```

## 4. Run the setup script

```bash
cd ~/moneymaker
chmod +x deploy/setup.sh
./deploy/setup.sh
```

This installs Python, opens port 8000 in the instance firewall, forces
`HOST=0.0.0.0` in `.env` (a server must accept external connections, unlike
your laptop), builds the venv, installs the systemd service, and starts it.
Re-running it later after a file update or `git pull` is safe — it just
restarts the service with whatever changed.

## 5. Point PUBLIC_BASE_URL at the real address

The script prints the instance's public IP at the end. Edit `.env` on the
server:

```bash
nano ~/moneymaker/.env
# set:  PUBLIC_BASE_URL=http://<INSTANCE_PUBLIC_IP>:8000
sudo systemctl restart moneymaker
```

**This step is not optional.** Every outbound email carries an unsubscribe
link built from this value; pointed at `127.0.0.1` the app already refuses to
send at all (see `services/mailer.py`), because a dead opt-out link is
unlawful to send under CAN-SPAM, PECR and the other regimes this app targets.

## 6. Check it's actually working

- Open `http://<INSTANCE_PUBLIC_IP>:8000` — you should hit the login page
  (password: whatever is in `ADMIN_PASSWORD`, then the Google Authenticator
  code).
- `sudo systemctl status moneymaker` — should show `active (running)`.
- `sudo journalctl -u moneymaker -f` — live logs; watch a scheduled campaign
  or the inbox poll fire in real time.

## Day-to-day operation

| Task | Command |
|---|---|
| View logs | `sudo journalctl -u moneymaker -f` |
| Restart (after editing `.env`) | `sudo systemctl restart moneymaker` |
| Stop | `sudo systemctl stop moneymaker` |
| Update code | upload new files, then `sudo systemctl restart moneymaker` |
| Reboot survival | already handled — `systemctl enable` was run by setup.sh |

## A TLS certificate, when you're ready for one

Running over plain `http://` is fine to get started, but logging in over
HTTP means the password and session cookie travel in the clear. The
straightforward free fix is **Caddy**, which gets a Let's Encrypt certificate
automatically — it needs a real domain name pointed at the instance's IP
first (a bare IP address can't get a certificate):

```bash
sudo apt-get install -y caddy
echo "yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}" | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Then set `PUBLIC_BASE_URL=https://yourdomain.com` and open port 443 (not
8000) in both firewall layers from step 2.

## Known rough edges with Oracle Always Free

- **ARM capacity** can be genuinely hard to get in busy regions on first
  signup. If this becomes a time sink, a Hetzner box (~€4/mo) is a ten-minute
  alternative with none of this friction.
- Oracle has been known to **reclaim instances that sit idle** with no
  outbound traffic for an extended period — a running scheduler making
  outbound HTTP calls every 10 minutes should count as activity, but it's
  worth knowing this is a stated policy, not a guarantee.
- **One instance only.** Two copies of this app running means every campaign
  fires twice and prospects get duplicate emails.
