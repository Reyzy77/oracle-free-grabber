# Oracle Cloud Free Tier — Grabber

Script qui tente en boucle de créer la **plus grosse machine Always Free**
d'Oracle Cloud, hébergé sur **GitHub Actions** (donc il tourne même PC éteint).

Cible : `VM.Standard.A1.Flex` — **4 OCPU / 24 GB RAM** (ARM Ampere), boot volume 50 GB.
C'est exactement le plafond Always Free : **0 € garanti**, aucune ressource payante
n'est jamais demandée.

La boucle ne s'arrête jamais tant qu'aucune machine n'est obtenue : chaque job
tourne 5h30, le cron (toutes les 10 min) enchaîne le suivant, et à la première
machine obtenue le workflow se désactive tout seul et ouvre une issue avec l'IP.

---

## 1. Créer la clé API Oracle

Dans la console OCI : avatar en haut à droite → **My profile** → **API keys** →
**Add API key** → *Generate API key pair* → **Download private key** → **Add**.

Oracle affiche ensuite un encart de configuration. Note :

| Valeur | Où la trouver |
|---|---|
| `user` | l'encart de config, ligne `user=ocid1.user...` |
| `tenancy` | l'encart, ligne `tenancy=ocid1.tenancy...` |
| `fingerprint` | l'encart, ligne `fingerprint=...` |
| `region` | l'encart, ligne `region=` (ex. `eu-paris-1`) |
| clé privée | le fichier `.pem` téléchargé |

## 2. Clé SSH pour se connecter à la machine

Si tu n'en as pas :

```bash
ssh-keygen -t ed25519 -f "$HOME/.ssh/oracle" -N ""
```

La **publique** (`~/.ssh/oracle.pub`, une seule ligne) va dans les secrets.

## 3. Renseigner les secrets GitHub

Depuis ce dossier (PowerShell), en remplaçant les valeurs par les tiennes :

```bash
gh secret set OCI_USER_OCID --body "ocid1.user.oc1..REMPLACE"
```

```bash
gh secret set OCI_TENANCY_OCID --body "ocid1.tenancy.oc1..REMPLACE"
```

```bash
gh secret set OCI_FINGERPRINT --body "aa:bb:cc:dd:REMPLACE"
```

```bash
gh secret set OCI_REGION --body "eu-paris-1"
```

```bash
Get-Content "$HOME\.ssh\oracle.pub" -Raw | gh secret set SSH_PUBLIC_KEY
```

La clé privée se pousse directement depuis le fichier `.pem` téléchargé
(PowerShell ne gère pas `<`, d'où le pipe) :

```bash
Get-Content "$HOME\Downloads\LE_FICHIER.pem" -Raw | gh secret set OCI_KEY_CONTENT
```

Le script normalise les fins de ligne Windows et refuse de démarrer avec un
secret malformé, en disant précisément lequel.

Secrets **optionnels** : `OCI_KEY_PASSPHRASE` (si ta clé en a une),
`OCI_COMPARTMENT_OCID` (par défaut = le tenancy, ce qui est correct en free tier),
`OCI_SUBNET_OCID` (par défaut : le script réutilise ton subnet public, ou crée
VCN + Internet Gateway + subnet + règle SSH si tu n'en as aucun).

## 4. Lancer

```bash
gh workflow run "Grab Oracle Free Tier VM"
```

Puis suivre :

```bash
gh run watch
```

Le cron prend le relais automatiquement ensuite.

---

## Réglages (onglet *Settings → Variables*, tous optionnels)

| Variable | Défaut | Rôle |
|---|---|---|
| `SHAPE_LADDER` | `4:24` | Tailles essayées, de la plus grosse à la plus petite. `4:24,2:12,1:6` = accepte plus petit si le max ne passe pas. |
| `BOOT_VOLUME_GB` | `50` | Disque. Max Always Free = 200 GB au total. |
| `OS_VERSION` | `24.04` | Version d'Ubuntu. |
| `RETRY_DELAY` | `60` | Secondes entre deux tentatives. Ne pas descendre sous 30 (rate-limit Oracle). |
| `DISPLAY_NAME` | `free-a1-grabbed` | Nom de la machine. |

## Lancer en local plutôt que sur Actions

```bash
pip install -r requirements.txt
```

```bash
OCI_USER_OCID=... OCI_TENANCY_OCID=... OCI_FINGERPRINT=... OCI_REGION=... OCI_KEY_FILE=~/oracle.pem SSH_PUBLIC_KEY="$(cat ~/.ssh/oracle.pub)" python scripts/grab.py
```

`MAX_RUNTIME_MIN` non défini = boucle infinie, sans limite de durée.

## Une fois la machine obtenue

```bash
ssh -i ~/.ssh/oracle ubuntu@IP_DE_LA_MACHINE
```

Le workflow s'est désactivé seul. Pour relancer la chasse (autre machine,
autre région) : `gh workflow enable "Grab Oracle Free Tier VM"`.

## Sécurité / coûts

- Le script ne demande **que** des tailles Always Free, jamais de shape payante.
- Il refuse de créer un doublon si une instance `A1.Flex` existe déjà.
- Sur `LimitExceeded` il vérifie l'existant avant toute nouvelle tentative.
- Compte « Free Tier » pur : Oracle bloque de toute façon toute ressource payante.
- Astuce : si ton compte a été upgradé en *Pay As You Go*, garde `SHAPE_LADDER`
  sur du Always Free et surveille le budget — le quota gratuit reste le même.
