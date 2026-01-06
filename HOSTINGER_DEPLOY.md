# Deploiement SignalSlice sur Hostinger

## METHODE 1 : Terminal Docker Hostinger (hPanel)

### Etape 1 : Acceder au Terminal Docker

1. Connecte-toi a **hPanel** (hpanel.hostinger.com)
2. Va dans **VPS** → Selectionne ton VPS
3. Clique sur **Terminal** dans le menu lateral

---

### Etape 2 : Commandes a executer dans le Terminal

Copie-colle ces commandes **une par une** :

```bash
# 1. Telecharger SignalSlice
cd /home
git clone https://github.com/Hack-Pac/SignalSlice.git
cd SignalSlice
```

```bash
# 2. Construire l'image Docker (prend 5-10 min)
docker build -t signalslice .
```

```bash
# 3. Lancer le conteneur
docker run -d \
  --name signalslice \
  --restart unless-stopped \
  -p 5000:5000 \
  -e TZ=America/New_York \
  signalslice
```

```bash
# 4. Verifier que ca tourne
docker ps
docker logs signalslice
```

---

### Etape 3 : Ouvrir le Port 5000

1. Dans hPanel, va dans **Firewall**
2. Clique **Ajouter une regle**
3. Configure :
   - Port : `5000`
   - Protocole : `TCP`
   - Source : `Tous`
4. **Sauvegarder**

---

### Etape 4 : Tester

Ouvre dans ton navigateur :
```
http://<IP_DE_TON_VPS>:5000
```

Tu devrais voir le dashboard SignalSlice !

---

### Commandes Utiles (Terminal Hostinger)

```bash
# Voir les logs en direct
docker logs -f signalslice

# Redemarrer
docker restart signalslice

# Arreter
docker stop signalslice

# Supprimer et recreer
docker stop signalslice && docker rm signalslice
docker run -d --name signalslice --restart unless-stopped -p 5000:5000 signalslice

# Mettre a jour (nouvelle version)
cd /home/SignalSlice
git pull
docker build -t signalslice .
docker stop signalslice && docker rm signalslice
docker run -d --name signalslice --restart unless-stopped -p 5000:5000 signalslice
```

---

---

## METHODE 2 : SSH Classique (alternative)

### Pre-requis

- Hostinger VPS (Ubuntu 22.04 recommande)
- Client SSH (Terminal Mac/Linux ou PuTTY Windows)

---

### Etape 1 : Connexion SSH

```bash
ssh root@<IP_VPS_HOSTINGER>
# Mot de passe : celui configure dans hPanel
```

---

### Etape 2 : Installer Docker (si pas deja fait)

```bash
curl -fsSL https://get.docker.com | sh
```

---

### Etape 3 : Deployer SignalSlice

```bash
cd /home
git clone https://github.com/Hack-Pac/SignalSlice.git
cd SignalSlice
docker build -t signalslice .
docker run -d --name signalslice --restart unless-stopped -p 5000:5000 signalslice
```

---

### Etape 4 : Configurer le Firewall (SSH)

```bash
ufw allow 5000/tcp
ufw reload
```

---

### Etape 5 (Optionnel) : Nginx + SSL

```bash
# Installer Nginx
apt install nginx certbot python3-certbot-nginx -y

# Config Nginx
cat > /etc/nginx/sites-available/signalslice << 'EOF'
server {
    listen 80;
    server_name votre-domaine.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 300s;
    }

    location /socket.io {
        proxy_pass http://127.0.0.1:5000/socket.io;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

ln -s /etc/nginx/sites-available/signalslice /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# SSL
certbot --nginx -d votre-domaine.com
```

---

## Etape 7 : Appeler depuis N8N

### URL de l'API

```
http://<IP_VPS>:5000/api/trigger_scan    # Declencher un scan manuel
http://<IP_VPS>:5000/api/status          # Obtenir le statut actuel
```

### Exemple N8N HTTP Request Node

```json
{
  "method": "GET",
  "url": "http://votre-domaine.com/api/status",
  "options": {
    "timeout": 30000
  }
}
```

### Reponse attendue

```json
{
  "pizza_index": 3.42,
  "gay_bar_index": 6.58,
  "active_locations": 127,
  "scan_count": 15,
  "anomaly_count": 2,
  "scanning": false,
  "scanner_running": true
}
```

---

## Commandes Utiles

```bash
# Voir les logs
docker-compose logs -f signalslice

# Redemarrer
docker-compose restart

# Arreter
docker-compose down

# Reconstruire apres modification
docker-compose up -d --build

# Statut
docker-compose ps
```

---

## Monitoring

- **Dashboard web** : http://<IP_VPS>:5000
- **API status** : http://<IP_VPS>:5000/api/status
- **Logs Docker** : `docker-compose logs -f`

---

## Couts Estimes

| Service | Prix/mois |
|---------|-----------|
| Hostinger VPS KVM 1 | ~5€ |
| Domaine (optionnel) | ~1€ |
| SSL Let's Encrypt | Gratuit |
| **Total** | **~6€/mois** |
